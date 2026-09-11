#!/bin/bash
# ==========================================================
# CPU Host 性能采集脚本 (cpu_trace_collect.sh) — 统一采集入口
# - 仅依赖 bash + ftrace(tracefs/debugfs) + /proc + /sys，无第三方软件
# - 采集前后保存/恢复 tracing 原配置，不破坏客户现场
# - 支持: --duration / --output / --buffer-size / --cpu-list / --minimal / --pid
# - --pid <PID[,PID...]>: 追加 HostBound 只读快照 (NUMA/NPU 拓扑、IRQ、目标进程
#   及线程 affinity 等), 写入采集包 hostbound/ 目录; 不改变 ftrace 事件与恢复行为
# - 产物: cpu_trace_YYYYMMDD_HHMMSS.tar.gz (trace + 系统快照, 加 --pid 时含 hostbound/)
# ==========================================================
set +e  # 单项能力缺失不致命，由脚本自行控制

# ---------- 颜色 ----------
info(){  echo -e "\033[36m[Info] $*\033[0m"; }
ok(){    echo -e "\033[32m[OK]   $*\033[0m"; }
warn(){  echo -e "\033[33m[Warn] $*\033[0m"; }
err(){   echo -e "\033[31m[Err]  $*\033[0m"; }

usage() {
cat <<EOF
CPU Host 性能采集工具 (统一入口: 全量 ftrace + 可选 HostBound 快照)
用法: sudo bash $0 [选项]
  -t, --duration <秒>     采集时长, 默认 30
  -o, --output <目录>     输出目录, 默认当前目录
  -b, --buffer-size <KB>  单核 buffer 大小, 默认 140800
  -c, --cpu-list <列表>   CPU 列表, 如 1,2,3 或 0-47 (默认全量)
  -m, --minimal           精简模式: 仅采集 sched_switch, 体积减小 90%+
  -p, --pid <PID[,PID]>   追加 HostBound 只读快照: NUMA/NPU 拓扑、IRQ、目标进程与
                          线程 affinity 等, 写入 hostbound/ 目录 (不改 ftrace 采集)
  -h, --help              显示帮助
示例:
  sudo bash $0 --duration 30
  sudo bash $0 -t 10 -m
  sudo bash $0 -t 60 -c 0-47 -b 281600
  sudo bash $0 --duration 30 --pid 12345
  sudo bash $0 --duration 30 --pid 12345,6789
EOF
}

# ---------- 参数解析 ----------
DURATION=30; OUTDIR="."; BUFFER_SIZE=140800; CPU_LIST=""; MINIMAL=0; PID_LIST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--duration)    DURATION="$2"; shift 2;;
    -o|--output)      OUTDIR="$2"; shift 2;;
    -b|--buffer-size) BUFFER_SIZE="$2"; shift 2;;
    -c|--cpu-list)    CPU_LIST="$2"; shift 2;;
    -m|--minimal)     MINIMAL=1; shift;;
    -p|--pid)         PID_LIST="$2"; shift 2;;
    -h|--help)        usage; exit 0;;
    *) err "未知参数: $1"; usage; exit 1;;
  esac
done
if [[ "$DURATION" =~ ^[0-9]+$ ]] && [[ "$BUFFER_SIZE" =~ ^[0-9]+$ ]]; then :; else
  err "时长与 buffer 必须为纯数字"; exit 1; fi
