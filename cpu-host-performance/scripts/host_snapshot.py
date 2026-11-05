#!/usr/bin/env python3
"""Read-only Linux sidecars for cpu_trace_collect.sh; Python 3.7+, stdlib.

Can be tested with fake --sys-root/--proc-root. Never writes proc/sys/cgroup.
"""
import argparse
import json
import os
import re
import time
from pathlib import Path


def read(path):
    try:
        return Path(path).read_text(errors='replace').strip()
    except OSError:
        return None


def read_failure(path):
    """Explain a failed optional read without flattening permission and exit."""
    try:
        Path(path).read_text(errors='replace')
        return {'status': 'PARSE_FAILED', 'failure_reason': 'malformed_or_changed_file'}
    except PermissionError as exc:
        return {'status': 'PERMISSION_DENIED', 'failure_reason': str(exc)}
    except FileNotFoundError as exc:
        return {'status': 'EXECUTION_FAILED', 'failure_reason': 'process_exited_or_file_missing: ' + str(exc)}
    except OSError as exc:
        return {'status': 'EXECUTION_FAILED', 'failure_reason': str(exc)}


def cpus(text):
    out = set()
    for token in (text or '').replace(',', ' ').split():
        try:
            a, b = (token.split('-', 1) + [token])[:2] if '-' in token else (token, token)
            out.update(range(int(a), int(b)+1))
        except ValueError:
            pass
    return sorted(out)


def frequency_snapshot(sys_root='/sys'):
    root = Path(sys_root) / 'devices/system/cpu'
    paths = sorted((root / 'cpufreq').glob('policy*'))
    if not paths:
        paths = sorted(root.glob('cpu[0-9]*/cpufreq'))
    policies, seen = [], set()
    for path in paths:
        related = read(path / 'related_cpus') or read(path / 'affected_cpus')
        related_cpus = cpus(related)
        if not related_cpus and path.parent.name.startswith('cpu') and path.parent.name[3:].isdigit():
            related_cpus = [int(path.parent.name[3:])]
        # Symlink aliases and related_cpus represent a single clock domain.
        identity = tuple(related_cpus) or str(path.resolve())
        if identity in seen:
            continue
        seen.add(identity)
        row = {'id': path.name if path.name.startswith('policy') else 'cpus_' + '_'.join(map(str, related_cpus)),
               'cpus': related_cpus, 'path': str(path), 'unit': 'kHz', 'errors': {}}
        for key in ('related_cpus', 'scaling_cur_freq', 'cpuinfo_cur_freq', 'cpuinfo_max_freq',
                    'scaling_min_freq', 'scaling_max_freq', 'scaling_governor'):
            value = read(path / key)
            if value is None:
                row['errors'][key] = 'missing_or_unreadable'
            else:
                row[key] = int(value) if value.isdigit() and key not in ('related_cpus', 'scaling_governor') else value
        policies.append(row)
    return {'schema_version': 2, 'ts': time.monotonic(), 'policies': policies,
            'capability': 'available' if policies else 'unsupported_or_unreadable'}


def topology_snapshot(sys_root='/sys'):
    """Keep actual sysfs node IDs and distance ordering; never invent a node map."""
    base = Path(sys_root) / 'devices/system'
    nodes = []
    for path in sorted((base / 'node').glob('node[0-9]*')):
        if not path.name[4:].isdigit():
            continue
        row = {'node': int(path.name[4:]), 'cpus': cpus(read(path / 'cpulist')),
               'distance': [], 'memory_kb': {}, 'errors': {}}
        for key in ('cpulist', 'distance', 'meminfo', 'numastat', 'vmstat'):
            value = read(path / key)
            row[key + '_raw'] = value
            if value is None:
                row['errors'][key] = 'missing_or_unreadable'
        try:
            row['distance'] = [int(x) for x in (row['distance_raw'] or '').split()]
        except ValueError:
            row['errors']['distance'] = 'parse_failed'
        for key, value in re.findall(r'Node\s+\d+\s+(\w+):\s+(\d+)\s+kB', row['meminfo_raw'] or ''):
            row['memory_kb'][key] = int(value)
        nodes.append(row)
    return {'schema_version': 1, 'ts': time.monotonic(),
            'online_cpus': cpus(read(base / 'cpu/online')),
            'online_nodes': cpus(read(base / 'node/online')),
            'nodes': nodes, 'distance_node_order': cpus(read(base / 'node/online'))}


def idle_snapshot(sys_root='/sys'):
    states = []
    for path in sorted((Path(sys_root) / 'devices/system/cpu').glob('cpu[0-9]*/cpuidle/state*')):
        row = {'cpu': int(path.parent.parent.name[3:]), 'state': path.name, 'errors': {}}
        for key in ('name', 'desc', 'disable', 'latency', 'residency', 'time', 'usage'):
            value = read(path / key)
            if value is None:
                row['errors'][key] = 'missing_or_unreadable'
            else:
                row[key] = int(value) if value.isdigit() else value
        states.append(row)
    return {'schema_version': 1, 'ts': time.monotonic(), 'states': states,
            'capability': 'available' if states else 'unsupported_or_unreadable',
            'time_unit': 'microseconds', 'usage_unit': 'entries'}


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')


