#!/usr/bin/env bash
set -euo pipefail
# 默认只读预检/预览；执行与回滚委托给带 journal 的闭环入口。
usage(){
  echo 'Preview: bash bind_cpu.sh'
  echo 'Apply: bash bind_cpu.sh --apply /path/cpu-host-performance /tmp/unique-state.json disabled|absent'
  echo 'Rollback: bash bind_cpu.sh --rollback /path/cpu-host-performance /tmp/unique-state.json'
}
case "${1:-}" in
  --help|-h) usage; exit 0;;
  --rollback)
    [[ $# == 3 ]] || { usage; exit 2; }
    exec python3 "$2/scripts/ascend_bind.py" rollback --state "$3" --execute;;
  --apply)
    [[ $# == 4 ]] || { usage; exit 2; }
    [[ "$4" == disabled || "$4" == absent ]] || { usage; exit 2; }
    skill=$2; state=$3
    here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    [[ -f "$here/report_data.json" ]] || { echo 'Keep report_data.json next to this script' >&2; exit 2; }
    python3 "$skill/scripts/ascend_bind.py" plan --report-data "$here/report_data.json" \
      --main-pid 4001,4002,4003,4004,4005,4006,4007 --framework-binding "$4" --output "$state.plan.json"
    exec python3 "$skill/scripts/ascend_bind.py" apply --plan "$state.plan.json" --state "$state" --execute;;
  '') ;;
  *) usage; exit 2;;
esac
fail(){ printf '%s\n' "$*" >&2; exit 1; }
starttime(){ local s; s=$(cat "/proc/$1/stat") || return 1; s=${s##*) }; set -- $s; printf '%s' "${20}"; }
check_process(){ [[ "$(starttime "$1")" == "$2" ]] || fail "PID $1 已退出或被复用，请重新采集"; }
effective_cpus(){
  awk -v pid="$1" '
  function readfile(p, s,x,r){s="";found=0;while((r=(getline x < p))>0){s=s (found?"\n":"") x;found=1}close(p);ok=r>=0;return s}
  function decode(s){gsub(/\\040/," ",s);gsub(/\\011/,"\t",s);gsub(/\\134/,"\\",s);return s}
  BEGIN{
    n=split(readfile("/proc/" pid "/cgroup"),cg,"\n");mi=readfile("/proc/self/mountinfo");nm=split(mi,mounts,"\n")
    for(i=1;i<=n;i++){
      split(cg[i],a,":");v=(a[1]=="0"?2:1);if(v==1 && ("," a[2] ",")!~/,cpuset,/)continue
      rel=substr(cg[i],length(a[1])+length(a[2])+3);best=-1;root="";path=""
      for(j=1;j<=nm;j++){
        split(mounts[j],f," ");k=7;while(k<=length(f)&&f[k]!="-")k++
        if(v==2&&f[k+1]!="cgroup2")continue
        if(v==1&&(f[k+1]!="cgroup"||("," f[k+3] ",")!~/,cpuset,/))continue
        r=decode(f[4]);if(r!="/"&&rel!=r&&index(rel,r "/")!=1)continue
        if(length(r)>best){best=length(r);root=decode(f[5]);tail=(r=="/"?rel:substr(rel,length(r)+1));path=root (tail=="/"?"":tail)}
      }
      if(root=="")continue
      while(1){
        value=readfile(path "/cpuset.cpus.effective");if(ok&&value!=""){print value;exit}
        value=readfile(path "/cpuset.effective_cpus");if(ok&&value!=""){print value;exit}
        if(v==1){value=readfile(path "/cpuset.cpus");if(!ok)exit;if(value!=""){print value;exit}}
        else{value=readfile(path "/cgroup.controllers");if(!ok||(" " value " ")~/ cpuset /)exit}
        if(path==root)break;sub(/\/[^/]+$/,"",path)
      }
    }
  }'
}
subset(){
  awk -v want="$1" -v have="$2" 'function expand(s,a, t,n,i,r,c){gsub(/,/," ",s);n=split(s,t," ");for(i=1;i<=n;i++){split(t[i],r,"-");for(c=r[1]+0;c<=(r[2]==""?r[1]:r[2]);c++)a[c]=1}} BEGIN{expand(want,w);expand(have,h);n=0;for(c in w){n++;if(!(c in h))exit 1}exit(n?0:1)}'
}
check_thread(){
  local pid=$1 tid=$2 target=$3 group ceiling current
  group=$(awk '/^Tgid:/ {print $2}' "/proc/$pid/task/$tid/status") || fail "TID $tid 已退出"
  [[ "$group" == "$pid" ]] || fail "TID $tid 不再属于 PID $pid"
  subset "$target" "$(cat /sys/devices/system/cpu/online)" || fail "目标 CPU 已离线"
  ceiling=$(effective_cpus "$tid")
  current=$(awk '/^Cpus_allowed_list:/ {print $2}' "/proc/$pid/task/$tid/status")
  [[ -n "$ceiling" ]] || ceiling=$current
  subset "$target" "$ceiling" || fail "TID $tid 当前 cpuset/亲和范围不允许目标 CPU"
  if subset "$current" "$target" && ! subset "$target" "$current"; then
    fail "TID $tid 当前窄掩码需保留，请重新分析"
  fi
}
[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || fail "请在宿主机执行"
if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then fail "请在宿主机执行"; fi
[[ "$(cat /proc/sys/kernel/random/boot_id)" == 'SYNTHETIC-NOT-A-REAL-BOOT-ID' ]] || fail '宿主机或启动周期已变化'
check_process 4001 400100
for task in /proc/4001/task/[0-9]*; do
  check_thread 4001 "${task##*/}" 32-63
done
check_process 4002 400200
for task in /proc/4002/task/[0-9]*; do
  check_thread 4002 "${task##*/}" 64-95
done
check_process 4003 400300
for task in /proc/4003/task/[0-9]*; do
  check_thread 4003 "${task##*/}" 96-127
done
check_process 4004 400400
for task in /proc/4004/task/[0-9]*; do
  check_thread 4004 "${task##*/}" 128-159
done
check_process 4005 400500
for task in /proc/4005/task/[0-9]*; do
  check_thread 4005 "${task##*/}" 160-191
done
check_process 4006 400600
for task in /proc/4006/task/[0-9]*; do
  check_thread 4006 "${task##*/}" 192-223
done
check_process 4007 400700
for task in /proc/4007/task/[0-9]*; do
  check_thread 4007 "${task##*/}" 224-255
done
echo "PREVIEW: no affinity changes; use --apply for journaled execution."
cat <<'HOSTBIND_PLAN'
taskset -apc 32-63 4001
taskset -apc 64-95 4002
taskset -apc 96-127 4003
taskset -apc 128-159 4004
taskset -apc 160-191 4005
taskset -apc 192-223 4006
taskset -apc 224-255 4007
HOSTBIND_PLAN
