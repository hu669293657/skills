#!/usr/bin/env bash
set -euo pipefail
# 仅在本次采集的宿主机执行；所有预检成功后才应用命令。
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
  local pid=$1 tid=$2 birth=$3 target=$4 group ceiling
  group=$(awk '/^Tgid:/ {print $2}' "/proc/$pid/task/$tid/status") || fail "TID $tid 已退出"
  [[ "$group" == "$pid" ]] || fail "TID $tid 不再属于 PID $pid"
  [[ -z "$birth" || "$(starttime "$tid")" == "$birth" ]] || fail "TID $tid 已被复用"
  subset "$target" "$(cat /sys/devices/system/cpu/online)" || fail "目标 CPU 已离线"
  ceiling=$(effective_cpus "$tid")
  [[ -n "$ceiling" ]] || ceiling=$(awk '/^Cpus_allowed_list:/ {print $2}' "/proc/$pid/task/$tid/status")
  subset "$target" "$ceiling" || fail "TID $tid 当前 cpuset/亲和范围不允许目标 CPU"
}
[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || fail "请在宿主机执行"
if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then fail "请在宿主机执行"; fi
[[ "$(cat /proc/sys/kernel/random/boot_id)" == 'SYNTHETIC-NOT-A-REAL-BOOT-ID' ]] || fail '宿主机或启动周期已变化'
check_process 3000 300000
check_thread 3000 3000 300000 2-3
check_process 3100 310000
check_thread 3100 3100 310000 4-5
check_process 4000 400000
check_thread 4000 4000 400000 0-31
check_thread 4000 5000 500000 0-15
check_process 4001 400100
check_thread 4001 4001 400100 32-63
check_thread 4001 5001 500100 32-63
check_process 4002 400200
check_thread 4002 4002 400200 64-95
check_thread 4002 5002 500200 64-95
check_process 4003 400300
check_thread 4003 4003 400300 96-127
check_thread 4003 5003 500300 96-127
check_process 4004 400400
check_thread 4004 4004 400400 128-159
check_thread 4004 5004 500400 128-159
check_process 4005 400500
check_thread 4005 4005 400500 160-191
check_thread 4005 5005 500500 160-191
check_process 4006 400600
check_thread 4006 4006 400600 192-223
check_thread 4006 5006 500600 192-223
check_process 4007 400700
check_thread 4007 4007 400700 224-255
check_thread 4007 5007 500700 224-255
check_process 6000 600000
check_thread 6000 6000 600000 6-7
# 具体绑核命令
taskset -pc 2-3 3000
taskset -pc 4-5 3100
taskset -pc 0-31 4000
taskset -pc 0-15 5000
taskset -pc 32-63 4001
taskset -pc 32-63 5001
taskset -pc 64-95 4002
taskset -pc 64-95 5002
taskset -pc 96-127 4003
taskset -pc 96-127 5003
taskset -pc 128-159 4004
taskset -pc 128-159 5004
taskset -pc 160-191 4005
taskset -pc 160-191 5005
taskset -pc 192-223 4006
taskset -pc 192-223 5006
taskset -pc 224-255 4007
taskset -pc 224-255 5007
taskset -pc 6-7 6000
