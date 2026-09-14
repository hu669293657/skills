#!/bin/bash
# ==========================================================
# CPU Host 性能采集脚本 (cpu_trace_collect.sh) — 统一采集入口 v2
# ----------------------------------------------------------
# 设计要点:
#   1. [P0] 绝不基于文件 size 判断 tracefs 伪文件 (ls/stat/[[ -s ]] 恒显示 0),
#      必须实际读取 (cat 返回码 + wc -c 字节数 + wc -l 行数) 后再下结论;
#      明确区分: [ERR] trace read failed / [WARN] trace contains no event
#   2. 导出架构: Global trace 单次读取为主路径; per-CPU 缓冲区仅作 fallback,
#      且并发受控 (-j 或 min(16, CPU 数) 分级), 禁止默认 256 路并行
#   3. --benchmark-export: 实测 A(global cat)/B(per-CPU 全并发)/C(per-CPU 受控并发)
#   4. trace-cmd 仅为可选快速路径 (command -v 探测), 绝不作为硬依赖
#   5. buffer 支持 auto 模式 (默认 16MB/CPU, 不超可用内存 25%), overrun>0 给出建议
#   6. 事件 enable 前存在性检查, 缺失则 skip+warn, 输出 Enabled/Skipped 列表
#   7. trap cleanup EXIT INT TERM, 恢复 tracing_on/current_tracer/buffer/cpumask/事件
#   8. 产物兼容分析器契约: trace.txt / metadata.txt(key:value) / trace_overrun.txt(纯整数)
#      / 快照文件名集合 / hostbound/ 相对路径 (新增报告文件名避开 trace 前缀)
# ==========================================================

# ---------- Bash 兼容性检查 (拒绝 sh/dash) ----------
if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "[Err] 请使用 bash 运行本脚本: bash $0 (当前 shell 非 bash, wait -n/[[ ]] 等不可用)" >&2
  exit 1
fi

set +e  # 单项能力缺失不致命, 由脚本自行控制返回码

# ---------- 颜色 ----------
info(){  echo -e "\033[36m[Info] $*\033[0m"; }
ok(){    echo -e "\033[32m[OK]   $*\033[0m"; }
warn(){  echo -e "\033[33m[Warn] $*\033[0m"; }
err(){   echo -e "\033[31m[Err]  $*\033[0m"; }

# 毫秒时间戳 (GNU date)
now_ms(){ date +%s%3N; }
# 字节数 -> MiB
to_mib(){ awk -v b="$1" 'BEGIN{ printf "%.1f", b/1048576 }'; }
# 速度 MB/s
calc_speed(){ awk -v b="$1" -v ms="$2" 'BEGIN{ if(ms>0) printf "%.1f", b/1048576/(ms/1000); else print "N/A" }'; }

usage() {
cat <<EOF
CPU Host 性能采集工具 v2 (统一入口: 全量 ftrace + 可选 HostBound 快照)
用法: sudo bash $0 [选项]
  -t, --duration <秒>       采集时长, 默认 30
  -o, --output <目录>       输出目录, 默认当前目录
  -b, --buffer-size <KB>    单核 buffer 大小; auto 或 0 = 自动模式 (默认 16MB/核,
                            且总量不超过可用内存 25%), 默认 auto
  -c, --cpu-list <列表>     CPU 列表, 如 1,2,3 或 0-47 (默认全量)
  -m, --minimal             精简模式: 仅采集 sched_switch, 体积减小 90%+
  -p, --pid <PID[,PID]>     追加 HostBound 只读快照, 写入 hostbound/ 目录
  -j, --jobs <N>            per-CPU fallback 导出并发数 (默认按核数分级:
                            <=32:4  <=64:8  <=128:12  >128:16)
      --export-mode <模式>  auto|global|percpu (默认 auto: global 优先,
                            失败/为空时回退 per-CPU 受控并发)
      --benchmark-export    基准测试模式: 实测 导出方法A(global cat)/
                            B(per-CPU 全并发)/C(per-CPU 受控并发) 的耗时与速度
      --use-trace-cmd       实验性: 若系统装有 trace-cmd 则尝试其快速导出,
                            任何失败自动回退内置 tracefs 路径 (非硬依赖)
  -h, --help                显示帮助
示例:
  sudo bash $0 --duration 30
  sudo bash $0 -t 10 -m
  sudo bash $0 -t 60 -c 0-47 -b auto
  sudo bash $0 -t 30 -j 8 --export-mode percpu
  sudo bash $0 -t 5 --benchmark-export
  sudo bash $0 --duration 30 --pid 12345
EOF
}

