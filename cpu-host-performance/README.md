# CPU Host 性能分析 Skill

新增统一绑核闭环：`python3 scripts/ascend_bind.py --help`。流程为 Trace 诊断 → 现场 NPU/Rank 标签/子进程线程识别 → NUMA 计划 → journal 执行与读回 → 同负载业务 A/B + Trace 验证 → 回滚。默认只读；实际执行需显式 `--execute`。完整命令、指标 JSON 格式、状态和限制见 [绑核闭环操作说明](references/binding_workflow.md)。

`bind_cpu.sh` 现在默认仅预检/预览；其 `--apply` / `--rollback` 委托同一个 Python 执行器，需要完整 Skill 目录和 Python 3.8+。原采集器仍为独立 Bash，无 Python/jq 依赖。新进程/线程或 affinity 漂移会要求重新规划，不自动追随重启 Rank；不默认改变 IRQ 或迁移内存页。

面向 openEuler / Kunpeng / Ascend 训练与推理的只读采集、离线分析与证据报告。升级保留原有入口、离线报告、NPU/NUMA亲和性与安全绑核建议，增加频率多源降级、进程线程发现、竞争候选与全核交互图。

## 采集

默认采集只需把 `scripts/cpu_trace_collect.sh` 复制到 Linux 宿主机。采集使用 Bash 4+、awk、核心工具和内核 tracefs。NPU/拓扑等可选命令按能力探测，缺失情况记入 Manifest。采集后把包带回有 Python 3.7+ 的机器离线分析。

```bash
# 默认3秒、完整安全ftrace、HostBound、NPU发现、全机/proc前后快照
sudo bash scripts/cpu_trace_collect.sh

# 自定义时长和新的输出目录
sudo bash scripts/cpu_trace_collect.sh --duration 60 --output /tmp/cpu-case-001

# 明确训练/推理根进程（宿主机TGID），可指定多个
sudo bash scripts/cpu_trace_collect.sh --full --duration 30 --output /tmp/cpu-case-002 --pid 12345,23456

# 明确关闭HostBound/NPU/全机进程增强快照
sudo bash scripts/cpu_trace_collect.sh --no-hostbound --duration 10 --output /tmp/cpu-case-003

# 精简只有sched_switch；sysfs频率轮询仍有效，不能计算唤醒延迟
sudo bash scripts/cpu_trace_collect.sh --minimal --frequency-interval 2 --duration 10 --output /tmp/cpu-case-004

# 限制采集CPU、buffer（KB/核）
sudo bash scripts/cpu_trace_collect.sh --duration 10 --cpu-list 128-159 --buffer-size 16384 --output /tmp/cpu-case-005
```

### 输出两个 Skill 共用的原生环境 JSON

将 `ascend-dump-analyzer` 自带的 `msprechecker_dump.py` 一并复制到宿主机。该模式需要 `python3`。

```bash
sudo bash scripts/cpu_trace_collect.sh --output /tmp/cpu-case --pid 12345 \
  --ascend-dump-script /path/to/msprechecker_dump.py
```

成功后输出同一前缀的 `cpu_trace_*.tar.gz` 和 `cpu_trace_*_msprechecker_dumped.json`。JSON 由原脚本以 `--filter --no-probes` 生成，内外两份原样保存。CPU 分析器接受采集包或独立 JSON，Ascend 环境分析器直接接受独立 JSON。独立文件写出失败时，可以原样取出包内的 `hostbound/environment/msprechecker_dumped.json`。

`--hostbound` 和 `--discover` 保留兼容，正常完整采集无需添加。`--minimal` 是主动选择的事件精简模式，并不关闭常规安全快照。每次在指定输出位置自动新建 `cpu_trace_YYYYMMDD_HHMMSS_随机后缀/` 及同名 tar.gz；可连续重复执行，不覆盖旧数据。默认 sysfs 轮询1秒，最小0.2秒；每个 policy 读取一次。默认 buffer 为 16384 KiB/CPU，256 个 CPU 约 4 GiB；请求值和内核读回值记录在采集元数据中。实际内核占用、文字导出大小和全机线程快照耗时需现场核验。30秒是 trace 业务窗口，完整命令还包括前后快照及打包时间。

