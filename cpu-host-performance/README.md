# CPU Host 性能分析 Skill

面向 openEuler / Kunpeng / Ascend 训练与推理的只读采集、离线分析与证据报告。升级保留原有入口、13节报告、NPU/NUMA亲和性与安全绑核建议，增加频率多源降级、进程线程发现、竞争候选与全核交互图。

## 采集

Linux宿主机上保持 `scripts/cpu_trace_collect.sh` 与 `scripts/host_snapshot.py` 在同一目录。Bash、核心utils和内核tracefs为基础；增强采集使用 Python 3.7+ 标准库。缺Python/helper时保留ftrace和原有HostBound快照，并标记降级。无需安装第三方包。

```bash
# 全量为默认；自动发现NPU PID并启用全机/proc前后快照
sudo bash scripts/cpu_trace_collect.sh --duration 30 --output /tmp/cpu-case-001 --hostbound

# 明确训练/推理根进程（宿主机TGID），可指定多个
sudo bash scripts/cpu_trace_collect.sh --full --duration 30 --output /tmp/cpu-case-002 --pid 12345,23456

# 不依赖npu-smi，也可启用/proc发现
sudo bash scripts/cpu_trace_collect.sh --discover --duration 10 --output /tmp/cpu-case-003

# 精简只有sched_switch；sysfs频率轮询仍有效，不能计算唤醒延迟
sudo bash scripts/cpu_trace_collect.sh --minimal --frequency-interval 2 --duration 10 --output /tmp/cpu-case-004

# 限制采集CPU、buffer（KB/核）
sudo bash scripts/cpu_trace_collect.sh --duration 10 --cpu-list 128-159 --buffer-size 4096 --output /tmp/cpu-case-005
```

每次使用新的输出目录；已存在的 `cpu_trace/` 会被拒绝覆盖。默认sysfs轮询1秒，最小0.2秒；动态读取每个policy一次。采集前后各保存一次静态频率和进程线程快照。默认buffer为4096KB/核，256核约1GiB；实际内核占用与文字导出大小须现场核验。

采集使用私有 `instances/cpu_host_<collector-pid>`，不清空全局trace或覆盖其他工具配置。实例创建不受支持/无权限时安全停止，保留全局环境；可用helper进行只读补采，或由管理员提供已有trace。正常结束、失败和信号退出都会清理私有实例。不可捕获的SIGKILL/断电可能遗留实例，应由管理员核验后处理。

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

第9节：按频率策略域展示当前、时间加权平均、窗口最低/最高、硬件最大参考、低频时间占比、Governor、样本数、时间序列、接口失败与可靠性。查看 `frequency.status` 与 `event_status`：

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

第10节：TID→TGID→进程→PPID→组→NPU关联，角色附证据与置信度；包括Top Runtime、Runqueue Wait、R/R+切出、Migration、NPU Worker链、热点CPU/NUMA、Affinity交集和cgroup quota/throttling增量。

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

示例包含256核、8NUMA、8NPU的明确合成数据，包含局部饱和、Worker等待、后台线程重叠、频率策略域与cgroup节流。它用于功能验证，不代表真实机器测量。原有examples/sample_report.*保留原样，是旧版本历史样例。

当前本地验证基于Windows/Python3.14、Git Bash和模拟/proc/sysfs/tracefs；目标openEuler22.03SP4/Kunpeng920/Ascend910B4未连接。真实事件支持、npu-smi版本、时钟对齐、驱动频率语义和采集开销仍需现场验证。

## 安全与兼容性

只读/proc、sysfs、cgroup、taskset查询与npu-smi；不会修改业务Affinity、IRQ、Governor或NUMA配置，不启动训练/推理/PMU。离线生成的优化命令仅为建议，执行须用户确认并保存/回滚配置。

/proc前后快照并非原子，短命任务可能只有生命周期而无TGID；无clone证据时保留未知。丢失trace只能报告已观测下界。全线程身份保留；Affinity成对比较按等待排序最多512个活跃关联线程，截断范围会明示。候选的总重叠时间全量累计，最多保存20个窗口例证。JSON包含完整运行/等待区间，可重新核查。

保留官方msprof、GIL、function_monitor、host_analyzer的独立工具边界，参阅 [官方工具适配](references/official_tool_integration.md)、[诊断规则](references/hostbound_diagnosis_rules.md)、[NPU/NUMA规则](references/npu_numa_binding.md)。
