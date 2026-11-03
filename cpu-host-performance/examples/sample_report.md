# HostBound 性能诊断报告

> 演示报告：仅 NPU/PID/topo 来自所给案例；NUMA、线程许可范围、PSR、trace 与身份字段为模拟数据，不代表客户当前状态，示例命令不可直接用于客户现场。

> 数据格式：ftrace 文本 (目录内) ｜ 采集时长：1.0 秒 ｜ 核心数：1 ｜ 分析时间：2026-11-02 17:34:44

## 1. Executive Summary（结论摘要）

### Host 状态：CRITICAL（严重异常）

### 核心问题

**CPU 整体饱和**

### 影响

Host 算力不足，任务排队等待，可能拖慢 NPU 供数

完整优化方案与可复制命令见第三部分。

## 2. 问题证据总表与根因判断

### 2.1 关键指标证据

| 指标 | 当前值 | 基线/阈值 | 状态 |
|------|-------:|-----------|------|
| CPU 平均利用率 | 100.0% | <70% 健康 | ⚠️ 偏高 |
| 单核最高利用率 | 100.0% | <90% 健康 | ⚠️ 饱和 |
| 上下文切换率 | 3 次/秒 | 全机 > 2000×核数/s 偏高 | 正常 |
| 平均 runnable 任务数 | 0.0 | 接近核心数说明排队 | 正常 |
| 硬中断总占比 | 0.00% | 单核≥15% 关注热点 | 正常 |
| 软中断总占比 | 0.00% | 单核≥20% 关注热点 | 正常 |
| 调度延迟 P99 | 无法计算 | - | 数据缺失 |

> 指标含义见第 13 节「指标与事件说明」。

### 2.2 根因判断与证据链

**现象** → CPU 整体饱和

**证据** → 全机平均 CPU 利用率=100.0%（阈值 ≥90% 判定饱和）

**推断** → 整机 CPU 计算需求超过供给

**候选判断** → `CPU_SATURATION`（CPU 饱和：整体 CPU 已接近打满，算力不足）

### 2.3 NPU 任务与 NUMA 亲和性

allowed CPU 是调度许可范围；PSR 是采样时最近运行核，ftrace 是窗口内观测核。绿色表示全量现有线程的许可范围落在 NPU 亲和 CPU 内；橙色表示可跨节点或证据不足；红色表示观测到非亲和 CPU/绑定范围偏离。CPU 绑定与内存页 NUMA 分布分别核验，亲和性风险不等于已确认的 HostBound 根因。主进程按占卡显存最高推定，所有占卡 PID 均保留。

| NPU/Chip | 主进程/其他进程 | 宿主机 PID | 线程数 | 亲和 NUMA / CPU | 当前 allowed CPU | 观测 NUMA | ftrace / PSR | CPU 亲和判断 | 建议分区 |
|---|---|---:|---:|---|---|---|---|---|---|
| 0/0 | python3（主进程（显存占用最高）） | 4115 | 2 | 3 / 144-167 | 0-191 | 0 | 30 / 30 | 观测到非亲和 CPU | 144-155 |
| 1/0 | python3（主进程（显存占用最高）） | 4116 | 2 | 0 / 0-23 | 0-11 | 0 | 未提供 / 2 | 已绑定亲和 CPU | 0-11 |
| 2/0 | python3（主进程（显存占用最高）） | 4117 | 2 | 3 / 144-167 | 0-191 | 0 | 未提供 / 30 | 观测到非亲和 CPU | 156-167 |
| 3/0 | python3（主进程（显存占用最高）） | 4118 | 2 | 0 / 0-23 | 0-191 | 0 | 未提供 / 30 | 观测到非亲和 CPU | 12-23 |
| 4/0 | python3（主进程（显存占用最高）） | 4119 | 2 | 2 / 96-119 | 0-191 | 2 | 未提供 / 100 | 允许跨 NUMA / 未收敛 | 96-107 |
| 5/0 | python3（主进程（显存占用最高）） | 4120 | 2 | 1 / 48-71 | 0-191 | 0 | 未提供 / 30 | 观测到非亲和 CPU | 48-59 |
| 6/0 | python3（主进程（显存占用最高）） | 4121 | 2 | 2 / 96-119 | 0-191 | 0 | 未提供 / 30 | 观测到非亲和 CPU | 108-119 |
| 7/0 | python3（主进程（显存占用最高）） | 4122 | 2 | 1 / 48-71 | 0-191 | 0 | 未提供 / 30 | 观测到非亲和 CPU | 60-71 |