# PID 合法性: 逗号分隔的正整数 (不校验进程是否存在, 采集阶段自动跳过)
for _p in ${PID_LIST//,/ }; do
  [[ "$_p" =~ ^[1-9][0-9]*$ ]] || { err "pid 必须为正整数: $_p"; exit 1; }
done
if [[ "$EUID" -ne 0 ]]; then
  err "请使用 root 权限运行: sudo bash $0"; exit 1; fi

# ---------- 定位 tracefs ----------
TRACE_DIR=""
for d in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  [[ -d "$d" ]] && TRACE_DIR="$d" && break
done
if [[ -z "$TRACE_DIR" ]]; then
  err "未找到 tracefs/debugfs (需内核开启 CONFIG_FTRACE 并挂载 debugfs)"; exit 1; fi
ok "使用 tracing 目录: $TRACE_DIR"
if ! echo test_probe_write >/dev/null 2>&1 && [[ ! -w "$TRACE_DIR/tracing_on" ]]; then
  err "tracing 目录不可写, 请确认 root 权限"; exit 1; fi

# ---------- CPU 掩码 ----------
to_mask() {  # "0-3,8" -> 16 进制 mask (每 32 核一组逗号分隔)
  echo "$1" | awk '{ gsub(/\[|\]|[[:space:]]/,""); n=split($0,p,",");
    delete cores; maxc=-1
    for(i=1;i<=n;i++){ if(p[i]~/-/){split(p[i],r,"-");for(c=r[1];c<=r[2];c++){cores[c]=1;if(c>maxc)maxc=c}}
      else if(p[i]~/^[0-9]+$/){cores[p[i]]=1;if(p[i]+0>maxc)maxc=p[i]+0} }
    if(maxc<0){print "";} else {
      nh=int((maxc+32)/32); nh*=32; mask=""
      for(c=0;c<nh;c++){bit=0; if(cores[c])bit=1; g=int(c/32); hex[g]+=bit*2^(c%32)}
      for(g=nh/32-1;g>=0;g--){s=sprintf("%08x",hex[g]); mask=(mask=="")?s:mask","s}
      print mask } }'
}
SAVED_MASK=$(cat "$TRACE_DIR/tracing_cpumask" 2>/dev/null)
if [[ -n "$CPU_LIST" ]]; then
  CPU_MASK=$(to_mask "$CPU_LIST")
  [[ -z "$CPU_MASK" ]] && { err "CPU 列表解析失败: $CPU_LIST"; exit 1; }
  echo "$CPU_MASK" > "$TRACE_DIR/tracing_cpumask" || { err "写入 tracing_cpumask 失败"; exit 1; }
fi

# ---------- 事件集合 ----------
if [[ "$MINIMAL" -eq 1 ]]; then
  EVENT_LIST="sched/sched_switch"
else
  EVENT_LIST="sched/sched_migrate_task sched/sched_wakeup sched/sched_waking sched/sched_switch \
sched/sched_wakeup_new task/task_newtask irq/irq_handler_entry irq/irq_handler_exit \
irq/softirq_entry irq/softirq_exit irq/softirq_raise power/cpu_idle power/cpu_frequency"
fi

# ---------- 保存原配置 (结束后恢复) ----------
SAVED_ON=$(cat "$TRACE_DIR/tracing_on" 2>/dev/null)
SAVED_EVENTS=$(cat "$TRACE_DIR/events/enable" 2>/dev/null)
SAVED_TRACER=$(cat "$TRACE_DIR/current_tracer" 2>/dev/null)
SAVED_BUF=$(cat "$TRACE_DIR/buffer_size_kb" 2>/dev/null)
cleanup() {
  info "恢复 tracing 原始配置..."
  echo "$SAVED_ON"     > "$TRACE_DIR/tracing_on"     2>/dev/null
  echo "$SAVED_TRACER" > "$TRACE_DIR/current_tracer" 2>/dev/null
  echo "$SAVED_BUF"    > "$TRACE_DIR/buffer_size_kb" 2>/dev/null
  [[ -n "$SAVED_MASK" ]] && echo "$SAVED_MASK" > "$TRACE_DIR/tracing_cpumask" 2>/dev/null
  # 关闭本次启用的事件后, 恢复全局 enable 原值
  for ev in $ENABLED_EVENTS; do echo 0 > "$TRACE_DIR/events/$ev/enable" 2>/dev/null; done
  echo "$SAVED_EVENTS" > "$TRACE_DIR/events/enable" 2>/dev/null
  echo > "$TRACE_DIR/trace" 2>/dev/null
}
abort(){ err "检测到中断信号, 停止采集并恢复环境..."; cleanup; exit 1; }
trap abort INT TERM
ENABLED_EVENTS=""

# ---------- 环境初始化 ----------
echo 0 > "$TRACE_DIR/tracing_on" 2>/dev/null
echo nop > "$TRACE_DIR/current_tracer" 2>/dev/null
echo "$BUFFER_SIZE" > "$TRACE_DIR/buffer_size_kb" 2>/dev/null \
  && ok "buffer_size_kb = $BUFFER_SIZE" || warn "设置 buffer 失败, 使用默认值"
echo > "$TRACE_DIR/trace" 2>/dev/null

for ev in $EVENT_LIST; do
  if [[ -f "$TRACE_DIR/events/$ev/enable" ]]; then
    echo 1 > "$TRACE_DIR/events/$ev/enable" && ENABLED_EVENTS="$ENABLED_EVENTS $ev"
  else
    warn "内核不支持事件: $ev (跳过)"
  fi
done
[[ -z "$ENABLED_EVENTS" ]] && { err "没有任何可用事件, 无法采集"; cleanup; exit 1; }
ok "已启用事件:$ENABLED_EVENTS"