# ---------- 参数解析 ----------
DURATION=30; OUTDIR="."; BUFFER_SIZE="auto"; CPU_LIST=""; MINIMAL=0; PID_LIST=""
MAX_EXPORT_JOBS=""; EXPORT_MODE="auto"; BENCHMARK=0; USE_TRACE_CMD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--duration)      DURATION="$2"; shift 2;;
    -o|--output)        OUTDIR="$2"; shift 2;;
    -b|--buffer-size)   BUFFER_SIZE="$2"; shift 2;;
    -c|--cpu-list)      CPU_LIST="$2"; shift 2;;
    -m|--minimal)       MINIMAL=1; shift;;
    -p|--pid)           PID_LIST="$2"; shift 2;;
    -j|--jobs)          MAX_EXPORT_JOBS="$2"; shift 2;;
    --export-mode)      EXPORT_MODE="$2"; shift 2;;
    --benchmark-export) BENCHMARK=1; shift;;
    --use-trace-cmd)    USE_TRACE_CMD=1; shift;;
    -h|--help)          usage; exit 0;;
    *) err "未知参数: $1"; usage; exit 1;;
  esac
done
if [[ "$DURATION" =~ ^[0-9]+$ ]]; then :; else err "时长必须为纯数字"; exit 1; fi
if [[ "$BUFFER_SIZE" == "auto" || "$BUFFER_SIZE" == "0" ]]; then
  BUFFER_MODE="auto"; BUFFER_SIZE_KB=0
else
  BUFFER_MODE="fixed"
  [[ "$BUFFER_SIZE" =~ ^[0-9]+$ ]] || { err "buffer 必须为纯数字或 auto"; exit 1; }
  BUFFER_SIZE_KB="$BUFFER_SIZE"
fi
case "$EXPORT_MODE" in auto|global|percpu) ;; *) err "export-mode 仅支持 auto|global|percpu"; exit 1;; esac
if [[ -n "$MAX_EXPORT_JOBS" ]]; then
  [[ "$MAX_EXPORT_JOBS" =~ ^[1-9][0-9]*$ ]] || { err "-j 必须为正整数"; exit 1; }
fi
# PID 合法性
for _p in ${PID_LIST//,/ }; do
  [[ "$_p" =~ ^[1-9][0-9]*$ ]] || { err "pid 必须为正整数: $_p"; exit 1; }
done
# root 检查 (CPU_TRACE_ALLOW_NON_ROOT=1 仅供本地 mock 测试)
if [[ "$EUID" -ne 0 && "${CPU_TRACE_ALLOW_NON_ROOT:-0}" != "1" ]]; then
  err "请使用 root 权限运行: sudo bash $0"; exit 1; fi

# ---------- 定位 tracefs ----------
TRACE_DIR="${CPU_TRACE_TRACEFS:-}"
if [[ -z "$TRACE_DIR" ]]; then
  for d in /sys/kernel/tracing /sys/kernel/debug/tracing; do
    [[ -d "$d" ]] && TRACE_DIR="$d" && break
  done
fi
if [[ -z "$TRACE_DIR" || ! -d "$TRACE_DIR" ]]; then
  err "未找到 tracefs/debugfs (需内核开启 CONFIG_FTRACE 并挂载 debugfs)"; exit 1; fi
ok "使用 tracing 目录: $TRACE_DIR"
if [[ ! -w "$TRACE_DIR" ]]; then
  err "tracing 目录不可写, 请确认 root 权限"; exit 1; fi

# ---------- CPU 数量与掩码 ----------
NCPU=$(nproc 2>/dev/null) || NCPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null) || NCPU=1
[[ "$NCPU" =~ ^[0-9]+$ && "$NCPU" -ge 1 ]] || NCPU=1
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

# ---------- 导出并发数: -j 优先, 否则按核数分级 (受控并发, 禁止默认全并发) ----------
compute_max_jobs() {
  if [[ -n "$MAX_EXPORT_JOBS" ]]; then return 0; fi
  local n="$NCPU"
  if   (( n <= 32  )); then MAX_EXPORT_JOBS=4
  elif (( n <= 64  )); then MAX_EXPORT_JOBS=8
  elif (( n <= 128 )); then MAX_EXPORT_JOBS=12
  else MAX_EXPORT_JOBS=16; fi
  (( MAX_EXPORT_JOBS > n )) && MAX_EXPORT_JOBS=$n
  (( MAX_EXPORT_JOBS < 1  )) && MAX_EXPORT_JOBS=1
}
compute_max_jobs

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
CLEANED=0
TMP_EXPORT_DIR="$OUTDIR/.tmp_export_$$"

