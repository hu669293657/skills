#!/usr/bin/env bash
# Offline, low-overhead Linux CPU Host ftrace collector. Requires root.
set -u; set -o pipefail
DURATION=30 BUFFER=8192 OUTPUT=. TRACE= WORK= RESTORE= ON=0
usage(){ echo 'Usage: bash cpu_trace_collect.sh [--duration SECONDS] [--output DIR] [--buffer-size KB]'; }
die(){ echo "ERROR: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do case "$1" in --duration) DURATION=${2:-};shift 2;;--output) OUTPUT=${2:-};shift 2;;--buffer-size) BUFFER=${2:-};shift 2;;-h|--help)usage;exit 0;;*)die "unknown option: $1";;esac;done
[[ "$DURATION" =~ ^[1-9][0-9]*$ && "$BUFFER" =~ ^[1-9][0-9]*$ ]] || die 'duration and buffer-size must be positive integers'
[ "${EUID:-$(id -u)}" -eq 0 ] || die 'root privileges are required (sudo)'
for d in /sys/kernel/tracing /sys/kernel/debug/tracing;do [ -d "$d" ]&&{ TRACE=$d;break;};done
[ -n "$TRACE" ]&&[ -w "$TRACE/tracing_on" ] || die 'tracefs/debugfs unavailable or not writable'
STAMP=$(date +%Y%m%d_%H%M%S); WORK="$OUTPUT/cpu_trace_$STAMP"; RESTORE="$WORK/.restore";mkdir -p "$RESTORE"||die 'cannot create output'
restore(){ [ "$ON" = 1 ]&&echo 0 >"$TRACE/tracing_on" 2>/dev/null||:;for x in tracing_on current_tracer buffer_size_kb;do [ -s "$RESTORE/$x" ]&&cat "$RESTORE/$x">"$TRACE/$x" 2>/dev/null||:;done;while read -r e;do [ -n "$e" ]&&[ -w "$TRACE/events/$e/enable" ]&&echo 1>"$TRACE/events/$e/enable" 2>/dev/null||:;done<"$RESTORE/events";rm -rf "$RESTORE"; }
trap 'rc=$?;restore;exit $rc' EXIT INT TERM
for x in tracing_on current_tracer buffer_size_kb;do cat "$TRACE/$x">"$RESTORE/$x" 2>/dev/null||:;done;awk '$1!~/^#/{print $1}' "$TRACE/set_event">"$RESTORE/events" 2>/dev/null||:
[ "$(cat "$TRACE/tracing_on" 2>/dev/null)" = 0 ] || die 'an existing ftrace session is active; refusing to disturb it'
[ ! -s "$RESTORE/events" ] || die 'existing ftrace events are enabled; refusing to overwrite customer tracing configuration'
snap(){ [ -r "$1" ]&&cat "$1">"$WORK/$2" 2>/dev/null||:; };snap /proc/cpuinfo cpuinfo.txt;snap /proc/stat proc_stat.txt;snap /proc/loadavg loadavg.txt;snap /proc/meminfo meminfo.txt;snap /proc/interrupts interrupts.txt;snap /proc/softirqs softirqs.txt;snap /proc/schedstat schedstat.txt
{ uname -a;date -Is;hostname;command -v lscpu&&lscpu;command -v numactl&&numactl -H;}>"$WORK/system_info.txt" 2>&1
find /sys/devices/system/cpu -path '*/cpufreq/scaling_cur_freq' -o -path '*/cpufreq/scaling_governor' -o -path '/sys/devices/system/cpu/online' -o -path '/sys/devices/system/cpu/present' 2>/dev/null|while read -r f;do echo "$f=$(cat "$f" 2>/dev/null)";done>"$WORK/cpufreq.txt";find /sys/devices/system/node -maxdepth 2 -type f \( -name cpulist -o -name meminfo \) -exec sh -c 'echo "$1=$(cat "$1" 2>/dev/null)"' _ {} \; 2>/dev/null>"$WORK/numa.txt"||:
echo 0>"$TRACE/tracing_on";echo 0>"$TRACE/events/enable" 2>/dev/null||:;echo nop>"$TRACE/current_tracer";echo "$BUFFER">"$TRACE/buffer_size_kb";echo >"$TRACE/trace"
EVENTS='sched/sched_switch sched/sched_wakeup sched/sched_waking sched/sched_wakeup_new sched/sched_migrate_task irq/irq_handler_entry irq/irq_handler_exit irq/softirq_entry irq/softirq_exit power/cpu_idle power/cpu_frequency power/cpu_frequency_switch_start power/cpu_frequency_switch_end';ENABLED=;for e in $EVENTS;do [ -w "$TRACE/events/$e/enable" ]&&{ echo 1>"$TRACE/events/$e/enable";ENABLED="$ENABLED $e";};done
printf 'duration_requested_seconds=%s\ntracefs=%s\nevents_enabled=%s\nstart_time=%s\n' "$DURATION" "$TRACE" "$ENABLED" "$(date -Is)">"$WORK/metadata.txt";echo "Collecting $DURATION seconds...";echo 1>"$TRACE/tracing_on";ON=1;sleep "$DURATION";echo 0>"$TRACE/tracing_on";ON=0;cat "$TRACE/trace">"$WORK/trace.txt"||die 'cannot export trace';echo "end_time=$(date -Is)">>"$WORK/metadata.txt";ARCHIVE="$OUTPUT/cpu_trace_$STAMP.tar.gz";tar -C "$OUTPUT" -czf "$ARCHIVE" "$(basename "$WORK")"||die 'cannot create archive';echo "Done: $ARCHIVE"