> NPU 1,3 共享 0-23，按 2 份分区；每份 CPU 互不重叠。

> NPU 5,7 共享 48-71，按 2 份分区；每份 CPU 互不重叠。

> NPU 4,6 共享 96-119，按 2 份分区；每份 CPU 互不重叠。

> NPU 0,2 共享 144-167，按 2 份分区；每份 CPU 互不重叠。

证据来源：hostbound/npu_smi_*.txt、numactl.txt/lscpu_parse.txt、pid_* 状态/线程/affinity/cpuset；ftrace 运行观测与配置快照可能位于不同采样时点。

## 3. 优化建议

### NPU / NUMA 绑核方案（宿主机执行）

以下方案仅在原物理机/宿主机执行，使用宿主机 PID；需要 sudo、taskset 和 python3。taskset -acp 覆盖指定 PID 的现有线程，已有子进程应逐 PID 处理；新线程通常继承创建线程的 affinity，业务可重新设置，执行后需复查。taskset 不迁移既有内存页，不提供跨重启持久化，也不独占 CPU；同机其他业务/IRQ 的 CPU 争用仍须结合负载核验。有效 cpuset 不允许的区间需先在宿主机调整资源配置，再重新采集。

脚本先核验主机/进程身份、在线 CPU 和有效 cpuset，逐 TID 保存原 affinity 后执行；输出备份目录与可运行的回滚命令。

