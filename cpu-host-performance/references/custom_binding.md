# 按实测负载定制 CPU / NPU / NIC 方案

用户需要按服务器情况分核、推理/训练区别、网卡预留或其他服务隔离时读取本页。先完成证据和容量表，再产出具体方案。**要交付脚本，接着按[客户脚本生成方法](customer_script.md)编译并核验，而不是运行旧 `bind_cpu.sh`**。本页的 `plan_custom_binding.py` 仍只做容量建议，不会执行。

## 1. 识别作业与所有权

采用同一采集与执行基础，按作业分 `inference` / `training` / `mixed` 策略；未知业务类型先从启动参数、框架与用户目标核实，不从一个 python 名称猜测。推理内部还区分请求入口、prefill/decode、分词、模型 worker 与通信；训练区分 Rank、DataLoader/CPU预处理、pin-memory、通信与检查点。混部按作业分别预算。

先列全部 NPU 占卡宿主机 PID，不以显存最大的一个代替全部。以 `(boot_id, host PID namespace, TGID, process starttime)` 标识进程，以 `TID + thread starttime` 标识线程；枚举每个主进程 `/proc/PID/task/` 的**全部线程**，包括名字未命中的线程。子进程需同代 PPID 链、cgroup/容器和占卡关系交叉核验；`taskset -a` 只覆盖线程组，不递归子进程。

以下名称是用户环境中的候选角色线索，大小写兼容并考虑 Linux comm 截断；用 cmdline/完整框架信息佐证截断前缀，不扩大所有权：

| 线索 | 候选角色 |
|---|---|
| vllm、python3、npu、以 `dev_` 开头 | 引擎/运行时/设备相关；python3 与 dev_* 尤其不能独立证明是本作业 |
| ZMQbg、hccl、hccn、hccp | IPC/集合通信/设备通信；需区分用户线程、共享守护进程与内核任务 |
| acl_thread、CaffeTaskThread | Host 下发/执行 |
| release_thread、RT_RECYCLE | 资源回收 |
| MONITOR、WatchdogMonitor、AtraceMonitor、Plog、PlogFlush、adx_data_dump_t、TraceClientRecv、REPORT_RAS | 监控、日志、跟踪与可靠性；按实测活动分配，不逐名独占一个核 |

名称只改变角色注释，不改变已验证的进程归属。独立的共享 daemon、`dev_*` 内核线程、客户 python 服务保持“未知/其他服务”，直到有归属证据。同卡多 PID 合并该卡需求，不能重复预算。同 PID 多卡只在全部卡拥有经过验证的同一本地公共池时作为一个共享 worker；跨不同池且无 TID→卡映射时阻断逐卡绑核。

## 2. 需要哪些证据

保留原 `cpu_trace_collect.sh` 的单文件采集。3秒默认窗口可作初诊，容量规划需另取代表性稳定窗口及突发/峰值窗口（例如多轮5～30秒，按业务周期调整），与业务压测记录对齐；避免用空闲窗口推断生产预算。Trace 丢失、热身、进程重启、CPU热插拔或工作负载变化时，不把缺项记作0。

| 证据 | 用途 |
|---|---|
| CPU online、NUMA cpulist、socket/core/thread_siblings、每 TID cpuset/affinity/quota | 可调度物理核组、硬边界、既有细分策略；核数足够也可能受quota限制 |
| NPU全局逻辑ID/物理映射、占卡PID、显式CPU Affinity | 设备本地范围；不从卡序号或LOCAL_RANK猜NUMA |
| 每线程窗口CPU时间、running/runnable并发分布、等待P95/P99、迁移、进程树 | 每卡需求、关键线程和是否被挤压 |
| 全机非目标线程的同期运行、等待、CPU许可、cgroup与服务标识 | 客户服务/其他作业/系统保留预算；top/ps累计CPU%仅线索 |
| NIC→PCI BDF→NUMA/local_cpulist、MSI IRQ、队列、RSS/RPS/RFS/XPS、irqbalance | 网络处理路径和本地隔离候选 |
| 同负载业务指标 | 推理TTFT/TPOT、P95/P99、tokens/s；训练step time/样本吞吐、Rank最大等待、数据供应、通信占比 |