cleanup() {
  (( CLEANED )) && return 0
  CLEANED=1
  info "恢复 tracing 原始配置..."
  [[ -n "$SAVED_ON" ]]     && echo "$SAVED_ON"     > "$TRACE_DIR/tracing_on"     2>/dev/null
  [[ -n "$SAVED_TRACER" ]] && echo "$SAVED_TRACER" > "$TRACE_DIR/current_tracer" 2>/dev/null
  [[ -n "$SAVED_BUF" ]]    && echo "$SAVED_BUF"    > "$TRACE_DIR/buffer_size_kb" 2>/dev/null
  [[ -n "$SAVED_MASK" ]]   && echo "$SAVED_MASK"   > "$TRACE_DIR/tracing_cpumask" 2>/dev/null
  # 关闭本次启用的事件后, 恢复全局 enable 原值
  for ev in $ENABLED_EVENTS; do echo 0 > "$TRACE_DIR/events/$ev/enable" 2>/dev/null; done
  [[ -n "$SAVED_EVENTS" ]] && echo "$SAVED_EVENTS" > "$TRACE_DIR/events/enable" 2>/dev/null
  # CPU_TRACE_SKIP_CLEAR=1: mock 测试钩子 (真实 tracefs 上 echo>trace 是 ioctl 清缓冲, 不影响生产)
  [[ "${CPU_TRACE_SKIP_CLEAR:-0}" == "1" ]] || echo > "$TRACE_DIR/trace" 2>/dev/null
  rm -rf "$TMP_EXPORT_DIR" 2>/dev/null
}
on_signal(){ err "检测到中断信号, 停止采集并恢复环境..."; cleanup; exit 1; }
trap cleanup EXIT
trap on_signal INT TERM
ENABLED_EVENTS=""

# ---------- buffer 决策 ----------
if [[ "$BUFFER_MODE" == "auto" ]]; then
  # 自动模式: 安全默认 16MB/CPU (per-CPU); 若 [总量] 超可用内存 25% 则收缩 (最低 1MB/CPU)
  BUFFER_SIZE_KB=16384
  AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)
  if [[ "$AVAIL_KB" =~ ^[0-9]+$ && "$AVAIL_KB" -gt 0 ]]; then
    TOTAL_KB=$(( BUFFER_SIZE_KB * NCPU ))
    MEM_CAP_KB=$(( AVAIL_KB * 25 / 100 ))
    if (( TOTAL_KB > MEM_CAP_KB )); then
      BUFFER_SIZE_KB=$(( MEM_CAP_KB / NCPU ))
      (( BUFFER_SIZE_KB < 1024 )) && BUFFER_SIZE_KB=1024
      warn "可用内存有限 ($(( AVAIL_KB / 1024 ))MB), buffer 自动降为 ${BUFFER_SIZE_KB}KB/核 (总量 $(( BUFFER_SIZE_KB * NCPU / 1024 ))MB, 上限 25%)"
    fi
  fi
  ok "buffer 自动模式: ${BUFFER_SIZE_KB}KB/核 x ${NCPU}核 = $(( BUFFER_SIZE_KB * NCPU / 1024 ))MB 总量"
else
  _total_mb=$(( BUFFER_SIZE_KB * NCPU / 1024 ))
  (( _total_mb >= 8 )) && warn "固定 buffer 总量约 ${_total_mb}MB (BUFFER x 核数), 请确认机器内存充足"
  ok "buffer 固定模式: ${BUFFER_SIZE_KB}KB/核"
fi

# ---------- 环境初始化 ----------
echo 0 > "$TRACE_DIR/tracing_on" 2>/dev/null
echo nop > "$TRACE_DIR/current_tracer" 2>/dev/null
if echo "$BUFFER_SIZE_KB" > "$TRACE_DIR/buffer_size_kb" 2>/dev/null; then
  ok "buffer_size_kb = $BUFFER_SIZE_KB"
else
  warn "设置 buffer 失败, 使用内核当前默认值"
fi
[[ "${CPU_TRACE_SKIP_CLEAR:-0}" == "1" ]] || echo > "$TRACE_DIR/trace" 2>/dev/null

# ---------- 事件启用 (存在性检查 + skip, 不因单事件失败整体失败) ----------
SKIPPED_EVENTS=""
for ev in $EVENT_LIST; do
  if [[ -f "$TRACE_DIR/events/$ev/enable" ]]; then
    if echo 1 > "$TRACE_DIR/events/$ev/enable" 2>/dev/null; then
      ENABLED_EVENTS="$ENABLED_EVENTS $ev"
    else
      SKIPPED_EVENTS="$SKIPPED_EVENTS $ev"
      warn "事件启用失败 (跳过): $ev"
    fi
  else
    SKIPPED_EVENTS="$SKIPPED_EVENTS $ev"
    warn "内核不支持事件: $ev (跳过)"
  fi
done
if [[ -z "$ENABLED_EVENTS" ]]; then
  err "没有任何可用事件, 无法采集 (Skipped:$SKIPPED_EVENTS)"
  cleanup; exit 1