# ---------- 系统快照 ----------
mkdir -p "$OUTDIR/cpu_trace" || { err "创建输出目录失败: $OUTDIR"; exit 1; }
SNAP="$OUTDIR/cpu_trace"
snap_file(){ [[ -r "$1" ]] && cp "$1" "$SNAP/$2" 2>/dev/null || echo "N/A (文件不存在)" > "$SNAP/$2"; }
info "采集系统静态快照..."
snap_file /proc/cpuinfo     cpuinfo.txt
snap_file /proc/stat        proc_stat.txt
snap_file /proc/loadavg     loadavg.txt
snap_file /proc/meminfo     meminfo.txt
snap_file /proc/interrupts  interrupts.txt
snap_file /proc/softirqs    softirqs.txt
snap_file /proc/schedstat   schedstat.txt
snap_file /proc/vmstat      vmstat.txt
snap_file /sys/devices/system/cpu/online  cpu_online.txt
# cpufreq 汇总 (存在则采集)
{ for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq; do
    [[ -r "$f" ]] && echo "$f: $(cat "$f" 2>/dev/null)"; done; } > "$SNAP/cpufreq.txt" 2>/dev/null
{ [[ -d /sys/devices/system/node ]] && for f in /sys/devices/system/node/node*/cpulist /sys/devices/system/node/node*/meminfo; do
    [[ -r "$f" ]] && echo "== $f =="; cat "$f" 2>/dev/null; done; } > "$SNAP/numa.txt" 2>/dev/null
# 系统信息 (命令存在才执行, 失败不致命)
{ echo "hostname: $(hostname 2>/dev/null)"; echo "date: $(date '+%Y-%m-%d %H:%M:%S')"; \
  echo "uname: $(uname -a 2>/dev/null)"; echo "nproc: $(nproc 2>/dev/null)"; \
  command -v lscpu   >/dev/null && lscpu; \
  command -v numactl >/dev/null && numactl -H; } > "$SNAP/system_info.txt" 2>/dev/null

# ---------- 执行采集 ----------
START_TS=$(date +%s); START_TIME=$(date "+%Y-%m-%d %H:%M:%S")
echo "$START_TIME" > "$SNAP/trace_start_time.txt"
info "开始采集 trace... ($START_TIME, 时长 ${DURATION}s, 模式: $([[ $MINIMAL -eq 1 ]] && echo 精简 || echo 全量)$([[ -n "$PID_LIST" ]] && echo ", HostBound快照: $PID_LIST"))"
echo 1 > "$TRACE_DIR/tracing_on" || { err "开启 tracing 失败"; cleanup; exit 1; }
sleep "$DURATION"
echo 0 > "$TRACE_DIR/tracing_on"
END_TIME=$(date "+%Y-%m-%d %H:%M:%S"); echo "$END_TIME" > "$SNAP/trace_end_time.txt"
ok "采集结束 ($END_TIME)"

