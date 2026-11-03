# -*- coding: utf-8 -*-
"""NPU -> host PID -> thread affinity evidence and read-only binding plans."""
import re
from collections import defaultdict


STATE_CN = {
    'LOCAL': '已绑定亲和 CPU', 'UNCONFINED': '允许跨 NUMA / 未收敛',
    'REMOTE_ALLOWED': '绑定范围偏离亲和 CPU', 'REMOTE_OBSERVED': '观测到非亲和 CPU',
    'UNKNOWN': '证据不足', 'NO_PROCESS': '无已识别进程',
}


def cpus(text):
    text = re.sub(r'\s+', '', text or '')
    if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', text):
        return set()
    result = set()
    for item in text.split(','):
        ends = [int(x) for x in item.split('-')]
        if len(ends) == 1:
            result.add(ends[0])
        elif 0 <= ends[1] - ends[0] < 100000:
            result.update(range(ends[0], ends[1]+1))
    return result


def cpu_list(values):
    values = sorted(set(values))
    ranges = []
    for value in values:
        if ranges and value == ranges[-1][1]+1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ','.join(str(a) if a == b else '%d-%d' % (a, b) for a, b in ranges)


def _get(snapshot, name):
    return snapshot.get('hostbound/'+name, snapshot.get(name, '')) or ''


def _topology(snapshot):
    affinity, nodes, cores = {}, defaultdict(set), {}
    for line in _get(snapshot, 'npu_smi_topology.txt').splitlines():
        match = re.match(r'^\s*NPU\s*(\d+)\s+.*?\s+(\d[\d,\-]*)\s*$', line)
        if match:
            affinity[int(match[1])] = cpus(match[2])
    for line in _get(snapshot, 'numactl.txt').splitlines():
        match = re.match(r'node\s+(\d+)\s+cpus:\s*(.*)', line.strip())
        if match:
            nodes[int(match[1])].update(cpus(','.join(match[2].split())))
    current = None
    for line in _get(snapshot, 'numa.txt').splitlines():
        match = re.match(r'==\s*/sys/devices/system/node/node(\d+)/cpulist\s*==', line.strip())
        if match:
            current = int(match[1])
        elif line.startswith('=='):
            current = None
        elif current is not None:
            nodes[current].update(cpus(line.strip()))
            current = None
    for line in _get(snapshot, 'lscpu_parse.txt').splitlines():
        fields = line.strip().split(',')
        if len(fields) >= 5 and all(x.isdigit() for x in fields[:4]):
            cpu, core, socket, node = map(int, fields[:4])
            if fields[4].upper() in ('Y', 'YES', '1'):
                nodes[node].add(cpu)
                cores[cpu] = (socket, node, core)
    return affinity, dict(nodes), cores


def _processes(text):
    result = defaultdict(dict)
    for line in text.splitlines():
        match = re.match(r'^\s*\|?\s*(\d+)\s+(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*(\d+)', line)
        if match:
            npu, chip, pid, name, memory = match.groups()
            proc = result[int(npu)].setdefault(int(pid), {'pid': int(pid), 'name': name.strip(), 'memory': 0, 'chip': int(chip), 'chips': [], 'memory_by_chip': {}})
            proc['memory_by_chip'][int(chip)] = int(memory)
            proc['memory'] = sum(proc['memory_by_chip'].values())
            proc['chips'] = sorted(proc['memory_by_chip'])
            proc['chip'] = proc['chips'][0] if len(proc['chips']) == 1 else ','.join(map(str, proc['chips']))
    return result


def _pid_evidence(snapshot, pid, m):
    status = dict(re.findall(r'^(\w+):\s*(.*?)\s*$', _get(snapshot, 'pid_%d_status.txt' % pid), re.M))
    allowed = {}
    for tid, text in re.findall(r"pid\s+(\d+)'s current affinity list:\s*([\d,\-]+)", _get(snapshot, 'pid_%d_thread_affinity.txt' % pid)):
        allowed[int(tid)] = cpus(text)
    tids, psr = set(), set()
    for line in _get(snapshot, 'pid_%d_threads.txt' % pid).splitlines():
        fields = line.split()
        if len(fields) >= 3 and all(x.isdigit() for x in fields[:3]) and int(fields[0]) == pid:
            tids.add(int(fields[1]))
            psr.add(int(fields[2]))
    trace = set()
    for tid, _name, values, _runtime in (m.get('tasks') or {}).get('task_cpus', []):
        if tid == pid or tid in tids:
            trace.update(values)
    for tid, values in (m.get('tasks') or {}).get('observed_cpus_by_tid', {}).items():
        if tid == pid or tid in tids:
            trace.update(values)
    complete = bool(tids and tids <= set(allowed))
    if status.get('Threads', '').isdigit():
        complete = complete and len(tids) == int(status['Threads'])
    complete = complete and bool(allowed) and all(allowed.values())
    starttime = _get(snapshot, 'pid_%d_starttime.txt' % pid).strip()
    return {'status': status, 'allowed': allowed, 'tids': tids, 'psr': psr, 'trace': trace,
            'complete': complete, 'cpuset': cpus(_get(snapshot, 'pid_%d_cpuset_effective.txt' % pid)),
            'starttime': starttime if starttime.isdigit() else ''}