```bash
# 仅在原物理机/宿主机执行；不要进入容器。先阅读报告，再运行。
sudo bash <<'HOST_CPU_BIND'
set -euo pipefail
[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || { echo "请回到宿主机"; exit 1; }
if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then echo "请回到宿主机"; exit 1; fi
[ "$(cat /proc/sys/kernel/random/boot_id)" = 'test-boot' ] || { echo '主机/启动周期已变化，请重新采集'; exit 1; }
backup=$(mktemp -d /var/tmp/npu-affinity.XXXXXX)
echo "备份和回滚脚本：$backup"
cat > "$backup/rollback.sh" <<'ROLLBACK_HEADER'
#!/usr/bin/env bash
set -euo pipefail
[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || exit 1
if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then exit 1; fi
[ "$(cat /proc/sys/kernel/random/boot_id)" = 'test-boot' ] || { echo '回滚主机/启动周期不一致'; exit 1; }
failed=0
restore_tid() {
  local tid=$1 expected=$2 previous=$3 s
  if [ ! -r "/proc/$tid/stat" ]; then echo "跳过已退出 TID $tid"; return; fi
  s=$(cat "/proc/$tid/stat") || { echo "跳过已退出 TID $tid"; return; }
  s=${s##*) }; set -- $s
  if [ "${20}" != "$expected" ]; then echo "跳过已复用 TID $tid"; return; fi
  if ! taskset -pc "$previous" "$tid"; then failed=1; fi
}
ROLLBACK_HEADER
starttime() { local s; s=$(cat "/proc/$1/stat"); s=${s##*) }; set -- $s; printf '%s' "${20}"; }
check_cpu_list() { local want=$1 have=$2; python3 - "$want" "$have" <<'CPU_CHECK'
import sys
def cpus(s):
    out=set()
    for part in s.strip().split(','):
        a,_,b=part.partition('-')
        if a: out.update(range(int(a),int(b or a)+1))
    return out
raise SystemExit(0 if cpus(sys.argv[1]) and cpus(sys.argv[1]) <= cpus(sys.argv[2]) else 1)
CPU_CHECK
}
effective_cpuset() { local p=$1 cg value; cg=$(awk -F: '$1==0 {print $3}' /proc/"$p"/cgroup);
if [ -n "$cg" ] && [ -r "/sys/fs/cgroup${cg}/cpuset.cpus.effective" ]; then cat "/sys/fs/cgroup${cg}/cpuset.cpus.effective"; return; fi
cg=$(awk -F: '$2 ~ /(^|,)cpuset(,|$)/ {print $3}' /proc/"$p"/cgroup)
[ -n "$cg" ] || return 1
for f in "/sys/fs/cgroup/cpuset${cg}/cpuset.effective_cpus" "/sys/fs/cgroup/cpuset${cg}/cpuset.cpus"; do if [ -r "$f" ]; then value=$(cat "$f"); if [ -n "$value" ]; then printf "%s\n" "$value"; return; fi; fi; done
return 1; }
# 全部预检、逐 TID 保存成功之后才执行绑核。
[ "$(starttime 4115)" = '411500' ] || { echo 'PID 4115 已变化，请重新采集'; exit 1; }
check_cpu_list '144-155' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '144-155' "$(effective_cpuset 4115)" || { echo 'PID 4115 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4115/task/*; do
  tid=${task##*/}
  check_cpu_list '144-155' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
[ "$(starttime 4117)" = '411700' ] || { echo 'PID 4117 已变化，请重新采集'; exit 1; }
check_cpu_list '156-167' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '156-167' "$(effective_cpuset 4117)" || { echo 'PID 4117 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4117/task/*; do
  tid=${task##*/}
  check_cpu_list '156-167' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
[ "$(starttime 4118)" = '411800' ] || { echo 'PID 4118 已变化，请重新采集'; exit 1; }
check_cpu_list '12-23' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '12-23' "$(effective_cpuset 4118)" || { echo 'PID 4118 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4118/task/*; do
  tid=${task##*/}
  check_cpu_list '12-23' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
[ "$(starttime 4119)" = '411900' ] || { echo 'PID 4119 已变化，请重新采集'; exit 1; }
check_cpu_list '96-107' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '96-107' "$(effective_cpuset 4119)" || { echo 'PID 4119 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4119/task/*; do
  tid=${task##*/}
  check_cpu_list '96-107' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
[ "$(starttime 4120)" = '412000' ] || { echo 'PID 4120 已变化，请重新采集'; exit 1; }
check_cpu_list '48-59' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '48-59' "$(effective_cpuset 4120)" || { echo 'PID 4120 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4120/task/*; do
  tid=${task##*/}
  check_cpu_list '48-59' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
[ "$(starttime 4121)" = '412100' ] || { echo 'PID 4121 已变化，请重新采集'; exit 1; }
check_cpu_list '108-119' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '108-119' "$(effective_cpuset 4121)" || { echo 'PID 4121 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4121/task/*; do
  tid=${task##*/}
  check_cpu_list '108-119' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
[ "$(starttime 4122)" = '412200' ] || { echo 'PID 4122 已变化，请重新采集'; exit 1; }
check_cpu_list '60-71' "$(cat /sys/devices/system/cpu/online)"
check_cpu_list '60-71' "$(effective_cpuset 4122)" || { echo 'PID 4122 的 cpuset 不允许目标 CPU'; exit 1; }
for task in /proc/4122/task/*; do
  tid=${task##*/}
  check_cpu_list '60-71' "$(effective_cpuset "$tid")" || { echo "TID $tid 的 cpuset 不允许目标 CPU"; exit 1; }
  taskset -pc "$tid" >> "$backup/before.txt"
  old=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  stamp=$(starttime "$tid")
  printf 'restore_tid %s %s %s\n' "$tid" "$stamp" "$old" >> "$backup/rollback.sh"
done
printf '%s\n' 'exit "$failed"' >> "$backup/rollback.sh"
# -a 覆盖该 PID 的现有线程；已有子进程需要各自的宿主机 PID。
[ "$(starttime 4115)" = '411500' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 144-155 4115
[ "$(starttime 4117)" = '411700' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 156-167 4117
[ "$(starttime 4118)" = '411800' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 12-23 4118
[ "$(starttime 4119)" = '411900' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 96-107 4119
[ "$(starttime 4120)" = '412000' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 48-59 4120
[ "$(starttime 4121)" = '412100' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 108-119 4121
[ "$(starttime 4122)" = '412200' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }
taskset -acp 60-71 4122
# 验证所有线程的 allowed CPU，保存执行后快照。
taskset -apc 4115 | tee -a "$backup/after.txt"
for task in /proc/4115/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '144-155' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
taskset -apc 4117 | tee -a "$backup/after.txt"
for task in /proc/4117/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '156-167' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
taskset -apc 4118 | tee -a "$backup/after.txt"
for task in /proc/4118/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '12-23' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
taskset -apc 4119 | tee -a "$backup/after.txt"
for task in /proc/4119/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '96-107' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
taskset -apc 4120 | tee -a "$backup/after.txt"
for task in /proc/4120/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '48-59' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
taskset -apc 4121 | tee -a "$backup/after.txt"
for task in /proc/4121/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '108-119' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
taskset -apc 4122 | tee -a "$backup/after.txt"
for task in /proc/4122/task/*; do
  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' "$task/status")
  [ "$actual" = '60-71' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }
done
echo "如需回滚，在本宿主机运行：sudo bash $backup/rollback.sh"
HOST_CPU_BIND
```