# 提速: 慢的根源是内核读取 trace 文件时逐条 format 成文本且只能单线程进行。
# 改为并行读取各 CPU 独立缓冲区 (多核同时 format, 近 N 核倍提速), 再拼接为单文件。
# 拼接结果与全局 trace 内容一致, 仅跨 CPU 行序不同 —— 分析器会按时间戳统一排序, 不影响结果。
info "导出 trace 数据 (并行读取各 CPU 缓冲区, 速度更快)..."
shopt -s nullglob
PER_CPU_TRACES=("$TRACE_DIR"/per_cpu/cpu*/trace)
if [[ ${#PER_CPU_TRACES[@]} -gt 0 ]]; then
  MAX_PAR=16; RUNNING=0; i=0
  for f in "${PER_CPU_TRACES[@]}"; do
    cat "$f" > "$SNAP/.part_$i.txt" 2>/dev/null &
    i=$((i+1)); RUNNING=$((RUNNING+1))
    # 控制并发数, 避免核数很多的机器一次性 fork 过多进程
    if [[ $RUNNING -ge $MAX_PAR ]]; then
      wait -n 2>/dev/null || wait; RUNNING=$((RUNNING-1))
    fi
  done
  wait
  cat "$SNAP"/.part_*.txt > "$SNAP/trace.txt" 2>/dev/null
  rm -f "$SNAP"/.part_*.txt
  ok "已并行导出 ${#PER_CPU_TRACES[@]} 个 CPU 缓冲区"
else
  # per_cpu 目录不存在时回退为读取全局 trace (兼容旧内核/特殊配置)
  cat "$TRACE_DIR/trace" > "$SNAP/trace.txt" 2>/dev/null
fi
shopt -u nullglob
[[ -s "$SNAP/trace.txt" ]] || { err "trace 为空, 事件可能未产生或 buffer 溢出"; }
# 记录 buffer 溢出情况 (注意: glob 不能加引号, 否则不展开)
cat "$TRACE_DIR"/per_cpu/cpu*/stats/overrun 2>/dev/null | paste -sd+ | bc > "$SNAP/trace_overrun.txt" 2>/dev/null

cleanup

# ---------- HostBound 只读快照 (仅 --pid 时执行) ----------
# 写入 $SNAP/hostbound/, 随采集包一并打包; 文件名布局与分析器约定一致。
snapshot_hostbound() {
  local HB="$SNAP/hostbound"
  mkdir -p "$HB" 2>/dev/null || { warn "无法创建 hostbound 快照目录, 跳过"; return 0; }
  info "采集 HostBound 只读快照 -> hostbound/"
  _hbcap(){ "$@" > "$HB/$1.txt" 2>&1 || true; }
  _hbcap nproc
  _hbcap lscpu
  _hbcap numactl -H
  _hbcap taskset -pc 1
  cp /proc/interrupts "$HB/interrupts_at_end.txt" 2>/dev/null || true
  cp /proc/softirqs    "$HB/softirqs_at_end.txt" 2>/dev/null || true
  { cat /proc/cmdline; echo; } > "$HB/kernel_cmdline.txt" 2>/dev/null || true
  if command -v npu-smi >/dev/null 2>&1; then
    npu-smi info         > "$HB/npu_smi_info.txt" 2>&1 || true
    npu-smi info -m      > "$HB/npu_smi_map.txt" 2>&1 || true
    npu-smi info -t topo > "$HB/npu_smi_topology.txt" 2>&1 || true
  fi
  local p tid
  for p in ${PID_LIST//,/ }; do
    [[ -d "/proc/$p" ]] || { warn "进程不存在, 跳过其快照: $p"; continue; }
    cp "/proc/$p/status" "$HB/pid_${p}_status.txt" 2>/dev/null || true
    taskset -pc "$p" > "$HB/pid_${p}_affinity.txt" 2>&1 || true
    ps -T -p "$p" -o pid,tid,psr,pcpu,stat,comm > "$HB/pid_${p}_threads.txt" 2>&1 || true
    while read -r tid; do taskset -pc "$tid"; done < <(ls "/proc/$p/task") \
      > "$HB/pid_${p}_thread_affinity.txt" 2>&1 || true
  done
  # 清单: 记录本次未运行的外部工具, 避免把缺失数据误当作正常
  cat > "$HB/collection_manifest.json" <<EOF
{"collector":"cpu_trace_collect.sh","mode":"unified","duration_sec":$DURATION,"pid":"${PID_LIST}","minimal":$MINIMAL,"ftrace":"cpu_trace_collect.sh","external_tools":{"gil_tracer":"not_run","function_monitor":"not_run","msprof":"not_run","host_analyzer":"not_run"}}
EOF
  ok "HostBound 快照完成 (hostbound/)"
}
if [[ -n "$PID_LIST" ]]; then
  snapshot_hostbound
fi

# ---------- 元数据与打包 ----------
cat > "$SNAP/metadata.txt" <<EOF
collect_tool: cpu_trace_collect.sh
start_time: $START_TIME
end_time: $END_TIME
duration_sec: $DURATION
minimal_mode: $MINIMAL
cpu_list_input: ${CPU_LIST:-all}
hostbound_pid: ${PID_LIST:-none}
enabled_events:$ENABLED_EVENTS
kernel: $(uname -r 2>/dev/null)
hostname: $(hostname 2>/dev/null)
trace_dir: $TRACE_DIR
EOF
PKG=$(realpath "$OUTDIR" 2>/dev/null || echo "$OUTDIR")/cpu_trace_$(date +%Y%m%d_%H%M%S).tar.gz
if tar -czf "$PKG" -C "$OUTDIR" cpu_trace 2>/dev/null; then
  ok "完成! 采集包: $PKG"
  ok "请将此文件回传用于分析 (可直接用本 Skill 分析, 或拖入 https://ui.perfetto.dev 查看)"
else
  warn "打包失败, 原始数据保留在: $SNAP/"
  ok "请将整个 cpu_trace 目录回传用于分析"
fi
exit 0
