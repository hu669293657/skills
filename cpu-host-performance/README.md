# CPU Host 性能分析 Skill

面向 openEuler / Kunpeng / Ascend 训练与推理的只读采集、离线分析与证据报告。升级保留原有入口、13节报告、NPU/NUMA亲和性与安全绑核建议，增加频率多源降级、进程线程发现、竞争候选与全核交互图。

## 采集

将完整 Skill 目录部署到 Linux 宿主机，保留 `scripts/cpu_trace_collect.sh`、`host_snapshot.py`、`collection_evidence.py` 和 `analyzer/npu_parser.py` 的相对位置。Bash 5.1、核心 utils、Python 3.7+ 标准库及内核 tracefs 为目标环境，不需要第三方 Python 包。缺少 Python/helper 时 ftrace 仍可运行，Manifest 会明确标记增强能力降级。

```bash
# 默认30秒、完整安全ftrace、HostBound、NPU发现、全机/proc前后快照
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
sudo bash scripts/cpu_trace_collect.sh --duration 10 --cpu-list 128-159 --buffer-size 4096 --output /tmp/cpu-case-005
```

`--hostbound` 和 `--discover` 保留兼容，正常完整采集无需添加。`--minimal` 是主动选择的事件精简模式，并不关闭常规安全快照。每次使用新的输出目录；已存在的 `cpu_trace/` 会被拒绝覆盖。默认 sysfs 轮询1秒，最小0.2秒；每个 policy 读取一次。默认 buffer 为4096KB/核，256核约1GiB；实际内核占用、文字导出大小和全机线程快照耗时需现场核验。30秒是 trace 业务窗口，完整命令还包括前后快照及打包时间。

采集使用私有 `instances/cpu_host_<collector-pid>`，不清空全局 trace 或覆盖其他工具配置。tracefs/实例不支持或无权限时停止 tracing，保留已获取的只读快照、Manifest 和错误信息，可直接分析 `cpu_trace/` 降级报告。正常结束、失败和信号退出都会清理私有实例。SIGKILL/断电可能遗留实例，应由管理员核验后处理。

`collection_manifest.json` 同时保存在采集根目录和 `hostbound/`。逐命令记录请求、命令存在性、返回码、原始路径/大小/空文件、解析状态/数量、起止时间和失败原因。`COLLECTED`、`NO_ACTIVE_PROCESS`、`COMMAND_NOT_FOUND`、`PERMISSION_DENIED`、`UNSUPPORTED`、`PARSE_FAILED`、`NOT_REQUESTED`、`NOT_INCLUDED` 分别呈现，额外区分执行失败。Analyzer 核对包内文件，保留 `collector_status` 并生成 `collection_integrity`。ftrace 和 HostBound 完整性分开判断，复采建议按实际原因生成；原始数据已存在时优先离线修复解析/打包。[完整契约](references/collection_contract.md)

全量启用：switch、成功wakeup/wakeup_new、waking（仅作背景）、migrate、task_newtask、process_fork/exec/exit、IRQ/SoftIRQ、cpu_idle、cpu_frequency。每项探测存在、可写、请求与开启读回结果；缺失事件不致命。sched_stat_wait只探测，避免默认增加高频事件；导入已有该事件时可提供补充累计值。

## 离线分析

```bash
python3 -m analyzer analyze /path/cpu_trace_*.tar.gz --output-dir /path/report
# 任意目录也可使用脚本入口
python3 /path/cpu-host-performance/analyzer/analyze_cpu_trace.py /path/capture -o /path/report
```

Windows控制台/管道若默认使用GBK，可用 `python -X utf8 -m analyzer analyze ...`；不需要修改系统编码设置。

支持 tar.gz、zip、目录、ftrace/trace-cmd文本、Chrome/Perfetto JSON。输出：

- `hostbound_report.html`：固定入口，单文件离线打开，无CDN/外部JS。
- `report.md`：同口径中文报告。
- `report_data.json`：schema_version=2，包含单位、任务身份、指标、原始快照、等待/运行区间、候选证据和诊断。

也允许分析只有频率/进程sidecar的目录：生成明确缺失调度数据的降级报告。解压累计默认上限512MiB；较大可信采集包请先解压到新的专用目录，再以目录分析。归档路径穿越、链接和设备成员被拒绝。

## 报告阅读

固定13节：1摘要；2关键指标解释/候选证据链/NPU亲和性；3优化建议；4发现详情；5整体状态；6全CPU负载；7调度；8中断；9频率/Idle；10进程线程与竞争；11Affinity/NUMA；12时间窗口；13数据覆盖。

第二节直接解释CPU平均利用率、单核最高利用率、上下文切换率、调度延迟P99、硬中断总占比、软中断总占比。每项有当前值、公式、关注原因、动态解读、诊断限制和可信度。阈值为可审查的经验筛选值，不是官方性能标准。

