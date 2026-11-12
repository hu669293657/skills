---
name: "cpu-host-performance"
description: "Use when Linux/Ascend 训练或推理疑似被 Host/CPU 拖慢，需要采集或分析 ftrace、Host 快照、系统与 Ascend 运行环境、GIL/PMU/msprof，或需要 Rank/线程识别、NUMA 感知绑核、同负载性能验证与回滚时。适用 acl_thread、release_thread、DataLoader、HCCL、IRQ、CPU affinity、调度等待、Python GIL 和 Host/Device overlap；不用于仅 NPU 算子分析、通用 Prof 比对或故障日志排错。"
---

# HostBound 性能诊断

面向 Ascend 训练/推理的 Host 侧定位与优化闭环。保留原有 ftrace 采集和离线分析能力；官方 `msProf`、`function_monitor`、`gil_tracer`、`host_analyzer` 始终作为独立外部工具管理，绝不修改或复制其源码。

## 固定交付

诊断交付物为 `<output-dir>/hostbound_report.html`、`report_data.json` 和 `bind_cpu.sh`。HTML直接从指标生成，单文件、离线可打开、中文；不生成Markdown报告。默认仅诊断/规划；`bind_cpu.sh` 默认只读预检/预览，显式执行委托同一 journal 执行器。闭环实验另保存 `plan.json`、`state.json`、前后业务指标及 `verification.json`。实际绑核与自动回滚须在用户已授权的范围内，不能把本地优化 skill 的请求当作对生产主机的执行授权。

## 绑核闭环入口

涉及绑核优化、Rank、执行、性能验证或恢复时，先读 [闭环操作与数据契约](references/binding_workflow.md)。统一入口为 `python3 scripts/ascend_bind.py`，执行器要求 Linux 宿主机、完整 Skill 目录和 Python 3.8+。

1. **Trace 诊断**：用原入口采集/分析稳定业务窗口，保留原包与 report_data.json；按下文证据规则区分 Host 风险与已确认 HostBound。诊断允许数据不足，执行计划不允许填补未知身份/拓扑。
2. **识别与计划**：`plan --report-data ... --pid ROOT_PID --output plan.json` 只读发现占卡主 PID、白名单 Rank 标签及主进程的全部线程，逐 TID 核对 cpuset。预览脚本用 `--main-pid` 指定确切的占卡主 PID。LOCAL_RANK/visible devices 仅作标签，不推断全局 NPU；同进程多卡且池不同则阻断。名称仅作角色线索。输出 plan 后查看所有 workers/actions/pools/warnings。
3. **NUMA 分配**：默认 locality，保留合规窄掩码；显式 partition 仅为均分实验候选，按全部 NPU 和完整 SMT sibling 组分片，不以当前活跃卡数量扩大池。缺映射、跨 NUMA、空交集或冲突拒绝计划；不固定预留 Main/ACL/Release 核数。
4. **执行**：先确认框架 binder 已关闭或不存在，记录 `--framework-binding disabled|absent`。`apply --plan ... --state ...` 只预检；授权后 `--execute` 全量预检、写入原掩码日志、按主 PID 执行 `taskset -apc`、逐线程读回和审计；失败尽力自动恢复。用户已经授权该实验时按范围继续，不逐命令重复确认。
5. **性能验证**：同负载复采并由业务提供 before/after 指标（默认各至少3轮、每轮去热身后30样本），用 verify 比较主指标中位数和坏尾、核验 after Trace 的目标身份/affinity 及 plan_id。绑核读回只证明配置生效；无业务数据/样本不足/缺配置证据为 INCONCLUSIVE。保存实际调度、竞争和 NUMA 辅证。
6. **恢复/保留**：收益达标且坏尾合格才建议保留；无收益/退化建议 rollback。`verify --rollback-on-fail --execute` 才授权任何非 PASS 自动恢复；`rollback --state ... --execute` 可单独恢复。记录每项恢复/退出/复用/冲突/失败；新任务继承状态无法精确还原时明确 UNTRACKED / RESTORE_INCOMPLETE。PID 重启或拓扑变化重新诊断/规划，不沿用旧身份。