已存在的窄线程掩码先保护。若确需改变，单列需要调整的 TID、原因和原值；不能由统一主进程掩码悄悄覆盖。未占卡的 tokenizer/DataLoader 等按真实负载纳入规划，但现有主进程执行器不会自动绑定它们。

## 3. 网卡与高频中断

补充采集需完整 skill 目录、Linux宿主机和Python 3.8+：

```bash
sudo python3 scripts/collect_network_evidence.py --interval 5 --output network-window-1.json
```

这是独立补充入口，未嵌入单文件 cpu_trace_collect.sh。一次采两次 `/proc/interrupts`、`softirqs`、`stat`、`net/dev`、`softnet_stat`，保留真实单调时钟间隔、读取跨度、boot/online 与前后 NIC 元数据。读取队列配置、requested/effective IRQ affinity，并在计数窗口后读取 ethtool 信息和 irqbalance 状态。缺命令/权限/属性保留 errors/status；不自动停止服务或写配置。JSON 中 topology、counter 和元数据时间窗并非原子快照。

1. 按表头 CPU编号逐列计算 `delta / 实际秒数`，不能假定 CPU0..N 连续。排序差值及频率筛选热点，再关联 handler 耗时与业务同核同期等待。
2. IRQ增加/消失、名字变化、计数回退、boot变化、CPU表头变化分开报缺项，不能给负速率或当成0。IPI、arch_timer 等每CPU本地中断不列为可迁移设备IRQ；若异常增加仍解释其开销，不能丢掉诊断。
3. 通过 `/sys/class/net/IF/device/{numa_node,local_cpulist,msi_irqs}` 与驱动信息定位物理网卡；`numa_node=-1` 是未知，不是node0。bond/VLAN/veth需沿lower/master追到物理设备，SR-IOV/RDMA需核实实际PCI功能与队列；HCCN设备网络不自动等同普通netdev。没有可靠本地性时只给条件方案。
4. 逐IRQ与队列核对 `/proc/interrupts` 和 ethtool，MSI IRQ列表不是完整队列语义，也可能含管理/异步事件。`irq/N/smp_affinity_list` 是请求许可，`effective_affinity_list` 是实际生效范围；managed IRQ/控制器限制时可能不可写。设备IRQ还可能来自NVMe、NPU等，不能全部算作网卡。
5. 只写IRQ不足以约束软中断：检查RSS indirection、`rx-*/rps_cpus`、RFS、`tx-*/xps_cpus`/xps_rxqs、NET_RX/NET_TX和ksoftirqd。RFS可能追随应用CPU，busy-poll线程可能在用户上下文运行；严格隔离是否可行应单独说明。多队列RSS足够时RPS可能不需要，不默认双重扩散。不要绑死每CPU ksoftirqd 或不可迁移内核线程。
6. irqbalance 可能覆盖手工设置。优先设计范围明确的排除/策略（按已安装版本核验）；停全局服务只能是明确列出的实验动作，记录原 active/enabled 状态和原策略，保留恢复路径。多个控制器不能同时反复改同一IRQ。

### 网卡预留比例

以**网卡所在NUMA的可调度物理核组数**为分母，同时报告核数与比例；明确分母是否已排除离线/不可分配核。先保护已有客户服务资源，但比例分母仍用完整可调度节点容量，避免混淆。“其他服务”所需容量必须独立预留，不能先分完再挤到余核。

没有代表性耗时数据时，10%～20%（典型15%）仅作为小步A/B试验的候选范围，并标 `INSUFFICIENT_EVIDENCE`，不生成伪精确正式核数。高速网卡/小包/PPS、队列、offload、RDMA、CPU代际差别很大，链路带宽和IRQ次数都不能单独确定比例。

