#!/bin/bash
# ==========================================================
# CPU Host 性能采集脚本 (cpu_trace_collect.sh) — 统一采集入口
# - Bash 4+ + awk + ftrace + /proc + /sys；单文件，无 Python/jq 依赖
# - 仅写独立 trace instance，退出时清理，不修改全局 tracing
# - 默认 3 秒全量采集；--no-hostbound 显式降级，--minimal 仅精简 trace 事件
# - --pid <PID[,PID...]>: 追加 HostBound 只读快照 (NUMA/NPU 拓扑、IRQ、目标进程
#   及线程 affinity 等), 写入采集包 hostbound/ 目录; 不改变 ftrace 事件与恢复行为
# - 全程进度展示: 阶段导航 + 动态进度条 (百分比/已用时/预计剩余/速率/事件数),
#   管道或重定向输出时自动降级为纯文本日志 (无 ANSI 颜色与 \r 刷新, 支持 NO_COLOR)
# - 产物: cpu_trace_YYYYMMDD_HHMMSS_XXXXXX.tar.gz (trace + 系统快照 + hostbound/ 证据清单)
# ==========================================================
set +e  # 单项能力缺失不致命，由脚本自行控制

# ==========================================================
# 进度展示组件 (自包含; 非终端输出时自动降级为纯文本日志)
# ==========================================================
IS_TTY=0; [[ -t 1 ]] && IS_TTY=1   # 进度条/颜色仅输出到终端, 管道下保持日志干净
if [[ $IS_TTY -eq 1 && -z "$NO_COLOR" ]]; then
  C_RST=$'\033[0m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'
  C_C=$'\033[36m'; C_BL=$'\033[34m'
else
  C_RST=""; C_DIM=""; C_B=""; C_R=""; C_G=""; C_Y=""; C_C=""; C_BL=""
fi
info(){  echo -e "$C_C[Info]$C_RST $*"; }
ok(){    echo -e "$C_G[OK]  $C_RST$*"; }
warn(){  echo -e "$C_Y[Warn]$C_RST $*"; }
err(){   echo -e "$C_B$C_R[Err] $C_RST$*"; }

# ---------- 数值/时间格式化 ----------
fmt_bytes(){ awk -v b="${1:-0}" 'BEGIN{split("B KB MB GB TB PB",u," ");i=1;while(b>=1024&&i<6){b/=1024;i++}printf "%.1f%s",b,u[i]}'; }
fmt_num(){   awk -v n="${1:-0}" 'BEGIN{if(n>=1e9)printf "%.2fG",n/1e9;else if(n>=1e6)printf "%.2fM",n/1e6;else if(n>=1e3)printf "%.1fK",n/1e3;else printf "%d",n}'; }
fmt_secs(){  local s=${1:-0}
  if   (( s>=3600 )); then printf "%dh%02dm%02ds" $((s/3600)) $((s%3600/60)) $((s%60))
  elif (( s>=60   )); then printf "%dm%02ds"      $((s/60))     $((s%60))
  else printf "%ds" "$s"; fi; }

# ---------- 刷新节奏 (不支持小数 sleep 的环境自动退化为 1s) ----------
SLEEP_FRAC=1; sleep 0.1 2>/dev/null || SLEEP_FRAC=0
SPIN_STEP=0.1;  (( SLEEP_FRAC )) || SPIN_STEP=1
POLL_STEP=0.3;  (( SLEEP_FRAC )) || POLL_STEP=1

# ---------- 阶段导航 ----------
PHASE_CUR=0; PHASE_TOTAL=4; PHASE_T0=0
phase_begin(){
  PHASE_CUR=$((PHASE_CUR+1)); PHASE_T0=$SECONDS; LAST_BUCKET=-1
  echo -e "\n${C_BL}${C_B}[$PHASE_CUR/$PHASE_TOTAL]${C_RST} ${C_C}▶${C_RST} ${C_B}$1${C_RST}"
}
phase_end(){
  echo -e "  ${C_G}✓${C_RST} $* ${C_DIM}· 用时 $(fmt_secs $((SECONDS-PHASE_T0)))${C_RST}"
}

# ---------- 平滑进度条 (█▉▊▋▌▍▎▏ 八级字符块) ----------
BAR_W=24; LAST_BUCKET=-1
bar(){
  local pct=$1; (( pct<0 )) && pct=0; (( pct>100 )) && pct=100
  local units=$(( pct*BAR_W*8/100 ))   # 注意: 同一 local 语句中后项引用不到前项, 须分行
  local full=$(( units/8 )) rem=$(( units%8 ))
  local filled="" blank="" i
  local seg=( "" ▏ ▎ ▍ ▌ ▋ ▊ ▉ )
  for ((i=0;i<full;i++)); do filled+="█"; done
  local rest=$(( BAR_W - full - (rem>0 ? 1 : 0) ))
  for ((i=0;i<rest;i++)); do blank+="░"; done
  local col=$C_C; (( pct>=100 )) && col=$C_G
  printf "%s%s%s%s%s" "$col$C_B" "$filled${seg[rem]}" "$C_RST$C_DIM" "$blank" "$C_RST"
}
progress_update(){   # 用法: progress_update <标签> <0-100> <附加信息>
  local label=$1 pct=$2 extra=$3
  if [[ $IS_TTY -eq 1 ]]; then
    printf "\r  %s %s %s%3d%%%s %s\033[K" "$C_B$label$C_RST" "$(bar "$pct")" "$C_B" "$pct" "$C_RST" "$extra"
  else
    local bucket=$(( pct/10 ))   # 非 TTY: 每跨过 10% 打一行纯文本日志
    (( bucket>LAST_BUCKET )) && { LAST_BUCKET=$bucket; info "$label 进度 ${pct}% $extra"; }
  fi
}
progress_clear(){ [[ $IS_TTY -eq 1 ]] && printf "\r\033[K"; return 0; }