执行后以相同负载复采，比较吞吐、Device 空泡、调度延迟与跨节点运行；无收益或出现副作用时按脚本输出回滚。

**P0（立即处理） — CPU 整体饱和**

1. 识别并优化 Top CPU 任务 (见 Task 分析)
2. 提高 Host 并行度配置或升级 CPU
3. 排查业务是否存在无意义忙等/自旋

**P1（尽快处理） — NPU 任务 CPU 亲和性存在风险（待性能验证）**

1. 按第二部分的 NPU/PID 对照核验，第三部分给出宿主机预检、逐 TID 备份、互斥分区、taskset 命令及回滚
2. 同负载复采验证吞吐与延迟；taskset 不迁移内存页，不越过有效 cpuset

## 4. 问题详情

### 问题 1：CPU 整体饱和（严重 / 置信度 HIGH）

- **类别**：`CPU_SATURATION` — CPU 饱和：整体 CPU 已接近打满，算力不足
- **证据**：
  - 全机平均 CPU 利用率 = **100.0%**（判定阈值：≥90% 判定饱和；来源：ftrace / 对应系统快照；时间窗：本次采集窗口；累计快照除外）
- **影响**：Host 算力不足，任务排队等待，可能拖慢 NPU 供数
- **可能根因**：整机 CPU 计算需求超过供给
- **优化动作**：见第三部分。

### 问题 2：NPU 任务 CPU 亲和性存在风险（待性能验证）（警告 / 置信度 MEDIUM）

- **类别**：`NUMA_RISK` — NUMA 风险：存在跨 NUMA 节点访问的可能性
- **证据**：
  - NPU/PID 与 CPU 亲和性 = **NPU0/PID4115: allowed=0-191, observed=30, local=144-167；NPU2/PID4117: allowed=0-191, observed=30, local=144-167；NPU3/PID4118: allowed=0-191, observed=30, local=0-23；NPU4/PID4119: allowed=0-191, observed=100, local=96-119；NPU5/PID4120: allowed=0-191, observed=30, local=48-71；NPU6/PID4121: allowed=0-191, observed=30, local=96-119；NPU7/PID4122: allowed=0-191, observed=30, local=48-71**（判定阈值：全量线程 allowed CPU 收敛到 NPU 亲和 CPU；观测核应位于亲和范围；来源：hostbound/npu_smi_topology.txt + npu_smi_info.txt + pid_*_thread_affinity.txt/threads.txt + ftrace；时间窗：ftrace 100.000~101.000s；配置/PSR 为采集结束快照）
