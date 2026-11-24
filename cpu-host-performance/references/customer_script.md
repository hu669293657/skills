# 从采集生成客户专用绑核脚本

分析器和定制生成器统一交付 `cpu_binder_simple.py`、`bind_design.json`。Python 文件逐字节复制自 [客户模板](../templates/cpu_binder_simple.py)。分析器仅把已核验的占卡主 PID 写入 JSON，并设置 `bind_sub_process=true` 来覆盖当前线程组，保留有关联的子进程供审阅。定制生成器逐个填写 TID，并对获准的设备输出 IRQ 条目。需要按负载、其他服务或设备 IRQ 定制时，使用本页生成器，它还输出 `customer_plan.json`，记录选择目标的依据。定制方案每 TID 默认使用 1 个逻辑 CPU，同一业务进程所有线程的目标并集限 1～2 个逻辑 CPU。同卡多进程共享卡池。预留按完整物理核（含 SMT sibling）计算。

## 1. 先采什么

在客户 **Linux 宿主机**上选代表性运行期和高峰期，各取多轮；记录作业、起止时间、吞吐/尾延迟。网络采集与 trace 尽量并行对齐。第一行是原有 ftrace 采集（按其权限要求运行），第二行只读取计数器；均不改亲和性。

```bash
sudo bash scripts/cpu_trace_collect.sh --hostbound --duration 15
sudo python3 scripts/collect_network_evidence.py --interval 10 --output net-peak.json
```

| 实际来源（相对解包后的 `hostbound/`） | 能回答的问题 | 不能推断的事 |
|---|---|---|
| `host_context.txt`、`cpu_online.txt`、`lscpu_parse.txt`（CPU/Core/Socket/Node/Online）、`numa_topology.json` | 是哪次启动、宿主 PID namespace；在线核、NUMA、跨 socket 重复 core ID、SMT 同胞 | 不能把逻辑核数等同可用物理核数；现场还与 sysfs sibling 对照 |
| `npu_smi_map.txt`、`npu_smi_topology.txt`、`npu_smi_info_start.txt` / `npu_smi_info.txt` | 物理卡/芯片→全局逻辑设备、显式 CPU Affinity、前后占卡主 PID | Rank、`python3` 名字或显存最高并不能证明所有权 |
| `process_start.json` / `process_end.json` 的 `processes[]`、`threads[]` | TGID/TID、启动时刻、PPID、名称、cgroup、内核标记、原 affinity、effective cpuset、CPU ticks 和逐项时刻 | 新建/退出/复用 TID 没有可靠窗口差值；某次 `ps` 不能代表长期热点 |
| 原始 trace、`report_data.json`、上述快照里的 `cgroup_cpu` | 稳态与峰值并发、运行/等待、竞争和 cgroup quota/节流；用于校准容量 | 快照利用率是窗口均值，不是 P95；单一窗口不足以证明客户服务经常抢核 |
| `net-peak.json` 的前后 `/proc/interrupts`、物理 NIC/PCI、MSI 向量、requested/effective affinity、队列、softirq、ethtool | 每核增量和每秒速率；设备→本地 NUMA/CPU、当前投递、收发队列及 RPS/XPS线索 | IRQ 频率不等于 CPU 时间；没抓到 PCI/队列/NUMA 不能凭中断名绑网卡 |

识别 A2/A3/A5 时，核对宿主机上两条只读命令的原文。

```bash
cat /sys/class/dmi/id/product_name
npu-smi info -t product -i 0 -c 0
```

优先使用设备显式 CPU Affinity。确认 A3 且缺少该字段时，按逻辑 NPU ID 排序，向实际可用的 NUMA 节点均衡分配，节点按数值排序。这是配置策略。硬件亲和在报告中仍应标为未知。

假定 `$HB` 指向本次解包的 `cpu_trace/hostbound` 目录，运行只读盘点。

```bash
python3 scripts/generate_customer_binding.py --hostbound "$HB" --inspect > thread-inventory.json
```