fi
ok "已启用事件:$ENABLED_EVENTS"
[[ -n "$SKIPPED_EVENTS" ]] && warn "已跳过事件:$SKIPPED_EVENTS"

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
{ for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq; do
    [[ -r "$f" ]] && echo "$f: $(cat "$f" 2>/dev/null)"; done; } > "$SNAP/cpufreq.txt" 2>/dev/null
{ [[ -d /sys/devices/system/node ]] && for f in /sys/devices/system/node/node*/cpulist /sys/devices/system/node/node*/meminfo; do
    [[ -r "$f" ]] && echo "== $f =="; cat "$f" 2>/dev/null; done; } > "$SNAP/numa.txt" 2>/dev/null
{ echo "hostname: $(hostname 2>/dev/null)"; echo "date: $(date '+%Y-%m-%d %H:%M:%S')"; \
  echo "uname: $(uname -a 2>/dev/null)"; echo "nproc: $(nproc 2>/dev/null)"; \
  command -v lscpu   >/dev/null && lscpu; \
  command -v numactl >/dev/null && numactl -H; } > "$SNAP/system_info.txt" 2>/dev/null

# ==========================================================
# 导出函数库
# ==========================================================
shopt -s nullglob
PER_CPU_TRACES=("$TRACE_DIR"/per_cpu/cpu*/trace)
shopt -u nullglob
PERCPU_COUNT=${#PER_CPU_TRACES[@]}

# 受控并发节流: 运行中任务达到上限时等待 (wait -n 不可用时退化为轮询)
throttle() {
  while (( $(jobs -rp | wc -l) >= MAX_EXPORT_JOBS )); do
    if (( BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 403 )); then
      wait -n || true
    else
      sleep 0.1
    fi
  done
}

# 提取 cpu 文件路径中的 CPU 编号
cpu_id_of(){ local d; d=$(basename "$(dirname "$1")"); d=${d#cpu}; [[ "$d" =~ ^[0-9]+$ ]] && echo "$d" || echo "x"; }

# 方法: per-CPU 导出 (受控并发 / 可选全并发)
# $1=并发模式 controlled|unlimited, $2=part 输出目录
# 逐 CPU 显式记录: cat 返回码 + stderr + 字节数, 禁止 2>/dev/null 吞错
export_percpu() {
  local mode="$1" partdir="$2"
  mkdir -p "$partdir" 2>/dev/null || return 3
  : > "$partdir/.detail"
  local i=0
  for f in "${PER_CPU_TRACES[@]}"; do
    local cid pname
    cid=$(cpu_id_of "$f")
    printf -v pname 'cpu_%04d' "$i"   # 零填充保证稳定拼接顺序
    [[ "$mode" == "controlled" ]] && throttle
    (
      if cat "$f" > "$partdir/$pname.part" 2> "$partdir/$pname.err"; then
        echo "OK|$cid|$pname" > "$partdir/$pname.rc"
      else
        echo "FAIL|$cid|$pname" > "$partdir/$pname.rc"
      fi
    ) &
    i=$((i+1))
  done
  wait
  # 稳定顺序 (按启动序 = 文件名字典序) 收集结果
  PERCPU_DETAIL=(); PERCPU_OK=0; PERCPU_FAIL=0; PERCPU_FIRST_ERR=""
  local rcfile cid status pmsg
  for rcfile in "$partdir"/cpu_*.rc; do
    IFS='|' read -r status cid _ < "$rcfile"
    local base="${rcfile%.rc}"
    if [[ "$status" == "OK" ]]; then
      local b; b=$(wc -c < "$base.part" 2>/dev/null) || b=0
      PERCPU_DETAIL+=("CPU${cid} OK ${b} bytes")
      PERCPU_OK=$((PERCPU_OK+1))
    else
      pmsg=$(head -n 1 "$base.err" 2>/dev/null)
      [[ -z "$pmsg" ]] && pmsg="unknown error"
      PERCPU_DETAIL+=("CPU${cid} ERROR ${pmsg}")
      PERCPU_FAIL=$((PERCPU_FAIL+1))
      [[ -z "$PERCPU_FIRST_ERR" ]] && PERCPU_FIRST_ERR="CPU${cid}: ${pmsg}"
    fi
  done
  if (( PERCPU_FAIL == 0 && PERCPU_OK > 0 )); then
    # 拼接前验证: part 文件数量与 OK 数一致
    local npart; npart=$(ls "$partdir"/cpu_*.part 2>/dev/null | wc -l)
    if (( npart == PERCPU_OK )); then
      cat "$partdir"/cpu_*.part > "$SNAP/trace.txt" 2> "$partdir/.concat_err" \
        || return 4
      return 0
    fi
    return 5
  fi
  return 1
}

# 方法: global trace 单次读取 (主路径)
export_global() {
  local errf="$SNAP/.global_err"
  cat "$TRACE_DIR/trace" > "$SNAP/trace.txt" 2> "$errf"
  return $?
}

# 统一导出入口: 设置 EXPORT_VERDICT(ok|empty|failed) / EXPORT_MODE_USED / 字节行数
run_export() {
  EXPORT_VERDICT="failed"; EXPORT_MODE_USED="none"
  TRACE_BYTES=0; TRACE_LINES=0; EXPORT_ERRMSG=""
  local es ms

  # 可选 trace-cmd 快速路径 (实验性, 失败即回退内置路径)
  if [[ "$USE_TRACE_CMD" -eq 1 ]] && command -v trace-cmd >/dev/null 2>&1; then
    info "尝试 trace-cmd 快速导出 (实验性)..."
    local dat="$TMP_EXPORT_DIR/tc.dat"
    mkdir -p "$TMP_EXPORT_DIR" 2>/dev/null
    if trace-cmd extract -o "$dat" 2> "$SNAP/.tc_err" \
       && trace-cmd report "$dat" > "$SNAP/trace.txt" 2>> "$SNAP/.tc_err"; then
      TRACE_BYTES=$(wc -c < "$SNAP/trace.txt" 2>/dev/null) || TRACE_BYTES=0
      if (( TRACE_BYTES > 0 )); then
        EXPORT_MODE_USED="trace-cmd"; EXPORT_VERDICT="ok"
        TRACE_LINES=$(wc -l < "$SNAP/trace.txt" 2>/dev/null) || TRACE_LINES=0
        return 0
      fi
    fi
    warn "trace-cmd 导出失败或为空, 回退内置 tracefs 路径: $(head -n 1 "$SNAP/.tc_err" 2>/dev/null)"
  fi

  if [[ "$EXPORT_MODE" == "percpu" ]]; then
    # 强制 per-CPU 模式
    if (( PERCPU_COUNT == 0 )); then
      EXPORT_ERRMSG="per_cpu 目录不存在且已指定 --export-mode percpu"; return 0
    fi
    es=$(now_ms)
    export_percpu controlled "$TMP_EXPORT_DIR/percpu" || true
    ms=$(( $(now_ms) - es ))
    finalize_after_percpu "$ms"; return 0
  fi

  # auto/global: Global trace 主路径
  es=$(now_ms)
  export_global; local grc=$?
  ms=$(( $(now_ms) - es ))
  TRACE_BYTES=$(wc -c < "$SNAP/trace.txt" 2>/dev/null) || TRACE_BYTES=0
  if (( grc == 0 )); then
    if (( TRACE_BYTES > 0 )); then
      EXPORT_MODE_USED="global"; EXPORT_VERDICT="ok"
      TRACE_LINES=$(wc -l < "$SNAP/trace.txt" 2>/dev/null) || TRACE_LINES=0
      EXPORT_DURATION_MS=$ms
      return 0
    fi
    # global 读到 0 字节: 不能直接下结论, 用实际 per-CPU 读取交叉验证
    if (( PERCPU_COUNT > 0 )); then
      info "global trace 实际读取为 0 字节, 回退 per-CPU 缓冲区交叉验证..."
      local es2 ms2
      es2=$(now_ms)
      export_percpu controlled "$TMP_EXPORT_DIR/percpu" || true
      ms2=$(( $(now_ms) - es2 ))
      EXPORT_DURATION_MS=$((ms + ms2))
      finalize_after_percpu "$EXPORT_DURATION_MS"; return 0
    fi
    EXPORT_MODE_USED="global"; EXPORT_VERDICT="empty"
    return 0
  fi
  # global 读取失败: 记录错误并回退 per-CPU
  EXPORT_ERRMSG=$(head -n 1 "$SNAP/.global_err" 2>/dev/null)
  [[ -z "$EXPORT_ERRMSG" ]] && EXPORT_ERRMSG="cat exit=$grc"
  warn "global trace 读取失败: $EXPORT_ERRMSG"
  if (( PERCPU_COUNT > 0 )); then
    info "回退 per-CPU 缓冲区导出 (受控并发 ${MAX_EXPORT_JOBS})..."
    local es3 ms3
    es3=$(now_ms)
    export_percpu controlled "$TMP_EXPORT_DIR/percpu" || true
    ms3=$(( $(now_ms) - es3 ))
    EXPORT_DURATION_MS=$((ms + ms3))
    finalize_after_percpu "$EXPORT_DURATION_MS"; return 0
  fi
  EXPORT_VERDICT="failed"
  return 0
}

# per-CPU 导出后的统一裁决
finalize_after_percpu() {
  local ms="$1"
  if (( PERCPU_FAIL == PERCPU_COUNT && PERCPU_COUNT > 0 )); then
    EXPORT_VERDICT="failed"
    EXPORT_ERRMSG="全部 ${PERCPU_COUNT} 个 CPU 读取失败${PERCPU_FIRST_ERR:+ (首个: $PERCPU_FIRST_ERR)}"
    return 0
  fi
  TRACE_BYTES=$(wc -c < "$SNAP/trace.txt" 2>/dev/null) || TRACE_BYTES=0
  TRACE_LINES=$(wc -l < "$SNAP/trace.txt" 2>/dev/null) || TRACE_LINES=0
  EXPORT_MODE_USED="percpu"
  if (( TRACE_BYTES > 0 )); then
    EXPORT_VERDICT="ok"
  else
    EXPORT_VERDICT="empty"
  fi
  EXPORT_DURATION_MS="$ms"
}

# 输出导出明细报告 (文件名避开 trace 前缀, 防止被分析器误当 trace 数据)
write_export_detail() {
  local detail_file="$SNAP/percpu_export_detail.txt"
  {
    echo "export_mode_used: $EXPORT_MODE_USED"
    echo "export_jobs: $MAX_EXPORT_JOBS"
    echo "percpu_total: $PERCPU_COUNT  ok: ${PERCPU_OK:-0}  fail: ${PERCPU_FAIL:-0}"
    echo "---- per-CPU 明细 ----"
    for line in "${PERCPU_DETAIL[@]}"; do echo "$line"; done
  } > "$detail_file" 2>/dev/null
}

# 基准测试: A(global cat) / B(per-CPU 全并发) / C(per-CPU 受控并发)
run_benchmark() {
  local BDIR="$OUTDIR/cpu_benchmark"
  rm -rf "$BDIR" 2>/dev/null; mkdir -p "$BDIR" || { err "无法创建基准目录"; return 0; }
  local res=()
  local es ms bytes lines t_concat

  # ---- 方法 A: global 单次读取 ----
  es=$(now_ms)
  cat "$TRACE_DIR/trace" > "$BDIR/A_trace.txt" 2> "$BDIR/A.err"
  local rcA=$?
  ms=$(( $(now_ms) - es ))
  bytes=$(wc -c < "$BDIR/A_trace.txt" 2>/dev/null) || bytes=0
  lines=$(wc -l < "$BDIR/A_trace.txt" 2>/dev/null) || lines=0
  res+=("A|global cat|${rcA}|1|1|${bytes}|${lines}|${ms}|0")

  if (( PERCPU_COUNT > 0 )); then
    # ---- 方法 B: per-CPU 全并发 (fork 全部) ----
    rm -f "$BDIR"/B_*.part 2>/dev/null
    es=$(now_ms); local i=0
    for f in "${PER_CPU_TRACES[@]}"; do
      ( cat "$f" > "$BDIR/B_part_$i.part" 2> "$BDIR/B_err_$i" ) &
      i=$((i+1))
    done
    wait
    ms=$(( $(now_ms) - es ))
    local t0; t0=$(now_ms)
    cat "$BDIR"/B_part_*.part > "$BDIR/B_trace.txt" 2>/dev/null
    t_concat=$(( $(now_ms) - t0 ))
    bytes=$(wc -c < "$BDIR/B_trace.txt" 2>/dev/null) || bytes=0
    lines=$(wc -l < "$BDIR/B_trace.txt" 2>/dev/null) || lines=0
    res+=("B|per-CPU 全并发 (${PERCPU_COUNT} 进程)|0|${PERCPU_COUNT}|${PERCPU_COUNT}|${bytes}|${lines}|${ms}|${t_concat}")

    # ---- 方法 C: per-CPU 受控并发 ----
    rm -f "$BDIR"/C_*.part 2>/dev/null
    es=$(now_ms); i=0
    for f in "${PER_CPU_TRACES[@]}"; do
      throttle
      ( cat "$f" > "$BDIR/C_part_$i.part" 2> "$BDIR/C_err_$i" ) &
      i=$((i+1))
    done
    wait
    ms=$(( $(now_ms) - es ))
    t0=$(now_ms)
    cat "$BDIR"/C_part_*.part > "$BDIR/C_trace.txt" 2>/dev/null
    t_concat=$(( $(now_ms) - t0 ))
    bytes=$(wc -c < "$BDIR/C_trace.txt" 2>/dev/null) || bytes=0
    lines=$(wc -l < "$BDIR/C_trace.txt" 2>/dev/null) || lines=0
    res+=("C|per-CPU 受控并发 (${MAX_EXPORT_JOBS} 进程)|0|${MAX_EXPORT_JOBS}|${PERCPU_COUNT}|${bytes}|${lines}|${ms}|${t_concat}")
  else
    warn "per_cpu 目录不存在, 仅测方法 A"
  fi

  # ---- 汇总表 ----
  {
    echo "================ 导出基准测试结果 ================"
    printf "%-4s %-40s %6s %10s %12s %10s %10s\n" "方法" "说明" "rc" "峰值进程" "数据MB" "导出ms" "MB/s"
    local row IFS='|' name desc rc pproc tfiles b l d sp t_concat
    for row in "${res[@]}"; do
      IFS='|' read -r _ name rc pproc tfiles b l d t_concat <<< "$row"
      sp=$(calc_speed "$b" "$d")
      printf "%-4s %-40s %6s %10s %12s %10s %10s\n" \
        "${row%%|*}" "$name" "$rc" "$pproc" "$(to_mib "$b")" "$d" "$sp"
    done
    echo "--------------------------------------------------"
    echo "结论建议: 取 MB/s 最高者为该机型默认方案;"
    echo "  - 方法 A 只有一次顺序读, 无进程 fork/调度开销; 内核侧 trace 读取逐条"
    echo "    format 成文本且单线程进行, 256 路并发并不能并行化 format, 反而引入"
    echo "    fork/调度/锁竞争与更多次 read 系统调用, 因此 Global 单次读取常反而更快。"
    echo "  - 以本机实测为准: 若 C 明显快于 A, 可用 --export-mode percpu 固化该方案。"
  } | tee "$BDIR/benchmark_export.txt"
  info "基准明细目录: $BDIR (含 A/B/C 导出文件)"
}

# ---------- 执行采集 ----------
START_TS=$(date +%s); START_TIME=$(date "+%Y-%m-%d %H:%M:%S")
echo "$START_TIME" > "$SNAP/trace_start_time.txt"
info "开始采集 trace... ($START_TIME, 时长 ${DURATION}s, 模式: $([[ $MINIMAL -eq 1 ]] && echo 精简 || echo 全量)$([[ -n "$PID_LIST" ]] && echo ", HostBound快照: $PID_LIST"))"
COLLECT_START=$(now_ms)
if ! echo 1 > "$TRACE_DIR/tracing_on" 2>/dev/null; then
  err "开启 tracing 失败"; cleanup; exit 1
fi
sleep "$DURATION"
echo 0 > "$TRACE_DIR/tracing_on" 2>/dev/null
COLLECT_MS=$(( $(now_ms) - COLLECT_START ))
END_TIME=$(date "+%Y-%m-%d %H:%M:%S"); echo "$END_TIME" > "$SNAP/trace_end_time.txt"
ok "采集结束 ($END_TIME)"

# ---------- 导出 ----------
if [[ "$BENCHMARK" -eq 1 ]]; then
  run_benchmark
  cleanup
  info "基准测试完成 (不产出常规采集包)"
  exit 0
fi

info "导出 trace 数据 (主路径: global trace 单次读取; fallback: per-CPU 受控并发 ${MAX_EXPORT_JOBS})..."
PERCPU_OK=0; PERCPU_FAIL=0; PERCPU_DETAIL=()
run_export
write_export_detail

# ---------- 导出结果三分支裁决 (P0: 基于实际读取, 不看文件 size) ----------
case "$EXPORT_VERDICT" in
  ok)
    ok "trace export status: SUCCESS (mode=$EXPORT_MODE_USED)"
    ok "  bytes: $TRACE_BYTES ($(to_mib "$TRACE_BYTES") MiB)  lines: $TRACE_LINES"
    ok "  export duration: ${EXPORT_DURATION_MS:-0} ms  speed: $(calc_speed "$TRACE_BYTES" "${EXPORT_DURATION_MS:-0}") MB/s"
    ;;
  empty)
    warn "trace contains no event: 实际读取 0 字节 / 0 行 (不是文件 size 假象, 是缓冲区确实无数据)"
    warn "  排查建议: 1) 检查上方 Skipped 事件列表  2) 确认采集期间目标负载在运行"
    warn "           3) 确认 tracing_cpumask 覆盖目标 CPU  4) 适当延长 --duration 或增大 buffer"
    ;;
  *)
    err "trace read failed: 无法从 tracefs 读取数据"
    err "  错误详情: ${EXPORT_ERRMSG:-unknown} (明细见 cpu_trace/percpu_export_detail.txt)"
    err "  排查建议: 1) 确认 root 与 tracefs 挂载  2) 检查 dmesg 是否有 tracing 报错"
    err "           3) 可尝试 --export-mode percpu 或 -j 4 降低读取压力"
    ;;
