# -*- coding: utf-8 -*-
"""NPU -> host PID -> thread affinity evidence and read-only binding plans."""
import re
import math
from collections import defaultdict
from .evidence import read_json
from .npu_parser import parse_npu_info, parse_npu_mapping, parse_npu_topology
from .process_evidence import process_batch


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
    for npu, row in _device_mapping(snapshot)['by_npu'].items():
        affinity[npu] = row['cpus']
    for line in _get(snapshot, 'numactl.txt').splitlines():
        match = re.match(r'node\s+(\d+)\s+cpus:\s*(.*)', line.strip())
        if match:
            nodes[int(match[1])].update(cpus(','.join(match[2].split())))
    current = None
    sysfs_nodes = defaultdict(set)
    for line in _get(snapshot, 'numa.txt').splitlines():
        match = re.match(r'==\s*/sys/devices/system/node/node(\d+)/cpulist\s*==', line.strip())
        if match:
            current = int(match[1])
        elif line.startswith('=='):
            current = None
        elif current is not None:
            sysfs_nodes[current].update(cpus(line.strip()))
            current = None
    for node, values in sysfs_nodes.items():
        if values:
            nodes[node] = values
    normalized = read_json(snapshot, 'numa_topology.json', {})
    if isinstance(normalized, dict) and isinstance(normalized.get('nodes'), list):
        for row in normalized['nodes']:
            if isinstance(row, dict) and str(row.get('node', '')).isdigit() and isinstance(row.get('cpus'), list):
                values = {int(c) for c in row['cpus'] if str(c).isdigit()}
                if values:
                    nodes[int(row['node'])] = values
    for path, text in snapshot.items():
        match = re.search(r'(?:^|/)sysfs/node/node(\d+)/cpulist$', path)
        if match and cpus(text.strip()):
            nodes[int(match[1])] = cpus(text.strip())
    lscpu_nodes = defaultdict(set)
    for line in _get(snapshot, 'lscpu_parse.txt').splitlines():
        fields = line.strip().split(',')
        if len(fields) >= 5 and all(x.isdigit() for x in fields[:4]):
            cpu, core, socket, node = map(int, fields[:4])
            if fields[4].upper() in ('Y', 'YES', '1'):
                lscpu_nodes[node].add(cpu)
                cores[cpu] = (socket, node, core)
    for node, values in lscpu_nodes.items():
        if not nodes.get(node):
            nodes[node] = values
    # A CPU mapped to two NUMA nodes is ambiguous evidence, never a binding input.
    duplicates = {c for n, values in nodes.items() for c in values
                  if sum(c in other for other in nodes.values()) > 1}
    for values in nodes.values():
        values.difference_update(duplicates)
    return affinity, dict(nodes), cores


def _device_mapping(snapshot):
    by_npu, errors, sources = {}, [], []
    for name, parser in [('npu_smi_topology.txt', parse_npu_topology), ('npu_smi_map.txt', parse_npu_mapping)]:
        text = _get(snapshot, name)
        if not text:
            continue
        sources.append('hostbound/' + name)
        parsed = parser(text)
        errors.extend(parsed['errors'])
        for row in parsed['devices']:
            item = by_npu.setdefault(row['npu'], {'cpus': set(), 'sources': [], 'conflict': False})
            values = cpus(row['cpu_affinity'])
            item['sources'].append('hostbound/%s:%d' % (name, row['source_line']))
            if values:
                if item['cpus'] and item['cpus'] != values:
                    item['conflict'] = True
                item['cpus'] = values
    for npu, item in by_npu.items():
        if item['conflict']:
            item['cpus'] = set()
            errors.append('NPU%d explicit CPU mappings conflict; no mapping selected' % npu)
    return {'by_npu': by_npu, 'errors': errors, 'sources': sources,
            'status': 'AVAILABLE' if any(r['cpus'] for r in by_npu.values()) else ('PARSE_FAILED' if errors else 'UNAVAILABLE')}


