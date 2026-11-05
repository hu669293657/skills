import json
import unittest

from analyzer.npu_affinity import analyze_npu_affinity
from tests.test_npu_affinity import snapshots, metrics


class NpuSafetyTests(unittest.TestCase):
    def test_reused_tid_old_generation_cpu_is_not_attributed_to_npu_worker(self):
        from analyzer.parser import TraceData, Event
        from analyzer.metrics import compute_metrics
        td = TraceData()
        def process(pid, stamp):
            return {'tgid': pid, 'starttime': stamp, 'ppid': 1, 'name': 'python3'}
        def thread(tid, tgid, stamp, cpu):
            return {'tid': tid, 'tgid': tgid, 'starttime': stamp, 'name': 'worker',
                    'allowed_cpus': [cpu], 'last_cpu': cpu, 'cpuset_cpus_effective': [cpu]}
        td.snapshot = {'hostbound/host_context.txt': 'execution_context=host\nboot_id=fixture',
                       'hostbound/cpu_online.txt': '0-1', 'hostbound/numactl.txt': 'node 0 cpus: 0\nnode 1 cpus: 1',
                       'hostbound/npu_smi_info.txt': '| 0 0 | 100 | python3 | 1 |',
                       'hostbound/npu_smi_topology.txt': 'NPU CPU Affinity\nNPU0 X 0',
                       'hostbound/process_start.json': json.dumps({'processes': [process(100, 10), process(200, 20)],
                           'threads': [thread(100, 100, 10, 0), thread(300, 200, 21, 1)]}),
                       'hostbound/process_end.json': json.dumps({'processes': [process(100, 10), process(200, 20)],
                           'threads': [thread(100, 100, 10, 0), thread(300, 100, 30, 0)]}),
                       'trace_overrun.txt': '0'}
        td.events = [Event(0, 1, 'sched_switch', fields={'prev_pid': 0, 'next_pid': 300}),
                     Event(0, 0, 'sched_switch', fields={'prev_pid': 0, 'next_pid': 100}),
                     Event(1, 1, 'sched_switch', fields={'prev_pid': 300, 'next_pid': 0}),
                     Event(1, 0, 'sched_switch', fields={'prev_pid': 100, 'next_pid': 0}),
                     Event(1.1, 1, 'sched_process_exit', pid=300, fields={'pid': 300}),
                     Event(2, 0, 'task_newtask', pid=100, fields={'pid': 300, 'clone_flags': '10000'}),
                     Event(2.1, 0, 'sched_switch', fields={'prev_pid': 0, 'next_pid': 300}),
                     Event(3, 0, 'sched_switch', fields={'prev_pid': 300, 'next_pid': 100}),
                     Event(4, 0, 'sched_switch', fields={'prev_pid': 100, 'next_pid': 0})]
        row = compute_metrics(td)['npu_affinity']['rows'][0]
        self.assertEqual(row['identity_status'], 'VERIFIED')
        self.assertEqual(row['trace'], '0')
        self.assertEqual(row['state'], 'LOCAL')

    def test_no_runtime_evidence_does_not_equally_partition_shared_pool(self):
        result = metrics(snapshots())['npu_affinity']
        self.assertEqual(result['commands'], '')
        self.assertTrue(all(not r['target'] for r in result['rows']))

    def test_mapping_table_is_used_when_topology_missing(self):
        s = snapshots()
        del s['hostbound/npu_smi_topology.txt']
        s['hostbound/npu_smi_map.txt'] = '| NPU ID | CPU Affinity | NUMA ID |\n| 0 | 144-167 | 3 |'
        row = next(r for r in metrics(s)['npu_affinity']['rows'] if r['npu'] == 0)
        self.assertEqual(row['affinity'], '144-167')
        self.assertIn('npu_smi_map.txt', ' '.join(row['sources']))

    def test_mem_allowed_is_not_actual_residency(self):
        s = snapshots()
        s['hostbound/pid_4115_status.txt'] += 'Mems_allowed_list:\t0-3\n'
        s['hostbound/pid_4115_numa_maps.txt'] = '1000 default anon=8 N0=8 kernelpagesize_kB=64\n2000 default anon=2 N3=2 kernelpagesize_kB=4\n'
        memory = metrics(s)['npu_affinity']['rows'][0]['memory_locality']
        self.assertEqual(memory['allowed_nodes'], [0, 1, 2, 3])
        self.assertEqual(memory['actual_pages_by_node'], {0: 8, 3: 2})
        self.assertEqual(memory['status'], 'MIXED')

    def test_numastat_fallback_reports_measured_mb_without_inventing_pages(self):
        s = snapshots()
        s['hostbound/pid_4115_numastat.txt'] = 'Per-node process memory usage (in MBs)\n Node 0 Node 3 Total\nTotal 1.5 20.0 21.5\n'
        memory = metrics(s)['npu_affinity']['rows'][0]['memory_locality']
        self.assertEqual(memory['actual_mb_by_node'], {0:1.5,3:20.0})
        self.assertEqual(memory['actual_pages_by_node'], {})
        self.assertFalse(memory['remote_evidence'])

    def test_sysfs_numa_cpulist_used_by_diagnosis(self):
        from analyzer.diagnosis import _numa_node_cpus
        self.assertEqual(_numa_node_cpus({'hostbound/numa.txt': '== /sys/devices/system/node/node2/cpulist ==\n8-11\n'}), {2: {8, 9, 10, 11}})

    def test_normalized_and_independent_sysfs_topology_without_numactl(self):
        from analyzer.npu_affinity import _topology
        snap={'hostbound/numa_topology.json': json.dumps({'nodes':[{'node':2,'cpus':[8,9]}]}),
              'hostbound/sysfs/node/node3/cpulist':'10-11'}
        self.assertEqual(_topology(snap)[1], {2:{8,9},3:{10,11}})

    def test_pid_reuse_blocks_binding_and_trace_attribution(self):
        s = snapshots()
        def batch(stamp):
            return json.dumps({'processes': [{'tgid': 4115, 'starttime': stamp}], 'threads': [{'tid': 4115, 'tgid': 4115, 'starttime': stamp, 'allowed_cpus': [0], 'cpuset_cpus_effective': [0]}]})
        s['hostbound/process_start.json'] = batch(1)
        s['hostbound/process_end.json'] = batch(2)
        row = metrics(s)['npu_affinity']['rows'][0]
        self.assertEqual(row['identity_status'], 'AMBIGUOUS')
        self.assertEqual(row['trace'], '')

    def test_reused_tid_observations_only_include_the_target_process_generation(self):
        from analyzer.parser import TraceData, Event
        from analyzer.metrics import compute_metrics
        def process(pid, stamp):
            return {'tgid':pid,'starttime':stamp,'ppid':1,'namespace_pids':[pid],
                    'cpuset_cpus_effective':[0,1]}
        def thread(tid, tgid, stamp, cpu):
            return {'tid':tid,'tgid':tgid,'starttime':stamp,'last_cpu':cpu,
                    'allowed_cpus':[0],'cpuset_cpus_effective':[0,1]}
        processes=[process(100,10),process(200,20)]
        before={'processes':processes,'threads':[thread(100,100,10,0),thread(300,200,21,1)]}
        after={'processes':processes,'threads':[thread(100,100,10,0),thread(300,100,30,0)]}
        td=TraceData();td.snapshot={
            'hostbound/process_start.json':json.dumps(before),'hostbound/process_end.json':json.dumps(after),
            'hostbound/host_context.txt':'execution_context=host\nboot_id=test-boot\n',
            'hostbound/npu_smi_info.txt':'| 0 0 | 100 | python | 1 |',
            'hostbound/npu_smi_topology.txt':'NPU CPU Affinity\nNPU0 X 0',
            'hostbound/lscpu_parse.txt':'0,0,0,0,Y\n1,1,0,1,Y',
            'hostbound/cpu_online.txt':'0-1','trace_overrun.txt':'0'}
        td.events=[Event(0,1,'sched_switch',fields={'prev_pid':0,'next_pid':300}),
                   Event(1,1,'sched_switch',fields={'prev_pid':300,'next_pid':0}),
                   Event(1.1,1,'sched_process_exit',pid=300,fields={'pid':300}),
                   Event(2,0,'task_newtask',pid=100,fields={'pid':300,'clone_flags':'10000','comm':'worker'}),
                   Event(2.1,0,'sched_switch',fields={'prev_pid':0,'next_pid':300}),
                   Event(3,0,'sched_switch',fields={'prev_pid':300,'next_pid':0})]
        m=compute_metrics(td)
        self.assertEqual(len([t for t in m['process_analysis']['tasks'] if t['tid']==300]),2)
        self.assertEqual(set(m['tasks']['observed_cpus_by_tid'][300]),{0,1})
        row=m['npu_affinity']['rows'][0]
        self.assertEqual(row['identity_status'],'VERIFIED')
        self.assertEqual(row['trace'],'0')
        self.assertEqual(row['state'],'LOCAL')

    def test_unambiguous_legacy_aggregate_observations_are_retained(self):
        from analyzer.npu_affinity import _pid_evidence
        ev=_pid_evidence(snapshots(),4115,{'tasks':{'observed_cpus_by_tid':{14115:[150]}}})
        self.assertEqual(ev['trace'],{150})

    def test_full_contention_windows_stay_in_json(self):
        from tests.test_contention import analyze, switch, wake
        events = [switch(0, 0, 9)]
        for i in range(30):
            events.extend([wake(i * 3 + 1, 101), switch(i * 3 + 2, 9, 101), switch(i * 3 + 3, 101, 9)])
        result = analyze(events)['contention']
        self.assertEqual(len(result['candidates'][0]['windows']), 30)

    def test_all_tid_cpuset_is_hard_boundary(self):
        m = metrics(snapshots(), measured=True)
        snap = m['snapshot']
        after = json.loads(snap['hostbound/process_end.json'])
        next(t for t in after['threads'] if t['tid'] == 14115)['cpuset_cpus_effective'] = [0, 1]
        snap['hostbound/process_end.json'] = json.dumps(after)
        row = analyze_npu_affinity(m)['rows'][0]
        self.assertEqual(row['target'], '')
        self.assertTrue(any('cpuset' in reason for reason in row['blockers']))

    def test_weighted_allocation_uses_measured_demand_instead_of_card_count(self):
        from analyzer.npu_affinity import cpus
        m = metrics(snapshots(), measured=True)
        for t in m['thread_metrics']:
            if t.get('tgid') == 4115:
                t['runtime_sec'] = 9
            elif t.get('tgid') == 4117:
                t['runtime_sec'] = 1
        rows = analyze_npu_affinity(m)['rows']
        a, b = (next(r for r in rows if r['pid'] == pid) for pid in (4115, 4117))
        self.assertGreater(len(cpus(a['target'])), len(cpus(b['target'])))
        self.assertFalse(cpus(a['target']) & cpus(b['target']))
        self.assertEqual(cpus(a['target']) | cpus(b['target']), set(range(144, 168)))

    def test_conflicting_explicit_cpu_maps_block_recommendations(self):
        s = snapshots()
        s['hostbound/npu_smi_map.txt'] = '| NPU ID | CPU Affinity |\n| 0 | 0-23 |'
        row = metrics(s)['npu_affinity']['rows'][0]
        self.assertEqual(row['affinity'], '')
        self.assertEqual(row['target'], '')

    def test_preserved_per_thread_masks_are_not_rewritten(self):
        s = snapshots()
        s['hostbound/pid_4115_thread_affinity.txt'] = "pid 4115's current affinity list: 144-146\npid 14115's current affinity list: 150-151\n"
        result = metrics(s, measured=True)['npu_affinity']
        row = result['rows'][0]
        self.assertEqual(row['recommendation_status'], 'PRESERVE')
        self.assertEqual(row['recommended_cpu'], '144-146,150-151')
        self.assertNotRegex(result['commands'], r'taskset -pc [^\n]+ (?:4115|14115)(?:\s|$)')

    def test_config_only_numa_risk_does_not_become_p1(self):
        from analyzer.diagnosis import diagnose
        risks = [f for f in diagnose(metrics(snapshots()))['findings'] if f['type'] == 'NUMA_RISK']
        self.assertTrue(risks)
        self.assertTrue(all(f['priority'] == 'P2' and f['evidence_state'] == 'POTENTIAL' for f in risks))

    def test_report_commands_only_contain_concrete_taskset_settings(self):
        import re
        commands = metrics(snapshots(), measured=True)['npu_affinity']['commands']
        self.assertTrue(commands)
        self.assertTrue(all(re.fullmatch(r'taskset -(?:apc|pc) [0-9,-]+ [1-9][0-9]*', line)
                            for line in commands.splitlines()))


if __name__ == '__main__':
    unittest.main()