# ---------- 旋转指示器 (用于无法计算百分比的步骤, 如 tar 打包) ----------
SPIN_PID=""
spin_start(){   # 用法: spin_start "提示"
  [[ $IS_TTY -eq 1 ]] || return 0
  local msg=$1 frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏) i=0 t0=$SECONDS
  ( while :; do
      printf "\r  %s%s%s %s %s已用时 %s%s\033[K" \
        "$C_C$C_B" "${frames[i]}" "$C_RST" "$msg" "$C_DIM" "$(fmt_secs $((SECONDS-t0)))" "$C_RST"
      i=$(( (i+1)%${#frames[@]} )); sleep "$SPIN_STEP"
    done ) & SPIN_PID=$!
}
spin_stop(){
  [[ -n "$SPIN_PID" ]] || return 0
  kill "$SPIN_PID" 2>/dev/null; wait "$SPIN_PID" 2>/dev/null
  SPIN_PID=""; progress_clear
}

# ---------- ftrace 实时统计 (采集进度条展示用; 读取零开销, 不污染 trace) ----------
trace_stats(){   # 汇总各 CPU buffer 统计, 输出: "已驻留事件 丢弃事件 溢出次数"
  # 先显式展开文件列表: mawk 在文件缺失时不执行 END 块, 不能依赖其回退
  local f files=()
  for f in "$TRACE_DIR"/per_cpu/cpu*/stats; do [[ -f "$f" ]] && files+=("$f"); done
  if ((${#files[@]})); then
    awk -F': *' '
      $1 ~ /^(entries|unread)/          {e+=$2}
      $1 ~ /^dropped/                   {d+=$2}
      $1 ~ /^overrun/ && $1 !~ /commit/ {o+=$2}
      END { printf "%d %d %d", e+0, d+0, o+0 }' "${files[@]}" 2>/dev/null
  else
    echo "0 0 0"
  fi
}

# ---------- 顶部横幅 ----------
print_banner(){
  local mode="全量"; [[ $MINIMAL -eq 1 ]] && mode="精简"
  local cpus="全部 ($(nproc 2>/dev/null || echo '?') 核)"; [[ -n "$CPU_LIST" ]] && cpus="$CPU_LIST"
  local hb="无"; (( HOSTBOUND )) && hb="自动发现全部 NPU PID + $PID_LIST"
  local evs; evs=$(echo $ENABLED_EVENTS | wc -w); evs=${evs//[!0-9]/}
  echo -e "$C_C"
  echo  "┌──────────────────────────────────────────────────────────────"
  echo -e "│ ${C_B}CPU Host 性能采集${C_RST}${C_C}  cpu_trace_collect.sh"
  echo  "│  模式: $mode   CPU: $cpus   时长: ${DURATION}s   Buffer: ${BUFFER_SIZE}KB/核"
  echo  "│  事件: ${evs:-?} 个   HostBound: $hb   输出: $(realpath "$OUTDIR" 2>/dev/null || echo "$OUTDIR")"
  echo  "└──────────────────────────────────────────────────────────────"
  echo -e "$C_RST"
}

usage() {
cat <<EOF
CPU Host 性能采集工具 (统一入口: 默认全量 ftrace + HostBound + 进程发现)
用法: sudo bash $0 [选项]
  -t, --duration <秒>     采集时长, 默认 3 (default: 3 s)
  -o, --output <目录>     输出目录, 默认当前目录
  -b, --buffer-size <KB>  单核 buffer 大小, 默认 16384
  -c, --cpu-list <列表>   CPU 列表, 如 1,2,3 或 0-47 (默认全量)
  -m, --minimal           精简模式: 仅 sched_switch；sysfs 频率仍低频轮询
  -p, --pid <PID[,PID]>   追加目标进程的 NUMA、affinity、启动变量白名单、已加载库与进程限额
      --hostbound        自动发现全部 NPU 占卡宿主机 PID 并采集亲和性快照
      --no-hostbound     显式关闭 NPU/目标进程/全进程发现；仍采集系统能力与频率
      --ascend-dump-script <路径>
                         可选，调用 msprechecker_dump.py 导出两种 Skill 共用的原生 JSON
      --discover         只读 /proc 全进程/全线程前后快照与生命周期事件
      --frequency-interval <秒>  sysfs 轮询，默认1，最小0.2
      --full             显式全量模式（默认）
  -h, --help              显示帮助
示例:
  sudo bash $0 --duration 30
  sudo bash $0 -t 10 -m
  sudo bash $0 -t 60 -c 0-47 -b 281600
  sudo bash $0 --duration 30 --pid 12345
  sudo bash $0 --duration 30 --pid 12345,6789
  sudo bash $0 --duration 30 --hostbound
EOF
}

# BEGIN standalone evidence
# All normalized sidecars are produced by Bash + POSIX awk; no interpreter helper.
HB_PROC=/proc; HB_SYS=/sys
hb_quote(){ LC_ALL=C awk 'BEGIN{for(i=1;i<32;i++)esc[sprintf("%c",i)]=sprintf("\\u%04x",i)} {if(NR>1)s=s "\n";s=s $0} END{out="\"";for(i=1;i<=length(s);i++){c=substr(s,i,1);out=out (c=="\\"?"\\\\":c=="\""?"\\\"":c in esc?esc[c]:c)}print out "\""}'; }
hb_now(){ date -u +%FT%TZ; }
hb_snapshot(){
  local kind=$1 output=$2 p t paths=()
  case "$kind" in
    process) for p in "$HB_PROC"/[0-9]*; do
      [[ -d "$p" ]] || continue; paths+=("P" "$p")
      for t in "$p"/task/[0-9]*; do [[ -d "$t" ]] && paths+=("T" "$t"); done
    done;;
    frequency)
      for p in "$HB_SYS"/devices/system/cpu/cpufreq/policy[0-9]*; do [[ -d "$p" ]] && paths+=(F "$p"); done
      if ((${#paths[@]}==0)); then
        for p in "$HB_SYS"/devices/system/cpu/cpu[0-9]*/cpufreq; do [[ -d "$p" ]] && paths+=(F "$p"); done
      fi;;
    idle) for p in "$HB_SYS"/devices/system/cpu/cpu[0-9]*/cpuidle/state*; do [[ -d "$p" ]] && paths+=(I "$p"); done;;
    topology) for p in "$HB_SYS"/devices/system/node/node[0-9]*; do [[ -d "$p" ]] && paths+=(N "$p"); done;;
  esac
  # One awk process per snapshot, not one process per thread or sysfs field.
  printf '%s\n' "${paths[@]}" | LC_ALL=C awk -v kind="$kind" -v pr="$HB_PROC" -v sy="$HB_SYS" -v ticks="${HB_TICKS:-100}" -v countfile="$SNAP/hostbound/.snapshot_count" '
  function q(s, i,c,o){o="\"";for(i=1;i<=length(s);i++){c=substr(s,i,1);o=o (c=="\\"?"\\\\":c=="\""?"\\\"":c in esc?esc[c]:c)}return o "\""}
  function readfile(p, x,s,r){s="";found=0;while((r=(getline x < p))>0){s=s (found?"\n":"") x;found=1}close(p);exists=(r>=0);return s}
  function stamp( a){split(readfile(pr "/uptime"),a," ");return a[1]+0}
  function basename(p,a,n){n=split(p,a,"/");return a[n]}
  function cpus(s, a,b,n,i,j,o,sep){gsub(/,/," ",s);n=split(s,a,/[[:space:]]+/);o="[";sep="";for(i=1;i<=n;i++){if(a[i]=="")continue;split(a[i],b,"-");if(b[1]!~/^[0-9]+$/ || (b[2]!="" && (b[2]!~/^[0-9]+$/ || b[2]<b[1] || b[2]-b[1]>100000)))return "null";for(j=b[1]+0;j<=(b[2]==""?b[1]:b[2]);j++){o=o sep sprintf("%.0f",j);sep=","}}return o "]"}
  function stat(p, a,s,n){s=readfile(p "/stat");if(!exists)return 0;sub(/^.*\) /,"",s);n=split(s,a," ");if(n<37 || a[20]!~/^[0-9]+$/)return 0;state=a[1];ppid=a[2]+0;flags=a[7]+0;ut=a[12]+0;st=a[13]+0;start=a[20]+0;cpu=a[37]+0;return 1}
  function status(s,k, a,n,i,v){n=split(s,a,"\n");for(i=1;i<=n;i++)if(index(a[i],k ":")==1){v=substr(a[i],length(k)+2);sub(/^[[:space:]]+/,"",v);return v}return ""}
  function allowed(s,k, v){v=status(s,k);return v=="" && index(s,k ":")==0?"null":cpus(v)}
  function unescape(s){gsub(/\\040/," ",s);gsub(/\\011/,"\t",s);gsub(/\\012/,"\n",s);gsub(/\\134/,"\\",s);return s}
  function mountgroup(rel,controllers,version, lines,n,i,a,j,fs,opts,root,point,best,tail,parts,nparts,k,matchall){
    n=split(mountinfo,lines,"\n");best=-1;cgroot="";cgpath=""
    for(i=1;i<=n;i++){
      split(lines[i],a," ");j=7;while(j<=length(a) && a[j]!="-")j++
      fs=a[j+1];opts="," a[j+3] ","
      if(version==2 && fs!="cgroup2")continue
      if(version==1){
        if(fs!="cgroup")continue
        nparts=split(controllers,parts,",");matchall=1
        for(k=1;k<=nparts;k++)if(index(opts,"," parts[k] ",")==0)matchall=0
        if(!matchall)continue
      }
      root=unescape(a[4]);point=unescape(a[5]);if(root!="/" && rel!=root && index(rel,root "/")!=1)continue
      if(length(root)>best){best=length(root);cgroot=point;tail=(root=="/"?rel:substr(rel,length(root)+1));cgpath=point (tail=="/"?"":tail)}
    }
    return cgroot!=""
  }
  function effective(path,version,which, root,v,controllers){root=cgroot;while(1){
    v=readfile(path "/cpuset." which ".effective");if(exists)return cpus(v)
    v=readfile(path "/cpuset.effective_" which);if(exists)return cpus(v)
    if(version==2){
      # A readable controllers file without cpuset proves this level inherits.
      # Missing/unreadable interfaces with cpuset enabled remain unknown.
      controllers=readfile(path "/cgroup.controllers");if(!exists || (" " controllers " ")~/ cpuset /)return "null"
    }else{v=readfile(path "/cpuset." which);if(!exists)return "null";if(v!="")return cpus(v)}
    if(path==root)break;sub(/\/[^/]+$/,"",path)
  }return "null"}
  function cgroups(cg, lines,n,i,a,v,controllers,rel,path,keys,nk,j,value,row,rows,sep,ver,candidates,k){
    if(cg in cgcache){cgrows=cgcache[cg];effcpu=effcache[cg];effmem=memcache[cg];return}
    rows="[";sep="";effcpu="null";effmem="null";n=split(cg,lines,"\n")
    nk=split("cpu.max cpu.stat cpu.cfs_quota_us cpu.cfs_period_us cpuset.cpus.effective cpuset.mems.effective cpuset.effective_cpus cpuset.effective_mems cpuset.cpus cpuset.mems memory.current memory.max memory.high memory.events memory.swap.current memory.swap.max memory.usage_in_bytes memory.limit_in_bytes memory.failcnt pids.current pids.max pids.events",keys," ")
    for(i=1;i<=n;i++){
      split(lines[i],a,":");controllers=a[2];rel=substr(lines[i],length(a[1])+length(a[2])+3)
      if(rel~/\/\.\.(\/|$)/ || rel~/\/\.(\/|$)/)continue
      if(a[1]=="0")ver=2
      else if(("," controllers ",")~/,(cpu|cpuset|memory|pids),/)ver=1
      else continue
      if(!mountgroup(rel,controllers,ver))continue
      path=cgpath
      if(ver==2 || ("," controllers ",")~/,cpuset,/){effcpu=effective(path,ver,"cpus");effmem=effective(path,ver,"mems")}
      while(1){row="{\"path\":" q(path) ",\"version\":" ver ",\"controllers\":" q(controllers==""?"unified":controllers)
        for(j=1;j<=nk;j++){value=readfile(path "/" keys[j]);if(exists)row=row "," q(keys[j]) ":" q(value)}
        rows=rows sep row "}";sep=",";if(path==cgroot)break;sub(/\/[^/]+$/,"",path);if(length(path)<length(cgroot))break
      }
    }
    cgrows=rows "]";cgcache[cg]=cgrows;effcache[cg]=effcpu;memcache[cg]=effmem
  }
  function mapjson(a, k,o,sep){o="{";sep="";for(k in a){o=o sep q(k) ":" sprintf("%.0f",a[k]);sep=","}return o "}"}
  function numa(p, text,lines,n,i,a,j,b,size,np,k,pol){
    text=readfile(p "/numa_maps");if(!exists){numamissing++;return "{\"available\":false,\"node_pages\":{},\"node_kb\":{},\"unknown_page_size_pages\":{},\"mapping_count\":0}"}
    for(k in pages)delete pages[k];for(k in kb)delete kb[k];for(k in unknown)delete unknown[k];for(k in policies)delete policies[k]
    n=split(text,lines,"\n");np=0;for(i=1;i<=n;i++){if(split(lines[i],a," ")<2)continue;np++;policies[a[2]]++;size=0;for(j=3;j<=length(a);j++)if(a[j]~/^kernelpagesize_kB=/){split(a[j],b,"=");size=b[2]+0}
      for(j=3;j<=length(a);j++)if(a[j]~/^N[0-9]+=[0-9]+$/){split(a[j],b,"=");k=substr(b[1],2);pages[k]+=b[2];if(size)kb[k]+=b[2]*size;else unknown[k]+=b[2]}
    }
    return "{\"available\":true,\"node_pages\":" mapjson(pages) ",\"node_kb\":" mapjson(kb) ",\"unknown_page_size_pages\":" mapjson(unknown) ",\"mapping_count\":" np ",\"policies\":" mapjson(policies) "}"
  }
  function error(id,reason){errcount++;errs=errs (errs==""?"":",") "{\"id\":" q(id) ",\"reason\":" q(reason) "}"}
  function rankenv(p, savedrs,entry,pos,key,value,o,sep){
    savedrs=RS;RS=sprintf("%c",0);o="{";sep=""
    while((getline entry < (p "/environ"))>0){
      pos=index(entry,"=");if(!pos)continue;key=substr(entry,1,pos-1);value=substr(entry,pos+1)
      if(key~/^(RANK|LOCAL_RANK|WORLD_SIZE|LOCAL_WORLD_SIZE|GROUP_RANK|ASCEND_RT_VISIBLE_DEVICES|ASCEND_VISIBLE_DEVICES|CUDA_VISIBLE_DEVICES)$/ && length(value)<=256){o=o sep q(key) ":" q(value);sep=","}
    }
    close(p "/environ");RS=savedrs;return o "}"
  }
  BEGIN{CONVFMT="%.17g";OFMT="%.17g";mountinfo=readfile(pr "/self/mountinfo");for(i=1;i<32;i++)esc[sprintf("%c",i)]=sprintf("\\u%04x",i);begin=stamp();pc=0;tc=0;count=0}
  NR%2==1{type=$0;next}
  {
    path=$0;if(path=="")next
    if(type=="P" || type=="T"){
      id=basename(path)+0;identity_begin=stamp();if(!stat(path)){error(id,"stat_unreadable_or_exited");next}
      saved=start;s=readfile(path "/status");if(!exists){error(id,"status_unreadable_or_exited");next}
      tgid=status(s,"Tgid")+0;if(tgid<=0 || (type=="P" && tgid!=id)){error(id,"invalid_tgid");next}
      cg=readfile(path "/cgroup");source=exists?"task":"leader_fallback";if(type=="T" && !exists)cg=readfile(pr "/" tgid "/cgroup");cgroups(cg)
      row="{\"tgid\":" tgid ",\"starttime\":" sprintf("%.0f",saved) ",\"state\":" q(state) ",\"ppid\":" ppid ",\"flags\":" sprintf("%.0f",flags) ",\"last_cpu\":" cpu ",\"utime_ticks\":" ut ",\"stime_ticks\":" st ",\"cpu_time_sec\":" ((ut+st)/ticks)
      row=row ",\"name\":" q(status(s,"Name")) ",\"namespace_pids\":" cpus(status(s,"NSpid")) ",\"allowed_cpus\":" allowed(s,"Cpus_allowed_list") ",\"cpus_allowed_list\":" q(status(s,"Cpus_allowed_list")) ",\"mems_allowed_list\":" q(status(s,"Mems_allowed_list"))
      row=row ",\"cgroup\":" q(cg) ",\"cgroup_cpu\":" cgrows ",\"cpuset_cpus_effective\":" (type=="T" && source!="task"?"null":effcpu) ",\"cpuset_mems_effective\":" (type=="T" && source!="task"?"null":effmem) ",\"identity_started_at\":" identity_begin
      if(type=="P"){cmd=readfile(path "/cmdline");gsub(sprintf("%c",0)," ",cmd);row=row ",\"pid\":" id ",\"cmdline\":" q(cmd) ",\"rank_env\":" rankenv(path) ",\"threads\":" (status(s,"Threads")+0) ",\"kernel_thread\":" (int(flags/2097152)%2?"true":"false") ",\"numa_maps\":" numa(path)}
      else row=row ",\"tid\":" id ",\"cgroup_source\":" q(source)
      if(!stat(path) || start!=saved){error(id,"identity_changed_during_snapshot");next}
      row=row ",\"identity_verified_at\":" stamp() "}"
      if(type=="P"){pjson[id]=row;pstamp[id]=saved;porder[++pc]=id}else{tjson[++tc]=row;tp[tc]=tgid}
    }else if(type=="F"){
      rel=readfile(path "/related_cpus");if(rel=="")rel=readfile(path "/affected_cpus");id=basename(path)
      if(rel=="" && id=="cpufreq"){parent=path;sub(/\/cpufreq$/,"",parent);rel=substr(basename(parent),4)}
      group=cpus(rel);identity=(group=="[]"?path:group);if(identity in seen)next;seen[identity]=1
      if(id=="cpufreq")id="cpus_" rel
      row="{\"id\":" q(id) ",\"cpus\":" group ",\"path\":" q(path) ",\"unit\":\"kHz\"";errors="";n=split("related_cpus scaling_cur_freq cpuinfo_cur_freq cpuinfo_max_freq scaling_min_freq scaling_max_freq scaling_governor",keys," ")
      for(i=1;i<=n;i++){v=readfile(path "/" keys[i]);if(exists)row=row "," q(keys[i]) ":" (v~/^[0-9]+$/ && keys[i]!="related_cpus" && keys[i]!="scaling_governor"?v:q(v));else errors=errors (errors==""?"":",") q(keys[i]) ":\"missing_or_unreadable\""}
      items=items (count++?",":"") row ",\"errors\":{" errors "}}"
    }else if(type=="I"){
      parent=path;sub(/\/cpuidle\/[^/]+$/,"",parent);row="{\"cpu\":" (substr(basename(parent),4)+0) ",\"state\":" q(basename(path));errors="";n=split("name desc disable latency residency time usage",keys," ")
      for(i=1;i<=n;i++){v=readfile(path "/" keys[i]);if(exists)row=row "," q(keys[i]) ":" (v~/^[0-9]+$/?v:q(v));else errors=errors (errors==""?"":",") q(keys[i]) ":\"missing_or_unreadable\""}
      items=items (count++?",":"") row ",\"errors\":{" errors "}}"
    }else if(type=="N"){
      row="{\"node\":" (substr(basename(path),5)+0) ",\"cpus\":" cpus(readfile(path "/cpulist"));errors="";n=split("cpulist distance meminfo numastat vmstat",keys," ")
      for(i=1;i<=n;i++){v=readfile(path "/" keys[i]);row=row "," q(keys[i] "_raw") ":" (exists?q(v):"null");if(!exists)errors=errors (errors==""?"":",") q(keys[i]) ":\"missing_or_unreadable\""}
      v=readfile(path "/distance");row=row ",\"distance\":" cpus(v);v=readfile(path "/meminfo");n=split(v,lines,"\n");mem="";for(i=1;i<=n;i++){split(lines[i],a," ");if(a[4]~/^[0-9]+$/){sub(/:$/,"",a[3]);mem=mem (mem==""?"":",") q(a[3]) ":" a[4]}}
      items=items (count++?",":"") row ",\"memory_kb\":{" mem "},\"errors\":{" errors "}}"
    }
  }
  END{
    if(kind=="resources"){
      nf=split("stat loadavg meminfo vmstat diskstats pressure/cpu pressure/memory pressure/io sys/fs/file-nr net/dev",rf," ");raw="";count=0
      for(ri=1;ri<=nf;ri++){value=readfile(pr "/" rf[ri]);ok=exists;if(ok)count++;raw=raw (ri>1?",":"") q("/proc/" rf[ri]) ":{\"text\":" (ok?q(value):"null") ",\"status\":" q(ok?"COLLECTED":"UNAVAILABLE") "}"}
      print "{\"schema_version\":1,\"ts\":" begin ",\"end_ts\":" stamp() ",\"clock\":\"boottime\",\"files\":{" raw "}}"
    }else if(kind=="process"){
      ps="";ts="";count=0;for(i=1;i<=pc;i++){id=porder[i];if(!stat(pr "/" id) || start!=pstamp[id]){error(id,"leader_changed_during_snapshot");delete pjson[id];continue}ps=ps (count++?",":"") pjson[id]}
      nt=0;for(i=1;i<=tc;i++)if(tp[i] in pjson)ts=ts (nt++?",":"") tjson[i]
      print "{\"schema_version\":2,\"clock\":\"boottime\",\"clock_ticks\":" ticks ",\"ts\":" begin ",\"end_ts\":" stamp() ",\"processes\":[" ps "],\"threads\":[" ts "],\"errors\":[" errs "]}"
    }else if(kind=="topology")print "{\"schema_version\":1,\"ts\":" begin ",\"online_cpus\":" cpus(readfile(sy "/devices/system/cpu/online")) ",\"online_nodes\":" cpus(readfile(sy "/devices/system/node/online")) ",\"distance_node_order\":" cpus(readfile(sy "/devices/system/node/online")) ",\"nodes\":[" items "]}"
    else print "{\"schema_version\":2,\"ts\":" begin ",\"clock\":\"boottime\",\"" (kind=="frequency"?"policies":"states") "\":[" items "],\"capability\":" q(count?"available":"unsupported_or_unreadable") (kind=="idle"?",\"time_unit\":\"microseconds\",\"usage_unit\":\"entries\"":"") "}"
    print count,errcount+0,numamissing+0 > countfile;close(countfile)
  }' > "$output.tmp" && mv -f -- "$output.tmp" "$output"
}
hb_manifest(){
  local tmp="$SNAP/hostbound/collection_manifest.json.tmp"
  { printf '{"schema_version":1,"collector":"cpu_trace_collect.sh","duration_sec":%s,"minimal":%s,"hostbound":%s,"discover":%s,"entries":[' "$DURATION" "$MINIMAL" "$HOSTBOUND" "$DISCOVER"
    awk 'NR>1{printf ","}{printf "%s",$0}' "$SNAP/hostbound/collection_entries.jsonl"
    printf ']}\n'
  } > "$tmp" && mv -f -- "$tmp" "$SNAP/hostbound/collection_manifest.json"
  cp -- "$SNAP/hostbound/collection_manifest.json" "$SNAP/collection_manifest.json"
}
hb_entry(){
  local category=$1 status=$2 rel=$3 reason=${4:-} rc=${5:-null} parsed=${6:-null} count=${7:-null} requested=${8:-true} command=${9:-null} command_exists=${10:-null}
  local present=false empty=null size=null source_field=""
  if [[ -n "${11:-}" ]]; then source_field=",\"source\":$(printf '%s' "${11}" | hb_quote)"; fi
  if [[ -n "$rel" && -f "$SNAP/$rel" ]]; then
    present=true;empty=true;[[ -s "$SNAP/$rel" ]] && empty=false;size=$(wc -c < "$SNAP/$rel");size=${size//[[:space:]]/}
  fi
  printf '{"category":"%s","requested":%s,"status":"%s","command":%s,"command_exists":%s,"returncode":%s,"raw_path":%s,"raw_exists":%s,"empty":%s,"size_bytes":%s,"parse_success":%s,"parsed_count":%s,"started_at":"%s","ended_at":"%s","failure_reason":%s%s}\n' \
    "$category" "$requested" "$status" "$command" "$command_exists" "$rc" "$(if [[ -n "$rel" ]]; then printf '%s' "$rel" | hb_quote; else printf null; fi)" "$present" "$empty" "$size" "$parsed" "$count" "${HB_STARTED:-$(hb_now)}" "$(hb_now)" "$(if [[ -n "$reason" ]]; then printf '%s' "$reason" | hb_quote; else printf null; fi)" "$source_field" >> "$SNAP/hostbound/collection_entries.jsonl"
}
hb_file(){
  local category=$1 source=$2 rel=$3 status=COLLECTED reason="" rc=0
  if [[ "$rel" == */* ]]; then mkdir -p -- "${SNAP}/${rel%/*}"; fi
  if [[ ! -e "$source" ]]; then status=UNSUPPORTED;reason=interface_missing;rc=1
  elif ! cat -- "$source" > "$SNAP/$rel" 2> "$SNAP/hostbound/.read_error"; then
    status=EXECUTION_FAILED;reason=$(cat "$SNAP/hostbound/.read_error");rc=1
    [[ "$reason" == *[Pp]ermission* || "$reason" == *'not permitted'* ]] && status=PERMISSION_DENIED
  fi
  hb_entry "$category" "$status" "$rel" "$reason" "$rc" null null true null null "${4:-}"
}
hb_command(){
  local category=$1 rel=$2;shift 2
  local status=COLLECTED reason="" rc=0 argv="[" sep="" arg
  for arg in "$@"; do argv+="$sep$(printf '%s' "$arg" | hb_quote)";sep=,;done;argv+=']'
  HB_STARTED=$(hb_now)
  if ! command -v "$1" >/dev/null 2>&1; then hb_entry "$category" COMMAND_NOT_FOUND "$rel" "executable_not_found:$1" null null null true "$argv" false;unset HB_STARTED;return 0;fi
  if command -v timeout >/dev/null 2>&1; then timeout -k 2 15 "$@" > "$SNAP/$rel" 2>&1;else "$@" > "$SNAP/$rel" 2>&1;fi;rc=$?
  if ((rc));then
    reason=$(tail -c 2000 "$SNAP/$rel");status=EXECUTION_FAILED
    [[ "$reason" == *[Pp]ermission* || "$reason" == *'not permitted'* || "$reason" == *'Access denied'* ]] && status=PERMISSION_DENIED
    [[ "$reason" == *[Uu]nsupported* || "$reason" == *'not supported'* || "$reason" == *'invalid option'* || "$reason" == *'Invalid argument'* ]] && status=UNSUPPORTED
    ((rc==124 || rc==137)) && reason=command_timed_out
  fi
  hb_entry "$category" "$status" "$rel" "$reason" "$rc" null null true "$argv" true
  unset HB_STARTED
}
hb_data(){
  local kind=$1 category=$2 rel=$3 count errors missing reason="" status=COLLECTED parsed=true
  HB_STARTED=$(hb_now)
  if hb_snapshot "$kind" "$SNAP/$rel"; then
    read -r count errors missing < "$SNAP/hostbound/.snapshot_count"
    [[ "$count" == 0 ]] && { status=UNSUPPORTED;[[ "$kind" == process ]] && status=NO_ACTIVE_PROCESS; }
    if [[ "$kind" == process ]];then
      HB_PROCESS_COUNT=$count;HB_NUMA_MISSING=$missing
      if ((errors>0));then reason="partial_process_snapshot:$errors";((count==0)) && { status=EXECUTION_FAILED;parsed=false; };fi
    fi
    hb_entry "$category" "$status" "$rel" "$reason" 0 "$parsed" "$count"
  else hb_entry "$category" EXECUTION_FAILED "$rel" snapshot_failed 1 false;fi
  unset HB_STARTED
}
hb_environment_launch(){
  set -o pipefail
  head -c 1048577 -- "$1" | LC_ALL=C awk '
    BEGIN { RS=sprintf("%c",0) }
    {
      bytes+=length($0)+1
      if(bytes>1048576) exit 3
      pos=index($0,"=")
      if(!pos) next
      key=substr($0,1,pos-1)
      val=substr($0,pos+1)
      if(length(val)>256) next
      if(key~/^(RANK|LOCAL_RANK|WORLD_SIZE|LOCAL_WORLD_SIZE|GROUP_RANK|RANK_ID|RANK_SIZE|DEVICE_ID|MKL_NUM_THREADS|OPENBLAS_NUM_THREADS|HCCL_BUFFSIZE|HCCL_CONNECT_TIMEOUT|HCCL_EXEC_TIMEOUT|TASK_QUEUE_ENABLE|ASCEND_LAUNCH_BLOCKING)$/ && val~/^[0-9]+$/ ||
         key~/^(OMP_NUM_THREADS|ASCEND_RT_VISIBLE_DEVICES|ASCEND_VISIBLE_DEVICES|CUDA_VISIBLE_DEVICES)$/ && val~/^[0-9]+([,-][0-9]+)*$/ ||
         key=="GOMP_CPU_AFFINITY" && val~/^[0-9]+([ ,:-][0-9]+)*$/ ||
         key=="OMP_WAIT_POLICY" && tolower(val)~/^(active|passive)$/ ||
         key=="OMP_PROC_BIND" && tolower(val)~/^(true|false|primary|master|close|spread)(,(primary|master|close|spread))*$/)
        print key "=" val
    }'
}

hb_environment_libraries(){
  set -o pipefail
  head -c 4194305 -- "$1" | LC_ALL=C awk '
    {
      bytes+=length($0)+1
      if(bytes>4194304) exit 3
      if(!sub(/^[^ ]+ +[^ ]+ +[^ ]+ +[^ ]+ +[^ ]+ +/,"")) next
      if($0~/^\/.*\.so([.][^\/[:space:]]+)*( \(deleted\))?$/ && !seen[$0]++){
        output+=length($0)+1
        if(output>65536) exit 3
        print
      }
    }'
}

hb_environment_field(){
  local file=$1 value="" status=COLLECTED reason="" rc=0 stderr="$SNAP/hostbound/.environment_error"
  shift
  value=$("$@" "$file" 2>"$stderr") || rc=$?
  if ((rc)); then
    status=EXECUTION_FAILED; reason=read_failed
    if ((rc==3)); then reason=size_limit_exceeded
    elif LC_ALL=C grep -Eqi 'permission denied|operation not permitted' "$stderr"; then status=PERMISSION_DENIED
    elif [[ ! -e "$file" && ! -L "$file" ]]; then status=UNSUPPORTED; reason=interface_missing
    fi
    value=""
  fi
  printf '{"status":"%s","source":%s,"reason":%s,"text":%s}' "$status" \
    "$(printf '%s' "$file" | hb_quote)" "$(printf '%s' "$reason" | hb_quote)" \
    "$(if ((rc)); then printf null; else printf '%s' "$value" | hb_quote; fi)"
}

hb_environment_pid(){
  local pid=$1 dir="$HB_PROC/$1" rel="hostbound/environment/pid_$1.json" start end
  local stat_start='sub(/^.*\) /,"") && $20~/^[0-9]+$/ {print $20; exit}'
  local HB_STARTED
  HB_STARTED=$(hb_now)
  start=$(awk "$stat_start" "$dir/stat" 2>/dev/null)
  if [[ -z "$start" || "$(awk '$1=="Tgid:"{print $2;exit}' "$dir/status" 2>/dev/null)" != "$pid" ]]; then
    hb_entry environment_process EXECUTION_FAILED "$rel" target_identity_unavailable 1
    return
  fi
  if {
    printf '{"pid":%s,"starttime":"%s","environment":' "$pid" "$start"
    hb_environment_field "$dir/environ" hb_environment_launch
    printf ',"libraries":'; hb_environment_field "$dir/maps" hb_environment_libraries
    printf ',"limits":'; hb_environment_field "$dir/limits" cat --
    printf ',"exe":'; hb_environment_field "$dir/exe" readlink --
    printf '}\n'
  } > "$SNAP/$rel.tmp"; then
    end=$(awk "$stat_start" "$dir/stat" 2>/dev/null)
    if [[ "$start" == "$end" ]] && mv -f -- "$SNAP/$rel.tmp" "$SNAP/$rel"; then
      hb_entry environment_process COLLECTED "$rel" "" 0
      return
    fi
  fi
  rm -f -- "$SNAP/$rel.tmp"
  hb_entry environment_process EXECUTION_FAILED "$rel" target_identity_changed_or_write_failed 1
}

hb_environment(){
  local etc=$1 ascend=$2 root=hostbound/environment id path candidate pid os_release
  local -A seen=()
  mkdir -p -- "$SNAP/$root/host" "$SNAP/$root/ascend" || return 1
  os_release="$etc/os-release"
  if [[ ! -e "$os_release" && "$etc" == /etc && -f /usr/lib/os-release ]]; then os_release=/usr/lib/os-release; fi
  hb_file environment_host "$os_release" "$root/host/os_release.txt" "$os_release"
  hb_command environment_host "$root/host/kernel.txt" uname -srmo
  for path in "$HB_SYS/kernel/mm/transparent_hugepage/enabled" "$HB_PROC/sys/kernel/numa_balancing" \
      "$HB_PROC/sys/vm/swappiness" "$HB_PROC/sys/vm/overcommit_memory" \
      "$HB_SYS/devices/system/clocksource/clocksource0/current_clocksource"; do
    id=${path##*/}
    case "$id" in enabled) id=thp;; current_clocksource) id=clocksource;; esac
    hb_file environment_host "$path" "$root/host/$id.txt" "$path"
  done
  for id in driver toolkit runtime compiler hccl opp atb mindie; do
    case "$id" in
      driver) path="$ascend/driver/version.info";;
      atb) path="$ascend/nnal/atb/latest/version.info";;
      mindie) path="$ascend/mindie/latest/version.info";;
      *)
        path="$ascend/cann/$id/version.info"
        candidate="$ascend/ascend-toolkit/latest/$id/version.info"
        if [[ ! -r "$path" && -f "$candidate" ]]; then path="$candidate"; fi;;
    esac
    if [[ -f "$path" || ! -e "$path" ]]; then
      hb_file environment_ascend "$path" "$root/ascend/$id.txt" "$path"
    else
      hb_entry environment_ascend UNSUPPORTED "$root/ascend/$id.txt" version_source_not_regular 1 null null true null null "$path"
    fi
  done
  if ((HOSTBOUND)) && [[ -n "$PID_LIST" ]]; then
    for pid in ${PID_LIST//,/ }; do
      [[ -n "${seen[$pid]+x}" ]] && continue
      seen[$pid]=1
      hb_environment_pid "$pid"
    done
  else
    hb_entry environment_process NOT_REQUESTED "" explicit_host_pid_required null null null false
  fi
  rm -f -- "$SNAP/hostbound/.environment_error"
}

hb_ascend_dump(){
  local script=$1 rel=hostbound/environment/msprechecker_dumped.json
  local pid rc=0 export_tmp="" HB_STARTED
  local -a args=(--filter --no-probes)
  local -A seen=()
  HB_STARTED=$(hb_now)
  if [[ ! -f "$script" || ! -r "$script" ]]; then
    hb_entry environment_dump EXECUTION_FAILED "$rel" collector_script_missing_or_unreadable 1 null null true null null "$script"
    warn "无法读取 Ascend Dump 采集脚本: $script"
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    hb_entry environment_dump COMMAND_NOT_FOUND "$rel" python3_not_found null null null true '["python3"]' false "$script"
    warn "找不到 python3，本次仅采集 CPU Host 证据"
    return
  fi
  if ((HOSTBOUND)); then
    for pid in ${PID_LIST//,/ }; do
      [[ -n "${seen[$pid]+x}" ]] && continue
      seen[$pid]=1
      args+=(--target "pid-$pid=$pid")
    done
  fi
  if ! mkdir -p -- "$SNAP/hostbound/environment"; then
    hb_entry environment_dump EXECUTION_FAILED "$rel" dump_directory_failed 1
    return
  fi
  (umask 077; python3 -- "$script" "${args[@]}" --output-path "$SNAP/$rel.tmp") \
    > "$SNAP/hostbound/environment/ascend_dump_log.txt" 2>&1 || rc=$?
  if ((rc)) || [[ ! -s "$SNAP/$rel.tmp" ]] || ! mv -f -- "$SNAP/$rel.tmp" "$SNAP/$rel"; then
    rm -f -- "$SNAP/$rel.tmp"
    ((rc)) || rc=1
    hb_entry environment_dump EXECUTION_FAILED "$rel" native_dump_failed_check_ascend_dump_log "$rc" null null true null null "$script"
    warn "Ascend Dump 采集失败，见 $SNAP/hostbound/environment/ascend_dump_log.txt"
    return
  fi
  hb_entry environment_dump COLLECTED "$rel" "" 0 null null true null null "$script"
  local output="$OUTDIR/${CAPTURE_NAME}_msprechecker_dumped.json"
  if export_tmp=$(mktemp "$OUTDIR/.${CAPTURE_NAME}_dump_XXXXXX") &&
      cp -- "$SNAP/$rel" "$export_tmp" && mv -f -- "$export_tmp" "$output"; then
    ok "Ascend Dump JSON: $output"
  else
    [[ -n "$export_tmp" ]] && rm -f -- "$export_tmp"
    warn "无法写出独立 JSON，原生快照已保留: $SNAP/$rel"
  fi
}

hb_phase(){
  local phase=$1 name rel suffix="" tool p node context=unknown detector pid f
  [[ "$phase" == end ]] && suffix=_at_end
  for name in stat interrupts softirqs schedstat;do
    rel="$name.txt";[[ "$name" == stat ]] && rel=proc_stat.txt
    [[ "$phase" == end ]] && rel="hostbound/$name$suffix.txt"
    case "$name" in stat) p=proc_stat;;interrupts)p=irq;;softirqs)p=softirq;;*)p=schedstat;;esac
    hb_file "$p" "$HB_PROC/$name" "$rel"
  done
  if [[ "$phase" == start ]];then
    for name in cpuinfo loadavg meminfo vmstat cmdline;do rel="$name.txt";[[ "$name" == cmdline ]] && rel=hostbound/kernel_cmdline.txt;hb_file host_context "$HB_PROC/$name" "$rel";done
    hb_file cpu_topology "$HB_SYS/devices/system/cpu/online" hostbound/cpu_online.txt
    hb_data topology cpu_topology hostbound/numa_topology.json
    for p in "$HB_SYS"/devices/system/node/online "$HB_SYS"/devices/system/node/node[0-9]*/{cpulist,distance,meminfo}; do
      [[ -e "$p" ]] && hb_file cpu_topology "$p" "hostbound/sysfs/node/${p#"$HB_SYS/devices/system/node/"}"
    done
    hb_command cpu_topology hostbound/lscpu.txt lscpu
    hb_command cpu_topology hostbound/lscpu_extended.txt lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE
    hb_command cpu_topology hostbound/lscpu_parse.txt lscpu -p=CPU,CORE,SOCKET,NODE,ONLINE
    hb_command cpu_topology hostbound/numactl.txt numactl -H
    hb_command cpu_topology hostbound/numastat.txt numastat
    hb_command cpu_topology hostbound/numastat_memory.txt numastat -m
    hb_command host_context hostbound/container_detector.txt systemd-detect-virt --container
    detector=$(cat "$SNAP/hostbound/container_detector.txt" 2>/dev/null)
    [[ "$detector" == none ]] && context=host
    [[ -n "$detector" && "$detector" != none && "$detector" =~ ^[a-zA-Z0-9_-]+$ ]] && context=container
    [[ -e /.dockerenv || -e /run/.containerenv ]] && context=container
    { printf 'execution_context=%s\nboot_id=%s\ncollector_pid=%s\npid_namespace=%s\ninit_pid_namespace=%s\n' "$context" "$(cat "$HB_PROC/sys/kernel/random/boot_id" 2>/dev/null)" "$$" "$(readlink "$HB_PROC/self/ns/pid" 2>/dev/null)" "$(readlink "$HB_PROC/1/ns/pid" 2>/dev/null)"; } > "$SNAP/hostbound/host_context.txt"
    hb_entry host_context COLLECTED hostbound/host_context.txt "" 0 true 1
    hb_environment /etc /usr/local/Ascend
    for tool in gil_tracer function_monitor msprof host_analyzer;do hb_entry external_tools NOT_REQUESTED "" "external_tool:$tool" null null null false;done
  fi
  hb_data resources resources "hostbound/resource_$phase.json"
  if [[ "$phase" == start ]];then
    mkdir -p -- "$SNAP/hostbound/resources"
    hb_command resources hostbound/resources/df_blocks.txt df -Pk
    hb_command resources hostbound/resources/df_inodes.txt df -Pi
    hb_command resources hostbound/resources/lsblk.txt lsblk -b -P -o NAME,SIZE,TYPE,ROTA,MOUNTPOINT
    hb_file resources "$HB_PROC/self/mountinfo" hostbound/resources/mountinfo.txt
  fi
  if [[ "$phase" == end ]];then
    hb_command resources hostbound/resources/df_blocks_end.txt df -Pk
    hb_command resources hostbound/resources/df_inodes_end.txt df -Pi
  fi
  hb_data frequency frequency "hostbound/frequency_$phase.json"
  hb_data idle idle "hostbound/idle_$phase.json"
  if ((HOSTBOUND));then
    hb_data process processes "hostbound/process_$phase.json"
    hb_command processes "hostbound/ps_$phase.txt" ps -e -o pid,ppid,psr,stat,comm,args
    hb_command processes "hostbound/pstree_$phase.txt" pstree -p
    rel=hostbound/npu_smi_info.txt;[[ "$phase" == start ]] && rel=hostbound/npu_smi_info_start.txt
    hb_command npu_info "$rel" npu-smi info
    # Raw vendor tables are parsed offline; never equate unknown format with no tasks.
    [[ "$phase" == start && -f "$SNAP/$rel" ]] && cp -- "$SNAP/$rel" "$SNAP/hostbound/npu_smi_info.txt"
    if [[ "$phase" == start ]];then
      hb_command npu_mapping hostbound/npu_smi_map.txt npu-smi info -m
      hb_command npu_topology hostbound/npu_smi_topology.txt npu-smi info -t topo
    fi
    # All processes (including NPU workers and descendants) have NUMA page summaries
    # in process_*.json. Explicit roots additionally retain complete raw maps.
    p=COLLECTED
    ((HB_PROCESS_COUNT==0 || HB_NUMA_MISSING>=HB_PROCESS_COUNT)) && p=EXECUTION_FAILED
    hb_entry numa_process "$p" "hostbound/process_$phase.json" "numa_maps_unavailable:${HB_NUMA_MISSING:-0};raw_maps_for_explicit_pids" 0 null
    for pid in ${PID_LIST//,/ };do
      for f in status cgroup numa_maps stat cmdline;do hb_file numa_process "$HB_PROC/$pid/$f" "hostbound/pid_${pid}_${f}_${phase}.txt";done
      hb_command numa_process "hostbound/pid_${pid}_numastat_${phase}.txt" numastat -p "$pid"
      hb_command processes "hostbound/pid_${pid}_affinity_${phase}.txt" taskset -pc "$pid"
    done
  else
    for p in processes npu_info npu_mapping npu_topology numa_process;do hb_entry "$p" NOT_REQUESTED "" disabled_by_no_hostbound null null null false;done
  fi
  hb_manifest
}
record_trace_status(){
  local status=$1 reason=${2:-} rel category
  printf '%s: %s\n' "$status" "$reason" > "$SNAP/hostbound/trace_error.txt"
  if [[ "$status" == COLLECTED && ! -s "$SNAP/trace.txt" ]];then status=PARSE_FAILED;reason=empty_trace_export;fi
  hb_entry trace "$status" trace.txt "$reason"
  for rel in hostbound/frequency_samples.jsonl hostbound/resource_samples.jsonl hostbound/capabilities.tsv;do
    category=frequency;[[ "$rel" == *resource_samples* ]] && category=resources;[[ "$rel" == *capabilities* ]] && category=trace_capabilities
    if [[ -f "$SNAP/$rel" ]];then hb_entry "$category" COLLECTED "$rel";else hb_entry "$category" "$([[ "$status" == COLLECTED ]] && echo EXECUTION_FAILED || echo "$status")" "$rel" not_generated;fi
  done
  hb_manifest
}
hb_poll(){
  while [[ ! -e "$SNAP/hostbound/poll.stop" ]];do
    hb_snapshot frequency "$SNAP/hostbound/.frequency_poll.json" || return 1
    cat "$SNAP/hostbound/.frequency_poll.json" >> "$SNAP/hostbound/frequency_samples.jsonl"
    hb_snapshot resources "$SNAP/hostbound/.resource_poll.json" || return 1
    cat "$SNAP/hostbound/.resource_poll.json" >> "$SNAP/hostbound/resource_samples.jsonl"
    sleep "$FREQ_INTERVAL"
  done
}
# END standalone evidence