def _processes(text):
    result = defaultdict(dict)
    for row in parse_npu_info(text)['processes']:
        npu, chip, pid = row['npu'], row['chip'], row['pid']
        proc = result[npu].setdefault(pid, {'pid': pid, 'name': row['name'], 'memory': None,
            'chip': chip, 'chips': [], 'memory_by_chip': {}, 'pid_namespace': row['pid_namespace'], 'source_lines': []})
        proc['source_lines'].append(row['source_line'])
        if chip is not None:
            proc['memory_by_chip'][chip] = row['memory']
            proc['chips'] = sorted(proc['memory_by_chip'])
        if row['memory'] is not None:
            proc['memory'] = sum(v for v in proc['memory_by_chip'].values() if v is not None) if chip is not None else row['memory']
        proc['chip'] = proc['chips'][0] if len(proc['chips']) == 1 else ','.join(map(str, proc['chips']))
    return result


def _pid_evidence(snapshot, pid, m):
    status_text = _get(snapshot, 'pid_%d_status_end.txt' % pid) or _get(snapshot, 'pid_%d_status.txt' % pid)
    status = dict(re.findall(r'^(\w+):[^\S\n]*(.*?)$', status_text, re.M))
    allowed = {}
    for tid, text in re.findall(r"pid\s+(\d+)'s current affinity list:\s*([\d,\-]+)", _get(snapshot, 'pid_%d_thread_affinity.txt' % pid)):
        allowed[int(tid)] = cpus(text)
    tids, psr = set(), set()
    for line in _get(snapshot, 'pid_%d_threads.txt' % pid).splitlines():
        fields = line.split()
        if len(fields) >= 3 and all(x.isdigit() for x in fields[:3]) and int(fields[0]) == pid:
            tids.add(int(fields[1]))
            psr.add(int(fields[2]))
    batches = [process_batch(snapshot, stage) for stage in ('start', 'end')]
    process_rows = [[p for p in batch.get('processes', []) if int(p.get('tgid', -1)) == pid] for batch in batches]
    current_process = (process_rows[1] or process_rows[0] or [{}])[0]
    if current_process:
        status.setdefault('Pid', str(pid))
        status.setdefault('Tgid', str(pid))
        status.setdefault('Mems_allowed_list', current_process.get('mems_allowed_list') or '')
        if current_process.get('threads'):
            status.setdefault('Threads', str(current_process['threads']))
    thread_rows = [dict(t) for t in batches[1].get('threads', []) if int(t.get('tgid', -1)) == pid]
    if not thread_rows:
        thread_rows = [dict(t) for t in batches[0].get('threads', []) if int(t.get('tgid', -1)) == pid]
    stamps = [str(rows[0].get('starttime', '')) for rows in process_rows if len(rows) == 1]
    raw_stamp = (_get(snapshot, 'pid_%d_starttime_end.txt' % pid) or _get(snapshot, 'pid_%d_starttime.txt' % pid)).strip()
    before_stamp = _get(snapshot, 'pid_%d_starttime_start.txt' % pid).strip()
    if raw_stamp:
        stamps.append(raw_stamp)
    if before_stamp:
        stamps.append(before_stamp)
    valid_stamps = [s for s in stamps if s.isdigit()]
    ambiguous = len(set(valid_stamps)) > 1 or pid in m.get('process_analysis', {}).get('unsafe_npu_pids', []) or any(t.get('identity_ambiguous') for t in m.get('process_analysis', {}).get('tasks', []) if t.get('tgid') == pid)
    stable = not ambiguous and len(valid_stamps) >= 2 and (all(process_rows) or bool(before_stamp and raw_stamp))
    thread_cpuset, thread_stamps = {}, {}
    for t in thread_rows:
        tid = int(t['tid'])
        tids.add(tid)
        if t.get('allowed_cpus') is not None:
            allowed[tid] = set(t['allowed_cpus'])
        raw_set = t.get('cpuset_cpus_effective', t.get('cpuset_cpus', ''))
        thread_cpuset[tid] = set(raw_set) if isinstance(raw_set, list) else cpus(raw_set)
        if str(t.get('starttime', '')).isdigit():
            thread_stamps[tid] = str(t['starttime'])
        if isinstance(t.get('last_cpu'), int) and t['last_cpu'] >= 0:
            psr.add(t['last_cpu'])
    trace = set()
    if not ambiguous:
        resolved = m.get('thread_metrics', [])
        seen_tids = {t['tid'] for t in resolved}
        for task in resolved:
            # TIDs can be recycled within the window. The resolved generation
            # carries its own TGID; the legacy numeric-TID union does not.
            if task.get('tgid') == pid and not task.get('identity_ambiguous'):
                process_identity = task.get('process_identity')
                expected_identity = '%d:%s' % (pid, valid_stamps[-1] if valid_stamps else 'unknown')
                if process_identity and valid_stamps and process_identity != expected_identity:
                    continue
                trace.update(task.get('cpus', []))
                trace.update((m.get('tasks') or {}).get('observed_cpus_by_identity', {}).get(task['identity'], []))
        for tid, values in (m.get('tasks') or {}).get('observed_cpus_by_tid', {}).items():
            if tid not in seen_tids and (tid == pid or tid in tids):
                trace.update(values)  # old custom metrics without identity rows
    complete = bool(tids and tids <= set(allowed))
    if status.get('Threads', '').isdigit():
        complete = complete and len(tids) == int(status['Threads'])
    complete = complete and bool(allowed) and all(allowed.values())
    starttime = valid_stamps[-1] if valid_stamps else ''
    namespaces = [p.get('namespace_pids', []) for rows in process_rows for p in rows]
    namespaces += [t.get('namespace_pids', []) for t in thread_rows if t['tid'] == pid]
    namespace_ok = not any(values and int(values[0]) != pid for values in namespaces)
    namespace_ok = namespace_ok and not any(t.get('namespace_pids') and t['namespace_pids'][0] != t['tid'] for t in thread_rows)
    namespace_ok = namespace_ok and pid not in m.get('process_analysis', {}).get('unsafe_npu_pids', [])
    for value in status.get('NSpid', '').split()[:1]:
        namespace_ok = namespace_ok and value == str(pid)
    process_cpuset = current_process.get('cpuset_cpus_effective')
    return {'status': status, 'allowed': allowed, 'tids': tids, 'psr': psr, 'trace': trace,
            'complete': complete, 'cpuset': set(process_cpuset) if isinstance(process_cpuset, list) else cpus(_get(snapshot, 'pid_%d_cpuset_effective.txt' % pid)),
            'starttime': starttime, 'thread_cpuset': thread_cpuset, 'thread_starttime': thread_stamps,
            'thread_rows': thread_rows, 'identity_status': 'AMBIGUOUS' if ambiguous else ('VERIFIED' if stable and namespace_ok else 'UNVERIFIED'),
            'namespace_ok': namespace_ok}