有可信多窗口数据时：

`C_net = ceil(P95(网络服务CPU核当量) / U_net)`，初始 `U_net=0.60`，按突发和尾延迟调整。

核当量的基本量纲为去重CPU秒/窗口秒；须包含可归因的硬IRQ、NET_RX/TX等softirq、网络poll线程，扣除嵌套与ksoftirqd重复计时。不能把全部softirq算本网卡，或把业务通信线程再次算入网络池。多网卡共享NUMA使用同步窗口的合计后取P95，不能简单把各自P95相加。多卡/多NIC无法归因时给上界/不确定性，不冒充实测精确值。

SMT逻辑CPU秒不是独立物理核吞吐，输入工具前应按每核单线程基线/业务压测校准；没有校准时保留物理核范围与不确定性，不直接除以SMT线程数。IRQ优先落在每个预留物理核的一个逻辑CPU，同核兄弟不交给待隔离的NPU任务。

例如32物理核节点、网络需求6核当量，候选10核=31.25%。超过20%不截断。若网络池负载持续偏高、softnet丢包/time_squeeze、单队列饱和或业务尾延迟恶化，先区分队列不均衡与总容量不足，再扩池/调整队列；多加核未必能缓解单流热点。

## 4. 每卡需求与CPU分配

`C_worker = max(经验证的并行物理核需求下界, ceil(P95 CPU核当量 / U_worker))`，初始 `U_worker=0.70`。这只是可修改的候选预算，不是官方标准。并行下界由同时running/runnable、等待尾部和业务瓶颈一起判断；不要把总线程数或平均runqueue等待直接当作物理核数。轮询线程100%CPU也不一定加核能加速，应配合设备与业务指标。

| 模式 | 分配重点 | 验收重点 |
|---|---|---|
| 推理 | 引擎下发/回收关键路径、通信、入口/分词；prefill/decode不同负载分窗，监控日志共享轻量容量 | 相同模型、输入输出长度分布、并发/QPS下TTFT/TPOT和P99，以及吞吐/SLO |
| 训练 | Rank主线程、通信、DataLoader和CPU预处理、pin-memory；按慢Rank和数据供应测量，不照搬推理小池 | 同batch/序列/并行/数据集的step time、吞吐、通信与数据等待，检查点另取窗口 |
| 混部 | 按作业标签各算预算，整节点汇总；共享daemon单计 | 每个作业都满足指标，不能只改善一个牺牲另一个 |

按完整在线 `(socket,node,core)` sibling组分配；跨隔离池不能拆SMT兄弟。每个worker候选范围取NPU显式亲和∩online∩各TID有效cpuset，并保护其他作业/已有本地窄掩码。空交集、未知SMT、多NUMA、部分核心组不可用时停止具体分配。

容量守恒：`客户保护 + 系统 + 网络 + 各worker + 剩余 = 可调度物理核`。各池互斥才可称规划上分离；真正独占还需其他作业/cgroup/IRQ遵守边界，taskset本身不提供独占。剩余池用于已获授权的后台服务方案和突发余量，不扫描后搬迁全机所有未知进程。

超额时报告缺核数或候选集合冲突，提供调整作业布局/并发、服务迁移、共享或增加资源等选择及指标代价。未授权迁移客户业务时保留原状态。未知或未采到的活跃卡/作业占用也要先保留，不能把空闲或未选中卡的既有资源当作可抢占容量。

## 5. 可复现离线建议

代理先根据真实采集整理 `assessment.json`，每个计算字段追溯原始来源/窗口；工具不自动从任意报告猜字段。结构示例在 [合成输入](../examples/custom_binding_assessment.json)，仅演示格式，不能代替现场证据。

```bash
python3 scripts/plan_custom_binding.py --input assessment.json --output binding_proposal.json
```