# ---------- 参数解析 ----------
DURATION=3; OUTDIR="."; BUFFER_SIZE=16384; CPU_LIST=""; MINIMAL=0; PID_LIST=""; HOSTBOUND=1; DISCOVER=1; FREQ_INTERVAL=1; ASCEND_DUMP_SCRIPT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--duration|-o|--output|-b|--buffer-size|-c|--cpu-list|-p|--pid|--frequency-interval|--ascend-dump-script)
      [[ $# -ge 2 && "$2" != --* ]] || { err "missing option value (缺少参数): $1"; exit 1; };;
  esac
  case "$1" in
    -t|--duration)    DURATION="$2"; shift 2;;
    -o|--output)      OUTDIR="$2"; shift 2;;
    -b|--buffer-size) BUFFER_SIZE="$2"; shift 2;;
    -c|--cpu-list)    CPU_LIST="$2"; shift 2;;
    --full) MINIMAL=0; shift;;
    --discover) DISCOVER=1; HOSTBOUND=1; shift;;
    --frequency-interval) FREQ_INTERVAL="$2"; shift 2;;
    --ascend-dump-script) ASCEND_DUMP_SCRIPT="$2"; shift 2;;
    -m|--minimal)     MINIMAL=1; shift;;
    -p|--pid)         PID_LIST="$2"; HOSTBOUND=1; shift 2;;
    --hostbound)      HOSTBOUND=1; shift;;
    --no-hostbound)   HOSTBOUND=0; DISCOVER=0; shift;;
    -h|--help)        usage; exit 0;;
    *) err "未知参数: $1"; usage; exit 1;;
  esac
