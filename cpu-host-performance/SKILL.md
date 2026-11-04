---
name: "cpu-host-performance"
description: "Linux 训练/推理 HostBound 定位（怀疑 Host/CPU 侧拖慢 NPU/GPU）。当用户需要现场采集或离线分析 ftrace、Ascend Host 快照、可选 GIL/函数 PMU/msprof 数据来定位 Host 侧瓶颈时使用，覆盖 acl_thread、release_thread、SQ/CQ、IRQ、NUMA、CPU affinity/抢占、Python GIL、函数长尾、Host/Device overlap。不用于 Prof 数据的性能分析与比对（用 cluster-analysis / prof-compare / cluster-compare）；不用于只分析 NPU 算子本身；不用于报错/中断等故障日志排查（用 issue-analyzer）。"
---

# HostBound 性能诊断

面向 Ascend 训练/推理的 Host 侧定位与优化闭环。保留原有 ftrace 采集和离线分析能力；官方 `msProf`、`function_monitor`、`gil_tracer`、`host_analyzer` 始终作为独立外部工具管理，绝不修改或复制其源码。

## 固定交付

最终交付物固定为 `<output-dir>/hostbound_report.html`，单文件、离线可打开、中文。可同时保留 `report.md` 作为审阅辅助，但不可用它替代 HTML 报告。

报告采用 13 节顺序：1 结论摘要；2 问题证据总表与根因判断；3 优化建议；4 问题详情；5 CPU 总体状态；6 各核心负载；7 调度；8 IRQ/SoftIRQ；9 频率/Idle；10 Task/Thread；11 Affinity/NUMA；12 时间窗口；13 数据说明与指标解释。

第二部分必须直接解释CPU平均利用率、单核最高利用率、全机上下文切换率、调度延迟P99、硬中断总占比、软中断总占比，并呈现根因判断/候选证据链，以及 NPU 任务与 NUMA 亲和性。HTML 按 NUMA 分组、每张 NPU 一张任务卡，列出主进程（按显存占用最高推定）、所有占卡宿主机 PID、线程数、NPU 亲和 CPU、当前线程 allowed CPU、ftrace 观测 CPU、PSR 最近运行核、“观测 NUMA → 亲和 NUMA”、CPU 绑定状态和建议分区；MD 用对应表格。根因不再单列尾部章节，具体优化动作统一放在第三部分。

## 统一入口与采集原则

默认发给客户的入口是：

```bash
sudo bash scripts/cpu_trace_collect.sh --duration 30 --output /tmp --hostbound
```

采集必须在物理机/宿主机运行。`scripts/cpu_trace_collect.sh` 是唯一采集入口，默认全量 ftrace，增强采集需同目录的 `host_snapshot.py`（Python3.7+标准库，缺失时明确降级）；`--hostbound` 自动发现全部 NPU 占卡 PID，`--pid <PID[,PID...]>` 还可指定宿主机根TGID；`--discover`可不依赖NPU直接启用/proc发现。两种方式均增加 CPU/NUMA/SMT、在线 CPU、IRQ、NPU 拓扑/进程、宿主机上下文、进程身份、有效 cpuset 及全线程 affinity 只读快照（`cpu_trace/hostbound/`）。仅在存储或风险限制明确时才用 `--minimal`。清单记录未运行的外部工具，避免把缺失误当正常。

采集前根据现象生成只读工具计划：

```bash
python3 tools/tool_router.py --signals python,gil,irq,numa,device-gap
```

工具选择不是自动执行授权。GIL、PMU、业务插桩、CANN Profiling 和绑核都必须逐项满足前置条件并获得用户授权。

## 工具选择

| 条件 | 补充工具 | 采集边界 |
|---|---|---|
| 一切 HostBound 初诊 | ftrace + HostBound 快照 | 必选；全量采集调度、迁移、IRQ/SoftIRQ、idle、频率 |
| Python 多线程、锁等待、下发线程看似空闲 | 官方 gil_tracer | root、sysTrace、指定 PID、短窗口；无侵入 |
| 已知业务段慢、Cache Miss/Page Fault/IPC 嫌疑 | 官方 function_monitor | 需用户允许 PyTorch 业务插桩；PMU 需 libkperf 与权限 |
| Device 空泡、Host Runtime 与 Device task 重叠关系 | 已有/按官方采集的 msprof collection-dir | 只导入/导出，不修改 CANN/msProf 源码 |
| IRQ/affinity/NUMA 争用已有证据，需要优化验证 | 官方 host_analyzer | 只生成建议和预检；不得自动绑核、写 IRQ affinity 或停止 irqbalance |

外部工具详细契约见 [官方工具适配说明](references/official_tool_integration.md)。

## 诊断方法