- `evidence`：`identity_verified/topology_verified/load_representative` 只有核验后才能true；`source/window`写文件和时间窗，可追加逐字段provenance。
- `nodes[].cores`：本节点可调度、完整在线SMT物理核组，非连续区间推断。`protected_cpus`覆盖客户/其他作业及必须保留的既有策略，工具扩展到整物理核；`system_cores`为实测/明确策略预算（至少1，工具下界，不代表所有机器足够）。`network_core_equivalents_p95`必须实测，未知用null；没有网络流量时0也需证据。
- `nics[]`：已经核验的物理设备名、node和需规划的IRQ。共享IRQ/重复PCI功能先合并，管理IRQ与数据队列由现场区分。脚本给轮询分配候选，后续按队列负载加权审核，不据此宣称负载均衡。
- `workers[]`：逻辑分配单元id、node、全局npus、同池pids、`eligible_cpus`、校准的`cpu_equivalents_p95`与`parallel_cores`。同NPU多PID合并；多NPU共享PID需验证所有设备公共池再设置`shared_device_pool_verified=true`。mixed时每worker提供workload标签。子进程归属及quota由代理核验，不由布尔标志代替真实证据。

返回 `PROPOSAL`、`INSUFFICIENT_EVIDENCE` 或 `BLOCKED`；后两者无部分CPU方案。成功输出客户/系统/网络/worker/剩余池、每节点网卡物理核数与比例、IRQ候选。确定性贪心先放受限worker再从弹性余量留网络/系统，最终检查总预算；复杂交叠候选可能需要人工重排，工具失败不证明全局无解。

**`binding_proposal.json` 是审阅建议，不是 `ascend_bind.py apply` 的 plan.json。** 不改其schema或手工伪造plan_id。当前执行器locality/partition不会消费建议CPU池，也不执行IRQ/RPS/客户服务迁移。用户要执行定制方案时，先把具体PID/TID、设备IRQ、原值、目标、前置核验、验证和回滚准备成可审阅执行方案；只有现有事务已支持的动作才能声称有该事务保护。运行旧bind_cpu.sh可能重新生成不同的locality目标，不能用它声称已应用本方案。

## 6. 方案交付与验收

保留原诊断HTML/JSON/shell交付，并为本分支补充 `assessment.json`、`binding_proposal.json`、网络原始证据；在给用户的方案中列出按NUMA的预算表、每卡所有PID及线程归属、每NIC/IRQ/队列目标、未知项、其他服务处理、执行能力边界。现有HTML不会自动渲染新建议；不要称已集成该展示。

执行前记录每TID原掩码与身份、IRQ对应PCI设备/队列与requested/effective值、RPS/XPS/RSS与irqbalance状态。先读回再观察实际分布；CPU/IRQ/设备重建或新线程出现时重新核验，旧IRQ号和PID不可盲目复用。内存迁移/NUMA内存策略另外评估，不由绑核隐式触发。

同负载A/B/A或交叉实验，保留业务主指标和坏尾、客户服务指标、网络drops/time_squeeze、队列偏斜与IRQ实际CPU、任务实际运行CPU/等待。仅读回成功不证明性能改善；既要验证改动，也要确认其他服务未退化。回滚按身份及当前值核验后恢复原值，IRQ重建/驱动不支持/外部更改要明确恢复不完整。

## 官方依据

- [Linux IRQ affinity](https://docs.kernel.org/core-api/irq/irq-affinity.html)：请求CPU列表/掩码和控制器支持。
- [Linux Networking Scaling](https://docs.kernel.org/networking/scaling.html)：RSS/RPS/RFS/XPS、队列与irqbalance。
- [util-linux taskset 手册](https://man7.org/linux/man-pages/man1/taskset.1.html)：`--all-tasks`覆盖PID的线程与亲和性限制。

60%、70%、10%～20%均为本skill可调整的试验参数，不来自上述文档的硬件标准。