采集使用私有 `instances/cpu_host_<collector-pid>`，不清空全局 trace 或覆盖其他工具配置。tracefs/实例不支持或无权限时停止 tracing，保留已获取的只读快照、Manifest 和错误信息，可直接分析 `cpu_trace/` 降级报告。正常结束、失败和信号退出都会清理私有实例。SIGKILL/断电可能遗留实例，应由管理员核验后处理。

`collection_manifest.json` 同时保存在采集根目录和 `hostbound/`。逐命令记录请求、命令存在性、返回码、原始路径/大小/空文件、解析状态/数量、起止时间和失败原因。`COLLECTED`、`NO_ACTIVE_PROCESS`、`COMMAND_NOT_FOUND`、`PERMISSION_DENIED`、`UNSUPPORTED`、`PARSE_FAILED`、`NOT_REQUESTED`、`NOT_INCLUDED` 分别呈现，额外区分执行失败。Analyzer 核对包内文件，保留 `collector_status` 并生成 `collection_integrity`。ftrace 和 HostBound 完整性分开判断，复采建议按实际原因生成；原始数据已存在时优先离线修复解析/打包。[完整契约](references/collection_contract.md)

全量启用：switch、成功wakeup/wakeup_new、waking（仅作背景）、migrate、task_newtask、process_fork/exec/exit、IRQ/SoftIRQ、cpu_idle、cpu_frequency。每项探测存在、可写、请求与开启读回结果；缺失事件不致命。sched_stat_wait只探测，避免默认增加高频事件；导入已有该事件时可提供补充累计值。

资源采集继续使用单个sh，无Python/jq依赖。`resource_start/end.json`、`resource_samples.jsonl` 保存带时间的proc原文，分析端Python计算CPU增量、可用内存/Swap/回收/OOM、磁盘容量/inode、块设备I/O和PSI，并结合任务内存/pids限额给出资源风险候选。资源窗口与trace窗口分别标明，3秒窗口不能保证捕获所有瞬时问题或消除trace丢失。

## 离线分析

```bash
python3 -m analyzer analyze /path/cpu_trace_*.tar.gz --output-dir /path/report
# 单独分析原生环境快照
python3 -m analyzer analyze /path/msprechecker_dumped.json --output-dir /path/env-report
# 任意目录也可使用脚本入口
python3 /path/cpu-host-performance/analyzer/analyze_cpu_trace.py /path/capture -o /path/report
```

Windows控制台/管道若默认使用GBK，可用 `python -X utf8 -m analyzer analyze ...`；不需要修改系统编码设置。

支持 tar.gz、zip、目录、ftrace/trace-cmd 文本、Chrome/Perfetto JSON 和 msprechecker 原生 Ascend Dump JSON。直接分析原生 JSON 时，报告展示环境信息，并标明调度证据缺失。分析输出如下。

- `hostbound_report.html`：固定入口，单文件离线打开，无CDN/外部JS。
- `bind_cpu.sh`：默认 Bash/awk 身份与CPU预检/预览；显式执行委托 Python 3.8+ journal 执行器（完整 Skill 目录）。
- `report_data.json`：schema_version=2，包含单位、任务身份、指标、原始快照、等待/运行区间、候选证据和诊断。

也允许分析只有频率/进程sidecar的目录：生成明确缺失调度数据的降级报告。解压累计默认上限512MiB；较大可信采集包请先解压到新的专用目录，再以目录分析。归档路径穿越、链接和设备成员被拒绝。

## 报告阅读

报告共 6 节，依次为摘要、关键指标与根因、优化建议、机器资源、全核负载和采集环境与软件栈。第1节的「核心问题」按严重、警告、提示排序，直接展示调度、中断、频率、线程与亲和性问题的依据、优先操作和复测方法。关键采集缺口在摘要中说明，完整明细保留在 report_data.json。第4节用一张离线图展示主要资源占用、压力与任务限额，颜色区分紧张、留意、正常与缺数据。