- **影响**：配置可能引入跨节点运行或共享 CPU 争用；未关联 Device 空泡/性能差异，不能确认 HostBound 根因
- **可能根因**：已识别 NPU 对应进程的调度许可范围未收敛，或观测到非亲和 CPU；内存页位置仍待核验
- **优化动作**：见第三部分。

### 问题 3：本次采集数据维度有限（提示 / 置信度 HIGH）

- **类别**：`INSUFFICIENT_EVIDENCE` — 证据不足：当前数据无法支持可靠结论
- **证据**：
  - 缺失维度 = **缺少 cpu_idle 事件，利用率基于 swapper 切换近似估算；缺少 wakeup 事件，无法计算调度延迟；缺少 IRQ 事件，无法分析硬中断；缺少 SoftIRQ 事件，无法分析软中断；缺少 cpu_frequency 事件，无法分析频率**（判定阈值：-；来源：ftrace / 对应系统快照；时间窗：本次采集窗口；累计快照除外）
  - 采集模式 = **精简模式(仅 sched_switch)**（判定阈值：-；来源：ftrace / 对应系统快照；时间窗：本次采集窗口；累计快照除外）
- **影响**：相关维度结论缺失，无法覆盖该类问题
- **可能根因**：采集脚本精简模式或内核不支持部分事件
- **优化动作**：见第三部分。

## 5. CPU 总体状态

无 /proc/stat 快照，总体占用以 trace 计算为准。

## 6. CPU Core Balance（各核心负载）

| 核心 | 利用率 | 空闲比 | 上下文切换/s | 硬中断占比 | 软中断占比 |
|-----|-------:|-------:|------------:|----------:|----------:|
| CPU 30 | 100.0% | 0.0% | 3 | 0.0% | 0.0% |

最高负载：30（100.0%），最低：30（100.0%）。负载分布尚可。

## 7. Scheduler 调度分析

- 上下文切换总计 **3** 次（3 次/秒）
- 唤醒总计 **0** 次（0 次/秒）— 唤醒率反映任务碎片化程度。
- 调度延迟：trace 中缺少 wakeup 与 switch 可配对事件，无法计算调度延迟
- 平均可运行任务数 0.0 — 平均可运行任务数 (runnable)：任意时刻处于等待 CPU 状态的平均任务数，>核心数说明调度拥挤。

## 8. IRQ / SoftIRQ 分析

### 8.1 硬中断

硬中断总 CPU 占比 **0.00%**（0 次）。

### 8.2 软中断

软中断总 CPU 占比 **0.00%**（0 次）。

各核中断分布见第 6 节表格与 HTML 图表。

## 9. CPU Frequency / Idle 分析

trace 中无 cpu_frequency 事件，频率分析不可用（Available: Unavailable）。

trace 中无 cpu_idle 事件，空闲比基于 swapper 任务近似。

## 10. Task / Thread 分析

| Top 运行时间任务 | 累计运行 ms | 说明 |
|-----------------|-----------:|------|
| python3 (pid 4115) | 100 | Python 训练/推理主进程或 dataloader |

| Top 切出次数任务 | 次数 |
|-----------------|-----:|
| python3 (pid 4115) | 1 |

## 11. Affinity / NUMA 分析

Top 任务运行分布与各核利用率见第 6 节；任务集中在少数核时可结合绑核配置判断。
CPU 亲和性、NPU/PID 对应及线程运行核见第二部分；互斥 CPU 分区和宿主机命令见第三部分。
内存页 NUMA 分布与跨节点内存访问须 `numastat -p PID`、`/proc/PID/numa_maps` 或性能数据验证；taskset 本身不迁移内存页。

## 12. 时间窗口分析

