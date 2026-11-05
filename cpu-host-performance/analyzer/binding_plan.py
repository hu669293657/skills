"""Concrete AI task inventory and a guarded Bash affinity script."""
import re
from collections import defaultdict
from .npu_affinity import _pid_evidence, cpus, cpu_list, _get, _topology
from .process_evidence import process_batch


def _role(name, process, tid, pid, direct):
    text = str(name or '').lower()
    if 'release_thread' in text:
        return '资源回收线程'
    if 'acl_thread' in text:
        return 'ACL 下发线程'
    if re.search(r'dataloader|pt_data_worker|data_loader', text):
        return '数据加载线程/进程'
    if 'tokeniz' in text:
        return '分词线程/进程'
    if 'hccl' in text or 'nccl' in text:
        return '集合通信线程'
    if re.search(r'engine_?core', text):
        return '推理引擎'
    if re.search(r'torchrun|deepspeed|accelerate', str(process.get('cmdline', '')), re.I) and tid == pid:
        return 'AI 启动器'
    if tid == pid and 'python' in (text + str(process.get('name', '')).lower()):
        return 'Python 主线程' + (' / NPU 主进程' if direct else '')
    if tid == pid and direct:
        return 'NPU 主进程'
    if 'python' in text:
        return 'Python 工作线程'
    return 'AI 工作线程（职责未确认）' if tid != pid else 'AI 关联进程'


def _legacy_threads(snapshot, pid, ev):
    rows = []
    for line in _get(snapshot, 'pid_%d_threads.txt' % pid).splitlines():
        fields = line.split()
        if len(fields) >= 6 and all(x.isdigit() for x in fields[:3]) and int(fields[0]) == pid:
            tid = int(fields[1])
            rows.append(dict(tid=tid, tgid=pid, name=' '.join(fields[5:]), last_cpu=int(fields[2]),
                             allowed_cpus=sorted(ev['allowed'].get(tid, set())), starttime=None))
    return rows


