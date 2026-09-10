#!/usr/bin/env bash
# 统一 HostBound 采集入口：保留并调用既有 ftrace 采集器；只增加只读快照。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION=30
OUTDIR="."
CPU_LIST=""
MINIMAL=0
PID=""

usage() {
  cat <<'EOF'
用法: sudo bash hostbound_collect.sh [--duration 秒] [--output 目录] [--cpu-list 列表] [--pid PID] [--minimal]

默认采集完整 ftrace，并追加 Ascend/NUMA/CPU affinity/IRQ/进程线程快照。
--pid 仅用于额外采集 /proc/<pid> 状态与线程 affinity；不会启用 GIL 或插桩。
GIL、Function Monitor、msprof 由独立官方工具按工具选择计划另行采集。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--duration) DURATION="$2"; shift 2 ;;
    -o|--output) OUTDIR="$2"; shift 2 ;;
    -c|--cpu-list) CPU_LIST="$2"; shift 2 ;;
    -p|--pid) PID="$2"; shift 2 ;;
    -m|--minimal) MINIMAL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done
[[ "$DURATION" =~ ^[0-9]+$ ]] || { echo "duration 必须为整数" >&2; exit 2; }
[[ -z "$PID" || "$PID" =~ ^[1-9][0-9]*$ ]] || { echo "pid 必须为正整数" >&2; exit 2; }

ARGS=(--duration "$DURATION" --output "$OUTDIR")
[[ -n "$CPU_LIST" ]] && ARGS+=(--cpu-list "$CPU_LIST")
[[ "$MINIMAL" -eq 1 ]] && ARGS+=(--minimal)
bash "$SCRIPT_DIR/cpu_trace_collect.sh" "${ARGS[@]}"

LATEST="$(find "$OUTDIR" -maxdepth 1 -type d -name cpu_trace -print -quit)"
[[ -n "$LATEST" ]] || { echo "未找到 ftrace 原始目录，跳过扩展快照" >&2; exit 0; }
SNAP="$LATEST/hostbound"
mkdir -p "$SNAP"

capture() { "$@" > "$SNAP/$1.txt" 2>&1 || true; }
capture nproc
capture lscpu
capture numactl -H
capture taskset -pc 1
cp /proc/interrupts "$SNAP/interrupts_at_end.txt" 2>/dev/null || true
cp /proc/softirqs "$SNAP/softirqs_at_end.txt" 2>/dev/null || true
{ cat /proc/cmdline; echo; } > "$SNAP/kernel_cmdline.txt" 2>/dev/null || true

if command -v npu-smi >/dev/null 2>&1; then
  npu-smi info > "$SNAP/npu_smi_info.txt" 2>&1 || true
  npu-smi info -m > "$SNAP/npu_smi_map.txt" 2>&1 || true
  npu-smi info -t topo > "$SNAP/npu_smi_topology.txt" 2>&1 || true
fi
if [[ -n "$PID" && -d "/proc/$PID" ]]; then
  cp "/proc/$PID/status" "$SNAP/pid_${PID}_status.txt" 2>/dev/null || true
  taskset -pc "$PID" > "$SNAP/pid_${PID}_affinity.txt" 2>&1 || true
  ps -T -p "$PID" -o pid,tid,psr,pcpu,stat,comm > "$SNAP/pid_${PID}_threads.txt" 2>&1 || true
  while read -r tid; do taskset -pc "$tid"; done < <(ls "/proc/$PID/task") > "$SNAP/pid_${PID}_thread_affinity.txt" 2>&1 || true
fi

cat > "$SNAP/collection_manifest.json" <<EOF
{"collector":"hostbound_collect.sh","duration_sec":$DURATION,"pid":"${PID}","minimal":$MINIMAL,"ftrace":"cpu_trace_collect.sh","external_tools":{"gil_tracer":"not_run","function_monitor":"not_run","msprof":"not_run","host_analyzer":"not_run"}}
EOF
ARCHIVE="$(find "$OUTDIR" -maxdepth 1 -type f -name 'cpu_trace_*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
if [[ -n "$ARCHIVE" ]]; then
  tar -czf "$ARCHIVE" -C "$(dirname "$LATEST")" "$(basename "$LATEST")"
  echo "统一采集包已更新: $ARCHIVE"
fi
echo "扩展 HostBound 快照已写入: $SNAP"