时间序列数据不足，无法进行时间窗口分析。

## 13. 数据说明与指标解释

### 数据概况

- trace 时间范围：100.000 ~ 101.000（相对秒），时长 1.0 秒
- CPU 数量（参与统计）：1；报告核数（nproc）：-
- 内核版本：synthetic ｜ 主机名：DEMO-ONLY-NOT-CUSTOMER
- 可用事件：sched_switch
- 解析统计：总行 3，成功 3，失败 0（失败行已自动跳过）
- buffer 溢出：0（未检测到溢出）
- HostBound 扩展快照：cpu_online.txt、host_context.txt、lscpu_parse.txt、npu_smi_info.txt、npu_smi_topology.txt、numactl.txt、pid_4115_cpuset_effective.txt、pid_4115_starttime.txt、pid_4115_status.txt、pid_4115_thread_affinity.txt、pid_4115_threads.txt、pid_4116_cpuset_effective.txt、pid_4116_starttime.txt、pid_4116_status.txt、pid_4116_thread_affinity.txt、pid_4116_threads.txt、pid_4117_cpuset_effective.txt、pid_4117_starttime.txt、pid_4117_status.txt、pid_4117_thread_affinity.txt、pid_4117_threads.txt、pid_4118_cpuset_effective.txt、pid_4118_starttime.txt、pid_4118_status.txt、pid_4118_thread_affinity.txt、pid_4118_threads.txt、pid_4119_cpuset_effective.txt、pid_4119_starttime.txt、pid_4119_status.txt、pid_4119_thread_affinity.txt、pid_4119_threads.txt、pid_4120_cpuset_effective.txt、pid_4120_starttime.txt、pid_4120_status.txt、pid_4120_thread_affinity.txt、pid_4120_threads.txt、pid_4121_cpuset_effective.txt、pid_4121_starttime.txt、pid_4121_status.txt、pid_4121_thread_affinity.txt、pid_4121_threads.txt、pid_4122_cpuset_effective.txt、pid_4122_starttime.txt、pid_4122_status.txt、pid_4122_thread_affinity.txt、pid_4122_threads.txt（已纳入证据覆盖说明；名称匹配本身不构成根因）。

### 分析限制

- trace 仅覆盖采集窗口，不代表长期行为。
- 利用率基于 idle 事件/swapper 近似；user/system 区分依赖 /proc/stat（开机累计）。
- 调度延迟为 wakeup→switch 配对估算；中断耗时为 entry→exit 配对。
- 频率单位自动换算可能存在平台差异，仅作相对比较。

### 指标解释（每个指标是什么）

| 指标 | 解释 |
|------|------|
| cpu_util | CPU 利用率：非空闲时间占比。trace 中可精确得到 busy/idle，user/system 区分依赖 /proc/stat 快照（为开机以来累计值）。 |
| cpu_util_p95 | CPU 利用率 P95：将所有核心利用率排序后取第 95 分位，反映高负载核心的普遍水平。 |
| cs_rate | 上下文切换率 (context switches/sec)：每秒进程/线程切换次数，过高说明任务碎片化或争抢激烈。 |
| wakeup_rate | 唤醒率 (wakeups/sec)：每秒任务被唤醒进入 runnable 的次数，反映任务碎片化程度与调度压力。 |
| sched_latency | 调度延迟 (scheduling latency)：任务从被唤醒 (sched_wakeup) 到真正获得 CPU (sched_switch) 的等待时间，直接体现 runnable 队列拥堵程度。 |
| runnable_avg | 平均可运行任务数 (runnable)：任意时刻处于等待 CPU 状态的平均任务数，>核心数说明调度拥挤。 |
| irq_time | 硬中断 CPU 占用：CPU 在处理硬件中断上花费的时间占比 (irq_handler_entry~exit)。 |
| softirq_time | 软中断 CPU 占用：CPU 在处理软中断上花费的时间占比 (softirq_entry~exit)。软中断是中断下半部延迟处理机制。 |
| irq_hotspot | 中断热点：单个核心 IRQ/SoftIRQ 占用远高于其他核心，通常由网卡中断亲和性 (IRQ affinity) 固定在同一核导致。 |
| freq_avg | CPU 平均频率：trace 期间 cpu_frequency 事件的平均值，高负载下频率低说明降频/温控/调度器策略问题。 |
| idle_ratio | CPU 空闲占比：idle 状态时间占比，过高说明该核负载不足或任务未调度到该核。 |
| migration | 任务迁移次数：任务在 CPU 之间搬移的次数 (sched_migrate_task 或跨核运行推断)，频繁迁移导致缓存失效、NUMA 跨访。 |
| preempt | 抢占次数：任务未运行完就被更高优先级任务切出 (prev_state=R 的 sched_switch)。 |
| buffer_overrun | buffer 溢出条数：采集期间 ring buffer 溢出而丢失的事件数，>0 说明 trace 可能不完整，结论置信度应下调。 |

