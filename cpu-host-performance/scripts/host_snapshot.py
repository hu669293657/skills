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
    return {'schema_version': 2, 'ts': time.monotonic(), 'policies': policies}


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
        elif 'cpu' in controllers.split(','):
            root=next((x for x in (base/'cpu', base/'cpu,cpuacct', base/'cpuacct,cpu') if x.is_dir()),base/'cpu')
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
            row={'path':str(path), 'version':version}
            for key in ('cpu.max','cpu.stat','cpu.cfs_quota_us','cpu.cfs_period_us','cpuset.cpus.effective'):
                value=read(path/key)
                if value is not None:
                    row[key]=value
            rows.append(row)
            if path==root:
                break
            path=path.parent
    return rows


def process_snapshot(proc_root='/proc', sys_root='/sys'):
    processes, threads, errors = [], [], []
    cache={}
    begin=time.monotonic()
    for path in sorted(Path(proc_root).glob('[0-9]*')):
        stat=parse_stat(read(path/'stat'))
        if not stat:
            errors.append({'pid':path.name,'reason':'stat_unreadable_or_exited'})
            continue
        tgid=int(path.name)
        cg=read(path/'cgroup')
        if cg not in cache:
            cache[cg]=cgroup_snapshot(cg,sys_root)
        cmd=read(path/'cmdline')
        process=dict(stat,tgid=tgid,name=read(path/'comm'),cmdline=(cmd or '').replace('\x00',' '),
                     kernel_thread=bool(stat['flags'] & 0x00200000), cgroup=cg,
                     cgroup_cpu=cache[cg])
        processes.append(process)
        for task in path.glob('task/[0-9]*'):
            tstat=parse_stat(read(task/'stat'))
            status=read(task/'status')
            if not tstat or not status:
                errors.append({'tid':task.name,'reason':'thread_exited_or_permission'})
                continue
            fields=dict(re.findall(r'^(\w+):\s*(.*?)$',status,re.M))
            if str(fields.get('Tgid',tgid)) != str(tgid):
                continue
            thread=dict(tstat,tid=int(task.name),tgid=tgid,name=fields.get('Name'),
                        allowed_cpus=cpus(fields.get('Cpus_allowed_list')) if 'Cpus_allowed_list' in fields else None,
                        mems_allowed_list=fields.get('Mems_allowed_list'),cgroup=cg,cpus_allowed_list=fields.get('Cpus_allowed_list'))
            ticks=os.sysconf('SC_CLK_TCK') if hasattr(os,'sysconf') else None
            thread['cpu_time_sec']=(tstat['utime_ticks']+tstat['stime_ticks'])/ticks if ticks else None
            # task may exit or PID may be recycled during this read.
            check=parse_stat(read(task/'stat'))
            if not check or check['starttime']!=tstat['starttime']:
                errors.append({'tid':task.name,'reason':'identity_changed_during_snapshot'})
                continue
            threads.append(thread)
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