第二节直接解释CPU平均利用率、单核最高利用率、上下文切换率、调度延迟P99、硬中断总占比、软中断总占比。每项有当前值、公式、关注原因、动态解读、诊断限制和可信度。阈值为可审查的经验筛选值，不是官方性能标准。

第5节：默认全部CPU利用率折线；可切柱状图、NUMA、CPU范围和指标显隐。百分比、次数/秒、MHz分成独立指标组。256核全部保留，静默/未采集核心显示N/A。悬停/键盘聚焦显示数值；标记热点、均值、极值。无JS和打印使用全核静态图及表格。

第6节展示 ftrace 启动前采集的系统、内核参数和 Ascend 组件版本。用 `--pid 12345` 指定业务宿主机 PID，可同时查看该进程的启动变量、可执行文件、共享库和资源限额。来源和状态保存在 `metrics.environment`。旧包缺少此快照时显示 N/A。 [环境采集契约](references/collection_contract.md#环境快照)

第三节的清单按名称汇总 CPU 运行时长和跨进程抢占切入，进程和线程各显示前十名。抢占次数来自 `sched_switch` 的 `R+` 标记及两端已知且不同的 TGID。用 PID/TID 核对同名实例，完整汇总见 `metrics.ai_rankings`。

HTML每类明细最多展示100条，并保留总记录数；长表默认收缩，搜索/排序仅作用于当前展示记录，完整数据在report_data.json中。2.3只保留当前亲和摘要，删除“依据、来源、内存与线程证据”和“建议CPU / NUMA”区块。第三节先展示可复制的 taskset 命令与脚本下载，再列待核验主 PID 和 AI 进程线程清单。Idle 由进入/退出事件持续时间计算，`-1` 和无符号退出值不作为状态编号；未知区间显示 N/A。频率按策略域展示来源、接口失败和可靠性。查看 `frequency.status` 与 `event_status`：

| 状态 | 含义 |
|---|---|
| TRACE_AVAILABLE | 有有效power/cpu_frequency样本 |
| SYSFS_AVAILABLE | 无有效trace样本，但sysfs有不同时间点的动态读取 |
| STATIC_ONLY | 仅静态/单次可用信息，动态平均可能缺失 |
| NO_TRANSITION | 事件开启已确认，但未观测转换；不等于降频 |
| UNSUPPORTED | 已知接口不支持/无可用数据 |
| COLLECTION_FAILED | 开启失败、导出/丢失、坏样本，或旧包能力无法确认 |

事件子状态还区分 `NOT_REQUESTED` 与 `UNKNOWN`。主状态可为SYSFS_AVAILABLE，同时事件子状态为未启用/不支持。没有数据不输出0MHz；没有cpuinfo_max_freq参考不算低频占比。标准trace/sysfs使用kHz，报告使用MHz，显式导出可声明Hz/MHz；不按数值量级猜单位。

`scaling_cur_freq`通常是驱动请求/报告频率，`cpuinfo_cur_freq`在实现时提供硬件反馈。报告不把它们或未经验证的PMU推导当作精确实测时钟。PMU默认不执行；cycles/ref-cycles需在目标平台核验语义、权限、参考基准和复用缩放后才能估算。

线程分析在摘要中聚焦受影响的业务线程与主要同核竞争者。TID→TGID→PPID→cgroup→NPU 的关联附 HIGH/MEDIUM/LOW 置信度；名称只作线索。完整线程身份、运行/等待、Affinity、全部竞争重叠窗口和原始快照保留在 JSON。

CPU→NUMA 基于本机 sysfs/lscpu/numactl，NPU→CPU 只接受明确 CPU Affinity 列；`info -m` 的 Logic/Physical ID 和 HCCS 拓扑不能推导 NUMA。CPU 节点、允许内存节点和实际页分布分别列出。缺少运行/等待证据时不均分共享 CPU 池；若共同的完整在线本地亲和池、宿主机身份和全TID cpuset均已核验，则生成共享池taskset命令（不承诺隔离）；每个执行方案校验身份和CPU范围；互斥分区仍需完整负载/SMT证据。第三部分仅对已核验的占卡主进程生成 `taskset -apc CPU_LIST PID`，每个 PID 一条。启动器和未占卡子进程仅展示。线程 cpuset 不同且存在安全公共范围时，使用交集。若需保留不同的本地掩码或无法共用目标，在命令区标出该 PID 和原因。缺逐线程 cpuset 时使用当前 allowed 作为安全边界。下载脚本默认预检与预览，显式执行在现场重建计划、记录原掩码、按主 PID 执行并读回。


竞争候选必须有可运行等待与同核运行时间重叠，Affinity只作旁证。NPU直接归属与父子关联不同；单纯Python名称、CPU高、共享Affinity、R/R+都不能认定根因。没有同域Device窗口时仅报告Host侧风险。详见 [数据契约、算法与限制](references/analysis_v2.md)。

## 已修正统计口径

- IRQ/SoftIRQ总占比：配对handler区间在有效CPU观测范围内的并集 / 全部有效CPU总观测秒数。另保留单核最高值。旧算法sum(time)/单窗口会在多核夸大（2核各1秒IRQ、10秒窗口，原20%，现全机10%）。
- sched_switch字段是TID；/proc starttime隔离身份代次；跨代或无身份证据不配对等待。
- 延迟使用成功wakeup→switch，不把waking当起点；重复唤醒去重；退出、边界、丢失及配对数量明确展示。
- R/R+ 都计入可运行态切出。跨进程抢占榜仅使用同一事件中切入和切出 TGID 均已知且不同的 R+ 切换，业务影响需结合等待和延迟判断。
- 显式迁移与观测CPU变化选择一种来源，不相加重复计数。
- 修复无cpu_idle时idle切换方向，缺失值保留null/N/A；不再把低均值当作排除CPU瓶颈的依据。

## 验证和示例

```bash
python3 -m unittest discover -s tests -v
# 此命令只生成测试数据，不读取Linux接口；输出目录需全新
python3 examples/generate_synthetic.py --output /tmp/cpu-synthetic-demo
```

示例包含256核、8NUMA、8NPU的明确合成数据，以及 Manifest、Idle、局部饱和、Worker等待、后台线程重叠、频率策略域和 cgroup 节流。示例既展示已合规进程，也展示带身份预检的推荐方案；虚构 boot_id 会阻止它在真实机器执行。它用于功能验证，不代表真实机器测量。可直接打开 examples/sample_report.html 查看生成结果。

当前本地验证基于Windows/Python3.14、Git Bash和模拟/proc/sysfs/tracefs；目标openEuler22.03SP4/Kunpeng920/Ascend910B4未连接。真实事件支持、npu-smi版本、时钟对齐、驱动频率语义和采集开销仍需现场验证。

## 安全与兼容性

默认只读 /proc、sysfs、cgroup 和 npu-smi，诊断/plan/preview 不改 affinity。显式授权 --execute 才执行逐 TID CPU 亲和变更，并保存 journal、读回和恢复结果；IRQ、Governor、内存策略不随 CPU 实验修改，不启动训练/推理/PMU。

/proc 前后快照并非原子，短命任务可能只有生命周期而无TGID；无clone证据时保留未知，PID复用和命名空间不明时阻止跨身份关联。丢失 trace 只报告已观测下界。Affinity 成对比较按等待排序最多512个活跃关联线程，截断范围会明示；完整线程身份、Affinity 和竞争时间窗口仍保留在 JSON。内存页位置不等于实测远端访存流量，仅拓扑偏离不提升为 P1。

保留官方msprof、GIL、function_monitor、host_analyzer的独立工具边界，参阅 [官方工具适配](references/official_tool_integration.md)、[诊断规则](references/hostbound_diagnosis_rules.md)、[NPU/NUMA规则](references/npu_numa_binding.md)。

HTML直接由metrics/diagnosis渲染，不经过Markdown。取消report.md的生成和写入，避免额外的渲染/编码/文件错误；完整证据保留JSON。复用输出目录时仅清理可识别为本生成器产物的旧report.md。