done
if ! awk -v x="$FREQ_INTERVAL" 'BEGIN {exit !(x ~ /^[0-9]+([.][0-9]+)?$/ && x>=0.2)}'; then
  err "frequency-interval 必须 >=0.2 秒"; exit 1
fi
[[ -z "$CPU_LIST" || "$CPU_LIST" =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || { err "CPU列表格式错误"; exit 1; }
if [[ "$DURATION" =~ ^[0-9]+$ ]] && [[ "$BUFFER_SIZE" =~ ^[0-9]+$ ]] && (( DURATION>=1 && 10#$BUFFER_SIZE>=1 )); then :; else
  err "时长和 buffer 必须是 ≥1 的纯数字"; exit 1; fi
# PID 合法性: 逗号分隔的正整数 (不校验进程是否存在, 采集阶段自动跳过)
(( HOSTBOUND )) && DISCOVER=1
for _p in ${PID_LIST//,/ }; do
  [[ "$_p" =~ ^[1-9][0-9]*$ ]] || { err "pid 必须为正整数: $_p"; exit 1; }
done
# 创建证据目录与只读侧车在 tracefs/root 探测之前；降级结果也必须可审计。
PHASE_TOTAL=5
phase_begin "采集只读能力、拓扑与进程前置证据"
mkdir -p -- "$OUTDIR" || exit 1
SNAP=$(mktemp -d "$OUTDIR/cpu_trace_$(date +%Y%m%d_%H%M%S)_XXXXXX") || { err "无法创建采集目录: $OUTDIR"; exit 1; }
CAPTURE_NAME=${SNAP##*/}
mkdir -p -- "$SNAP/hostbound" || exit 1
HB_TICKS=$(getconf CLK_TCK 2>/dev/null || echo 100)
: > "$SNAP/hostbound/collection_entries.jsonl"
if [[ -n "$ASCEND_DUMP_SCRIPT" ]]; then hb_ascend_dump "$ASCEND_DUMP_SCRIPT"; fi
hb_phase start
phase_end "前置证据状态已保存: $SNAP/hostbound/collection_manifest.json"
if [[ "$EUID" -ne 0 ]]; then
  record_trace_status PERMISSION_DENIED root_required_for_private_ftrace_instance
  err "请使用 root 权限运行: sudo bash $0；只读证据已保留: $SNAP"; exit 1; fi
# 阶段总数: 前置证据/兼容快照/采集/导出/打包。

# ---------- 定位 tracefs ----------
TRACE_DIR=""
for d in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  [[ -d "$d" ]] && TRACE_DIR="$d" && break
done
if [[ -z "$TRACE_DIR" ]]; then
  record_trace_status UNSUPPORTED tracefs_not_found
  err "未找到 tracefs/debugfs；只读证据已保留: $SNAP"; exit 1; fi
ok "使用 tracing 目录: $TRACE_DIR"
# per_cpu stats 可读时, 采集进度条额外展示已捕获事件数/速率/丢弃告警
STATS_OK=0; compgen -G "$TRACE_DIR/per_cpu/cpu*/stats" >/dev/null 2>&1 && STATS_OK=1
if [[ ! -w "$TRACE_DIR/tracing_on" ]]; then
  record_trace_status PERMISSION_DENIED tracefs_not_writable
  err "tracing 目录不可写, 请确认 root 权限"; exit 1; fi

# 独立 instance 避免破坏其他诊断工具的 events、buffer 和原始 trace。
TRACE_BASE="$TRACE_DIR"
TRACE_DIR="$TRACE_BASE/instances/cpu_host_$$"
INSTANCE_CREATED=0
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
CPU_MASK=""
if [[ -n "$CPU_LIST" ]]; then CPU_MASK=$(to_mask "$CPU_LIST"); fi

# ---------- 事件集合 ----------
if [[ "$MINIMAL" -eq 1 ]]; then
  EVENT_LIST="sched/sched_switch"
else
  EVENT_LIST="sched/sched_migrate_task sched/sched_wakeup sched/sched_waking sched/sched_switch \
sched/sched_wakeup_new task/task_newtask sched/sched_process_fork sched/sched_process_exec sched/sched_process_exit irq/irq_handler_entry irq/irq_handler_exit \
irq/softirq_entry irq/softirq_exit irq/softirq_raise power/cpu_idle power/cpu_frequency"
fi

# ---------- 私有 instance 清理（包括失败/信号/正常退出） ----------
FREQ_PID=""; CLEANED=0
cleanup() {
  (( CLEANED )) && return 0
  CLEANED=1
  [[ -n "$FREQ_PID" ]] && { kill "$FREQ_PID" 2>/dev/null; wait "$FREQ_PID" 2>/dev/null; }
  if [[ "$INSTANCE_CREATED" == 1 ]]; then
    echo 0 > "$TRACE_DIR/tracing_on" 2>/dev/null
    echo 0 > "$TRACE_DIR/events/enable" 2>/dev/null
    rmdir "$TRACE_DIR" 2>/dev/null || warn "独立 trace instance 清理失败: $TRACE_DIR"
  fi
}
abort(){
  progress_clear
  for j in $(jobs -pr 2>/dev/null); do kill "$j" 2>/dev/null; done
  record_trace_status EXECUTION_FAILED collection_interrupted
  err "采集被中断，清理独立 instance，保留已写入的证据"
  exit 130
}
trap cleanup EXIT
trap abort INT TERM
mkdir "$TRACE_DIR" 2>/dev/null || {
  record_trace_status UNSUPPORTED private_trace_instance_unavailable
  err "内核/权限不支持独立 trace instance；只读证据已保留: $SNAP"; exit 1;
}
INSTANCE_CREATED=1
ENABLED_EVENTS=""
CAPS="$SNAP/hostbound/capabilities.tsv"
printf 'event\texists\twritable\trequested\tenabled\treason\n' > "$CAPS"
[[ -n "$CPU_MASK" ]] && { echo "$CPU_MASK" > "$TRACE_DIR/tracing_cpumask" || { record_trace_status PERMISSION_DENIED tracing_cpumask_write_failed; exit 1; }; }
# /proc/uptime 使用 boottime；仅 boot trace clock 与其对齐。
TRACE_CLOCK="unknown"
if grep -qw boot "$TRACE_DIR/trace_clock"; then
  echo boot > "$TRACE_DIR/trace_clock" && TRACE_CLOCK="boot"
fi
printf '%s\n' "$TRACE_CLOCK" > "$SNAP/hostbound/trace_clock.txt"

# ---------- 环境初始化 ----------
echo 0 > "$TRACE_DIR/tracing_on" 2>/dev/null
echo nop > "$TRACE_DIR/current_tracer" 2>/dev/null
if ! { printf '%s\n' "$BUFFER_SIZE" > "$TRACE_DIR/buffer_size_kb"; } 2>/dev/null; then
  warn "未能写入 $BUFFER_SIZE KiB/CPU 缓冲区，请检查内存与 tracefs"
fi
BUFFER_EFFECTIVE=$(cat "$TRACE_DIR/buffer_size_kb" 2>/dev/null || echo unknown)
if [[ "$BUFFER_EFFECTIVE" != "$BUFFER_SIZE" ]]; then
  warn "请求 buffer $BUFFER_SIZE KiB/CPU，实际读回 $BUFFER_EFFECTIVE KiB/CPU"
else
  ok "buffer_size_kb = $BUFFER_EFFECTIVE"
fi
echo > "$TRACE_DIR/trace" 2>/dev/null

for ev in $EVENT_LIST; do
  exists=0; writable=0; enabled=0; reason="unsupported"
  if [[ -f "$TRACE_DIR/events/$ev/enable" ]]; then
    exists=1; reason="permission_or_enable_failed"
    [[ -w "$TRACE_DIR/events/$ev/enable" ]] && writable=1
    if echo 1 > "$TRACE_DIR/events/$ev/enable" 2>/dev/null && [[ "$(cat "$TRACE_DIR/events/$ev/enable")" == 1 ]]; then
      enabled=1; reason="ok"; ENABLED_EVENTS="$ENABLED_EVENTS $ev"
    fi
  fi
  printf '%s\t%s\t%s\t1\t%s\t%s\n' "$ev" "$exists" "$writable" "$enabled" "$reason" >> "$CAPS"
  (( enabled )) || warn "事件不可用: $ev ($reason)"
done
if (( MINIMAL )); then
  exists=0; writable=0
  [[ -f "$TRACE_DIR/events/power/cpu_frequency/enable" ]] && exists=1
  [[ -w "$TRACE_DIR/events/power/cpu_frequency/enable" ]] && writable=1
  printf 'power/cpu_frequency\t%s\t%s\t0\t0\tnot_requested_minimal\n' "$exists" "$writable" >> "$CAPS"
fi
[[ -z "$ENABLED_EVENTS" ]] && { record_trace_status UNSUPPORTED no_trace_events_enabled; err "没有任何可用事件, 无法采集"; cleanup; exit 1; }
ok "已启用事件:$ENABLED_EVENTS"
print_banner

# sched_stat_wait 仅探测，不默认开启（依赖内核schedstats配置，避免额外高频事件）。
exists=0; writable=0
[[ -f "$TRACE_DIR/events/sched/sched_stat_wait/enable" ]] && exists=1
[[ -w "$TRACE_DIR/events/sched/sched_stat_wait/enable" ]] && writable=1
printf 'sched/sched_stat_wait\t%s\t%s\t0\t0\tnot_requested_overhead_budget\n' "$exists" "$writable" >> "$CAPS"
# ---------- 系统快照 ----------

snap_file(){ [[ -e "$SNAP/$2" ]] && return 0; [[ -r "$1" ]] && cp "$1" "$SNAP/$2" 2>/dev/null || echo "N/A (文件不存在)" > "$SNAP/$2"; }
phase_begin "采集系统静态快照 (/proc, /sys)"
: > "$SNAP/hostbound/export_errors.txt"
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
phase_end "静态快照已写入 $SNAP/"

# 频率轮询复用内嵌 awk；能力缺失写入 manifest。
# ---------- 执行采集 ----------
START_TS=$(date +%s); START_TIME=$(date "+%Y-%m-%d %H:%M:%S")
echo "$START_TIME" > "$SNAP/trace_start_time.txt"
phase_begin "ftrace 采集 (${DURATION}s · $([[ $MINIMAL -eq 1 ]] && echo 精简 || echo 全量)模式$([[ -n "$PID_LIST" ]] && echo " · HostBound:$PID_LIST"))"
echo 1 > "$TRACE_DIR/tracing_on" || { record_trace_status PERMISSION_DENIED tracing_start_failed; err "开启 tracing 失败"; cleanup; exit 1; }
hb_poll & FREQ_PID=$!
echo 'CPU_HOST_WINDOW_START' > "$TRACE_DIR/trace_marker"
T_COL0=$SECONDS; PREV_E=0; PREV_T=$T_COL0
# 刷新节奏自适应: 短采集 0.5s 刷新, 越长越稀疏, 减少无谓开销
COLLECT_STEP=0.5
if (( DURATION>120 && DURATION<=600 )); then COLLECT_STEP=1; elif (( DURATION>600 )); then COLLECT_STEP=2; fi
(( SLEEP_FRAC )) || COLLECT_STEP=1
while :; do
  sleep "$COLLECT_STEP"
  ELAPSED=$((SECONDS-T_COL0)); (( ELAPSED>DURATION )) && ELAPSED=$DURATION
  PCT=$(( ELAPSED*100/DURATION ))
  EXTRA="│ ${C_B}$(fmt_secs "$ELAPSED")${C_RST}/$(fmt_secs "$DURATION") │ 余 $(fmt_secs $((DURATION-ELAPSED)))"
  if [[ $STATS_OK -eq 1 ]]; then
    read -r EV DROP OV <<< "$(trace_stats)"
    EV=${EV:-0}; DROP=${DROP:-0}
    DELTA=$(( EV-PREV_E )); (( DELTA<0 )) && DELTA=0
    RATE=0; (( SECONDS>PREV_T )) && RATE=$(( DELTA/(SECONDS-PREV_T) ))
    PREV_E=$EV; PREV_T=$SECONDS
    EXTRA+=" │ 已捕获 ${C_B}$(fmt_num "$EV")${C_RST} 事件 (${C_DIM}$(fmt_num "$RATE")/s${C_RST})"
    if (( DROP>0 )); then EXTRA+=" │ ${C_Y}丢弃 $DROP${C_RST}"; else EXTRA+=" │ ${C_DIM}丢弃 0${C_RST}"; fi
  fi
  progress_update "采集" "$PCT" "$EXTRA"
  (( ELAPSED>=DURATION )) && break
done
progress_clear
echo 'CPU_HOST_WINDOW_END' > "$TRACE_DIR/trace_marker"
echo 0 > "$TRACE_DIR/tracing_on"
if [[ -n "$FREQ_PID" ]]; then
  touch "$SNAP/hostbound/poll.stop"
  wait "$FREQ_PID" || echo "frequency_poll_failed" >> "$SNAP/hostbound/helper_error.txt"; FREQ_PID=""
fi
hb_phase end
END_TIME=$(date "+%Y-%m-%d %H:%M:%S"); echo "$END_TIME" > "$SNAP/trace_end_time.txt"
phase_end "采集结束 ($END_TIME)"

# 提速: 慢的根源是内核读取 trace 文件时逐条 format 成文本且只能单线程进行。
# 改为并行读取各 CPU 独立缓冲区 (多核同时 format, 近 N 核倍提速), 再拼接为单文件。
# 拼接结果与全局 trace 内容一致, 仅跨 CPU 行序不同 —— 分析器会按时间戳统一排序, 不影响结果。
shopt -s nullglob
PER_CPU_TRACES=("$TRACE_DIR"/per_cpu/cpu*/trace)
if [[ ${#PER_CPU_TRACES[@]} -gt 0 ]]; then
  phase_begin "导出 trace (并行读取 ${#PER_CPU_TRACES[@]} 个 CPU 缓冲区, 并发 ≤16)"
  MAX_PAR=16; T_EXP0=$SECONDS; TOTAL=${#PER_CPU_TRACES[@]}
  declare -A RUNNING=(); NEXT=0; DONE=0; BYTES=0
  while (( DONE < TOTAL )); do
    # 有空闲并发槽位则继续派发
    if (( NEXT < TOTAL )) && (( ${#RUNNING[@]} < MAX_PAR )); then
      cat "${PER_CPU_TRACES[NEXT]}" > "$SNAP/.part_$NEXT.txt" 2>/dev/null &
      RUNNING[$!]=$NEXT; NEXT=$((NEXT+1)); continue
    fi
    sleep "$POLL_STEP"
    # 回收已完成的导出任务
    for pid in "${!RUNNING[@]}"; do
      if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || printf 'cpu%s export_failed\n' "${RUNNING[$pid]}" >> "$SNAP/hostbound/export_errors.txt"; unset "RUNNING[$pid]"; DONE=$((DONE+1))
      fi
    done
    BYTES=$(stat -c %s "$SNAP"/.part_*.txt 2>/dev/null | awk '{s+=$1}END{print s+0}')
    ELAPSED=$((SECONDS-T_EXP0)); (( ELAPSED<1 )) && ELAPSED=1
    ETA="…"; SPD="…"
    if (( DONE>0 )); then
      ETA=$(fmt_secs $(( ELAPSED/DONE*(TOTAL-DONE) )))
      SPD="$(fmt_bytes $(( BYTES/ELAPSED )))/s"
    fi
    progress_update "导出" $(( DONE*100/TOTAL )) \
      "│ ${C_B}$DONE${C_RST}/$TOTAL 核 │ $(fmt_bytes "$BYTES") │ ${C_C}$SPD${C_RST} │ 余 $ETA"
  done
  progress_clear
  BYTES=$(stat -c %s "$SNAP"/.part_*.txt 2>/dev/null | awk '{s+=$1}END{print s+0}')
  EXP_ELAPSED=$((SECONDS-T_EXP0)); (( EXP_ELAPSED<1 )) && EXP_ELAPSED=1
  spin_start "拼接各 CPU 分片 -> trace.txt"
  cat "$SNAP"/.part_*.txt > "$SNAP/trace.txt" 2>/dev/null
  rm -f "$SNAP"/.part_*.txt
  spin_stop
  phase_end "已并行导出 $TOTAL 个 CPU 缓冲区, 共 $(fmt_bytes "$BYTES") (平均 $(fmt_bytes $(( BYTES/EXP_ELAPSED )))/s)"
else
  # per_cpu 目录不存在时回退为读取全局 trace (兼容旧内核/特殊配置)
  phase_begin "导出 trace (兼容模式: 全局缓冲区)"
  spin_start "读取全局 trace 缓冲区"
  cat "$TRACE_DIR/trace" > "$SNAP/trace.txt" 2>/dev/null || echo "global_export_failed" >> "$SNAP/hostbound/export_errors.txt"
  spin_stop
  phase_end "已导出全局 trace 缓冲区 ($(fmt_bytes "$(stat -c %s "$SNAP/trace.txt" 2>/dev/null || echo 0)"))"
fi
shopt -u nullglob
[[ -s "$SNAP/trace.txt" ]] || { err "trace 为空, 事件可能未产生或 buffer 溢出"; }
# 记录 buffer 溢出情况 (注意: glob 不能加引号, 否则不展开)
# stats 是文件而不是 stats/overrun；无 bc 依赖，记录每核丢失证据。
mkdir -p "$SNAP/hostbound/trace_stats"
for f in "$TRACE_DIR"/per_cpu/cpu*/stats; do
  [[ -f "$f" ]] || continue
  c="${f%/stats}"; c="${c##*/}"
  cp "$f" "$SNAP/hostbound/trace_stats/$c.txt"
done
awk -F': *' '$1 ~ /^(overrun|commit overrun|dropped events)$/ {s+=$2} END {print s+0}' "$SNAP"/hostbound/trace_stats/cpu*.txt > "$SNAP/trace_overrun.txt"
printf '%s\n' "${CPU_LIST:-$(cat "$HB_SYS/devices/system/cpu/online")}" > "$SNAP/hostbound/collection_cpus.txt"

cleanup

# 默认证据由内嵌 Bash/awk 生成。可选原生 Dump 随同归档。
record_trace_status COLLECTED

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
trace_clock: $TRACE_CLOCK
buffer_size_requested_kb: $BUFFER_SIZE
buffer_size_effective_kb: $BUFFER_EFFECTIVE
frequency_trace_unit: kHz
frequency_interval_sec: $FREQ_INTERVAL
process_discovery: $DISCOVER
nproc: $(nproc 2>/dev/null)
kernel: $(uname -r 2>/dev/null)
hostname: $(hostname 2>/dev/null)
trace_dir: $TRACE_DIR
EOF
phase_begin "打包采集包 (tar.gz 压缩)"
PKG=$(realpath "$OUTDIR" 2>/dev/null || echo "$OUTDIR")/${CAPTURE_NAME}.tar.gz
spin_start "压缩打包中, 数据量大时需要数分钟"
if tar -czf "$PKG" -C "$OUTDIR" "$CAPTURE_NAME" 2>/dev/null; then
  spin_stop
  PKG_SIZE=$(stat -c %s "$PKG" 2>/dev/null || echo 0)
  phase_end "打包完成"
  echo
  echo -e "${C_G}┌──────────────────────────────────────────────────────────────"
  echo -e "│ ${C_B}✔ 采集完成!${C_RST}${C_G} 总耗时 $(fmt_secs "$SECONDS")"
  echo -e "│${C_RST} 采集包: ${C_B}$PKG${C_RST} ${C_DIM}($(fmt_bytes "$PKG_SIZE"))${C_RST}"
  echo -e "${C_DIM}│ 提示: 可直接用本 Skill 分析, 或拖入 https://ui.perfetto.dev 查看${C_RST}"
  echo -e "${C_G}└──────────────────────────────────────────────────────────────${C_RST}"
else
  spin_stop
  warn "打包失败, 原始数据保留在: $SNAP/"
  ok "请将整个 cpu_trace 目录回传用于分析"
fi
exit 0