`owners[]` 按宿主 PID 列出 `npus` 与 **所有** TID，包括不认识的名字；`descendants` 是待复核子进程，不是线程。`hot_others` 排的是未直接占卡的用户态和内核候选，可能包含 tokenizer、DataLoader 或共享 daemon，不能原样当作迁移清单。`unsampled_tids/exited_tids` 要保留。检查 `collection_manifest.json`、原快照 `errors[]` 以及不在快照中的占卡 PID。无法打开/解析时报告具体缺项，不编造替代数据。

## 2. 根据业务填两份输入

1. **容量评估 `assessment.json`**。按 NUMA 填写完整 sibling 组、保护核、系统与网络预算。worker 要涵盖同卡所有已核验主 PID。用多窗口的 `cpu_equivalents_p95/parallel_cores` 表示实测并发，附来源、时段和已核验的 quota。推理看 TTFT/TPOT 和关键下发线程，训练看 Rank step、DataLoader、通信和 checkpoint 峰值。对需要使用 A3 策略的 worker 可省略 `node` 和 `eligible_cpus`，由生成器按可用节点容量及线程许可选择。显式填写的节点和核范围会约束决策。数据不足时列缺项。[合成输入](../examples/custom_binding_assessment.json) · [容量方法](custom_binding.md)
2. **操作范围 `policy.json`**：复制[待填模板](../templates/customer_policy.json)。先核实框架的绑核器再将 `framework_binding` 从 `unknown` 改为 `absent` 或 `disabled`；模板的空数组表示不动这些对象。不可照抄客户标识，示例只说明字段：

| 字段 | 每条怎么填、何时准入 |
|---|---|
| `children` | `{"pid":321,"owner_pid":300,"reason":"经业务确认是该 rank 的 DataLoader"}`。前后同代进程树、与占卡父进程相同 cgroup；独立核验后纳入该卡 PID 预算。进程关系不清、共享服务或多卡不同池时不猜 |
| `customers` | `{"pid":700,"tid":704,"node":1,"reason":"服务负责人确认；稳态及峰值重复进入热点榜"}`。逐个 TID 列出，不按名整机匹配。已知非本作业、非其他 NPU 作业/其子孙、非内核且窗口利用率达到 `hot_fraction` 才生成；只用本节点剩余核，不借业务核或其 SMT sibling。未指定的服务保持原样 |
| `irqs`（NIC） | `{"nic":"eth0","irq":90,"reason":"多轮高频 RX 队列且占用落在业务核"}`。在 assessment 的对应物理 NIC 中列该 IRQ；核验前后设备、NUMA、local_cpulist 和 MSI 身份，才在其本地网络池选 CPU |
| `irqs`（其他 PCI） | `{"pci_bdf":"0000:41:00.0","irq":92,"reason":"实测高频存储完成队列"}`。必须有补采到的唯一非 NIC PCI MSI/MSI-X 向量；走该设备本地系统池，`system_cores` 要含这部分处理预算。NIC 和 BDF 二选一 |

如果客户热点线程**已经钉死在业务核**，默认保留原掩码并阻断互不相容的方案。只有服务负责人明确同意更换那个 TID 的旧掩码时，才在对应的 `customers` 条目增加 `"allow_repin":true` 并记明原因；目标仍受有效 cpuset、节点和客户余量池限制。该开关不修改业务线程、cpuset 或整组服务。

对每 TID 用 `Δ(utime+stime)/CLK_TCK/实际秒数` 得出核当量，再与 ftrace 等待、并发核实。`hot_fraction` 默认 0.20（单逻辑 CPU 的 20%，仅筛候选）。[角色词表](custom_binding.md)用于注释 vLLM、ZMQbg、HCCL、HCCN、NPU、RT_RECYCLE、MONITOR、Plog、HCCP、WatchdogMonitor、AtraceMonitor、PlogFlush、python3、acl_thread、release_thread、CaffeTaskThread、adx_data_dump_t、TraceClientRecv、REPORT_RAS、`dev_*`，不是白名单。先认占卡主 PID，再枚举线程，而不是反过来。

