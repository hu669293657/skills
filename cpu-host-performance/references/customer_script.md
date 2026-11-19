# 从采集生成客户专用绑核脚本

这条路径面向“给这台服务器生成脚本”，不改变通用报告。生成器离线工作，现场默认只读。**单线程绑 1 个逻辑 CPU；一个业务进程（TGID）所有线程的目标并集为 1～2 个逻辑 CPU；每卡的池不是每线程的掩码。** 同卡多个进程可以合计超过 2 核。CPU 池是完整物理核预留，包含 SMT sibling；优先用不同物理核做实际执行。CPU 编号不要求连续，不用 `卡号 × 常数`。

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

只在解包目录读，不对客户机器重新猜拓扑。假定 `$HB` 已指向一次完整采集中的 `cpu_trace/hostbound`：

```bash
python3 scripts/generate_customer_binding.py --hostbound "$HB" --inspect > thread-inventory.json
```

`owners[]` 按宿主 PID 列出 `npus` 与 **所有** TID，包括不认识的名字；`descendants` 是待复核子进程，不是线程。`hot_others` 排的是未直接占卡的用户态和内核候选，可能包含 tokenizer、DataLoader 或共享 daemon，不能原样当作迁移清单。`unsampled_tids/exited_tids` 要保留。检查 `collection_manifest.json`、原快照 `errors[]` 以及不在快照中的占卡 PID。无法打开/解析时报告具体缺项，不编造替代数据。

## 2. 根据业务填两份输入

1. **容量评估 `assessment.json`**：结构见[合成格式示例](../examples/custom_binding_assessment.json)和[容量方法](custom_binding.md)。每个 NUMA 列全部可用的完整 sibling 组、已保护核、系统与网络预算；worker 以**同卡所有已核验主 PID** 为单位。`eligible_cpus` 是该卡的实际本地可用范围，不是所有线程原掩码的机械交集。按多窗口真实并行需求给 `cpu_equivalents_p95/parallel_cores`，附来源、时间和已核验的 quota。推理看 TTFT/TPOT 和关键下发线程；训练看 Rank step、DataLoader、通信和 checkpoint 峰值；混部逐 worker 标记。没有这些数据先出盘点/缺项，不从名字填 `verified=true`。
2. **操作范围 `policy.json`**：复制[待填模板](../templates/customer_policy.json)。先核实框架的绑核器再将 `framework_binding` 从 `unknown` 改为 `absent` 或 `disabled`；模板的空数组表示不动这些对象。不可照抄客户标识，示例只说明字段：

| 字段 | 每条怎么填、何时准入 |
|---|---|
| `children` | `{"pid":321,"owner_pid":300,"reason":"经业务确认是该 rank 的 DataLoader"}`。前后同代进程树、与占卡父进程相同 cgroup；独立核验后纳入该卡 PID 预算。进程关系不清、共享服务或多卡不同池时不猜 |
| `customers` | `{"pid":700,"tid":704,"node":1,"reason":"服务负责人确认；稳态及峰值重复进入热点榜"}`。逐个 TID 列出，不按名整机匹配。已知非本作业、非其他 NPU 作业/其子孙、非内核且窗口利用率达到 `hot_fraction` 才生成；只用本节点剩余核，不借业务核或其 SMT sibling。未指定的服务保持原样 |
| `irqs`（NIC） | `{"nic":"eth0","irq":90,"reason":"多轮高频 RX 队列且占用落在业务核"}`。在 assessment 的对应物理 NIC 中列该 IRQ；核验前后设备、NUMA、local_cpulist 和 MSI 身份，才在其本地网络池选 CPU |
| `irqs`（其他 PCI） | `{"pci_bdf":"0000:41:00.0","irq":92,"reason":"实测高频存储完成队列"}`。必须有补采到的唯一非 NIC PCI MSI/MSI-X 向量；走该设备本地系统池，`system_cores` 要含这部分处理预算。NIC 和 BDF 二选一 |

如果客户热点线程**已经钉死在业务核**，默认保留原掩码并阻断互不相容的方案。只有服务负责人明确同意更换那个 TID 的旧掩码时，才在对应的 `customers` 条目增加 `"allow_repin":true` 并记明原因；目标仍受有效 cpuset、节点和客户余量池限制。该开关不修改业务线程、cpuset 或整组服务。