def analyze_npu_affinity(m):
    snapshot = m.get('snapshot', {})
    affinity, nodes, cores = _topology(snapshot)
    processes = _processes(_get(snapshot, 'npu_smi_info.txt'))
    online = cpus(_get(snapshot, 'cpu_online.txt'))
    context = dict(re.findall(r'^([a-z_]+)=(.*)$', _get(snapshot, 'host_context.txt'), re.M))
    rows, candidates = [], {}
    for npu in sorted(set(affinity) | set(processes)):
        pool = affinity.get(npu, set())
        local_nodes = sorted(n for n, values in nodes.items() if pool & values)
        # A single local NUMA node must be established before a concrete plan is emitted.
        candidate = pool & online & nodes[local_nodes[0]] if len(local_nodes) == 1 else set()
        procs = sorted(processes.get(npu, {}).values(), key=lambda p: (-p['memory'], p['pid']))
        evidence = {p['pid']: _pid_evidence(snapshot, p['pid'], m) for p in procs}
        for ev in evidence.values():
            if ev['cpuset']:
                candidate &= ev['cpuset']
            else:
                candidate = set()
        candidates[npu] = candidate
        for i, proc in enumerate(procs or [{'pid': None, 'name': '—', 'memory': None, 'chip': None}]):
            pid = proc['pid']
            ev = evidence.get(pid, {})
            permitted = set().union(*ev.get('allowed', {}).values()) if ev else set()
            observed = ev.get('trace', set()) | ev.get('psr', set())
            observed_numa = sorted(n for n, values in nodes.items() if values & observed)
            known_node_cpus = set().union(*nodes.values()) if nodes else set()
            if observed and not (observed - known_node_cpus) and len(local_nodes) == 1:
                locality = 'LOCAL_OBSERVED' if observed_numa == local_nodes else 'REMOTE_OBSERVED'
            else:
                locality = 'UNKNOWN'
            if not pid:
                state = 'NO_PROCESS'
            elif not pool or not ev['complete'] or len(local_nodes) != 1:
                state = 'UNKNOWN'
            elif observed - pool:
                state = 'REMOTE_OBSERVED'
            elif permitted <= pool:
                state = 'LOCAL'
            elif permitted.isdisjoint(pool):
                state = 'REMOTE_ALLOWED'
            else:
                state = 'UNCONFINED'
            blockers = []
            if pid:
                if context.get('execution_context') != 'host':
                    blockers.append('未确认在物理机/宿主机采集')
                if not ev['complete']:
                    blockers.append('全量 TID/线程 affinity 快照不完整')
                if ev['status'].get('Tgid') != str(pid) or ev['status'].get('Pid') != str(pid):
                    blockers.append('宿主机 PID/TGID 对应关系未核验')
                if not ev['starttime'] or not context.get('boot_id'):
                    blockers.append('缺少进程 starttime 或主机 boot_id')
                if not candidate:
                    blockers.append('亲和 NUMA/在线 CPU/有效 cpuset 交集不可用')
            rows.append(dict(proc, npu=npu, role='主进程（显存占用最高）' if i == 0 and pid else '其他占卡进程',
                             numa=local_nodes, affinity=cpu_list(pool), allowed=cpu_list(permitted),
                             observed=cpu_list(observed), trace=cpu_list(ev.get('trace', set())),
                             observed_numa=observed_numa, numa_locality=locality,
                             allowed_numa=sorted(n for n, values in nodes.items() if values & permitted),
                             psr=cpu_list(ev.get('psr', set())), state=state, target='', blockers=blockers,
                             threads=len(ev.get('tids', [])), evidence=ev))
    # Equal shared pools are partitioned by physical core, keeping SMT siblings together.
    # Unequal overlapping pools need a site-specific allocation rather than a guessed split.
    groups = defaultdict(list)
    active = {r['npu'] for r in rows if r['pid']}
    for npu in sorted(active):
        groups[tuple(sorted(candidates[npu]))].append(npu)
    targets, plan_notes = {}, []
    for pool_tuple, npus in sorted(groups.items()):
        pool = set(pool_tuple)
        if not pool:
            continue
        conflict = any(pool & candidates[n] and candidates[n] != pool for n in active if n not in npus)
        if conflict:
            for row in rows:
                if row['npu'] in npus:
                    row['blockers'].append('不同 NPU 的可用 CPU 部分重叠，需人工分区')
            continue
        units = defaultdict(set)
        for cpu in sorted(pool):
            units[cores.get(cpu, ('logical', cpu))].add(cpu)
        ordered = sorted(units.values(), key=min)
        if len(ordered) < len(npus):
            for row in rows:
                if row['npu'] in npus:
                    row['blockers'].append('可分配核心组少于 NPU 数量')
            continue
        offset = 0
        for i, npu in enumerate(npus):
            count = len(ordered)//len(npus) + (1 if i < len(ordered)%len(npus) else 0)
            targets[npu] = set().union(*ordered[offset:offset+count])
            offset += count
        if len(npus) > 1:
            plan_notes.append('NPU %s 共享 %s，按 %d 份分区；每份 CPU 互不重叠。' % (','.join(map(str, npus)), cpu_list(pool), len(npus)))
    # Disjoint logical pools can still share physical cores through SMT.
    core_owners = defaultdict(set)
    for npu, pool in targets.items():
        for cpu in pool:
            if cpu in cores:
                core_owners[cores[cpu]].add(npu)
    smt_conflicts = set().union(*(owners for owners in core_owners.values() if len(owners) > 1)) if core_owners else set()
    for row in rows:
        if row['npu'] in smt_conflicts:
            row['blockers'].append('不同 NPU 分区存在 SMT 同胞核心冲突，需人工调整')
    pid_npus = defaultdict(set)
    for row in rows:
        if row['pid']:
            pid_npus[row['pid']].add(row['npu'])
    actions = []
    for row in rows:
        pid, npu = row['pid'], row['npu']
        if not pid:
            continue
        row['target'] = cpu_list(targets.get(npu, set()))
        if len(pid_npus[pid]) > 1:
            row['blockers'].append('同一 PID 使用多张 NPU；需明确 TID/rank 分工，不能逐卡覆盖整个 PID')
        if row['target'] and not row['blockers']:
            ev = row['evidence']
            if any(not values <= targets[npu] for values in ev['allowed'].values()):
                actions.append(row)
    if active and not cores:
        plan_notes.append('缺少 SMT 拓扑：仅能保证逻辑 CPU 集合互斥，物理核心隔离待核验。')
    return {'rows': rows, 'notes': plan_notes, 'host_context': context,
            'commands': _binding_script(actions, context) if actions else '',
            'coverage': 'NPU 拓扑/进程/全线程 affinity/NUMA/在线 CPU/cpuset/宿主机上下文'}


