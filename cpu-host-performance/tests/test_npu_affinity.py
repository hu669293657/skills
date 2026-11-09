import re
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
import zipfile

from analyzer.parser import TraceData, Event, load_input, cleanup_temp
from analyzer.metrics import compute_metrics
from analyzer.diagnosis import diagnose
from analyzer.npu_report import affinity_html
from analyzer.npu_affinity import cpus
from analyzer.report import render_html, svg_bar_chart, svg_line_chart, _esc, SEV_COLOR, SEV_CN, STATUS_CN


def snapshots():
    affinities = ['144-167', '0-23', '144-167', '0-23', '96-119', '48-71', '96-119', '48-71']
    s = {
        'hostbound/npu_smi_topology.txt': 'NPU0 NPU1 CPU Affinity\n' + '\n'.join(
            'NPU%d X HCCS %s' % (n, cpus) for n, cpus in enumerate(affinities)),
        'hostbound/npu_smi_info.txt': 'NPU Chip | Process id | Process name | Process memory (MB)\n' + '\n'.join(
            '| %d 0 | %d | python3 | 26415 |' % (n, 4115+n) for n in range(8)),
        'hostbound/numactl.txt': '\n'.join('node %d cpus: %s' % (n, ' '.join(str(c) for c in range(n*48, n*48+48))) for n in range(4)),
        'hostbound/cpu_online.txt': '0-191\n',
        'hostbound/host_context.txt': 'execution_context=host\nboot_id=test-boot\n',
        'hostbound/lscpu_parse.txt': '# CPU,Core,Socket,Node,Online\n' + '\n'.join('%d,%d,0,%d,Y' % (c, c, c//48) for c in range(192)),
    }
    for pid in range(4115, 4123):
        s['hostbound/pid_%d_status.txt' % pid] = 'Name:\tpython3\nTgid:\t%d\nPid:\t%d\nThreads:\t2\nNSpid:\t%d\t1\nCpus_allowed_list:\t0-191\n' % (pid, pid, pid)
        s['hostbound/pid_%d_thread_affinity.txt' % pid] = "pid %d's current affinity list: 0-191\npid %d's current affinity list: 0-191\n" % (pid, pid+10000)
        s['hostbound/pid_%d_threads.txt' % pid] = 'PID TID PSR %%CPU STAT COMMAND\n%d %d 30 0 S python3\n%d %d 30 0 S worker\n' % (pid, pid, pid, pid+10000)
        s['hostbound/pid_%d_cpuset_effective.txt' % pid] = '0-191\n'
        s['hostbound/pid_%d_starttime.txt' % pid] = str(pid*100)
    return s


def add_complete_identity(s):
    """A full /proc fixture, including the identity and cpuset of every TID."""
    s = dict(s)
    processes, threads = [], []
    for pid in range(4115, 4123):
        processes.append({'tgid': pid, 'ppid': 1, 'name': 'python3', 'cmdline': 'vllm worker',
                          'starttime': pid*100, 'namespace_pids': [pid],
                          'cpuset_cpus_effective': list(range(192))})
        raw = s.get('hostbound/pid_%d_thread_affinity.txt' % pid, '')
        allowed = {int(t): list(cpus(v))
                   for t, v in re.findall(r"pid (\d+)'s current affinity list: ([\d,\-]+)", raw)}
        for index, tid in enumerate((pid, pid+10000)):
            threads.append({'tid': tid, 'tgid': pid, 'name': 'worker', 'starttime': pid*100+index,
                            'allowed_cpus': allowed.get(tid),
                            'cpuset_cpus_effective': list(range(192)), 'namespace_pids': [tid]})
    value = json.dumps({'processes': processes, 'threads': threads})
    s['hostbound/process_start.json'] = value
    s['hostbound/process_end.json'] = value
    s['trace_overrun.txt'] = '0'
    return s


def workload_events():
    return [Event(ts, cpu, 'sched_switch', fields={'prev_pid': 0 if ts == 0 else pid,
                   'prev_comm': 'swapper' if ts == 0 else 'python3',
                   'next_pid': pid if ts == 0 else 0, 'next_comm': 'python3' if ts == 0 else 'swapper'})
            for cpu, pid in enumerate(range(4115, 4123), 30) for ts in (0, 1)]


def metrics(s, measured=False):
    td = TraceData()
    td.snapshot = add_complete_identity(s) if measured else s
    td.format_name = 'synthetic ftrace'
    td.events = [Event(0, 30, 'sched_switch', fields={'prev_pid': '0', 'prev_comm': 'swapper/30', 'next_pid': '4115', 'next_comm': 'python3'}),
                 Event(1, 30, 'sched_switch', fields={'prev_pid': '4115', 'prev_comm': 'python3', 'next_pid': '0', 'next_comm': 'swapper/30'})]
    if measured:
        td.events = workload_events()
    return compute_metrics(td)


class NpuAffinityTests(unittest.TestCase):
    def analyze(self, s=None, measured=False):
        result = metrics(s if s is not None else snapshots(), measured=measured).get('npu_affinity', {})
        self.assertIn('rows', result, 'NPU snapshots must be converted into report evidence')
        return result

    def test_shared_affinity_is_split_without_leaving_advertised_cpus(self):
        result = self.analyze(measured=True)
        self.assertEqual([(r['npu'], r['pid'], r['target']) for r in result['rows']], [
            (0, 4115, '144-155'), (1, 4116, '0-11'), (2, 4117, '156-167'), (3, 4118, '12-23'),
            (4, 4119, '96-107'), (5, 4120, '48-59'), (6, 4121, '108-119'), (7, 4122, '60-71')])
        self.assertEqual(result['rows'][0]['state'], 'REMOTE_OBSERVED')
        self.assertEqual(result['rows'][0]['observed'], '30')
        self.assertEqual(result['rows'][0]['numa'], [3])
        self.assertIn('taskset -apc 144-155 4115', result['commands'])
        self.assertTrue(all(line.startswith('taskset ') for line in result['commands'].splitlines()))

    def test_local_last_cpu_does_not_prove_affinity_is_confined(self):
        s = snapshots()
        s['hostbound/pid_4115_threads.txt'] = s['hostbound/pid_4115_threads.txt'].replace(' 30 ', ' 150 ')
        result = self.analyze(s)
        self.assertEqual(result['rows'][0]['state'], 'REMOTE_OBSERVED')  # ftrace still saw CPU30
        s['hostbound/pid_4116_threads.txt'] = s['hostbound/pid_4116_threads.txt'].replace(' 30 ', ' 2 ')
        self.assertEqual(self.analyze(s)['rows'][1]['state'], 'UNCONFINED')

    def test_cpu_outside_topo_affinity_can_still_be_on_the_local_numa(self):
        rows = self.analyze()['rows']
        self.assertEqual(rows[0].get('observed_numa'), [0])
        self.assertEqual(rows[0].get('numa_locality'), 'REMOTE_OBSERVED')
        self.assertEqual(rows[1].get('observed_numa'), [0])
        self.assertEqual(rows[1].get('numa_locality'), 'LOCAL_OBSERVED')

    def test_missing_thread_or_host_evidence_blocks_actionable_commands(self):
        for suffix in ['thread_affinity', 'starttime', 'cpuset_effective']:
            s = snapshots()
            del s['hostbound/pid_4115_%s.txt' % suffix]
            result = self.analyze(s)
            row = result['rows'][0]
            self.assertTrue(row['blockers'])
            self.assertNotRegex(result['commands'], r'taskset -acp [^\n]* 4115(?:\s|$)')
        s = snapshots()
        s['hostbound/host_context.txt'] = 'execution_context=container\n'
        self.assertEqual(self.analyze(s)['commands'], '')

    def test_cpuset_is_a_hard_boundary(self):
        s = snapshots()
        s['hostbound/pid_4115_cpuset_effective.txt'] = '0-23'
        result = self.analyze(s)
        self.assertEqual(result['rows'][0]['target'], '')
        self.assertTrue(result['rows'][0]['blockers'])
        self.assertNotRegex(result['commands'], r'taskset -acp [^\n]* 4115(?:\s|$)')

    def test_same_pid_on_multiple_npus_is_not_bound_twice(self):
        s = snapshots()
        s['hostbound/npu_smi_info.txt'] = s['hostbound/npu_smi_info.txt'].replace('| 4117 |', '| 4115 |')
        result = self.analyze(s)
        self.assertTrue(all(r['blockers'] for r in result['rows'] if r['pid'] == 4115))
        self.assertNotRegex(result['commands'], r'taskset -acp [^\n]* 4115(?:\s|$)')

    def test_smt_siblings_stay_in_one_partition(self):
        s = snapshots()
        s['hostbound/lscpu_parse.txt'] = '# CPU,Core,Socket,Node,Online\n' + '\n'.join('%d,%d,0,%d,Y' % (c, (c%48)//24*12+c%12, c//48) for c in range(192))
        rows = self.analyze(s, measured=True)['rows']
        self.assertEqual(rows[1]['target'], '0-5,12-17')
        self.assertEqual(rows[3]['target'], '6-11,18-23')

    def test_disjoint_logical_pools_with_shared_smt_cores_are_blocked(self):
        s = snapshots()
        s['hostbound/npu_smi_topology.txt'] = s['hostbound/npu_smi_topology.txt'].replace('NPU1 X HCCS 0-23', 'NPU1 X HCCS 0-11').replace('NPU3 X HCCS 0-23', 'NPU3 X HCCS 12-23')
        s['hostbound/lscpu_parse.txt'] = '# CPU,Core,Socket,Node,Online\n' + '\n'.join('%d,%d,0,%d,Y' % (c, (c%48)//24*12+c%12, c//48) for c in range(192))
        result = self.analyze(s, measured=True)
        self.assertTrue(all(r['blockers'] for r in result['rows'] if r['npu'] in (1, 3)))
        self.assertNotRegex(result['commands'], r'taskset -acp [^\n]* 411[68](?:\s|$)')

    def test_ftrace_observations_include_threads_outside_top_eight(self):
        s = snapshots()
        s['hostbound/pid_4115_threads.txt'] = s['hostbound/pid_4115_threads.txt'].replace(' 30 ', ' 150 ')
        td = TraceData()
        td.snapshot = s
        td.events = []
        for i in range(9):
            td.events += [Event(0, i, 'sched_switch', fields={'next_pid': 8000+i, 'next_comm': 'heavy'}),
                          Event(1, i, 'sched_switch', fields={'prev_pid': 8000+i, 'prev_comm': 'heavy'})]
        td.events += [Event(0, 30, 'sched_switch', fields={'next_pid': 4115, 'next_comm': 'python3'}),
                      Event(.01, 30, 'sched_switch', fields={'prev_pid': 4115, 'prev_comm': 'python3'})]
        row = compute_metrics(td)['npu_affinity']['rows'][0]
        self.assertEqual(row['trace'], '30')

    def test_missing_topology_yields_unknown_instead_of_a_guessed_numa(self):
        s = snapshots()
        del s['hostbound/numactl.txt']
        del s['hostbound/lscpu_parse.txt']
        result = self.analyze(s)
        self.assertEqual(result['rows'][0]['numa'], [])
        self.assertEqual(result['commands'], '')

    def test_multiline_npu_process_table_and_empty_card(self):
        s = snapshots()
        s['hostbound/npu_smi_info.txt'] = '| NPU Chip | Process id | Process name | Process memory(MB) |\n| 0 0 | 4115 | python3 | 26415 |\n| 0 0 | 9000 | helper | 20 |\n| 1 0 | No running processes found |'
        result = self.analyze(s)
        rows = [r for r in result['rows'] if r['npu'] == 0]
        self.assertEqual([r['pid'] for r in rows], [4115, 9000])
        self.assertEqual(rows[0]['role'], '主进程（显存占用最高）')
        self.assertEqual([r['pid'] for r in result['rows'] if r['npu'] == 1], [None])
        self.assertEqual(affinity_html(result).count('class="npu-card '), 8)

    def test_already_confined_per_thread_masks_are_preserved(self):
        s = snapshots()
        s['hostbound/pid_4115_thread_affinity.txt'] = "pid 4115's current affinity list: 144-146\npid 14115's current affinity list: 150-151\n"
        result = self.analyze(s)
        self.assertNotRegex(result['commands'], r'taskset -acp [^\n]* 4115(?:\s|$)')

    def test_process_memory_is_aggregated_without_losing_chip_ids(self):
        s = snapshots()
        s['hostbound/npu_smi_info.txt'] = '| 0 0 | 4115 | python3 | 100 |\n| 0 1 | 4115 | python3 | 1 |\n| 0 0 | 4117 | python3 | 20 |'
        rows = self.analyze(s)['rows']
        self.assertEqual([(r['pid'], r['memory']) for r in rows if r['npu'] == 0], [(4115, 101), (4117, 20)])
        self.assertEqual(rows[0].get('chips'), [0, 1])

    def test_report_merges_cause_and_puts_resources_after_advice(self):
        m = metrics(snapshots(), measured=True)
        diag = diagnose(m)
        html = render_html(m, diag, svg_bar_chart, svg_line_chart, _esc, SEV_COLOR, SEV_CN, STATUS_CN)
        sections = re.findall(r"<h2 id='sec-(\d+)'>([^<]+)",html)
        self.assertEqual([int(n) for n,_ in sections],list(range(1,7)))
        self.assertIn('根因',sections[1][1])
        self.assertIn('优化建议',sections[2][1])
        self.assertIn('机器资源',sections[3][1])
        self.assertIn('采集环境',sections[5][1])
        self.assertIn('4115',html)
        self.assertIn('144-155',html)
        self.assertIn('taskset -apc 144-155 4115',html)
        self.assertIn('宿主机',html)

    def test_npu_mismatch_is_a_configuration_risk_in_diagnosis(self):
        diag = diagnose(metrics(snapshots()))
        risks = [f for f in diag['findings'] if f['type'] == 'NUMA_RISK' and 'NPU' in f['title']]
        self.assertEqual(len(risks), 1)
        self.assertIn('HostBound', risks[0]['impact'])
        self.assertIn('source', risks[0]['evidence'][0])

    def test_collection_wrapper_preserves_hostbound_snapshots_in_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            collection = base/'cpu_trace'
            collection.mkdir()
            for name, text in add_complete_identity(snapshots()).items():
                p = collection/name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding='utf-8')
            lines = []
            for ev in workload_events():
                lines.append('task-1 [%03d] d..2 %.6f: sched_switch: prev_comm=%s prev_pid=%d next_comm=%s next_pid=%d' %
                             (ev.cpu, 100+ev.ts, ev.fields['prev_comm'], ev.fields['prev_pid'], ev.fields['next_comm'], ev.fields['next_pid']))
            (collection/'trace.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
            archive = base/'case.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                for p in collection.rglob('*'):
                    if p.is_file(): z.write(p, p.relative_to(base))
            tar = base/'case.tar.gz'
            with tarfile.open(tar, 'w:gz') as tf:
                tf.add(collection, arcname='cpu_trace')
            for source in [base, archive, tar]:
                with self.subTest(source=str(source)):
                    td = load_input(str(source))
                    try:
                        self.assertIn('hostbound/pid_4115_thread_affinity.txt', td.snapshot)
                        self.assertIn('taskset -apc 144-155 4115', compute_metrics(td)['npu_affinity']['commands'])
                    finally:
                        cleanup_temp(td)


if __name__ == '__main__':
    unittest.main()