不默认执行 IRQ/dev 绑核、停 irqbalance、migratepages、强制内存绑定、训练启动或全局调优。有限 watch 仅审计，发现漂移/新任务即停止并恢复已跟踪项，不持续抢写框架策略。`taskset` 按 PID 枚举线程，执行器在调用前后核验身份和线程集合。

报告采用 6 节。1 结论摘要，2 问题证据总表与根因判断，3 优化建议，4 机器资源与 CPU 总体状态，5 各核心负载，6 采集环境与软件栈。

第4节用一张离线资源图展示占用、等待压力与可能的性能限制。先筛选会影响当前任务的资源，再用可用快照计算指标。整机展示 CPU 忙碌与等待、内存峰值、磁盘 I/O 和最紧张挂载点；识别到任务资源限额时，再展示对应限额或限流。用颜色和文字区分正常、留意、紧张与缺数据。该节只展示图与简短图注，原始记录保留在 report_data.json。摘要与其他章节引用第4节。

第6节分开展示宿主机系统与内核、Ascend 安装版本和明确指定的业务进程。进程按 PID 与 starttime 标识，展示可执行文件、启动变量、已加载共享库与进程资源限额，并注明证据来源和采集状态。旧包环境快照标为 N/A，仍按原规则分析调度、资源和亲和性。软件版本和静态参数用于解释环境差异，根因判断沿用时间窗证据和同负载对照。

第1节在关键指标下的「核心问题」区域集中展示调度、线程、中断、频率/空闲和亲和性相关的问题。沿用诊断引擎的严重度，从严重、警告到提示排序。同级时首要诊断优先。仅供核查且尚未达到诊断规则的线索标为提示。每项同时标记「已观测到」或「待验证」，写明受影响的对象和关键证据、核查与调整条件、同负载复测方法。关键数据缺口另用一条「数据不足」说明影响和处理路径。完整技术指标、时序、线程排名及采集记录保留在 report_data.json。

- 按实际任务和同核同窗证据合并调度等待、竞争者和已对齐的设备时间窗。CPU 配额限制单列，因为检查和调整对象不同。
- 中断以热点核的配对占用时间和本核来源解释。核实业务线程同核同期排队后，再考虑分开设备队列和业务线程。
- 忙核所在组的整窗低频只作为线索。核验业务忙段是否低频，再查上限、温度和功耗。空闲节能只作为判断低频线索的背景。
- CPU 许可范围、实际运行位置和内存页位置分别判断。安全绑核预检与命令集中在第3节对应主 PID。
- 回看时段与样本量放到对应问题。设备事件与 Host 等待同期用于定位，业务影响靠同负载对照。按实际丢失、解析或打包记录决定如何补齐缺口。

第二部分必须直接解释CPU平均利用率、单核最高利用率、全机上下文切换率、调度延迟P99、硬中断总占比、软中断总占比，并呈现根因判断/候选证据链，以及 NPU 任务与 NUMA 亲和性。HTML 按 NUMA 分组、每张 NPU 一张任务卡，列出主进程（按显存占用最高推定）、所有占卡宿主机 PID、线程数、NPU 亲和 CPU、当前线程 allowed CPU、ftrace 观测 CPU、PSR 最近运行核、“观测 NUMA → 亲和 NUMA”、CPU 绑定状态。2.3只展示当前亲和摘要，移除“依据、来源、内存与线程证据”展开区和“建议CPU / NUMA”字段；AI对象清单、目标CPU、缺项及执行命令统一放第三部分。根因不再单列尾部章节，具体优化动作统一放在第三部分。

## 统一入口与采集原则

默认发给客户的入口是：

```bash
sudo bash scripts/cpu_trace_collect.sh
```

采集必须在物理机/宿主机运行。现场只需复制新版 `scripts/cpu_trace_collect.sh` 一个文件。CPU 采集使用 Linux、Bash 4+、awk 和常见核心工具。原生 Ascend Dump 使用宿主机的 Python 3.7+ 运行内置原版采集器。`npu-smi`、lscpu、numactl、numastat、pstree 等按能力探测，缺失或权限不足分别记入 Manifest。