def parse_stat(text):
    if not text or ')' not in text:
        return None
    fields = text[text.rfind(')')+2:].split()
    try:
        return {'state': fields[0], 'ppid':int(fields[1]), 'flags':int(fields[6]),
                'utime_ticks':int(fields[11]), 'stime_ticks':int(fields[12]),
                'starttime':int(fields[19]), 'last_cpu':int(fields[36])}
    except (ValueError, IndexError):
        return None


def cgroup_snapshot(proc_cgroup, sys_root):
    rows=[]
    base=Path(sys_root) / 'fs/cgroup'
    for line in (proc_cgroup or '').splitlines():
        parts=line.split(':',2)
        if len(parts)!=3:
            continue
        hierarchy, controllers, rel=parts
        if hierarchy=='0':
            root=base
            version=2
        elif 'cpu' in controllers.split(',') or 'cpuset' in controllers.split(','):
            if 'cpu' in controllers.split(','):
                candidates = (base / 'cpu', base / 'cpu,cpuacct', base / 'cpuacct,cpu')
            else:
                candidates = (base / 'cpuset',)
            root=next((x for x in candidates if x.is_dir()), candidates[0])
            version=1
        else:
            continue
        path=(root / rel.lstrip('/')).resolve()
        root=root.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        while True:
            row={'path':str(path), 'version':version, 'controllers':controllers or 'unified'}
            for key in ('cpu.max','cpu.stat','cpu.cfs_quota_us','cpu.cfs_period_us',
                        'cpuset.cpus.effective','cpuset.mems.effective','cpuset.effective_cpus',
                        'cpuset.effective_mems','cpuset.cpus','cpuset.mems'):
                value=read(path/key)
                if value is not None:
                    row[key]=value
            rows.append(row)
            if path==root:
                break
            path=path.parent
    return rows


def status_fields(text):
    return dict(re.findall(r'^(\w+):[^\S\n]*(.*?)$', text or '', re.M))


def namespace_pids(fields):
    return [int(x) for x in fields.get('NSpid', '').split() if x.isdigit()]


def summarize_numa_maps(text):
    """Convert pages using each mapping's page size, not the host default."""
    if text is None:
        return {'available': False, 'node_pages': {}, 'node_kb': {}, 'unknown_page_size_pages': {}, 'mapping_count': 0}
    pages, kb, unknown, policies = {}, {}, {}, {}
    count = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        count += 1
        policy = fields[1]
        policies[policy] = policies.get(policy, 0) + 1
        size = re.search(r'\bkernelpagesize_kB=(\d+)\b', line)
        for node, value in re.findall(r'\bN(\d+)=(\d+)\b', line):
            value = int(value); pages[node] = pages.get(node, 0) + value
            if size:
                kb[node] = kb.get(node, 0) + value * int(size.group(1))
            else:
                unknown[node] = unknown.get(node, 0) + value
    return {'available': True, 'node_pages': pages, 'node_kb': kb,
            'unknown_page_size_pages': unknown, 'mapping_count': count, 'policies': policies}


def effective_cpuset(rows, kind='cpus'):
    # Only a readable empty v1 configured cpuset establishes inheritance.
    # Missing leaf files do not establish that a broader ancestor is effective.
    applicable = [row for row in rows if row.get('version') == 2 or
                  'cpuset' in row.get('controllers', '').split(',')]
    for row in applicable:
        for key in ('cpuset.%s.effective' % kind, 'cpuset.effective_%s' % kind):
            if key in row:
                return cpus(row[key])
        if row.get('version') == 2:
            return None
        configured = row.get('cpuset.%s' % kind)
        if configured is None:
            return None
        if configured:
            return cpus(configured)
    return None