def _memory_locality(snapshot, pid, ev, local_nodes):
    pages, kb, sources, mb = defaultdict(int), defaultdict(int), [], {}
    raw = _get(snapshot, 'pid_%d_numa_maps.txt' % pid)
    if raw:
        sources.append('hostbound/pid_%d_numa_maps.txt' % pid)
        for line in raw.splitlines():
            size = re.search(r'\bkernelpagesize_kB=(\d+)', line)
            for node, count in re.findall(r'\bN(\d+)=(\d+)', line):
                pages[int(node)] += int(count)
                if size:
                    kb[int(node)] += int(count) * int(size[1])
    if not pages:
        batch = process_batch(snapshot, 'end')
        for p in batch.get('processes', []):
            if int(p.get('tgid', -1)) == pid:
                summary = p.get('numa_maps') or {}
                if not isinstance(summary, dict):
                    continue
                for field, destination in [('node_pages', pages), ('node_kb', kb)]:
                    values = summary.get(field, {})
                    if isinstance(values, dict):
                        destination.update({int(n): v for n, v in values.items() if str(n).isdigit() and isinstance(v, (int, float)) and v >= 0})
                if pages:
                    sources.append('hostbound/process_end.json:numa_maps')
    if not pages:
        raw_stat = _get(snapshot, 'pid_%d_numastat.txt' % pid)
        header = []
        for line in raw_stat.splitlines():
            if re.search(r'Node\s+\d+', line):
                header = [int(n) for n in re.findall(r'Node\s+(\d+)', line)]
            elif header and re.match(r'^\s*Total\s+', line, re.I):
                values = line.split()[1:]
                try:
                    if len(values) >= len(header):
                        mb = dict(zip(header, [float(v) for v in values[:len(header)]]))
                        sources.append('hostbound/pid_%d_numastat.txt' % pid)
                except ValueError:
                    pass
    actual = sorted(n for n, count in (pages or mb).items() if count > 0)
    local = set(local_nodes)
    status = 'UNKNOWN' if not actual or not local else ('LOCAL' if set(actual) <= local else ('REMOTE' if set(actual).isdisjoint(local) else 'MIXED'))
    return {'allowed_nodes': sorted(cpus(ev['status'].get('Mems_allowed_list', ''))),
            'actual_pages_by_node': dict(pages), 'actual_kb_by_node': dict(kb), 'actual_nodes': actual,
            'actual_mb_by_node': mb,
            'thread_allowed_nodes': {t['tid']: sorted(cpus(t.get('mems_allowed_list', ''))) for t in ev.get('thread_rows', [])},
            'source': sources, 'status': status, 'remote_evidence': False,
            'placement_mismatch': bool(actual and local and set(actual) - local),
            'note': 'Mems_allowed 是许可范围；numa_maps 是已分配页位置，不证明远程访存流量；taskset 不迁移内存页。'}