第6节：默认全部CPU利用率折线；可切柱状图、NUMA、CPU范围和指标显隐。百分比、次数/秒、MHz分成独立指标组。256核全部保留，静默/未采集核心显示N/A。悬停/键盘聚焦显示数值；标记热点、均值、极值。无JS和打印使用全核静态图及表格。

所有长表默认收缩，展开后支持搜索/排序并保留总记录数。第9节将 Frequency 与 Idle Residency 放在一个离线交互图中：共用时间轴，MHz 左轴、% 右轴；可按 CPU、NUMA、Policy 筛选并勾选系列，默认仅少量重点曲线。Idle 由进入/退出事件持续时间计算，`-1` 和无符号退出值不作为状态编号；未知区间显示 N/A。频率按策略域展示来源、接口失败和可靠性。查看 `frequency.status` 与 `event_status`：

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

第10节首先展示 AI 进程、潜在竞争者和系统任务摘要，Top N 之外及无关进程进入可展开列表。TID→TGID→PPID→cgroup→NPU 的关联附 HIGH/MEDIUM/LOW 置信度；名称只作线索。完整线程身份、运行/等待、Affinity、全部竞争重叠窗口和原始快照保留在 JSON。

CPU→NUMA 基于本机 sysfs/lscpu/numactl，NPU→CPU 只接受明确 CPU Affinity 列；`info -m` 的 Logic/Physical ID 和 HCCS 拓扑不能推导 NUMA。CPU 节点、允许内存节点和实际页分布分别列出。缺少运行/等待证据时不均分共享 CPU 池；每个可执行方案检查稳定宿主机 PID/TID、在线 CPU、逐线程 cpuset、完整 SMT 核心组和分区互斥。已合规进程不重复生成命令。脚本提供身份预检、备份、逐 TID 应用、核验及回滚；taskset 不迁移现有内存页。

竞争候选必须有可运行等待与同核运行时间重叠，Affinity只作旁证。NPU直接归属与父子关联不同；单纯Python名称、CPU高、共享Affinity、R/R+都不能认定根因。没有同域Device窗口时仅报告Host侧风险。详见 [数据契约、算法与限制](references/analysis_v2.md)。

## 已修正统计口径

- IRQ/SoftIRQ总占比：配对handler区间在有效CPU观测范围内的并集 / 全部有效CPU总观测秒数。另保留单核最高值。旧算法sum(time)/单窗口会在多核夸大（2核各1秒IRQ、10秒窗口，原20%，现全机10%）。
- sched_switch字段是TID；/proc starttime隔离身份代次；跨代或无身份证据不配对等待。
- 延迟使用成功wakeup→switch，不把waking当起点；重复唤醒去重；退出、边界、丢失及配对数量明确展示。
- R/R+仅叫可运行态切出；等待另外统计，不能断言谁抢占谁。
- 显式迁移与观测CPU变化选择一种来源，不相加重复计数。
- 修复无cpu_idle时idle切换方向，缺失值保留null/N/A；不再把低均值当作排除CPU瓶颈的依据。

## 验证和示例

```bash
python3 -m unittest discover -s tests -v
# 此命令只生成测试数据，不读取Linux接口；输出目录需全新
python3 examples/generate_synthetic.py --output /tmp/cpu-synthetic-demo
```

示例包含256核、8NUMA、8NPU的明确合成数据，以及 Manifest、Idle、局部饱和、Worker等待、后台线程重叠、频率策略域和 cgroup 节流。示例既展示已合规进程，也展示带身份预检的推荐方案；虚构 boot_id 会阻止它在真实机器执行。它用于功能验证，不代表真实机器测量。原有 examples/sample_report.* 是历史样例。

当前本地验证基于Windows/Python3.14、Git Bash和模拟/proc/sysfs/tracefs；目标openEuler22.03SP4/Kunpeng920/Ascend910B4未连接。真实事件支持、npu-smi版本、时钟对齐、驱动频率语义和采集开销仍需现场验证。

## 安全与兼容性

只读/proc、sysfs、cgroup、taskset查询与npu-smi；不会修改业务Affinity、IRQ、Governor或NUMA配置，不启动训练/推理/PMU。离线生成的优化命令仅为建议，执行须用户确认并保存/回滚配置。

/proc 前后快照并非原子，短命任务可能只有生命周期而无TGID；无clone证据时保留未知，PID复用和命名空间不明时阻止跨身份关联。丢失 trace 只报告已观测下界。Affinity 成对比较按等待排序最多512个活跃关联线程，截断范围会明示；完整线程身份、Affinity 和竞争时间窗口仍保留在 JSON。内存页位置不等于实测远端访存流量，仅拓扑偏离不提升为 P1。

保留官方msprof、GIL、function_monitor、host_analyzer的独立工具边界，参阅 [官方工具适配](references/official_tool_integration.md)、[诊断规则](references/hostbound_diagnosis_rules.md)、[NPU/NUMA规则](references/npu_numa_binding.md)。
