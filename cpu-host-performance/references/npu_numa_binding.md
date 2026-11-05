# NPU、NUMA 与宿主机绑核规则

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

报告第三部分只生成具体 `taskset` 设置命令。整进程使用 `taskset -apc CPU_LIST PID`，指定线程使用 `taskset -pc CPU_LIST TID`；CPU 列表和身份必须有本次证据支持。第二区域保留身份、cpuset、NUMA、线程掩码与阻碍；第三部分代码块不含辅助脚本、Python、查询、备份/回滚和启动示例。

`-a` 包括该进程所有现有线程，不递归处理子进程。前后快照 PPID 与 starttime 均稳定的子进程可以继承父进程建议分区，但必须独立核验其所有 TID 的有效 cpuset；有自己 NPU 归属的子进程采用自己的分区。每个需要变更的子 PID 另列一条命令。若部分线程已有合规窄掩码，则保留这些线程，只给待调整 TID 的具体命令。单核建议需独立目标证据，不能仅凭线程名称将全部线程分别塞入单核。

命令由用户授权后在原宿主机执行；直接命令不包含自动身份预检、原子线程集合锁定、失败回滚或未来任务的持续约束。执行前确认 PID/TID、cpuset 与采集窗口未变。原 affinity 保留在采集快照中；用户需要恢复时再按原掩码生成具体 taskset 命令。

taskset 不迁移已分配内存、不改变内存策略、不提供重启持久化。`numactl --physcpubind=... --membind=... COMMAND` 仅是新任务启动策略，需要独立内存证据，不是现有 PID 的迁移命令。

## 风险等级与缺口

拓扑/许可偏离仅是潜在配置风险，不直接列 P1。提高性能风险需关联跨节点运行迁移、实际内存位置/远端访问、明显等待及性能下降证据；没有远端流量测量不能声称已证实远端访存。默认不建议修改全局调度参数。

缺证据先看 Manifest：无活动任务说明窗口事实；工具缺失/权限不足说明条件；原文解析失败先修解析器；已生成未入包先修打包。只有重新采集确实能解决的问题才给完整命令，不机械输出附加 `--hostbound` 的复采模板。