def analyze_npu_affinity(m):
    snapshot = m.get('snapshot', {})
    mapping = _device_mapping(snapshot)
    affinity, nodes, cores = _topology(snapshot)
    parsed_info = parse_npu_info(_get(snapshot, 'npu_smi_info.txt'))
    processes = _processes(_get(snapshot, 'npu_smi_info.txt'))
    online = cpus(_get(snapshot, 'cpu_online.txt'))
    context = dict(re.findall(r'^([a-z_]+)=(.*)$', _get(snapshot, 'host_context.txt'), re.M))
    rows, notes = [], []
    for npu in sorted(set(affinity) | set(processes) | set(parsed_info['devices'])):
        pool = affinity.get(npu, set())
        local_nodes = sorted(n for n, values in nodes.items() if pool & values)
        procs = sorted(processes.get(npu, {}).values(), key=lambda p: (-(p['memory'] or 0), p['pid']))
        for i, proc in enumerate(procs or [{'pid': None, 'name': '—', 'memory': None, 'chip': None}]):
            pid = proc['pid']
            ev = _pid_evidence(snapshot, pid, m) if pid else {}
            permitted = set().union(*ev.get('allowed', {}).values()) if ev else set()
            observed = ev.get('trace', set()) | ev.get('psr', set())
            observed_numa = sorted(n for n, values in nodes.items() if values & observed)
            known = set().union(*nodes.values()) if nodes else set()
            locality = ('LOCAL_OBSERVED' if set(observed_numa) <= set(local_nodes) else 'REMOTE_OBSERVED') if observed and not observed - known and local_nodes else 'UNKNOWN'
            if not pid:
                state = 'NO_PROCESS'
            elif not pool or not ev['complete'] or len(local_nodes) != 1 or ev['identity_status'] == 'AMBIGUOUS':
                state = 'UNKNOWN'
            elif observed - pool:
                state = 'REMOTE_OBSERVED'
            elif permitted and permitted <= pool:
                state = 'LOCAL'
            elif permitted.isdisjoint(pool):
                state = 'REMOTE_ALLOWED'
            else:
                state = 'UNCONFINED'
            blockers = []
            candidate = pool & online & nodes.get(local_nodes[0], set()) if len(local_nodes) == 1 else set()
            if pid:
                if context.get('execution_context') != 'host':
                    blockers.append('未确认在物理机/宿主机采集')
                if not ev['complete']:
                    blockers.append('全量 TID/线程 affinity 快照不完整')
                if ev['status'].get('Tgid') != str(pid) or ev['status'].get('Pid') != str(pid) or not ev['namespace_ok']:
                    blockers.append('宿主机 PID/TGID 或 PID namespace 对应关系未核验')
                if ev['identity_status'] != 'VERIFIED':
                    blockers.append('进程采集前后身份未核验或 PID 已复用')
                if not ev['starttime'] or not context.get('boot_id'):
                    blockers.append('缺少进程 starttime 或主机 boot_id')
                if ev['cpuset']:
                    candidate &= ev['cpuset']
                else:
                    candidate = set()
                for tid in ev['tids']:
                    if not ev['thread_cpuset'].get(tid) or not ev['thread_starttime'].get(tid):
                        blockers.append('TID%d 缺少有效 cpuset 或 starttime' % tid)
                        candidate = set()
                    else:
                        candidate &= ev['thread_cpuset'][tid]
                if not candidate:
                    blockers.append('亲和 NUMA/在线 CPU/全 TID 有效 cpuset 交集不可用')
            sources = mapping['by_npu'].get(npu, {}).get('sources', []) + ['hostbound/npu_smi_info.txt', 'process_start/end.json', 'pid_*_status/thread_affinity/threads/cpuset_effective.txt', 'ftrace']
            row = dict(proc, npu=npu, role='主进程（显存占用最高）' if i == 0 and pid else '其他占卡进程',
                       numa=local_nodes, affinity=cpu_list(pool), allowed=cpu_list(permitted),
                       observed=cpu_list(observed), trace=cpu_list(ev.get('trace', set())),
                       observed_numa=observed_numa, numa_locality=locality,
                       allowed_numa=sorted(n for n, values in nodes.items() if values & permitted),
                       psr=cpu_list(ev.get('psr', set())), state=state, target='', blockers=blockers,
                       threads=len(ev.get('tids', [])), evidence=ev, sources=sources,
                       identity_status=ev.get('identity_status', 'UNAVAILABLE'),
                       mapping_status='AVAILABLE' if pool else 'UNKNOWN',
                       current_cpu=cpu_list(permitted), current_numa=observed_numa,
                       recommended_cpu='', recommended_numa=[], recommendation_status='INSUFFICIENT_EVIDENCE',
                       reasons=[], risks=['调整 affinity 会改变调度资源；需同负载灰度验证，收益尚未测量。',
                                          'taskset 只约束目标线程，不排除其他进程或 IRQ 使用这些 CPU。'],
                       expected_improvement='待验证：减少范围外运行或已观测同核等待；不估算未经测量的吞吐收益。',
                       verification=['逐 TID taskset -pc 与有效 cpuset 核验', '同负载复采比较 runnable wait P99、吞吐/时延和 Device 空泡'],
                       rollback='执行脚本逐 TID 保存原 affinity/starttime；在同宿主机运行备份目录 rollback.sh。',
                       thread_rows=ev.get('thread_rows', []), _candidate=candidate,
                       memory_locality=_memory_locality(snapshot, pid, ev, local_nodes) if pid else {})
            rows.append(row)
    pid_rows = defaultdict(list)
    for row in rows:
        if row['pid']:
            pid_rows[row['pid']].append(row)
    for group in pid_rows.values():
        if len(group) > 1:
            for row in group:
                row['blockers'].append('同一 PID 使用多张 NPU；缺少 TID/rank 到卡的分工证据')
                row['_candidate'] = set()
    active = [r for r in rows if r['pid'] and r['_candidate']]
    # Connected overlapping CPU pools are allocation domains; arbitrary partial overlaps are not guessed.
    remaining = list(active)
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for row in list(remaining):
                if any(row['_candidate'] & other['_candidate'] for other in group):
                    group.append(row); remaining.remove(row); changed = True
        pool = group[0]['_candidate']
        preserved = []
        for row in group:
            allowed = row['evidence']['allowed']
            if allowed and all(values and values <= row['_candidate'] for values in allowed.values()):
                row['target'] = row['allowed']
                row['recommendation_status'] = 'PRESERVE'
                row['reasons'].append('全 TID 当前许可范围已在实测亲和范围及各自 cpuset 内，保留各线程原掩码。')
                preserved.append(row)
        pending = [r for r in group if r not in preserved]
        if not pending:
            continue
        if any(r['_candidate'] != pool for r in group):
            for row in pending:
                row['blockers'].append('工作进程可用 CPU 部分重叠，缺少可核验的分配策略')
            continue
        units = defaultdict(set)
        for cpu in pool:
            if cpu in cores:
                units[cores[cpu]].add(cpu)
        # Do not split online SMT siblings, including siblings excluded by cpuset.
        all_units = defaultdict(set)
        for cpu, key in cores.items():
            if cpu in online:
                all_units[key].add(cpu)
        available = [values for key, values in sorted(units.items(), key=lambda item: min(item[1])) if values == all_units[key]]
        occupied = set().union(*(cpus(r['allowed']) for r in preserved)) if preserved else set()
        available = [u for u in available if not u & occupied]
        if len(available) < len(pending):
            for row in pending:
                row['blockers'].append('缺少完整且互斥的在线物理核心/SMT 同胞组')
            continue
        weights = []
        for row in pending:
            ts = [t for t in m.get('thread_metrics', []) if t.get('tgid') == row['pid'] and not t.get('identity_ambiguous')]
            runtime = sum(t.get('runtime_sec') or 0 for t in ts)
            wait = sum(t.get('runqueue_wait_sec') or 0 for t in ts)
            weights.append(runtime + wait)
        if len(group) > 1 and (not all(w > 0 for w in weights) or not m.get('scheduler_quality', {}).get('complete')):
            for row in pending:
                row['blockers'].append('共享亲和 CPU 缺少各工作进程完整 runtime/等待负载证据，不能按卡数平均分区')
            continue
        counts = [1] * len(pending)
        extra = len(available) - len(pending)
        total = sum(weights)
        fractions = [extra * w / total if total else extra / len(pending) for w in weights]
        for i, value in enumerate(fractions):
            counts[i] += int(math.floor(value))
        left = len(available) - sum(counts)
        for i in sorted(range(len(pending)), key=lambda i: (-(fractions[i] % 1), pending[i]['pid']))[:left]:
            counts[i] += 1
        offset = 0
        for row, count in zip(pending, counts):
            target = set().union(*available[offset:offset + count]); offset += count
            row['target'] = cpu_list(target)
            row['recommendation_status'] = 'PROPOSED' if not row['blockers'] else 'BLOCKED'
            row['reasons'].append('仅使用显式 NPU CPU 映射与全 TID cpuset 交集；完整 SMT 核心组互斥。')
            if len(group) > 1:
                row['reasons'].append('按已观测进程 runtime + runnable wait 权重分配核心组；分配需业务灰度验证。')
    # Pools that look disjoint at logical CPU level may still share one physical core.
    owners = defaultdict(list)
    for row in rows:
        for key in {cores[c] for c in cpus(row['target']) if c in cores}:
            owners[key].append(row)
    for owners_rows in owners.values():
        if len({r['pid'] for r in owners_rows}) > 1:
            for row in owners_rows:
                row['blockers'].append('不同工作进程目标涉及同一物理核心的 SMT 同胞')
    actions = []
    for row in rows:
        row.pop('_candidate', None)
        row['blockers'] = list(dict.fromkeys(row['blockers']))
        row['recommended_cpu'] = row['target']
        row['recommended_numa'] = sorted(n for n, values in nodes.items() if values & cpus(row['target']))
        if row['blockers']:
            row['recommendation_status'] = 'BLOCKED' if row['target'] else 'INSUFFICIENT_EVIDENCE'
        if row['target'] and not row['blockers'] and row['recommendation_status'] != 'PRESERVE':
            actions.append(row)
        if not row['reasons']:
            row['reasons'] = ['当前证据不足以提出安全的具体 CPU 掩码。']
    notes.append('不按卡数或 NUMA 节点等分 CPU；不默认预留系统核。共享范围只有完整运行/等待证据支持时才提出互斥核心组方案。')
    return {'rows': rows, 'notes': notes, 'host_context': context,
            'commands': _binding_script(actions, context) if actions else '',
            'mapping': {k: v for k, v in mapping.items() if k != 'by_npu'},
            'process_parser': {k: v for k, v in parsed_info.items() if k != 'processes'},
            'coverage': '显式 NPU CPU 映射/宿主机进程身份/全线程 affinity 与 cpuset/实际 NUMA 页分布/运行与等待证据'}

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
    lines += ['thread_ids() { local p=$1 task; for task in /proc/"$p"/task/*; do printf "%s\\n" "${task##*/}"; done | sort -n | paste -sd " " -; }',
              '# 先暂停业务层线程创建；脚本发现线程集合变化会停止并回滚已保存的线程。']
    for row in actions:
        pid, target, ev = row['pid'], row['target'], row['evidence']
        expected_tids = ' '.join(str(t) for t in sorted(ev['tids']))
        lines += ["[ \"$(starttime %d)\" = '%s' ] || { echo 'PID %d 已变化，请重新采集'; exit 1; }" % (pid, ev['starttime'], pid),
                  "[ \"$(thread_ids %d)\" = '%s' ] || { echo '线程集合已变化，请重新核验'; exit 1; }" % (pid, expected_tids),
                  "check_cpu_list '%s' \"$(cat /sys/devices/system/cpu/online)\"" % target,
                  "check_cpu_list '%s' \"$(effective_cpuset %d)\" || { echo 'PID %d 的 cpuset 不允许目标 CPU'; exit 1; }" % (target, pid, pid)]
        for tid in sorted(ev['tids']):
            lines += ["[ \"$(starttime %d)\" = '%s' ] || { echo 'TID %d 已变化'; exit 1; }" % (tid, ev['thread_starttime'][tid], tid),
                      "check_cpu_list '%s' \"$(effective_cpuset %d)\" || { echo 'TID %d 的 cpuset 不允许目标 CPU'; exit 1; }" % (target, tid, tid),
                      'taskset -pc %d >> "$backup/before.txt"' % tid,
                      "old=$(awk '/^Cpus_allowed_list:/ {print $2}' /proc/%d/task/%d/status)" % (pid, tid),
                      "[ \"$old\" = '%s' ] || { echo 'TID %d affinity 已变化，请重新核验'; exit 1; }" % (cpu_list(ev['allowed'][tid]), tid),
                      "printf 'restore_tid %d %s %%s\\n' \"$old\" >> \"$backup/rollback.sh\"" % (tid, ev['thread_starttime'][tid])]
    lines += ["printf '%s\\n' 'exit \"$failed\"' >> \"$backup/rollback.sh\"",
              'on_error() { local rc=$?; trap - EXIT INT TERM; echo "操作未完成，回滚已保存线程；检查 $backup"; bash "$backup/rollback.sh" || true; exit "$rc"; }',
              'trap on_error EXIT', 'trap \'exit 130\' INT', 'trap \'exit 143\' TERM',
              '# 逐 TID 应用，避免 -a 将未备份的新线程一并改动。']
    for row in actions:
        lines.append("[ \"$(starttime %d)\" = '%s' ] || { echo 'PID 已变化，请检查并按需回滚'; exit 1; }" % (row['pid'], row['evidence']['starttime']))
        lines.append("[ \"$(thread_ids %d)\" = '%s' ] || { echo '线程集合已变化'; false; }" % (row['pid'], ' '.join(str(t) for t in sorted(row['evidence']['tids']))))
        for tid in sorted(row['evidence']['tids']):
            lines += ["[ \"$(starttime %d)\" = '%s' ] || { echo 'TID 已复用'; false; }" % (tid, row['evidence']['thread_starttime'][tid]),
                      "check_cpu_list '%s' \"$(cat /sys/devices/system/cpu/online)\"" % row['target'],
                      "check_cpu_list '%s' \"$(effective_cpuset %d)\"" % (row['target'], tid),
                      'taskset -pc %s %d' % (row['target'], tid)]
    lines += ['# 验证所有线程的 allowed CPU，保存执行后快照。']
    for row in actions:
        pid, target = row['pid'], row['target']
        lines += ["[ \"$(starttime %d)\" = '%s' ] || { echo '执行期间 PID 已复用'; false; }" % (pid, row['evidence']['starttime'])]
        for tid in sorted(row['evidence']['tids']):
            lines.append("[ \"$(starttime %d)\" = '%s' ] || { echo '执行期间 TID 已复用'; false; }" % (tid, row['evidence']['thread_starttime'][tid]))
        lines += ['taskset -apc %d | tee -a "$backup/after.txt"' % pid,
                  "[ \"$(thread_ids %d)\" = '%s' ] || { echo '执行期间线程集合变化，原线程回滚后需核验新线程'; false; }" % (pid, ' '.join(str(t) for t in sorted(row['evidence']['tids']))),
                  'for task in /proc/%d/task/*; do' % pid,
                  "  actual=$(awk '/^Cpus_allowed_list:/ {print $2}' \"$task/status\")",
                  "  [ \"$actual\" = '%s' ] || { echo '线程 affinity 与目标不一致'; false; }" % target, 'done']
    lines += ['trap - EXIT INT TERM', 'echo "如需回滚，在本宿主机运行：sudo bash $backup/rollback.sh"', 'HOST_CPU_BIND']
    return '\n'.join(lines)