### 事件/术语解释

| 术语 | 解释 |
|------|------|
| NET_RX | 网络接收软中断：网卡收到数据包后，协议栈处理（IP/TCP 解析、socket 分发）在软中断中延迟执行。NET_RX 软中断打满单核是分布式训练/推理通信中最常见的 IRQ 热点根因。 |
| NET_TX | 网络发送软中断：网卡发送完成后的收尾处理。 |
| TIMER | 定时器软中断：内核定时器 (tick/timer_list) 的到期处理，过高说明大量定时器密集到期。 |
| TASKLET | TASKLET 软中断：驱动常用的延迟执行机制，常见于网卡/NVMe 驱动。 |
| SCHED | 调度软中断：进程调度相关延迟处理（负载均衡、调度类节流等）。 |
| RCU | RCU 软中断：Read-Copy-Update 宽限期回收，核心数多或 RCU pressure 大时会明显。 |
| BLOCK | 块设备软中断：块 I/O 完成处理，存储密集场景明显。 |
| HI | 高优先级软中断：最高优先级软中断，数量少但可抢占其他软中断。 |
| HRTIMER | 高精度定时器软中断：hrtimer 到期处理。 |
| irq_handler_entry | 硬件中断进入事件：CPU 开始处理某条硬件中断线，irq=中断号，name=中断名（如 nvme0q3、eth0、iri/pci-MSI）。 |
| irq_handler_exit | 硬件中断退出事件：与 entry 配对可得单次中断耗时，ret=handled 表示有实际处理。 |
| softirq_entry | 软中断进入事件：vec=软中断编号，[action=NAME] 是软中断名。 |
| softirq_exit | 软中断退出事件：与 entry 配对可得单次软中断耗时。 |
| softirq_raise | 软中断触发事件：标记即将执行某类软中断（不代表已执行，仅作触发频度参考）。 |
| sched_switch | 进程切换事件：CPU 上 prev（让出 CPU 的任务）切换到 next（获得 CPU 的任务），是计算利用率、上下文切换、运行时间的基础。 |
| sched_wakeup | 任务唤醒事件：任务因 I/O 完成/锁释放等被唤醒进入 runnable 队列，与 sched_switch 配对可算调度延迟。 |
| sched_waking | 任务开始唤醒事件：wakeup 的前置动作，一般与 sched_wakeup 成对。 |
| sched_wakeup_new | 新创建任务首次唤醒事件。 |
| task_newtask | 新建任务事件 (fork/clone)。 |
| sched_migrate_task | 任务迁移事件：任务被调度器迁往其他 CPU。 |
| cpu_idle | CPU 空闲状态事件：state=进入的 C-state，state=4294967295 表示退出 idle 恢复运行。 |
| cpu_frequency | CPU 频率变化事件：state=新频率（单位通常 kHz，x86 平台）或 Hz（ARM 部分平台）。 |
| irq_line_unknown | 未收录中断名：按中断号展示，可在 /proc/interrupts 中查询对应设备。 |