默认 3 秒、全量安全 ftrace、HostBound、全部进程/线程前后快照、NPU 原始表、CPU/NUMA/IRQ/频率/Idle/cgroup/NUMA 页分布，并记录机器资源原始快照。资源快照覆盖 CPU、内存/换页/OOM、磁盘容量/inode/I/O 和 PSI，任务 cgroup 快照补充 CPU/内存/PID 限额。默认 `--buffer-size 16384` KiB/CPU，实测丢失时按该配置重采并对比丢失计数。进程快照记录 PPID、TGID、TID、starttime、namespace、逐线程有效 cpuset 与 NUMA 页汇总，便于离线发现 AI 主进程、子进程和线程。`--duration` 调整时长，`--pid` 增加明确宿主机根 TGID 并保存其 NUMA 原始文件；`--hostbound`/`--discover` 为兼容参数。`--no-hostbound` 关闭进程/NPU采集；`--minimal` 仅精简 ftrace 事件，保留安全快照和低频轮询。每次在输出位置自动新建 `cpu_trace_YYYYMMDD_HHMMSS_随机后缀/`，压缩包同名；重复执行不会覆盖旧数据，也不要求用户换目录。

环境快照在 ftrace 开始前采集。默认读取操作系统、内核参数和 Ascend 常见安装目录的组件版本。沿用 `--pid PID[,PID]` 追加明确的宿主机进程环境，读取前后核对 starttime，只保留经过键名和值格式筛选的启动变量。目标进程以其自身的 `/proc/<PID>` 证据为准，采集器环境不可替代。环境采集范围见 [采集契约](references/collection_contract.md#环境快照)。

默认命令会生成两个 Skill 共用的原生环境快照。用 `--pid` 补充指定业务进程的环境。

```bash
sudo bash scripts/cpu_trace_collect.sh --pid 12345 --output /tmp/cpu-case
```

原生采集和打包成功时，`cpu_trace_*.tar.gz` 包含 `<采集目录名>/hostbound/environment/msprechecker_dumped.json`。输出目录中的独立副本为 `cpu_trace_*_msprechecker_dumped.json`，与包内内容逐字节一致。独立副本写出失败时，从包内提取原始 JSON 交给 `ascend-dump-analyzer`。原版采集器在 ftrace 前使用 `--filter --no-probes`，并将启用 HostBound 时显式指定的 PID 转成 `--target pid-<PID>=<PID>`。缺少 `python3` 或原生采集失败时，终端和 Manifest 记录 `environment_dump` 的失败原因，CPU 采集仍可完成。`--no-ascend-dump` 跳过原生采集，`--ascend-dump-script PATH` 可指定原版脚本路径。`--no-hostbound` 模式需要显式指定该路径才会采原生 Dump。

采集或完整性判断时读取 [采集契约与缺失状态](references/collection_contract.md)。先核对 `metrics.collection_integrity`：ftrace 的解析/丢失与 HostBound/NPU/NUMA 的文件/语义完整性独立呈现。`collector_status` 保留采集端声明，`status` 是 Analyzer 对实际包内文件的对账结果。仅 `actions[].recollect=true` 才提供相应复采命令；解析失败先修复解析器，未打包先取回已有文件，工具缺失/权限不足先说明条件，无活动进程不要求重复采集。

采集端打印能直接识别的缺项、状态和原因，结束时汇总。第1节的「采集缺项与原因」展示包内对账、ftrace 探测和资源快照子项发现的问题，包含项目或原始路径。主动跳过和无活动进程按各自状态记录。原生 Ascend Dump 子项的采集状态和原因见第6节。

需要补充官方工具时，在离线分析机器根据现象生成只读工具计划（现场采集无需运行）：

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
7. 涉及 NPU、NUMA 或绑核方案时，读取 [NPU/NUMA 判断与绑核规则](references/npu_numa_binding.md)。仅显式 CPU Affinity 字段建立 NPU→CPU 映射，HCCS、卡号、Logic/Physical ID 不表示 NUMA。CPU 所属节点、Mems_allowed 许可范围、numa_maps 实际页分布分别分析。共享 CPU 池有完整 Worker 运行/等待需求证据时才分配互斥 SMT 核心组；调度证据不足时，若显式原始亲和池一致、全部 CPU 归属同一 NUMA、宿主机身份及全 TID cpuset 允许完整在线池，给出共享本地亲和池的具体命令，标明共享而非隔离。映射/身份/cpuset 证据缺失时仍保留未知。已有合规线程保留原掩码。每个动作须满足稳定宿主机 PID/TID 身份及逐 TID 有效 cpuset；第二部分保留当前摘要，第三部分列出对象清单、可执行命令及逐对象缺项。

## 优化与安全边界

- 先保留原始采集包、affinity、IRQ 和 NUMA 快照，再提出优化方案。
- `host_analyzer` 是可选的独立外部优化工具，不是诊断器。默认不执行系统变更；用户授权 CPU 绑核实验后使用上述 journal 执行器。`migratepages`、IRQ affinity 和 irqbalance 不随 CPU 实验自动执行。
- 优化必须由用户确认，并以前后相同负载、同一窗口的复采结果验证；无收益或副作用须给出回滚方案。
- 第三节先展示可复制的绑核命令和脚本，再列出待核验的主 PID 和进程线程清单。每个已核验的占卡主进程给一条具体的 `taskset -apc CPU_LIST PID`。该命令作用于指定 PID 的全部现有线程。启动器、未占卡子进程及辅助线程仅作清单展示。
- AI 进程与线程清单按名称汇总。CPU 运行时长和跨进程抢占切入各按进程名、线程名展示前十位，保留 PID/TID 供核查。进程时长汇总已观测线程的 CPU·秒。跨进程抢占切入仅统计同一次 `sched_switch` 中 `prev_state=R+` 且两端 TGID 已知并不同的记录，计给切入方。用等待时间和业务延迟核查影响。没有可归属样本时明确标注，完整排名数据见 `metrics.ai_rankings`。
- 先核对主进程的宿主机身份、NPU 本地 CPU 范围和全线程的有效 cpuset。从 NPU 本地在线 CPU 与所有线程许可范围的交集选择统一目标。线程许可范围可以不同。公共目标为空或会改变需保留的本地线程掩码时，在命令区标出主 PID 和原因。
- `bind_cpu.sh` 默认 Bash 预检和预览。`--apply` 委托现场执行器重新发现主进程和线程，先保存原 affinity，再按主 PID 调用 `taskset -apc` 并读回。恢复时按日志逐线程恢复原掩码。完整操作见闭环契约。
- 离线生成命令无需申请执行权限；实际绑核仍需用户授权。执行器在任何变更前主动保存当前真实 affinity 和稳定身份用于恢复，不能仅依赖较旧采集快照。`taskset` 不迁移既有内存页。
- 纯拓扑偏离只能是潜在配置风险。提高 NUMA 性能风险需相互关联的跨节点运行迁移、实际内存位置/远端访问、明显等待与性能证据；页分布本身不能证明远端访存流量。不机械建议修改全局调度参数。复采根据 Manifest 的原因和可解决性判断，常规完整入口不再依赖额外开关。

## 离线分析

```bash
# 在本 Skill 根目录 (cpu-host-performance/) 下执行
python3 -m analyzer analyze <采集包、trace或原生JSON> --output-dir <输出目录>
# 或在任意目录直接运行入口脚本
python3 <Skill路径>/analyzer/analyze_cpu_trace.py <采集包、trace或原生JSON> --output-dir <输出目录>
```

查看生成的 `hostbound_report.html`。分析器不应因缺少 GIL、PMU、msprof 而失败；工具证据覆盖保留在 report_data.json。影响当前问题判断的缺口写入第1节。

CPU 分析器自动读取采集包内的原生 Dump，也接受任意文件名的原生 JSON，包括从 `ascend-dump-analyzer` 直接采集的文件。单独分析 JSON 时，第6节展示可用环境证据，调度、负载和绑核动作仍需 CPU trace 与相应身份证据。Ascend 环境分析和多份环境比对直接使用同一份独立 JSON。


## 频率、线程竞争与全核报告（v2）

采集与数据契约需要核对时读取 [v2分析说明](references/analysis_v2.md)。全量显式使用`--full`，默认低开销sysfs轮询1秒，可用`--frequency-interval 2`调整（最小0.2秒）；`--minimal`仅sched_switch，仍保留sysfs频率，不支持完整wakeup延迟。重复采集自动生成独立时间目录，不覆盖旧数据。私有trace instance不受支持时安全停止全局采集，可由客户提供已有trace或已保留的只读快照。

分析后查看`hostbound_report.html`，并保留`report_data.json`（包含`metrics.binding_plan.tasks/actions/commands/script`，schema_version=2，时间秒/延迟ms/频率MHz/比例百分比；新增字段向后兼容）。HTML 每类明细最多展示100条，标明总数；长表默认收缩，搜索/排序作用于展示的前100条。全部指标、进程/线程和证据仍保留在 JSON，不因展示限制截断分析。2.3不展示逐PID全线程原始证据表，只保留判断摘要与来源。第5节展示全 CPU 图和收缩表。频率/空闲时序、线程排名和所有候选竞争重叠窗口见 JSON。

- 缺少cpu_frequency事件时先查看主状态与事件子状态：区分TRACE_AVAILABLE、SYSFS_AVAILABLE、STATIC_ONLY、NO_TRANSITION、UNSUPPORTED、COLLECTION_FAILED，以及NOT_REQUESTED/UNKNOWN。有效sysfs数据不能因为trace没有转换而被否定。NO_TRANSITION不表示降频，缺失不显示0MHz。
- 频率按related_cpus策略域去重；标准接口kHz显式转MHz；时间加权平均只覆盖已知样本持续区间。低频比例仅使用cpuinfo_max_freq参考；驱动请求频率不是已经验证的实际频率。PMU未获平台语义/权限验证时保持NOT_VALIDATED，不运行或输出估算频率。
- sched_switch prev_pid/next_pid是TID。依据/proc任务starttime及生命周期区分身份；无TGID证据保留null，跨代/丢失/退出/边界不完整等待不填补。角色依据命令、关系、NPU/cgroup及comm并给出证据，不能将所有Python进程认定为训练。
- Runqueue Wait包含已完成成功唤醒与R/R+切出等待；Wait P99仅成功wakeup→switch。报告注明样本量、重复/未配对和丢失情况。R/R+是可运行态切出事实，不能断言其他线程因果性抢占。
- 只有可运行等待和同CPU执行重叠才形成竞争候选；共享Affinity或CPU占用高不能单独形成根因。cgroup quota及throttling用前后增量和祖先限制解释，不能归为整机饱和。未对齐Device时间线仅报告Host侧相关风险。
- IRQ/SoftIRQ全机比例使用所有有效CPU总观测时间分母，同时保留单核最大值。静默/未采集CPU或缺失事件保留N/A；低平均利用率不能排除局部热点。
- 第二节六个解释均需包含当前值、定义/公式、作用、动态说明、结论限制与可信度。工具阈值为经验筛选，性能结论需同负载基线验证。

回归与合成示例命令见README。合成数据必须保留SYNTHETIC_TEST_DATA标记，不冒充真实采集。禁止自动执行优化命令、训练或PMU，不自动commit/push。

## 旧包绑核兼容

报告和预览以 metrics.binding_plan 为准。旧包已确认宿主机身份、主进程 starttime、显式 NPU 映射和完整线程掩码时，可生成待现场核验的主进程命令。缺逐线程 cpuset 时，以该线程当前 allowed 与 NPU 本地在线 CPU 的交集为边界。执行前用新 plan.json 核验现场线程，使用 state.json 恢复。身份冲突、未知 NPU 映射、缺少安全公共目标或同一 PID 使用不同 NPU CPU 池时，在命令区标出主 PID 与具体阻碍。