def build_binding_plan(m):
    snapshot = m.get('snapshot', {})
    data = m.get('npu_affinity', {})
    context = data.get('host_context', {})
    _, nodes, _ = _topology(snapshot)
    online = cpus(_get(snapshot, 'cpu_online.txt'))
    batches = [process_batch(snapshot, phase) for phase in ('start', 'end')]
    before, after = [{p['tgid']: p for p in batch.get('processes', [])} for batch in batches]
    processes = dict(before)
    processes.update(after)
    raw_threads = defaultdict(list)
    for row in batches[1].get('threads', []) or batches[0].get('threads', []):
        if isinstance(row.get('tgid'), int) and row['tgid'] > 0:
            raw_threads[row['tgid']].append(dict(row))
    by_pid = defaultdict(list)
    for row in data.get('rows', []):
        if row.get('pid'):
            by_pid[row['pid']].append(row)

    roots = {}
    for pid, rows in by_pid.items():
        ev = _pid_evidence(snapshot, pid, m)
        mapped = [cpus(r.get('affinity', '')) for r in rows]
        pool = set().union(*mapped) if all(mapped) else set()
        known = set().union(*nodes.values()) if nodes else set()
        reason = ''
        if not pool or not pool <= known:
            reason = '缺少明确的 NPU→CPU/NUMA 映射'
        elif len({tuple(sorted(values)) for values in mapped}) > 1:
            reason = '同进程使用不同 NPU CPU 池；缺少 TID/rank 分工，不能替所有线程选择并集'
        elif not online:
            reason = '缺少在线 CPU 范围'
        if (ev['identity_status'] == 'AMBIGUOUS' or not ev['namespace_ok'] or
            ev['status'].get('Pid') != str(pid) or ev['status'].get('Tgid') != str(pid) or
            not str(ev['starttime']).isdigit()):
            reason = 'NPU 主进程 %d 的身份或命名空间未通过核验' % pid
        pool &= online
        if len(rows) == 1 and rows[0].get('target') and not rows[0].get('blockers'):
            pool = cpus(rows[0]['target'])
        roots[pid] = dict(pool=pool, evidence=ev, reason=reason,
                          npu_ids=sorted({r['npu'] for r in rows}))
        if pid not in processes:
            processes[pid] = dict(tgid=pid, name=rows[0].get('name'), ppid=int(ev['status'].get('PPid', '0')) if str(ev['status'].get('PPid', '0')).isdigit() else None)
        if not raw_threads[pid]:
            raw_threads[pid] = _legacy_threads(snapshot, pid, ev)
        if not raw_threads[pid]:
            raw_threads[pid] = [dict(tid=pid, tgid=pid, name=processes[pid].get('name'),
                                    allowed_cpus=sorted(ev['allowed'].get(pid, set())), starttime=None)]

    # Relate descendants and AI ancestors through observed, stable parent links.
    associations = defaultdict(set)
    ancestors = set()
    for root in roots:
        associations[root].add(root)
        current, seen = root, set()
        while current in before and current in after and current not in seen:
            seen.add(current)
            old, new = before[current], after[current]
            if old.get('starttime') != new.get('starttime') or old.get('ppid') != new.get('ppid'):
                break
            current = int(new.get('ppid') or 0)
            if current <= 1:
                break
            p = processes.get(current, {})
            if re.search(r'python|torchrun|vllm|sglang|mindie|engine|deepspeed|accelerate',
                         str(p.get('name', '')) + ' ' + str(p.get('cmdline', '')), re.I):
                associations[current].add(root)
                ancestors.add(current)
    for pid in processes:
        current, seen = pid, set()
        while current in processes and current not in seen:
            seen.add(current)
            if current in roots:
                associations[pid].add(current)
                break
            old, new = before.get(current), after.get(current)
            if not old or not new or old.get('starttime') != new.get('starttime') or old.get('ppid') != new.get('ppid'):
                break
            current = int(new.get('ppid') or 0)

    # Explicit workload roots can at least reproduce their observed configuration.
    explicit = cpus(str(m.get('metadata', {}).get('hostbound_pid', '')))
    for task in m.get('process_analysis', {}).get('tasks', []):
        pid = task.get('tgid')
        if pid and task.get('related') and (pid in explicit or task.get('role') != 'SYSTEM_PROCESS'):
            associations.setdefault(pid, set())

    inventory, actions = [], []
    for pid, root_ids in sorted(associations.items()):
        p = processes.get(pid, {})
        ev = roots[pid]['evidence'] if pid in roots else _pid_evidence(snapshot, pid, m)
        desired = set().union(*(roots[root]['pool'] for root in root_ids)) if root_ids else set()
        root_reasons = [roots[root]['reason'] for root in root_ids if roots[root]['reason']]
        root_reason = '; '.join(dict.fromkeys(root_reasons))
        problem = root_reason
        if not root_ids and pid not in explicit:
            problem = '未建立稳定的 NPU 主进程关系，不能继承其 CPU 方案'
        if context.get('execution_context') != 'host':
            problem = '未确认采集宿主机命名空间'
        elif not context.get('boot_id'):
            problem = '缺少宿主机 boot_id'
        elif (context.get('pid_namespace') and context.get('init_pid_namespace') and
              context['pid_namespace'] != context['init_pid_namespace']):
            problem = '采集端 PID 命名空间与 init 不一致'
        elif ev['identity_status'] == 'AMBIGUOUS' or not ev['namespace_ok']:
            problem = 'PID/TID 身份变化或命名空间对应关系不明确'
        elif ev['status'].get('Pid') != str(pid) or ev['status'].get('Tgid') != str(pid):
            problem = '宿主机 PID/TGID 对应关系不一致'
        elif not ev['starttime'] or not str(ev['starttime']).isdigit():
            problem = '缺少可供脚本核验的进程 starttime'
        tids = raw_threads.get(pid, [])
        if not tids:
            tids = [dict(tid=pid, tgid=pid, name=p.get('name'), allowed_cpus=[], starttime=None)]
        is_legacy = all(not str(t.get('starttime', '')).isdigit() for t in tids)
        masks = [set(t.get('allowed_cpus') or ev['allowed'].get(t['tid'], set())) for t in tids]
        uniform_masks = bool(masks and masks[0] and all(values == masks[0] for values in masks))
        bulk = None
        if is_legacy and uniform_masks and ev['complete'] and desired and not problem:
            # Old packages know the process birth but not individual TID births.
            # Bind the verified process, validating *live* members in the script.
            bulk_target = masks[0] if masks[0] <= desired else desired
            command = 'taskset -apc %s %d' % (cpu_list(bulk_target), pid)
            bulk = dict(pid=pid, tid=pid, target=cpu_list(bulk_target), scope='process',
                        process_starttime=ev['starttime'], thread_starttime=None, command=command)
            actions.append(bulk)
        for t in sorted(tids, key=lambda t: (t['tid'] != pid, t['tid'])):
            tid = t['tid']
            thread_birth = t.get('starttime')
            if tid == pid and not str(thread_birth).isdigit():
                thread_birth = ev['starttime']
            allowed = set(t.get('allowed_cpus') or ev['allowed'].get(tid, set()))
            target = set(desired)
            note = problem
            source = 'NPU 亲和池；已知窄掩码保留'
            cpuset = t.get('cpuset_cpus_effective')
            if not target and not root_reason:
                target = set(allowed) & online
                source = '复现当前亲和范围；未获得独立的优化分区证据'
            if allowed and allowed <= target:
                target = allowed
            elif isinstance(cpuset, list) and cpuset:
                target &= set(cpuset)
            elif not bulk:
                target &= allowed
                source = '有效 cpuset 未提供；仅使用已观测 allowed CPU 子集'
            if not target and not note:
                note = '未得到该线程可用的目标 CPU 交集'
            command = ''
            if bulk:
                command = bulk['command'] if tid == pid else '由主进程命令覆盖'
                target = cpus(bulk['target'])
                source = '旧包缺逐线程出生时间；完整脚本在执行前核验进程及当前全部线程'
            elif target and not note and str(thread_birth).isdigit():
                command = 'taskset -pc %s %d' % (cpu_list(target), tid)
                actions.append(dict(pid=pid, tid=tid, target=cpu_list(target), scope='thread',
                    process_starttime=ev['starttime'], thread_starttime=str(thread_birth), command=command))
            elif not note:
                note = '缺少逐线程身份，无法生成该 TID 的独立执行命令'
            npu_ids = sorted(set().union(*(set(roots[r]['npu_ids']) for r in root_ids))) if root_ids else []
            inventory.append(dict(pid=pid, tid=tid, ppid=p.get('ppid'), name=t.get('name') or p.get('name') or '',
                role=_role(t.get('name'), p, tid, pid, pid in roots),
                object_type='NPU 主进程' if tid == pid and pid in roots else ('AI 线程' if tid != pid else ('AI 启动/父进程' if pid in ancestors else ('AI 子进程' if root_ids else 'AI 关联进程'))),
                npu_ids=npu_ids, current_cpu=cpu_list(allowed), target_cpu=cpu_list(target),
                numa=sorted(n for n, values in nodes.items() if values & target),
                command=command, status='BLOCKED' if note else ('RUNTIME_CHECK_REQUIRED' if bulk else 'READY'),
                reason=note or source))
    commands = '\n'.join(dict.fromkeys(a['command'] for a in actions))
    return dict(tasks=inventory, actions=actions, commands=commands,
                script=render_binding_script(actions, context),
                note='角色按 NPU/父子关系和名称线索整理，不凭线程名称任意分配单核；仅当现有窄掩码或有效目标为单核时输出单核命令。')