1. 先解析 ftrace 和快照，建立 CPU、调度、抢占、IRQ/SoftIRQ、迁移、频率、NUMA、affinity 的时间窗证据。
2. 读取 [Ascend Host Thread Knowledge](references/ascend_host_thread_knowledge.md)，将 `acl_thread`、`release_thread`、SQ/CQ、IRQ 作为候选对象；名称匹配不能单独形成结论。
3. 有补充数据时，按相同业务稳定窗口关联 GIL、函数耗时/PMU 和 msprof Host/Device 时间线。时钟域不能证明一致时，明确标注为近似关联。
4. 只按 [HostBound Diagnosis Rules](references/hostbound_diagnosis_rules.md) 输出 finding。每个 finding 必须含 Metric、Value、Threshold 或 Baseline、Evidence source、Time window、Confidence。
5. 无至少两类独立证据时，输出 `INSUFFICIENT_EVIDENCE` 或“待验证假设”，不得声称根因。CPU 高、IRQ 多、GIL 事件多、Cache Miss 高均不能单独判定 HostBound。
6. HostBound 的确认还必须有 Device 空泡/Host-Device overlap 失配，以及至少一项对应 Host 侧证据；没有 CANN/Device 时间线时只能表述为“Host 侧风险/疑似”。
7. 涉及 NPU 任务、NUMA 或绑核方案时，读取 [NPU/NUMA 判断与绑核规则](references/npu_numa_binding.md)。用真实 NUMA 拓扑映射节点；allowed CPU 与运行观测分别判断。共享亲和 CPU 的 NPU 需分配互不重叠区间，保持 SMT 同胞同组；现有亲和区间已互斥时保留分区。信息不足显示“待核验”，并给出最小补采集，不能从卡号推断 NUMA/PID。

## 优化与安全边界

- 先保留原始采集包、affinity、IRQ 和 NUMA 快照，再提出优化方案。
- `host_analyzer` 是优化执行器，不是诊断器。禁止自动执行 `taskset`、`migratepages`、写 `/proc/irq/*/smp_affinity` 或停止 `irqbalance`。
- 优化必须由用户确认，并以前后相同负载、同一窗口的复采结果验证；无收益或副作用须给出回滚方案。
- 第三部分对亲和性偏离/未收敛的任务给出具体到宿主机 PID 的可复制 `taskset -acp CPU列表 PID` 方案，连同预检、逐 TID 备份、核验和回滚。生成建议属于离线报告，不需要另行申请执行权限；实际执行仍须用户授权。证据不足、同 PID 跨多卡或有效 cpuset 不允许的项，明确阻塞原因并补采，不能生成猜测命令。
- 离线报告仅使用现有包；不要求客户为已有数据重复采集。信息不足时一次性给出“最小补采集组合”，优先同窗口 ftrace + 进程/拓扑快照，随后才按疑点请求 GIL、PMU 或 msprof。

## 离线分析

```bash
# 在本 Skill 根目录 (cpu-host-performance/) 下执行
python3 -m analyzer analyze <采集包或trace> --output-dir <输出目录>
# 或在任意目录直接运行入口脚本
python3 <Skill路径>/analyzer/analyze_cpu_trace.py <采集包或trace> --output-dir <输出目录>
```

查看生成的 `hostbound_report.html`。分析器不应因缺少 GIL、PMU、msprof 而失败；必须在报告的数据覆盖与限制章节明确这些数据是否存在。


## 频率、线程竞争与全核报告（v2）

采集与数据契约需要核对时读取 [v2分析说明](references/analysis_v2.md)。全量显式使用`--full`，默认低开销sysfs轮询1秒，可用`--frequency-interval 2`调整（最小0.2秒）；`--minimal`仅sched_switch，仍保留sysfs频率，不支持完整wakeup延迟。每次使用新的输出目录，不覆盖已有cpu_trace。私有trace instance不受支持时安全停止全局采集，可由客户提供已有trace或helper只读快照。

分析后查看`hostbound_report.html`，并保留`report_data.json`（schema_version=2，时间秒/延迟ms/频率MHz/比例百分比）。第6节支持全CPU折线/柱状图、指标显隐、NUMA与编号范围；第9节按policy展示频率来源与状态；第10节展示TID/TGID发现和四类Top榜、Worker-NPU-CPU/NUMA候选链与cgroup节流增量。

- 缺少cpu_frequency事件时先查看主状态与事件子状态：区分TRACE_AVAILABLE、SYSFS_AVAILABLE、STATIC_ONLY、NO_TRANSITION、UNSUPPORTED、COLLECTION_FAILED，以及NOT_REQUESTED/UNKNOWN。有效sysfs数据不能因为trace没有转换而被否定。NO_TRANSITION不表示降频，缺失不显示0MHz。
- 频率按related_cpus策略域去重；标准接口kHz显式转MHz；时间加权平均只覆盖已知样本持续区间。低频比例仅使用cpuinfo_max_freq参考；驱动请求频率不是已经验证的实际频率。PMU未获平台语义/权限验证时保持NOT_VALIDATED，不运行或输出估算频率。
- sched_switch prev_pid/next_pid是TID。依据/proc任务starttime及生命周期区分身份；无TGID证据保留null，跨代/丢失/退出/边界不完整等待不填补。角色依据命令、关系、NPU/cgroup及comm并给出证据，不能将所有Python进程认定为训练。
- Runqueue Wait包含已完成成功唤醒与R/R+切出等待；Wait P99仅成功wakeup→switch。报告注明样本量、重复/未配对和丢失情况。R/R+是可运行态切出事实，不能断言其他线程因果性抢占。
- 只有可运行等待和同CPU执行重叠才形成竞争候选；共享Affinity或CPU占用高不能单独形成根因。cgroup quota及throttling用前后增量和祖先限制解释，不能归为整机饱和。未对齐Device时间线仅报告Host侧相关风险。
- IRQ/SoftIRQ全机比例使用所有有效CPU总观测时间分母，同时保留单核最大值。静默/未采集CPU或缺失事件保留N/A；低平均利用率不能排除局部热点。
- 第二节六个解释均需包含当前值、定义/公式、作用、动态说明、结论限制与可信度。工具阈值为经验筛选，性能结论需同负载基线验证。

回归与合成示例命令见README。合成数据必须保留SYNTHETIC_TEST_DATA标记，不冒充真实采集。禁止自动执行优化命令、训练或PMU，不自动commit/push。