def process_snapshot(proc_root='/proc', sys_root='/sys', pids=None):
    processes, threads, errors = [], [], []
    cache={}
    begin=time.monotonic()
    paths = ([Path(proc_root) / str(p) for p in pids] if pids is not None else sorted(Path(proc_root).glob('[0-9]*')))
    for path in paths:
        if not path.name.isdigit():
            continue
        identity_started = time.monotonic()
        stat=parse_stat(read(path/'stat'))
        if not stat:
            errors.append(dict(read_failure(path/'stat'), pid=path.name, reason='stat_unreadable_or_exited'))
            continue
        tgid=int(path.name)
        cg=read(path/'cgroup')
        if cg not in cache:
            cache[cg]=cgroup_snapshot(cg,sys_root)
        cmd=read(path/'cmdline')
        fields = status_fields(read(path/'status'))
        ticks=os.sysconf('SC_CLK_TCK') if hasattr(os,'sysconf') else None
        process=dict(stat,tgid=tgid,pid=tgid,name=read(path/'comm'),cmdline=(cmd or '').replace('\x00',' '),
                     kernel_thread=bool(stat['flags'] & 0x00200000), cgroup=cg,
                     cgroup_cpu=cache[cg], cpuset_cpus_effective=effective_cpuset(cache[cg]),
                     cpuset_mems_effective=effective_cpuset(cache[cg], 'mems'),
                     namespace_pids=namespace_pids(fields), allowed_cpus=cpus(fields.get('Cpus_allowed_list')) if 'Cpus_allowed_list' in fields else None,
                     cpus_allowed_list=fields.get('Cpus_allowed_list'), mems_allowed_list=fields.get('Mems_allowed_list'),
                     numa_maps=summarize_numa_maps(read(path/'numa_maps')),
                     identity_started_at=identity_started,
                     cpu_time_sec=(stat['utime_ticks']+stat['stime_ticks'])/ticks if ticks else None)
        process_threads = []
        for task in sorted(path.glob('task/[0-9]*')):
            tid_started = time.monotonic()
            tstat=parse_stat(read(task/'stat'))
            status=read(task/'status')
            if not tstat or not status:
                errors.append(dict(read_failure(task/('stat' if not tstat else 'status')), tid=task.name, reason='thread_exited_or_permission'))
                continue
            fields=status_fields(status)
            if str(fields.get('Tgid',tgid)) != str(tgid):
                continue
            task_cg = read(task / 'cgroup')
            # Older procfs fixtures/kernels may omit task/cgroup; keep the
            # fallback visible instead of pretending a thread-specific read.
            cgroup_source = 'task' if task_cg is not None else 'leader_fallback'
            task_cg = task_cg if task_cg is not None else cg
            if task_cg not in cache:
                cache[task_cg] = cgroup_snapshot(task_cg, sys_root)
            thread=dict(tstat,tid=int(task.name),tgid=tgid,name=fields.get('Name'),
                        allowed_cpus=cpus(fields.get('Cpus_allowed_list')) if 'Cpus_allowed_list' in fields else None,
                        mems_allowed_list=fields.get('Mems_allowed_list'),cgroup=task_cg,cpus_allowed_list=fields.get('Cpus_allowed_list'),
                        cgroup_cpu=cache[task_cg], cgroup_source=cgroup_source,
                        namespace_pids=namespace_pids(fields), cpuset_cpus_effective=effective_cpuset(cache[task_cg]) if cgroup_source == 'task' else None,
                        cpuset_mems_effective=effective_cpuset(cache[task_cg], 'mems') if cgroup_source == 'task' else None,
                        identity_started_at=tid_started)
            ticks=os.sysconf('SC_CLK_TCK') if hasattr(os,'sysconf') else None
            thread['cpu_time_sec']=(tstat['utime_ticks']+tstat['stime_ticks'])/ticks if ticks else None
            # task may exit or PID may be recycled during this read.
            check=parse_stat(read(task/'stat'))
            if not check or check['starttime']!=tstat['starttime']:
                errors.append({'tid':task.name,'reason':'identity_changed_during_snapshot'})
                continue
            thread['identity_verified_at'] = time.monotonic()
            process_threads.append(thread)
        # Verify the leader too: keeping threads from a recycled TGID is unsafe.
        check = parse_stat(read(path / 'stat'))
        if not check or check['starttime'] != stat['starttime']:
            errors.append({'pid': path.name, 'reason': 'identity_changed_during_snapshot'})
            continue
        process['identity_verified_at'] = time.monotonic()
        processes.append(process)
        threads.extend(process_threads)
    return {'schema_version':2,'ts':begin,'end_ts':time.monotonic(),'clock':'monotonic',
            'clock_ticks':os.sysconf('SC_CLK_TCK') if hasattr(os,'sysconf') else None,
            'processes':processes,'threads':threads,'errors':errors}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['frequency', 'poll', 'process'])
    ap.add_argument('--output', required=True)
    ap.add_argument('--sys-root', default='/sys')
    ap.add_argument('--proc-root', default='/proc')
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--stop-file')
    args = ap.parse_args()
    if args.command == 'frequency':
        write_json(args.output, frequency_snapshot(args.sys_root))
    elif args.command == 'process':
        write_json(args.output, process_snapshot(args.proc_root, args.sys_root))
    else:
        if args.interval < .2 or not args.stop_file:
            ap.error('poll requires --interval >= 0.2 and --stop-file')
        with open(args.output, 'a', encoding='utf-8') as stream:
            while not Path(args.stop_file).exists():
                row = frequency_snapshot(args.sys_root)
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
                time.sleep(args.interval)


if __name__ == '__main__':
    main()