对每 TID 用 `Δ(utime+stime)/CLK_TCK/实际秒数` 得出核当量，再与 ftrace 等待、并发核实。`hot_fraction` 默认 0.20（单逻辑 CPU 的 20%，仅筛候选）。[角色词表](custom_binding.md)用于注释 vLLM、ZMQbg、HCCL、HCCN、NPU、RT_RECYCLE、MONITOR、Plog、HCCP、WatchdogMonitor、AtraceMonitor、PlogFlush、python3、acl_thread、release_thread、CaffeTaskThread、adx_data_dump_t、TraceClientRecv、REPORT_RAS、`dev_*`，不是白名单。先认占卡主 PID，再枚举线程，而不是反过来。

中断读取**两次**，按 CPU 表头对齐，比较 `Δ计数/实际间隔`，连同逐核分布排序。结合重复热点、与业务重叠和 IRQ/softirq 耗时选择 `irqs`，不要把每个有增量的 IRQ 都迁走。IPI、`arch_timer` 等本地项不进入候选；共享线、不可迁移的 managed IRQ、未知节点、动态改号的设备只记录。把高频向量优先分散到不同本地物理核；不自动停 irqbalance，不改 RSS/RPS/XPS 或迁内存。若网络路径经过虚拟 NIC，追到实际 PCI 设备，不能直接使用虚拟接口的 `-1` NUMA。

## 3. 生成、审阅、交付

```bash
python3 scripts/generate_customer_binding.py --hostbound "$HB" --assessment assessment.json \
  --policy policy.json --network net-peak.json --out customer-bundle
```

`--out` 必须是新目录。没有网络证据且没有规划 NIC/IRQ 时可省略 `--network`，但要在结论中写明 IRQ 隔离未评估。生成时从评估重新计算**有每 PID 上限的**卡池，不复用旧提案中不受上限约束的 CPU 数字；先保留必须使用的物理核，再由负载高、目标受限的线程优先选择。疑似 CPU 密集的进程需求本身超过两核、掩码矛盾、容量/卡归属不明时阻断。不要删除证据强行通过；考虑拆 worker 或让客户重新确认性能取舍。

审阅 `customer_plan.json`（boot、namespace、设备、每卡池、每 TID 原值→目标、窗口负载、IRQ 速率与目标、未处理热点）；另交付给客户一张简表：`卡/节点 | 主PID及线程数 | 活跃线程/核当量 | 预留物理核 | 实际逻辑CPU | 客户/IRQ隔离对象 | 未解决竞争`。例如主线程与繁忙通信线程可分别固定在两个不同物理核的首选逻辑 CPU，冷日志线程共享其中一个；这不表示每张卡一律要两个核。显式核亲和仅约束列出的对象，不代表整机独占或未来新线程已隔离。

`bind_design.review.json` 沿用参考脚本的 `custom_bind`、`pid` **或** `irq_id`、`cpu_list`；`pid` 放确切 TID，`bind_sub_process=false`、`mem_bind=false`。它是**对照件**，不是带防护的执行入口，不应用参考工具绕过预检。可执行入口由 [shell 模板](../templates/bind_customer.sh.in) 和随包 Python 标准库程序组成；两文件同目录，无需在客户 Linux 宿主机安装 Skill 或第三方包，需 Python 3.8+、可用的 `npu-smi` 和相应权限。

```bash
sh bind_customer.sh show                         # 离线看完整冻结计划，不读取宿主机
sh bind_customer.sh                              # 现场只读预检；不会重新算目标
sh bind_customer.sh apply --state /private/run1.json --include-customers --include-irqs
sh bind_customer.sh verify                        # 读回、复核身份/拓扑/线程集合
sh bind_customer.sh rollback --state /private/run1.json
```

只在得到变更授权时执行 `apply`，选项按实际计划范围给；状态文件须新建在受信任的私有目录。相同计划不要并行执行。先复核 boot/namespace、NPU 所有者、SMT、TID 出生时间/cgroup/cpuset、IRQ 的设备/本地性和原掩码；写前落日志，逐项读回，失败尽力回滚。驱动拒写、CPU 下线、线程漂移或 irqbalance 活跃/状态不明时放弃，不自动停服务或抢写。恢复遇对象复用、第三方已改值或不可恢复时跳过并报不完整。`verify` 只证明配置；同负载复采看业务 KPI、客户服务尾延迟/吞吐、IRQ 实际落核、网络 drops 和 backlog；不达标则回滚。没有在客户机执行时，不写“已隔离”或“性能提升”。