中断读取两次，按 CPU 表头对齐，比较每秒计数增量和逐核分布。结合重复热点、与业务重叠和 IRQ/softirq 耗时选择 `irqs`。IPI、`arch_timer` 等本地项、共享线、不可迁移的 managed IRQ、未知节点或动态改号的设备只记录。高频向量优先分散到不同本地物理核。虚拟 NIC 需追到实际 PCI 设备。审核 IRQ 条目时，应覆盖第 4 节所述 irqbalance 服务操作。

## 3. 生成、审阅、交付

```bash
python3 scripts/generate_customer_binding.py --hostbound "$HB" --assessment assessment.json \
  --policy policy.json --network net-peak.json --out customer-bundle
```

`--out` 必须是新目录。没有网络证据且没有规划 NIC/IRQ 时可省略 `--network`，但要在结论中写明 IRQ 隔离未评估。生成时从评估重新计算**有每 PID 上限的**卡池，不复用旧提案中不受上限约束的 CPU 数字；先保留必须使用的物理核，再由负载高、目标受限的线程优先选择。疑似 CPU 密集的进程需求本身超过两核、掩码矛盾、容量/卡归属不明时阻断。不要删除证据强行通过；考虑拆 worker 或让客户重新确认性能取舍。

审阅 `customer_plan.json`（boot、namespace、设备、每卡池、每 TID 原值→目标、窗口负载、IRQ 速率与目标、未处理热点）；另交付给客户一张简表：`卡/节点 | 主PID及线程数 | 活跃线程/核当量 | 预留物理核 | 实际逻辑CPU | 客户/IRQ隔离对象 | 未解决竞争`。例如主线程与繁忙通信线程可分别固定在两个不同物理核的首选逻辑 CPU，冷日志线程共享其中一个；这不表示每张卡一律要两个核。显式核亲和仅约束列出的对象，不代表整机独占或未来新线程已隔离。

## 4. 在原宿主机运行

先打开 `bind_design.json` 核对每条 `custom_bind`。每条指定 `pid`（实际 PID 或 TID）或 `irq_id`，搭配 `cpu_list` 字符串数组。一个 CPU 范围可供本条全部 ID 共享，多个范围须与 ID 逐个对应。范围支持单核、逗号和连字符。进程名仅作角色线索，模板按数字 ID 执行。

| 配置 | 执行效果与现场核对 |
|---|---|
| `pid` + `cpu_list` | 需要 Linux 上可用的 `taskset` 和设置目标 affinity 的权限。默认仅绑定列出的 ID。定制生成器逐个列出已核实的 TID，`bind_sub_process: true` 则会设置指定 PID 当前线程组的全部线程，不递归子进程。重新核对 boot、PID namespace、进程/线程 starttime、cgroup、在线 CPU、有效 cpuset 与原掩码；作业重启或线程集合变化后重新生成。 |
| `irq_id` + `cpu_list` | 脚本可能先通过 systemctl 停止运行中的 irqbalance，再写 `/proc/irq/<IRQ>/smp_affinity`。应核对设备/队列和当前 IRQ 号、requested/effective affinity、写权限及停止服务的授权。 |
| `mem_bind` | 默认 false。当设为 true 时，CPU 绑定之后尝试用 `migratepages` 将可迁移的现有页迁往目标 CPU 所在的单一 NUMA 节点。需要 `lscpu`、`numactl`、`migratepages` 及相应权限；多节点或缺少 CPU 的节点映射时跳过迁移。该选项不为未来分配设置内存策略。 |

生成文件本身只写输出目录。执行前在原 Linux 宿主机确认上述条件、保存实际原值并获得操作授权。该模板没有自动身份预检、事务日志或回滚。将两文件放在同一目录，在此目录运行以下命令。

```bash
python3 cpu_binder_simple.py -c bind_design.json
```

运行后逐项检查日志并读回各 TID 的 `Cpus_allowed_list`、IRQ 的 `effective_affinity_list` 及相关服务状态。模板会记录部分错误后继续处理其他条目，单靠退出码不能确认全部生效。按相同负载复采并比较业务 KPI、客户服务尾延迟和 IRQ 实际落核。需要带 journal 的 CPU 实验时，从现场审计入口单独创建计划；其恢复功能只对应其自身记录的操作。[现场审计与回滚](binding_workflow.md)
