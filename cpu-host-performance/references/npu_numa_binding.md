# NPU 任务、NUMA 亲和性与宿主机绑核

此规则用于第二部分任务卡/表格及第三部分可复制方案。分析器实现位于 `analyzer/npu_affinity.py`，显示由 `analyzer/npu_report.py` 统一生成。

## 证据与判断

在物理机/宿主机采集 `npu-smi info`、`npu-smi info -t topo`、NUMA/在线 CPU/SMT 拓扑、所有占卡 PID 的 status/TGID、线程清单、全 TID affinity、进程 starttime 和有效 cgroup cpuset。容器内 PID 先映射到宿主机 PID，再关联宿主机 `/proc/PID/status`。报告保留 NPU/Chip 与所有占卡 PID；显存占用最高者只是“主进程”的可解释启发式，不能据此漏掉其他 PID。

| 状态 | 判据 | 呈现 |
|---|---|---|
| 已绑定亲和 CPU | 全量现有线程 allowed CPU 都包含于 NPU CPU Affinity，且未观测到范围外运行 | 绿色 |
| 允许跨 NUMA / 未收敛 | 线程 allowed CPU 与亲和区间相交，但仍允许范围外 CPU | 橙色 |
| 绑定范围偏离 | 全线程 allowed CPU 并集与亲和区间不相交 | 红色 |
| 观测到非亲和 CPU | ftrace 观测 CPU 或快照 PSR 含亲和范围外 CPU | 红色，标明证据时点 |
| 证据不足 | 缺 NPU/PID、NUMA 或全线程 affinity 对照 | 橙色“待核验” |

`PSR` 是最近运行核，不能替代 allowed CPU，也不能独立证明整个窗口的运行分布。配置快照在采集结束时获取，与 ftrace 窗口可能存在先后差异。CPU 绑定配置可确认为偏离/未收敛；性能根因须结合延迟、迁移、吞吐及 Device 空泡验证。没有 Device 时间线时，不确认 HostBound。CPU 亲和并不能证明内存页本地，内存需另看 `numastat -p PID`、`/proc/PID/numa_maps`。

卡片另列“观测 NUMA → 亲和 NUMA”。超出 NPU 公布的 CPU Affinity 不必然跨 NUMA：例如节点 0 的 CPU 是 0-47，NPU 亲和 CPU 为 0-23，运行在 CPU30 仍位于节点0，只能说偏离公布的 CPU 范围，不能说跨 NUMA。

## 共享 NUMA 的分区规则

1. 每卡候选 CPU = `npu-smi topo CPU Affinity ∩ 实际亲和 NUMA CPU ∩ online CPU ∩ 该卡所有占卡 PID 的有效 cpuset`。只在节点映射唯一且交集非空时生成可执行分区。
2. 两卡共享同一候选区间时一分为二；N 卡时按 N 份尽量均分，余数按 NPU 编号分给靠前卡。CPU 可不连续，输出合法 cpulist。
3. 按 `(socket,node,core)` 将 SMT 同胞作为一个分配单元，避免逻辑 CPU 互斥却共享物理核心。缺 SMT 时标注仅保证逻辑 CPU 互斥。
4. 已互斥的候选区间直接保留；部分重叠且不同的候选集合交由人工分区，报告说明阻塞，避免错误覆盖。一个 NPU 多 PID 共用该卡分区；同一个 PID 使用多卡时，先明确 TID/rank 分工，不对整个 PID 执行互相覆盖的逐卡绑定。
5. 同 NUMA 不表示允许随意使用整节点所有 CPU。只有证据明确允许才扩展 NPU CPU Affinity；默认保持在其公布区间内。

用户案例只有四组亲和区间，每组两卡；在 CPU 全在线、cpuset 放行、无 SMT 同胞冲突的条件下，可推导：

| NPU | 宿主机 PID | topo CPU Affinity | 每卡独立分区 |
|---|---:|---|---|
| 0 | 4115 | 144-167 | 144-155 |
| 1 | 4116 | 0-23 | 0-11 |
| 2 | 4117 | 144-167 | 156-167 |
| 3 | 4118 | 0-23 | 12-23 |
| 4 | 4119 | 96-119 | 96-107 |
| 5 | 4120 | 48-71 | 48-59 |
| 6 | 4121 | 96-119 | 108-119 |
| 7 | 4122 | 48-71 | 60-71 |

对应核心命令（这是条件示例；报告必须用客户当前快照确认，不可把本表 PID 当默认值）：

```bash
# 仅在该案例的物理机/宿主机运行，先备份各线程 affinity。
sudo taskset -acp 144-155 4115
sudo taskset -acp 0-11    4116
sudo taskset -acp 156-167 4117
sudo taskset -acp 12-23   4118
sudo taskset -acp 96-107  4119
sudo taskset -acp 48-59   4120
sudo taskset -acp 108-119 4121
sudo taskset -acp 60-71   4122
```

原例的 `taskset -acp 24-47 4119` 不在 NPU4 公布的 `96-119` 内，不能作为该卡方案。将 4115/4117 都绑 `144-167` 只是共享同一组 CPU，尚未做到分区。仅凭 topo 中 `144-167` 无法推出整 NUMA 是 `144-191`；需以实际拓扑为准。

## 第三部分的完整方案

报告给出包含实际数字的完整 Bash 代码块，HTML 带复制按钮。脚本在宿主机校验 boot_id、PID starttime、online CPU、当前有效 cpuset，逐 TID 保存原始 affinity，然后运行 `taskset -acp` 并逐线程核验；输出备份目录和 `sudo bash .../rollback.sh` 回滚入口。数据缺失时只给最小补采集，不把占位符伪装成可执行方案：

```bash
sudo bash scripts/cpu_trace_collect.sh --duration 30 --output /tmp --hostbound
```

`-a` 包含指定 PID 的现有线程，已有子进程需各自处理；新线程通常继承创建线程 affinity，后续业务也可重设。taskset 不迁移已分配内存、不自动修改 cgroup、不提供重启持久化、不独占 CPU。使用相同负载复采验证收益。

实现的 cgroup 路径覆盖常见 v2 `/sys/fs/cgroup` 和 v1 `/sys/fs/cgroup/cpuset` 挂载；自定义挂载未能解析时显示待核验，不放宽 cpuset。宿主机检测缺少 `systemd-detect-virt` 时记录 unknown，由人工核实运行环境；不要根据单个 namespace 字段猜测宿主机。

官方依据：[util-linux taskset 文档](https://raw.githubusercontent.com/util-linux/util-linux/master/schedutils/taskset.1.adoc)、[Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)、[Linux proc 文件系统](https://docs.kernel.org/filesystems/proc.html)。