_SCRIPT_HELPERS = r'''
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
'''


def render_binding_script(actions, context):
    lines = ['#!/usr/bin/env bash', 'set -euo pipefail', '# 仅在本次采集的宿主机执行；所有预检成功后才应用命令。']
    if not actions:
        return '\n'.join(lines + ['echo "未获得可执行绑核项，请查看 HTML 中逐对象的原因。"', 'exit 1']) + '\n'
    boot = str(context.get('boot_id', '')).replace("'", "'\"'\"'")
    lines += [_SCRIPT_HELPERS.strip(),
              '[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || fail "请在宿主机执行"',
              'if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then fail "请在宿主机执行"; fi',
              "[[ \"$(cat /proc/sys/kernel/random/boot_id)\" == '%s' ]] || fail '宿主机或启动周期已变化'" % boot]
    for key, path in [('pid_namespace', '/proc/self/ns/pid'), ('init_pid_namespace', '/proc/1/ns/pid')]:
        if context.get(key):
            expected = str(context[key]).replace("'", "'\"'\"'")
            lines.append("[[ \"$(readlink %s)\" == '%s' ]] || fail 'PID 命名空间不匹配'" % (path, expected))
    checked = set()
    for action in actions:
        pid = action['pid']
        if pid not in checked:
            lines.append('check_process %d %s' % (pid, action['process_starttime']));checked.add(pid)
        if action['scope'] == 'process':
            lines += ['for task in /proc/%d/task/[0-9]*; do' % pid,
                      '  check_thread %d "${task##*/}" "" %s' % (pid, action['target']), 'done']
        else:
            lines.append('check_thread %d %d %s %s' % (pid, action['tid'], action['thread_starttime'], action['target']))
    lines += ['# 具体绑核命令'] + list(dict.fromkeys(a['command'] for a in actions))
    return '\n'.join(lines) + '\n'
