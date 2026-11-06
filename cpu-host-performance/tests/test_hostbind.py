"""Synthetic host fixtures: no writes to real affinity, procfs or sysfs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

try:
    from hostbind.planner import make_plan, BindError
    from hostbind.transaction import apply_plan, restore_state, audit_plan
    from hostbind.verify import compare_measurements
except ImportError:
    make_plan = None


def snapshot():
    return dict(context=dict(boot_id='test-boot', pid_namespace='pid:[1]',
                             init_pid_namespace='pid:[1]', execution_context='host'),
                topology=dict(online=list(range(16)),
                    cpu_node={str(c): 0 for c in range(16)},
                    siblings={str(c): [c % 8, c % 8 + 8] for c in range(16)},
                    npu_cpus={'0': list(range(16)), '1': list(range(16))}),
                selection=dict(pids=[100], workers={}, process_regex=None, topology_json=None),
                processes=[dict(pid=100, ppid=1, starttime='1000', npus=[0],
                    env={'LOCAL_RANK':'3', 'RANK':'11'}, name='python3', threads=[
                        dict(tid=100, starttime='1000', name='python3',
                             affinity=list(range(16)), ceiling=list(range(16))),
                        dict(tid=101, starttime='1001', name='acl_thread',
                             affinity=list(range(16)), ceiling=list(range(16)))]),
                    dict(pid=110, ppid=100, starttime='1100', npus=[],
                         env={'LOCAL_RANK':'3'}, name='pt_data_worker', threads=[
                             dict(tid=110, starttime='1100', name='pt_data_worker',
                                  affinity=list(range(16)), ceiling=list(range(16)))])])


def report():
    return dict(schema_version=2, metrics=dict(npu_affinity=dict(host_context=snapshot()['context']),
        binding_plan=dict(actions=[dict(pid=100, process_starttime='1000')]),
        metadata={'duration':3}), diagnosis={'host_status':'WARNING'})


class FakeHost:
    def __init__(self, snap=None):
        self.snap=copy.deepcopy(snap or snapshot()); self.calls=[]; self.process_calls=[]; self.fail_tid=None
        self.partial_tid=None

    def snapshot(self, selection):
        return copy.deepcopy(self.snap)

    def get_affinity(self, tid):
        return set(self.thread(tid)['affinity'])

    def thread(self, tid):
        return next(t for p in self.snap['processes'] for t in p['threads'] if t['tid']==tid)

    def set_process_affinity(self, pid, cpus):
        self.process_calls.append((pid, sorted(cpus)))
        process = next(p for p in self.snap['processes'] if p['pid'] == pid)
        for thread in process['threads']:
            self.set_affinity(thread['tid'], cpus)

    def set_affinity(self, tid, cpus):
        self.calls.append((tid, sorted(cpus)))
        if self.fail_tid == tid:
            self.fail_tid=None
            raise PermissionError('synthetic denial')
        self.thread(tid)['affinity']=sorted(cpus)
        if self.partial_tid == tid:
            self.partial_tid=None
            self.thread(tid)['affinity']=sorted(cpus)[:1]


class HostbindTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(make_plan, 'hostbind closed-loop implementation is missing')

    def test_rank_is_metadata_not_global_device(self):
        p=make_plan(snapshot(), report())
        self.assertEqual(p['workers'][0]['npus'], [0])
        self.assertEqual(p['workers'][0]['local_rank'], '3')
        self.assertEqual({a['pid'] for a in p['actions']}, {100})
        self.assertEqual(p['actions'][1]['target'], p['actions'][0]['target'])

    def test_partition_preserves_smt_and_reserves_inactive_npu(self):
        p=make_plan(snapshot(), report(), allocation='partition')
        pool=set(p['pools']['0'])
        self.assertEqual(pool, {0,1,2,3,8,9,10,11})
        self.assertFalse(pool & set(p['pools']['1']))
        self.assertTrue(all(set(p['topology']['siblings'][str(c)])<=pool for c in pool))

    def test_topology_partial_overlap_rejected(self):
        s=snapshot();s['topology']['npu_cpus']['1']=[4,5,6,7,12,13,14,15]
        with self.assertRaises(BindError): make_plan(s, report(), allocation='partition')

    def test_cpuset_is_per_thread(self):
        s=snapshot();s['processes'][0]['threads'][0]['ceiling']=[2,3]
        with self.assertRaisesRegex(BindError, 'different safe thread masks'):
            make_plan(s, report(), allocation='partition')
        s['processes'][0]['threads'][1]['ceiling']=[2,3]
        p=make_plan(s, report(), allocation='partition')
        self.assertEqual(p['actions'][0]['target'], [2,3])

    def test_missing_cpuset_uses_current_affinity(self):
        s=snapshot()
        for t in s['processes'][0]['threads']:
            t.update(ceiling=None,affinity=[5,6])
        self.assertEqual(make_plan(s,report())['actions'][0]['target'],[5,6])

    def test_stale_trace_identity_rejected(self):
        s=snapshot();s['processes'][0]['starttime']='9000'
        with self.assertRaises(BindError):make_plan(s,report())

    def test_pid_reuse_within_trace_cannot_be_hidden_by_end_snapshot(self):
        r=report();r['metrics']['snapshot']={
            'hostbound/process_start.json':json.dumps({'processes':[{'tgid':100,'starttime':999}]}),
            'hostbound/process_end.json':json.dumps({'processes':[{'tgid':100,'starttime':1000}]})}
        with self.assertRaises(BindError):make_plan(snapshot(),r)

    def test_wrong_boot_rejected(self):
        r=report();r['metrics']['npu_affinity']['host_context']['boot_id']='other'
        with self.assertRaises(BindError):make_plan(snapshot(),r)

    def test_multi_device_pid_different_pools_rejected(self):
        s=snapshot();s['processes'][0]['npus']=[0,1]
        with self.assertRaises(BindError):make_plan(s, report(), allocation='partition')

    def test_apply_requires_all_preflight_before_any_write(self):
        host=FakeHost();p=make_plan(host.snapshot(None), report(),allocation='partition')
        host.thread(101)['starttime']='999'
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(BindError):apply_plan(p,Path(d)/'state.json',host)
        self.assertFalse(host.calls)

    def test_journal_restore_idempotent_and_readback(self):
        host=FakeHost();p=make_plan(host.snapshot(None), report(),allocation='partition')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'state.json';apply_plan(p,f,host)
            self.assertEqual(json.loads(f.read_text())['status'],'APPLIED')
            self.assertEqual(host.process_calls, [(100, p['actions'][0]['target'])])
            self.assertEqual(host.get_affinity(110), set(range(16)))
            self.assertEqual(restore_state(f,host)['status'],'RESTORED')
            self.assertEqual(host.get_affinity(100), set(range(16)))
            self.assertEqual(restore_state(f,host)['status'],'RESTORED')

    def test_partial_failure_rolls_back_prior_changes(self):
        host=FakeHost();host.fail_tid=101;p=make_plan(host.snapshot(None),report(),allocation='partition')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json'
            with self.assertRaises(BindError):apply_plan(p,f,host)
            self.assertEqual(host.get_affinity(100),set(range(16)))
            self.assertEqual(json.loads(f.read_text())['status'],'RESTORED')

    def test_partial_kernel_readback_is_recovered(self):
        host=FakeHost();host.partial_tid=100;p=make_plan(host.snapshot(None),report(),allocation='partition')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json'
            with self.assertRaises(BindError):apply_plan(p,f,host)
            self.assertEqual(host.get_affinity(100),set(range(16)))

    def test_external_drift_is_conflict_not_success(self):
        host=FakeHost();p=make_plan(host.snapshot(None),report(),allocation='partition')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json';apply_plan(p,f,host);host.thread(100)['affinity']=[7]
            result=restore_state(f,host)
            self.assertEqual(result['status'],'RESTORE_INCOMPLETE')
            self.assertEqual(host.get_affinity(100),{7})

    def test_failed_syscall_cannot_claim_external_mask(self):
        host=FakeHost();p=make_plan(host.snapshot(None),report(),allocation='partition')
        original_set=host.set_affinity
        failures=[True]
        def denied(tid,cpus):
            if tid==100 and failures:
                failures.pop()
                host.thread(tid)['affinity']=[7]
                raise PermissionError('another controller wrote affinity during denial')
            return original_set(tid,cpus)
        host.set_affinity=denied
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json'
            with self.assertRaises(BindError):apply_plan(p,f,host)
            self.assertEqual(host.get_affinity(100),{7})
            self.assertEqual(json.loads(f.read_text())['status'],'RESTORE_INCOMPLETE')

    def test_successful_syscall_cannot_claim_out_of_target_external_mask(self):
        host=FakeHost();p=make_plan(host.snapshot(None),report(),allocation='partition')
        original_set=host.set_affinity;first=[True]
        def drift(tid,cpus):
            original_set(tid,cpus)
            if tid==100 and first:
                first.pop();host.thread(tid)['affinity']=[7]
        host.set_affinity=drift
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json'
            with self.assertRaises(BindError):apply_plan(p,f,host)
            self.assertEqual(host.get_affinity(100),{7})
            self.assertEqual(json.loads(f.read_text())['status'],'RESTORE_INCOMPLETE')

    def test_state_not_overwritten(self):
        host=FakeHost();p=make_plan(host.snapshot(None),report())
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json';f.write_text('keep')
            with self.assertRaises((BindError,FileExistsError)):apply_plan(p,f,host)
            self.assertEqual(f.read_text(),'keep');self.assertFalse(host.calls)

    def test_journal_io_failure_does_not_prevent_best_effort_restore(self):
        from unittest.mock import patch
        from hostbind import transaction
        host=FakeHost();p=make_plan(host.snapshot(None),report(),allocation='partition')
        real_save=transaction.save_json
        def disk_full(path,data,exclusive=False):
            if host.calls:raise OSError('synthetic disk full after first affinity write')
            return real_save(path,data,exclusive)
        with tempfile.TemporaryDirectory() as d,patch.object(transaction,'save_json',side_effect=disk_full):
            with self.assertRaises(BindError):apply_plan(p,Path(d)/'s.json',host)
        self.assertEqual(host.get_affinity(100),set(range(16)))

    def test_audit_detects_new_thread(self):
        host=FakeHost();p=make_plan(host.snapshot(None),report())
        host.snap['processes'][0]['threads'].append(dict(tid=102,starttime='1002',name='new',affinity=[0],ceiling=[0]))
        self.assertEqual(audit_plan(p,host)['status'],'DRIFT')

    def test_restore_reports_untracked_inherited_threads(self):
        host=FakeHost();p=make_plan(host.snapshot(None),report(),allocation='partition')
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'s.json';apply_plan(p,f,host)
            host.snap['processes'][0]['threads'].append(dict(tid=102,starttime='1002',name='new',
                affinity=p['actions'][0]['target'],ceiling=list(range(16))))
            self.assertEqual(restore_state(f,host)['status'],'RESTORE_INCOMPLETE')

    def test_before_trace_reused_as_after_is_not_proof(self):
        from hostbind.cli import compare_traces
        p=make_plan(snapshot(),report(),allocation='partition')
        self.assertTrue(compare_traces(report(),report(),p)['issues'])


def measurements(values, plan_id=None):
    obj=dict(schema_version=1, workload=dict(model='test',batch_size=8,parallelism='tp1',
        dataset='fixed',framework='test-1',precision='bf16',sequence_length=128),
        metric=dict(name='step_time',unit='ms',direction='lower'),warmup=2,
        runs=[[1000,1000]+list(values) for _ in range(3)],errors=0)
    if plan_id:obj['plan_id']=plan_id
    return obj


class VerificationTests(unittest.TestCase):
    def setUp(self):self.assertIsNotNone(make_plan,'hostbind implementation missing')

    def test_gain_and_no_gain(self):
        b=measurements([100]*30)
        self.assertEqual(compare_measurements(b,measurements([90]*30))['status'],'PASS')
        self.assertEqual(compare_measurements(b,measurements([100]*30))['status'],'NO_GAIN')

    def test_exact_gain_threshold_is_inclusive(self):
        self.assertEqual(compare_measurements(measurements([100]*30),measurements([98]*30))['status'],'PASS')
        self.assertEqual(compare_measurements(measurements([100]*30),measurements([90]*30),min_gain=10)['status'],'PASS')
        self.assertEqual(compare_measurements(measurements([100]*30),measurements([80]*27+[105]*3))['status'],'PASS')

    def test_incomparable_insufficient_nan_and_errors(self):
        b=measurements([100]*30)
        cases=[measurements([90]*2),measurements([float('nan')]*30)]
        other=measurements([90]*30);other['workload']['batch_size']=9;cases.append(other)
        for a in cases:
            self.assertEqual(compare_measurements(b,a)['status'],'INCONCLUSIVE')
        a=measurements([90]*30);a['errors']=1
        self.assertEqual(compare_measurements(b,a)['status'],'REGRESSION')

    def test_tail_regression_overrides_median_gain(self):
        a=measurements([80]*27+[200]*3)
        self.assertEqual(compare_measurements(measurements([100]*30),a)['status'],'REGRESSION')

    def test_sampling_protocol_mismatch_and_throughput_bad_tail(self):
        b=measurements([100]*30);a=measurements([90]*30);a['warmup']=1
        self.assertEqual(compare_measurements(b,a)['status'],'INCONCLUSIVE')
        b=measurements([100]*30);a=measurements([120]*27+[50]*3)
        b['metric']=a['metric']=dict(name='tokens_per_second',unit='tokens/s',direction='higher')
        self.assertEqual(compare_measurements(b,a)['status'],'REGRESSION')


class LinuxReaderTests(unittest.TestCase):
    def test_cgroup_v2_mount_root_and_unknown(self):
        from hostbind.linux import effective_cpuset
        files={'/cg/job/cpuset.cpus.effective':'2-3'}
        mount='1 0 0:1 /parent /cg rw - cgroup2 cgroup rw'
        self.assertEqual(effective_cpuset('0::/parent/job',mount,files.get),[2,3])
        self.assertIsNone(effective_cpuset('0::/parent/other',mount,files.get))

    def test_cgroup_v1_inherited_and_traversal(self):
        from hostbind.linux import effective_cpuset
        files={'/cg/job/cpuset.cpus':'','/cg/cpuset.cpus':'1-2'}
        mount='1 0 0:1 / /cg rw - cgroup cgroup rw,cpuset'
        self.assertEqual(effective_cpuset('2:cpuset:/job',mount,files.get),[1,2])
        with self.assertRaises(BindError):effective_cpuset('2:cpuset:/../other',mount,files.get)

    def test_environment_whitelist_and_stat_parentheses(self):
        from hostbind.linux import parse_env,parse_stat
        self.assertEqual(parse_env(b'LOCAL_RANK=3\0TOKEN=secret\0'),{'LOCAL_RANK':'3'})
        fields=['S','10']+['0']*17+['123']
        self.assertEqual(parse_stat('12 (a ) b) '+' '.join(fields)),{'starttime':'123','ppid':10})


if __name__=='__main__':unittest.main()
