"""Report recommendations are executable taskset lines, without a wrapper."""
import unittest
from analyzer.npu_affinity import _binding_script


class DirectBindingTests(unittest.TestCase):
    def test_process_and_verified_child_are_separate_commands(self):
        actions = [dict(pid=4001, target='32-63', evidence={}),
                   dict(pid=4002, target='32-63', evidence={})]
        commands = _binding_script(actions, {'boot_id': 'fixture'})
        self.assertEqual(commands.splitlines(),
                         ['taskset -apc 32-63 4001', 'taskset -apc 32-63 4002'])

    def test_narrow_compliant_thread_is_preserved(self):
        actions = [dict(pid=4001, target='32', evidence={
            'allowed': {4001: set(range(64)), 4002: {32}}})]
        self.assertEqual(_binding_script(actions, {}), 'taskset -pc 32 4001')

    def test_known_descendant_gets_own_command_and_cpuset_is_checked(self):
        import json
        from tests.test_npu_affinity import snapshots, add_complete_identity, workload_events
        from analyzer.parser import TraceData
        from analyzer.metrics import compute_metrics
        td = TraceData()
        td.events = workload_events()
        td.snapshot = add_complete_identity(snapshots())
        for phase in ('start','end'):
            name = 'hostbound/process_%s.json' % phase
            batch = json.loads(td.snapshot[name])
            batch['processes'].append(dict(tgid=6000, ppid=4115, starttime=99,
                namespace_pids=[6000], cpuset_cpus_effective=list(range(192))))
            batch['threads'].append(dict(tid=6000,tgid=6000,starttime=99,
                namespace_pids=[6000],allowed_cpus=list(range(192)),
                cpuset_cpus_effective=list(range(192))))
            td.snapshot[name] = json.dumps(batch)
        commands = compute_metrics(td)['npu_affinity']['commands']
        self.assertIn('taskset -apc 144-155 6000',commands)
        for phase in ('start','end'):
            name = 'hostbound/process_%s.json' % phase
            batch = json.loads(td.snapshot[name])
            batch['threads'][-1]['cpuset_cpus_effective'] = [0,1]
            td.snapshot[name] = json.dumps(batch)
        self.assertNotIn(' 6000', compute_metrics(td)['npu_affinity']['commands'])

