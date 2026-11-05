#!/usr/bin/env python3
"""Read-only sidecar collection and evidence ledger (Python 3.7+, stdlib).

Every attempted command/file has an outcome. A raw capture is not a successful
parse; opaque text deliberately has parse_success=null. No writes to proc/sys.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import host_snapshot


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def entry(category, requested, command, raw_path):
    stamp = now()
    return {'category': category, 'requested': bool(requested), 'command': command,
            'command_exists': None, 'returncode': None, 'raw_path': raw_path,
            'raw_exists': False, 'empty': None, 'parse_success': None,
            'parsed_count': None, 'started_at': stamp, 'ended_at': stamp,
            'failure_reason': None, 'status': 'NOT_REQUESTED' if not requested else 'NOT_INCLUDED'}


def raw_state(row, root):
    path = Path(root) / row['raw_path'] if row.get('raw_path') else None
    row['raw_exists'] = bool(path and path.is_file())
    row['size_bytes'] = path.stat().st_size if row['raw_exists'] else None
    row['empty'] = row['size_bytes'] == 0 if row['raw_exists'] else None
    row['ended_at'] = now()
    return row


def classify_error(text):
    lower = (text or '').lower()
    if any(x in lower for x in ('permission denied', 'operation not permitted', 'access denied', 'not authorized')):
        return 'PERMISSION_DENIED'
    if any(x in lower for x in ('unsupported', 'not supported', 'invalid option', 'unrecognized option', 'unknown option', 'invalid argument')):
        return 'UNSUPPORTED'
    return 'EXECUTION_FAILED'


def parse_result(row, text, parser, empty_status):
    try:
        parsed = parser(text)
        if isinstance(parsed, dict):
            count = parsed.get('parsed_count')
            if count is None:
                count = len(parsed.get('records', []))
            errors = parsed.get('errors') or []
            if errors:
                raise ValueError('; '.join(str(e) for e in errors))
        else:
            count = len(parsed)
        row.update(parse_success=True, parsed_count=count,
                   status=empty_status if count == 0 else 'COLLECTED')
        return parsed
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        row.update(parse_success=False, status='PARSE_FAILED', failure_reason=str(exc))
        return None


def capture_command(root, category, argv, raw_path, requested=True, parser=None,
                    empty_status='COLLECTED', timeout=15):
    row = entry(category, requested, list(argv), raw_path)
    if not requested:
        return raw_state(row, root)
    executable = shutil.which(argv[0])
    row['command_exists'] = executable is not None
    if not executable:
        row.update(status='COMMAND_NOT_FOUND', failure_reason='executable not found: ' + argv[0])
        return raw_state(row, root)
    path = Path(root) / raw_path
    path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ); env['LC_ALL'] = 'C'; env['LANG'] = 'C'
    try:
        # stdout and stderr are retained together, including failed probes.
        with path.open('wb') as stream:
            completed = subprocess.run(list(argv), stdout=stream, stderr=subprocess.STDOUT,
                                       env=env, timeout=timeout, check=False)
        row['returncode'] = completed.returncode
        text = path.read_text(encoding='utf-8', errors='replace')
        if completed.returncode:
            row.update(status=classify_error(text), failure_reason=text.strip()[-2000:] or 'command exited %s' % completed.returncode)
        elif parser is not None:
            parse_result(row, text, parser, empty_status)
        else:
            row.update(status='COLLECTED', failure_reason=None)
    except subprocess.TimeoutExpired:
        row.update(status='EXECUTION_FAILED', failure_reason='command timed out after %ss' % timeout)
    except PermissionError as exc:
        row.update(status='PERMISSION_DENIED', failure_reason=str(exc))
    except OSError as exc:
        row.update(status='EXECUTION_FAILED', failure_reason=str(exc))
    return raw_state(row, root)


def capture_file(root, category, source, raw_path, requested=True):
    row = entry(category, requested, None, raw_path)
    row['source_path'] = str(source)
    if not requested:
        return raw_state(row, root)
    target = Path(root) / raw_path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # read_bytes handles virtual files whose st_size is zero.
        data = Path(source).read_bytes()
        target.write_bytes(data)
        row.update(status='COLLECTED', returncode=0)
    except PermissionError as exc:
        row.update(status='PERMISSION_DENIED', failure_reason=str(exc))
    except FileNotFoundError as exc:
        row.update(status='UNSUPPORTED', failure_reason=str(exc))
    except OSError as exc:
        row.update(status='EXECUTION_FAILED', failure_reason=str(exc))
    return raw_state(row, root)


def capture_data(root, category, raw_path, callback, count_key, requested=True,
                 empty_status='UNSUPPORTED'):
    row = entry(category, requested, None, raw_path)
    row['operation'] = 'host_snapshot.' + {'cpu_topology': 'topology_snapshot', 'frequency': 'frequency_snapshot',
                                          'idle': 'idle_snapshot', 'processes': 'process_snapshot'}.get(category, category)
    if not requested:
        return raw_state(row, root), None
    try:
        data = callback()
        target = Path(root) / raw_path
        target.parent.mkdir(parents=True, exist_ok=True)
        host_snapshot.write_json(target, data)
        count = len(data.get(count_key, []))
        row.update(parse_success=True, parsed_count=count,
                   status='COLLECTED' if count else empty_status)
        if isinstance(data.get('errors'), list) and data['errors']:
            row['item_errors'] = data['errors']
            if not count:
                row['status'] = 'PERMISSION_DENIED' if any(e.get('status') == 'PERMISSION_DENIED' for e in data['errors']) else 'PARSE_FAILED'
                row['failure_reason'] = 'No readable stable process identity; see item_errors'
        return raw_state(row, root), data
    except PermissionError as exc:
        row.update(status='PERMISSION_DENIED', failure_reason=str(exc), parse_success=False)
    except (OSError, ValueError) as exc:
        row.update(status='PARSE_FAILED', failure_reason=str(exc), parse_success=False)
    return raw_state(row, root), None


def load_manifest(root):
    path = Path(root) / 'hostbound/collection_manifest.json'
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    return {'schema_version': 1, 'collector': 'cpu_trace_collect.sh',
            'started_at': now(), 'entries': []}


def save_manifest(root, manifest):
    manifest['updated_at'] = now()
    # Update actual path state before each write; never claim an absent raw file.
    for row in manifest['entries']:
        prior_end = row['ended_at']
        raw_state(row, root)
        row['ended_at'] = prior_end
    for rel in ('hostbound/collection_manifest.json', 'collection_manifest.json'):
        dest = Path(root) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix('.json.tmp')
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(temp), str(dest))


def npu_parsers():
    # Share the analyzer's conservative parser; the raw text remains independent.
    parent = str(Path(__file__).resolve().parents[1])
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        from analyzer.npu_parser import parse_npu_info, parse_npu_mapping, parse_npu_topology
        return parse_npu_info, parse_npu_mapping, parse_npu_topology
    except ImportError:
        return None, None, None


def resolve_target_pids(explicit, npu_processes, context):
    """Explicit PIDs are local; numeric tool PIDs need verified host context."""
    targets = set(int(p) for p in explicit if str(p).isdigit() and int(p) > 0)
    unresolved = []
    for process in npu_processes:
        if context == 'host':
            targets.add(process['pid'])
        else:
            unresolved.append({'pid': process['pid'], 'npu': process.get('npu'),
                               'pid_namespace': process.get('pid_namespace', 'unknown'),
                               'reason': 'NPU PID cannot be matched to local procfs without verified host namespace'})
    return targets, unresolved


def collect_context(root, proc_root, entries):
    def readlink(path):
        try:
            return os.readlink(str(path))
        except OSError:
            return None
    detected = capture_command(root, 'host_context', ['systemd-detect-virt', '--container'], 'hostbound/container_detector.txt')
    entries.append(detected)
    path = Path(root) / 'hostbound/container_detector.txt'
    detector = path.read_text(errors='replace').strip() if path.is_file() else ''
    if detector == 'none' and detected['returncode'] in (0, 1):
        detected.update(status='COLLECTED', parse_success=True, parsed_count=0,
                        failure_reason='no container detected')
        context = 'host'
    elif detected['returncode'] == 0 and re.fullmatch(r'[a-zA-Z0-9_-]+', detector):
        detected.update(status='COLLECTED', parse_success=True, parsed_count=1, failure_reason=None)
        context = 'container'
    else:
        if detected['returncode'] == 0:
            detected.update(status='PARSE_FAILED', parse_success=False, failure_reason='unrecognized container detector response')
        context = 'unknown'
    if Path('/.dockerenv').exists() or Path('/run/.containerenv').exists():
        context = 'container'
    data = {'execution_context': context, 'container_detector': detector or 'unavailable',
            'boot_id': host_snapshot.read(Path(proc_root) / 'sys/kernel/random/boot_id'),
            'collector_pid': os.getpid(), 'pid_namespace': readlink(Path(proc_root) / 'self/ns/pid'),
            'init_pid_namespace': readlink(Path(proc_root) / '1/ns/pid'), 'collected_at': now()}
    target = Path(root) / 'hostbound/host_context.txt'
    target.write_text('\n'.join('%s=%s' % (k, '' if v is None else v) for k, v in data.items()) + '\n', encoding='utf-8')
    row = entry('host_context', True, None, 'hostbound/host_context.txt')
    row.update(status='COLLECTED', parse_success=True, parsed_count=1)
    entries.append(raw_state(row, root))
    return data


def collect_phase(root, phase, hostbound=True, discover=True, pids=None,
                  proc_root='/proc', sys_root='/sys', duration=30, minimal=False):
    root = Path(root); (root / 'hostbound').mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(root)
    manifest.update(duration_sec=duration, minimal=bool(minimal), hostbound=bool(hostbound), discover=bool(discover))
    entries = manifest['entries']
    suffix = '_at_end' if phase == 'end' else ''
    for category, filename in (('proc_stat', 'stat'), ('irq', 'interrupts'), ('softirq', 'softirqs'), ('schedstat', 'schedstat')):
        rel = 'hostbound/%s%s.txt' % (filename, suffix) if phase == 'end' else {'stat': 'proc_stat.txt'}.get(filename, filename + '.txt')
        entries.append(capture_file(root, category, Path(proc_root) / filename, rel))
    if phase == 'start':
        for name in ('cpuinfo', 'loadavg', 'meminfo', 'vmstat', 'cmdline'):
            rel = 'hostbound/kernel_cmdline.txt' if name == 'cmdline' else name + '.txt'
            entries.append(capture_file(root, 'host_context', Path(proc_root) / name, rel))
        entries.append(capture_file(root, 'cpu_topology', Path(sys_root) / 'devices/system/cpu/online', 'hostbound/cpu_online.txt'))
        row, topology = capture_data(root, 'cpu_topology', 'hostbound/numa_topology.json', lambda: host_snapshot.topology_snapshot(sys_root), 'nodes')
        entries.append(row)
        # Raw sysfs counterparts make normalized topology independently auditable.
        nodes = Path(sys_root) / 'devices/system/node'
        for source in [nodes / 'online'] + sorted(nodes.glob('node[0-9]*/cpulist')) + sorted(nodes.glob('node[0-9]*/distance')) + sorted(nodes.glob('node[0-9]*/meminfo')):
            entries.append(capture_file(root, 'cpu_topology', source, 'hostbound/sysfs/node/' + source.relative_to(nodes).as_posix()))
        for argv, rel in ((['lscpu'], 'lscpu.txt'), (['lscpu', '-e=CPU,CORE,SOCKET,NODE,ONLINE'], 'lscpu_extended.txt'),
                          (['lscpu', '-p=CPU,CORE,SOCKET,NODE,ONLINE'], 'lscpu_parse.txt'),
                          (['numactl', '-H'], 'numactl.txt'), (['numastat'], 'numastat.txt'), (['numastat', '-m'], 'numastat_memory.txt')):
            entries.append(capture_command(root, 'cpu_topology', argv, 'hostbound/' + rel))
        manifest['host_context'] = collect_context(root, proc_root, entries)
        for tool in ('gil_tracer', 'function_monitor', 'msprof', 'host_analyzer'):
            omitted = entry('external_tools', False, [tool], None)
            omitted.update(status='NOT_REQUESTED', failure_reason='deliberately excluded from this read-only collector')
            entries.append(omitted)
    freq, _ = capture_data(root, 'frequency', 'hostbound/frequency_%s.json' % phase, lambda: host_snapshot.frequency_snapshot(sys_root), 'policies')
    entries.append(freq)
    idle, _ = capture_data(root, 'idle', 'hostbound/idle_%s.json' % phase, lambda: host_snapshot.idle_snapshot(sys_root), 'states')
    entries.append(idle)
    processes, process_data = capture_data(root, 'processes', 'hostbound/process_%s.json' % phase,
                                         lambda: host_snapshot.process_snapshot(proc_root, sys_root), 'processes', requested=hostbound and discover,
                                         empty_status='NO_ACTIVE_PROCESS')
    entries.append(processes)
    if hostbound and discover:
        for argv, name in ((['ps', '-e', '-o', 'pid,ppid,psr,stat,comm,args'], 'ps'), (['pstree', '-p'], 'pstree')):
            entries.append(capture_command(root, 'processes', argv, 'hostbound/%s_%s.txt' % (name, phase)))
    parsers = npu_parsers()
    normalized, npu_processes = {}, []
    current_npu_info_status = None
    probes = [('npu_info', ['npu-smi', 'info'], 'npu_smi_info', parsers[0], 'NO_ACTIVE_PROCESS')]
    if phase == 'start':
        probes += [('npu_mapping', ['npu-smi', 'info', '-m'], 'npu_smi_map', parsers[1], 'PARSE_FAILED'),
                   ('npu_topology', ['npu-smi', 'info', '-t', 'topo'], 'npu_smi_topology', parsers[2], 'PARSE_FAILED')]
    for category, argv, name, parser, empty_status in probes:
        rel = 'hostbound/%s%s.txt' % (name, '_start' if category == 'npu_info' and phase == 'start' else '')
        result = capture_command(root, category, argv, rel, requested=hostbound, parser=parser, empty_status=empty_status)
        entries.append(result)
        if category == 'npu_info':
            current_npu_info_status = result['status']
        if result['returncode'] == 0 and parser:
            raw = (root / rel).read_text(encoding='utf-8', errors='replace')
            normalized[category] = parser(raw)
            if category == 'npu_info':
                npu_processes.extend(normalized[category].get('processes', []))
        elif result['returncode'] == 0 and hostbound:
            result['failure_reason'] = 'shared NPU parser unavailable; raw capture retained'
    if normalized:
        normalized_path = 'hostbound/npu_normalized_%s.json' % phase
        host_snapshot.write_json(root / normalized_path, normalized)
        normalized_entry = entry('npu_normalized', True, None, normalized_path)
        normalized_entry['operation'] = 'analyzer.npu_parser'
        normalized_entry.update(status='COLLECTED', parse_success=True,
                                parsed_count=sum(data.get('parsed_count', 0) for data in normalized.values()))
        entries.append(raw_state(normalized_entry, root))
    # NPU tool PIDs may belong to another PID namespace. Never attach an unverified
    # numeric PID to local procfs unless collection is positively in host context.
    context = manifest.get('host_context', {}).get('execution_context')
    targets, unresolved = resolve_target_pids(pids or [], npu_processes, context)
    if not hostbound or (not targets and not unresolved):
        absent = entry('numa_process', hostbound, None, None)
        if hostbound:
            no_active = current_npu_info_status == 'NO_ACTIVE_PROCESS'
            absent.update(status='NO_ACTIVE_PROCESS' if no_active else 'NOT_INCLUDED',
                          failure_reason=None if no_active else 'no explicit or namespace-verified NPU target PID')
        entries.append(absent)
    for process in unresolved:
        unresolved_entry = entry('numa_process', True, None, None)
        unresolved_entry.update(status='NOT_INCLUDED', failure_reason=process['reason'],
                                pid=process['pid'], pid_namespace=process['pid_namespace'], npu=process['npu'])
        entries.append(unresolved_entry)
    stable = {p['tgid']: p['starttime'] for p in (process_data or {}).get('processes', [])}
    for pid in sorted(targets) if hostbound else []:
        before = host_snapshot.parse_stat(host_snapshot.read(Path(proc_root) / str(pid) / 'stat'))
        if not before or (pid in stable and before['starttime'] != stable[pid]):
            failed = entry('numa_process', True, None, None)
            failed.update(status='EXECUTION_FAILED', failure_reason='target missing, unreadable, or identity changed', pid=pid)
            entries.append(failed)
            continue
        begin_index = len(entries)
        for name in ('status', 'cgroup', 'numa_maps', 'stat', 'cmdline'):
            entries.append(capture_file(root, 'numa_process', Path(proc_root) / str(pid) / name,
                                        'hostbound/pid_%d_%s_%s.txt' % (pid, name, phase)))
        entries.append(capture_command(root, 'numa_process', ['numastat', '-p', str(pid)], 'hostbound/pid_%d_numastat_%s.txt' % (pid, phase)))
        # Per-thread effective affinity is sourced from status, not taskset changes.
        entries.append(capture_command(root, 'processes', ['taskset', '-pc', str(pid)], 'hostbound/pid_%d_affinity_%s.txt' % (pid, phase)))
        after = host_snapshot.parse_stat(host_snapshot.read(Path(proc_root) / str(pid) / 'stat'))
        for row in entries[begin_index:]:
            row.update(pid=pid, starttime=before['starttime'], identity_verified=bool(after and after['starttime'] == before['starttime']))
            if not row['identity_verified']:
                row.update(status='EXECUTION_FAILED', failure_reason='identity_changed_during_target_capture')
        if after and after['starttime'] == before['starttime']:
            # Preserve stable historical aliases consumed by older analyzers.
            stamp = root / ('hostbound/pid_%d_starttime_%s.txt' % (pid, phase))
            stamp.write_text(str(before['starttime']) + '\n', encoding='utf-8')
            if phase == 'end':
                for name in ('status', 'cgroup', 'numa_maps', 'stat', 'cmdline', 'affinity', 'numastat', 'starttime'):
                    source = root / ('hostbound/pid_%d_%s_end.txt' % (pid, name))
                    if source.is_file():
                        shutil.copyfile(str(source), str(root / ('hostbound/pid_%d_%s.txt' % (pid, name))))
    save_manifest(root, manifest)
    return manifest


def missing_artifact_outcome(manifest, raw_path, trace_status, reason):
    generated = any(row.get('raw_path') == raw_path and row.get('raw_exists')
                    for row in manifest['entries'])
    if generated:
        return 'NOT_INCLUDED', 'previously_generated_artifact_is_missing'
    if trace_status == 'COLLECTED':
        return 'EXECUTION_FAILED', 'acquisition_artifact_not_generated'
    failure = trace_status if trace_status in ('UNSUPPORTED', 'PERMISSION_DENIED', 'COMMAND_NOT_FOUND',
                                               'EXECUTION_FAILED', 'PARSE_FAILED', 'NOT_REQUESTED') else 'EXECUTION_FAILED'
    return failure, 'not_started_due_to_trace_%s: %s' % (trace_status, reason or 'trace acquisition did not complete')


def mark_trace(root, status, reason=None):
    manifest = load_manifest(root)
    row = entry('trace', True, ['ftrace', 'private_instance'], 'trace.txt')
    row.update(status=status, failure_reason=reason)
    raw_state(row, root)
    if status == 'COLLECTED' and not row['raw_exists']:
        missing_status, missing_reason = missing_artifact_outcome(manifest, 'trace.txt', status, reason)
        row.update(status=missing_status, failure_reason=missing_reason)
    elif status == 'COLLECTED' and row['empty']:
        row.update(status='PARSE_FAILED', parse_success=False, failure_reason='trace export is empty')
    manifest['entries'].append(row)
    for category, rel, parser in (
            ('frequency', 'hostbound/frequency_samples.jsonl', parse_frequency_samples),
            ('trace_capabilities', 'hostbound/capabilities.tsv', parse_capabilities)):
        target = Path(root) / rel
        evidence = entry(category, True, None, rel)
        if target.is_file():
            parse_result(evidence, target.read_text(encoding='utf-8', errors='replace'), parser, 'COLLECTED')
        else:
            missing_status, missing_reason = missing_artifact_outcome(manifest, rel, status, reason)
            evidence.update(status=missing_status, failure_reason=missing_reason)
        manifest['entries'].append(raw_state(evidence, root))
    manifest['ended_at'] = now()
    save_manifest(root, manifest)


def parse_frequency_samples(text):
    samples = []
    for line in text.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get('ts'), (int, float)) or not isinstance(value.get('policies'), list):
            raise ValueError('frequency sample requires numeric ts and policies list')
        samples.append(value)
    if not samples:
        raise ValueError('no frequency samples')
    return samples


def parse_capabilities(text):
    lines = text.splitlines()
    columns = 'event exists writable requested enabled reason'.split()
    if not lines or lines[0].split('\t') != columns:
        raise ValueError('unexpected trace capability header')
    records = []
    for line in lines[1:]:
        values = line.split('\t')
        if len(values) != 6 or any(x not in ('0', '1') for x in values[1:5]):
            raise ValueError('malformed trace capability row')
        records.append(dict(zip(columns, values)))
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('start', 'end', 'trace'))
    parser.add_argument('--root', required=True)
    parser.add_argument('--proc-root', default='/proc')
    parser.add_argument('--sys-root', default='/sys')
    parser.add_argument('--hostbound', type=int, default=1)
    parser.add_argument('--discover', type=int, default=1)
    parser.add_argument('--pids', default='')
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--minimal', type=int, default=0)
    parser.add_argument('--status', default='COLLECTED')
    parser.add_argument('--reason')
    args = parser.parse_args()
    if args.phase == 'trace':
        mark_trace(args.root, args.status, args.reason)
    else:
        collect_phase(args.root, args.phase, bool(args.hostbound), bool(args.discover), args.pids.split(','),
                      args.proc_root, args.sys_root, args.duration, bool(args.minimal))


if __name__ == '__main__':
    main()