def _binding_script(actions, context):
    # Numeric PID/CPU/starttime inputs are parsed above; boot_id is shell-quoted defensively.
    boot_id = context['boot_id'].replace("'", "'\"'\"'")
    lines = ["# 仅在原物理机/宿主机执行；不要进入容器。先阅读报告，再运行。",
             "sudo bash <<'HOST_CPU_BIND'", 'set -euo pipefail',
             '[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || { echo "请回到宿主机"; exit 1; }',
             'if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then echo "请回到宿主机"; exit 1; fi',
             "[ \"$(cat /proc/sys/kernel/random/boot_id)\" = '%s' ] || { echo '主机/启动周期已变化，请重新采集'; exit 1; }" % boot_id,
             'backup=$(mktemp -d /var/tmp/npu-affinity.XXXXXX)',
             'echo "备份和回滚脚本：$backup"',
             "cat > \"$backup/rollback.sh\" <<'ROLLBACK_HEADER'",
             '#!/usr/bin/env bash', 'set -euo pipefail',
             '[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] || exit 1',
             'if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then exit 1; fi',
             "[ \"$(cat /proc/sys/kernel/random/boot_id)\" = '%s' ] || { echo '回滚主机/启动周期不一致'; exit 1; }" % boot_id,
             'failed=0', 'restore_tid() {', '  local tid=$1 expected=$2 previous=$3 s',
             '  if [ ! -r "/proc/$tid/stat" ]; then echo "跳过已退出 TID $tid"; return; fi',
             '  s=$(cat "/proc/$tid/stat") || { echo "跳过已退出 TID $tid"; return; }',
             '  s=${s##*) }; set -- $s',
             '  if [ "${20}" != "$expected" ]; then echo "跳过已复用 TID $tid"; return; fi',
             '  if ! taskset -pc "$previous" "$tid"; then failed=1; fi', '}', 'ROLLBACK_HEADER',
             "starttime() { local s; s=$(cat \"/proc/$1/stat\"); s=${s##*) }; set -- $s; printf '%s' \"${20}\"; }",
             "check_cpu_list() { local want=$1 have=$2; python3 - \"$want\" \"$have\" <<'CPU_CHECK'",
             'import sys',
             'def cpus(s):',
             '    out=set()',
             "    for part in s.strip().split(','):",
             "        a,_,b=part.partition('-')",
             '        if a: out.update(range(int(a),int(b or a)+1))',
             '    return out',
             'raise SystemExit(0 if cpus(sys.argv[1]) and cpus(sys.argv[1]) <= cpus(sys.argv[2]) else 1)',
             'CPU_CHECK', '}',
             "effective_cpuset() { local p=$1 cg value; cg=$(awk -F: '$1==0 {print $3}' /proc/\"$p\"/cgroup);",
             'if [ -n "$cg" ] && [ -r "/sys/fs/cgroup${cg}/cpuset.cpus.effective" ]; then cat "/sys/fs/cgroup${cg}/cpuset.cpus.effective"; return; fi',
             "cg=$(awk -F: '$2 ~ /(^|,)cpuset(,|$)/ {print $3}' /proc/\"$p\"/cgroup)",
             '[ -n "$cg" ] || return 1',
             'for f in "/sys/fs/cgroup/cpuset${cg}/cpuset.effective_cpus" "/sys/fs/cgroup/cpuset${cg}/cpuset.cpus"; do if [ -r "$f" ]; then value=$(cat "$f"); if [ -n "$value" ]; then printf "%s\\n" "$value"; return; fi; fi; done',
             'return 1; }', '# 全部预检、逐 TID 保存成功之后才执行绑核。']
    for row in actions:
        pid, target, ev = row['pid'], row['target'], row['evidence']
        lines += ["[ \"$(starttime %d)\" = '%s' ] || { echo 'PID %d 已变化，请重新采集'; exit 1; }" % (pid, ev['starttime'], pid),
                  "check_cpu_list '%s' \"$(cat /sys/devices/system/cpu/online)\"" % target,
                  "check_cpu_list '%s' \"$(effective_cpuset %d)\" || { echo 'PID %d 的 cpuset 不允许目标 CPU'; exit 1; }" % (target, pid, pid),
                  'for task in /proc/%d/task/*; do' % pid,
                  '  tid=${task##*/}',
                  "  check_cpu_list '%s' \"$(effective_cpuset \"$tid\")\" || { echo \"TID $tid 的 cpuset 不允许目标 CPU\"; exit 1; }" % target,
                  '  taskset -pc "$tid" >> "$backup/before.txt"',
                  "  old=$(awk '/^Cpus_allowed_list:/ {print $2}' \"$task/status\")",
                  '  stamp=$(starttime "$tid")',
                  "  printf 'restore_tid %s %s %s\\n' \"$tid\" \"$stamp\" \"$old\" >> \"$backup/rollback.sh\"",
                  'done']
    lines += ["printf '%s\\n' 'exit \"$failed\"' >> \"$backup/rollback.sh\"", '# -a 覆盖该 PID 的现有线程；已有子进程需要各自的宿主机 PID。']
    for row in actions:
        lines.append("[ \"$(starttime %d)\" = '%s' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }" % (row['pid'], row['evidence']['starttime']))
        lines.append('taskset -acp %s %d' % (row['target'], row['pid']))
    lines += ['# 验证所有线程的 allowed CPU，保存执行后快照。']
    for row in actions:
        pid, target = row['pid'], row['target']
        lines += ['taskset -apc %d | tee -a "$backup/after.txt"' % pid,
                  'for task in /proc/%d/task/*; do' % pid,
                  "  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' \"$task/status\")",
                  "  [ \"$actual\" = '%s' ] || { echo '线程 affinity 与目标不一致，请检查或回滚'; exit 1; }" % target, 'done']
    lines += ['echo "如需回滚，在本宿主机运行：sudo bash $backup/rollback.sh"', 'HOST_CPU_BIND']
    return '\n'.join(lines)