esac

# ---------- buffer 溢出统计 (纯整数, 兼容分析器; 不依赖 bc) ----------
TRACE_OVERRUN=0
for f in "$TRACE_DIR"/per_cpu/cpu*/stats/overrun; do
  v=$(cat "$f" 2>/dev/null)
  [[ "$v" =~ ^[0-9]+$ ]] && TRACE_OVERRUN=$((TRACE_OVERRUN + v))
done
echo "$TRACE_OVERRUN" > "$SNAP/trace_overrun.txt"
(( TRACE_OVERRUN > 0 )) && warn "检测到 buffer 溢出 overrun=${TRACE_OVERRUN}, 建议增大 buffer: -b auto 或更高值 (如 -b 32768)"

# ---------- stats 目录辅助快照 (仅辅助诊断, 不作为数据有无的判断依据) ---
if [[ -d "$TRACE_DIR/per_cpu/cpu0/stats" ]]; then
  mkdir -p "$SNAP/trace_stats_aux" 2>/dev/null
  for f in "$TRACE_DIR"/per_cpu/cpu0/stats/*; do
    [[ -r "$f" ]] && cp "$f" "$SNAP/trace_stats_aux/$(basename "$f").txt" 2>/dev/null
  done
fi

cleanup

# ---------- HostBound 只读快照 (仅 --pid 时执行) ----------
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
  cat > "$HB/collection_manifest.json" <<EOF
{"collector":"cpu_trace_collect.sh","mode":"unified","duration_sec":$DURATION,"pid":"${PID_LIST}","minimal":$MINIMAL,"ftrace":"cpu_trace_collect.sh","external_tools":{"gil_tracer":"not_run","function_monitor":"not_run","msprof":"not_run","host_analyzer":"not_run"}}
EOF
  ok "HostBound 快照完成 (hostbound/)"
}
if [[ -n "$PID_LIST" ]]; then
  snapshot_hostbound
fi

# ---------- 元数据 (key:value 契约不变) 与性能统计 ----------
TRACE_CMD_AVAIL="no"; command -v trace-cmd >/dev/null 2>&1 && TRACE_CMD_AVAIL="yes"
cat > "$SNAP/metadata.txt" <<EOF
collect_tool: cpu_trace_collect.sh
collect_tool_version: 2
start_time: $START_TIME
end_time: $END_TIME
duration_sec: $DURATION
minimal_mode: $MINIMAL
cpu_list_input: ${CPU_LIST:-all}
cpu_count: $NCPU
hostbound_pid: ${PID_LIST:-none}
enabled_events:$ENABLED_EVENTS
skipped_events:${SKIPPED_EVENTS:- none}
kernel: $(uname -r 2>/dev/null)
hostname: $(hostname 2>/dev/null)
trace_dir: $TRACE_DIR
buffer_mode: $BUFFER_MODE
buffer_size_kb: $BUFFER_SIZE_KB
buffer_total_mb: $(( BUFFER_SIZE_KB * NCPU / 1024 ))
export_mode_requested: $EXPORT_MODE
export_mode_used: $EXPORT_MODE_USED
export_jobs: $MAX_EXPORT_JOBS
trace_bytes: $TRACE_BYTES
trace_lines: $TRACE_LINES
trace_overrun: $TRACE_OVERRUN
collect_duration_ms: $COLLECT_MS
export_duration_ms: ${EXPORT_DURATION_MS:-0}
export_speed_MBps: $(calc_speed "$TRACE_BYTES" "${EXPORT_DURATION_MS:-0}")
trace_cmd_available: $TRACE_CMD_AVAIL
EOF

COMPRESS_MS=0; ARCHIVE_BYTES=0
PKG=$(realpath "$OUTDIR" 2>/dev/null || echo "$OUTDIR")/cpu_trace_$(date +%Y%m%d_%H%M%S).tar.gz
_t0=$(now_ms)
if tar -czf "$PKG" -C "$OUTDIR" cpu_trace 2>/dev/null; then
  COMPRESS_MS=$(( $(now_ms) - _t0 ))
  ARCHIVE_BYTES=$(wc -c < "$PKG" 2>/dev/null) || ARCHIVE_BYTES=0
  ok "完成! 采集包: $PKG"
  ok "请将此文件回传用于分析 (可直接用本 Skill 分析, 或拖入 https://ui.perfetto.dev 查看)"
else
  COMPRESS_MS=$(( $(now_ms) - _t0 ))
  warn "打包失败, 原始数据保留在: $SNAP/"
  ok "请将整个 cpu_trace 目录回传用于分析"
fi

# ---------- 性能统计汇总 ----------
echo "==================== 性能统计 ===================="
printf "collect_time      : %s ms\n" "$COLLECT_MS"
printf "export_time       : %s ms  (%s MB/s)\n" "${EXPORT_DURATION_MS:-0}" "$(calc_speed "$TRACE_BYTES" "${EXPORT_DURATION_MS:-0}")"
printf "trace_bytes       : %s (%s MiB), lines: %s\n" "$TRACE_BYTES" "$(to_mib "$TRACE_BYTES")" "$TRACE_LINES"
printf "compress_time     : %s ms\n" "$COMPRESS_MS"
printf "archive_size      : %s (%s MiB), %s MB/s\n" "$ARCHIVE_BYTES" "$(to_mib "$ARCHIVE_BYTES")" "$(calc_speed "$ARCHIVE_BYTES" "$COMPRESS_MS")"
echo "==================================================="

exit 0
