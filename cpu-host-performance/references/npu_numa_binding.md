# NPU、NUMA 与宿主机绑核规则

实际执行、业务性能判定与恢复统一使用 [绑核闭环操作说明](binding_workflow.md) 的 `scripts/ascend_bind.py`。本页下述 taskset 为可审阅建议；报告 `bind_cpu.sh` 默认只读预览，显式执行委托 journal 入口，不再直接依次 taskset。执行器保存当前原掩码和稳定身份，失败尽力恢复；恢复不完整会明确报出。

实现：`analyzer/npu_parser.py`、`npu_affinity.py`、`topology.py`；展示：`npu_report.py`。采集/Manifest 缺失处理统一见 [采集契约](collection_contract.md)。

## 证据语义

默认保留 `npu-smi info`、`info -m`、`info -t topo` 原文并标准化。进程表支持不同列、组合/拆分 NPU/Chip、续行、空任务和显存单位；无法解析的非空数据是 PARSE_FAILED。只有明确标注的 CPU Affinity 列才能提供 NPU→CPU。HCCS/PCIe、卡号、Logic/Physical ID 不提供 CPU/NUMA 位置。

CPU→NUMA 来自本机 sysfs cpulist、标准化 NUMA JSON、numactl、lscpu。稀疏节点、冲突与缺失保持显式。NPU→PID→TGID→TID 需要原始身份与父子/线程证据；容器数字 PID 未经映射不能直接关联宿主机进程。显存占用最高者只是角色提示，不得漏掉其他 PID。

| 字段/状态 | 含义 |
|---|---|
| allowed CPU | 每个线程当前调度许可，非历史运行分布 |
| PSR | 快照最近运行核，非全窗口分布 |
| ftrace CPU/NUMA | 已观测运行核/节点，丢失时仅下界 |
| 有效 cpuset | cgroup 可运行 CPU 硬边界，逐 TID 核对 |
| Mems_allowed | 允许分配内存的节点，非实际页位置 |
| numa_maps/numastat | 已分配内存位置，不能单独证明远端访存流量 |
| LOCAL | 全线程许可落在已知 NPU CPU 范围且无范围外运行观测 |
| UNCONFINED/REMOTE_ALLOWED | 许可范围可越出或偏离 NPU 公布范围 |
| REMOTE_OBSERVED | 观测 CPU 超出亲和范围；需另判是否跨 NUMA |
| UNKNOWN | 缺映射、身份、线程或拓扑证据 |

NPU 亲和 CPU 可能只是某节点的一部分，超出该范围仍可能处于同一 NUMA。此时只能说偏离公布 CPU 范围，不能自动声称远端 NUMA。

## 计划生成

1. 使用本次真实 PID/TID、boot_id、starttime 核对前后身份。PID复用、namespace未确认、同 PID 多卡但线程职责未知时阻止执行方案。
2. 候选集 = 显式 NPU CPU Affinity ∩ 真实节点 CPU ∩ online CPU ∩ 全部目标 TID 有效 cpuset。各线程可能具有不同 cgroup，需要分别核对。自定义挂载未能解析时保持未知，不放宽边界。
3. 当前各线程掩码已合规且无冲突时保留，避免覆盖业务关键线程细分。CPU绑定不等于独占，仍核对其他业务和IRQ的运行/等待重叠。
4. 共享候选池必须有完整 Worker 运行/等待需求及可靠性证据，才按观测需求分配完整 `(socket,node,core)` SMT 组。证据不足不均分；部分重叠/物理核心冲突而无法安全分配时报告阻塞。
5. 预留系统 CPU、改配额或搬移 IRQ 需要具体负载/干扰证据，不使用固定预留数。运行/等待只是需求代理，方案须同负载实测，不能承诺收益。
6. 行记录保留当前/建议 CPU 和 NUMA、原因、原始来源、运行/等待/IRQ/cgroup证据、风险、预期方向、执行/验证/回滚。没有可信 NPU→CPU 映射时不输出猜测命令。

## 执行与内存

报告第三部分为每个已核验的占卡主进程生成一条 `taskset -apc CPU_LIST PID`。CPU 列表和宿主机 PID 取自本次采集。

`-a` 覆盖目标进程现有线程。启动器、未占卡子进程和各线程用于清单展示；有独立 NPU 归属的进程按自己的主 PID 评估。各线程的有效 cpuset 可以不同，统一目标使用公共许可范围。交集为空或会改变已配置的本地线程掩码时，在命令区标出该主 PID。

命令由用户授权后在原宿主机执行；直接命令不包含自动身份预检、原子线程集合锁定、失败回滚或未来任务的持续约束。执行前确认 PID/TID、cpuset 与采集窗口未变。采集快照保留原 affinity；实际闭环执行仍主动记录变更前的实时 affinity，恢复统一使用 state.json，不以旧快照覆盖现场较新的策略。

taskset 不迁移已分配内存、不改变内存策略、不提供重启持久化。`numactl --physcpubind=... --membind=... COMMAND` 仅是新任务启动策略，需要独立内存证据，不是现有 PID 的迁移命令。

## 风险等级与缺口

拓扑/许可偏离仅是潜在配置风险，不直接列 P1。提高性能风险需关联跨节点运行迁移、实际内存位置/远端访问、明显等待及性能下降证据；没有远端流量测量不能声称已证实远端访存。默认不建议修改全局调度参数。

缺证据先看 Manifest：无活动任务说明窗口事实；工具缺失/权限不足说明条件；原文解析失败先修解析器；已生成未入包先修打包。只有重新采集确实能解决的问题才给完整命令，不机械输出附加 `--hostbound` 的复采模板。

## 调度证据不完整时的本地化建议

互斥核心组仍要求完整runtime/等待证据。若同组显式原始NPU亲和池相同，全部池内CPU归属唯一NUMA，online范围已知，宿主机进程/全TID身份和cpuset均核验，允许把各进程约束到相同的完整在线亲和池（SHARED_LOCALITY）。这只约束本地性，组内共享，不按卡数均分、不提供隔离或吞吐收益承诺。部分重叠池、未知CPU归属、不同cpuset候选、多NUMA或缺失身份继续阻断。已合规窄掩码保留。报告没有NUMA建议时显示“待核验”，不打印空列表。

## HTML第三部分与脚本交付

2.3 展示当前 NPU/NUMA 摘要。第三节和脚本以 metrics.binding_plan 为准。旧包只要具备完整线程掩码、主进程身份、宿主机上下文与显式 NPU 映射，即可输出待现场核验的主进程命令。缺逐线程 cpuset 时，以各自的 allowed 作为安全边界。
