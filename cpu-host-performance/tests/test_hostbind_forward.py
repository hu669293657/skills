"""Independent forward tests of documented CLI flow; all affinity writes are fake."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hostbind import cli
try:
    from tests.test_hostbind import FakeHost, measurements, report
except ModuleNotFoundError:
    from test_hostbind import FakeHost, measurements, report


def trace_report(host):
    value=report()
    batch=dict(processes=[],threads=[])
    for p in host.snap['processes']:
        batch['processes'].append(dict(tgid=p['pid'],ppid=p['ppid'],starttime=p['starttime'],
                                      rank_env=p.get('env',{})))
        for t in p['threads']:
            batch['threads'].append(dict(tid=t['tid'],tgid=p['pid'],starttime=t['starttime'],
                                         allowed_cpus=t['affinity']))
    value['metrics']['snapshot']={'hostbound/process_'+phase+'.json':json.dumps(batch)
                                  for phase in ('start','end')}
    value['metrics']['sched']={'lat_p99':2.5,'lat_count':30}
    return value


class CliForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.host=FakeHost()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(cli,'LinuxHost',return_value=self.host).start()
        mock.patch.object(cli,'mutation_lock',side_effect=contextlib.nullcontext).start()
        mock.patch.object(cli,'handle_signals',side_effect=contextlib.nullcontext).start()
        self.write('before-report.json',trace_report(self.host))

    def write(self,name,value):
        path=self.base/name
        path.write_text(json.dumps(value),encoding='utf-8')
        return path

    def invoke(self,*args):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return cli.main([str(a) for a in args])

    def plan(self,binder='disabled'):
        self.assertEqual(self.invoke('plan','--report-data',self.base/'before-report.json',
            '--pid','100','--allocation','partition','--framework-binding',binder,
            '--output',self.base/'plan.json'),0)
        return json.loads((self.base/'plan.json').read_text())

    def apply(self):
        return self.invoke('apply','--plan',self.base/'plan.json','--state',self.base/'state.json','--execute')

    def verify_args(self,plan,after_value=90):
        self.write('before.json',measurements([100]*30))
        self.write('after.json',measurements([after_value]*30,plan['plan_id']))
        self.write('after-report.json',trace_report(self.host))
        return ['verify','--before',self.base/'before.json','--after',self.base/'after.json',
            '--before-report',self.base/'before-report.json','--after-report',self.base/'after-report.json',
            '--plan',self.base/'plan.json','--state',self.base/'state.json',
            '--output',self.base/'verification.json','--live-audit']

    def test_documented_plan_preview_apply_verify_rollback(self):
        plan=self.plan()
        self.assertEqual(plan['workers'][0]['local_rank'],'3')
        self.assertEqual(plan['workers'][0]['npus'],[0])
        self.assertEqual(self.invoke('apply','--plan',self.base/'plan.json',
            '--state',self.base/'state.json'),0)
        self.assertFalse(self.host.calls)
        self.assertFalse((self.base/'state.json').exists())
        self.assertEqual(self.apply(),0)
        self.assertEqual(self.invoke(*self.verify_args(plan)),0)
        outcome=json.loads((self.base/'verification.json').read_text())
        self.assertEqual(outcome['status'],'PASS')
        self.assertEqual(outcome['trace_comparison']['after']['sched']['lat_p99'],2.5)
        calls=copy.deepcopy(self.host.calls)
        self.assertEqual(self.invoke('rollback','--state',self.base/'state.json'),0)
        self.assertEqual(calls,self.host.calls)
        self.assertEqual(self.invoke('rollback','--state',self.base/'state.json','--execute'),0)
        self.assertEqual(self.host.get_affinity(100),set(range(16)))
        self.assertEqual(self.host.get_affinity(101),set(range(16)))

    def test_no_gain_triggers_only_explicitly_authorized_rollback(self):
        plan=self.plan();self.assertEqual(self.apply(),0)
        args=self.verify_args(plan,100)
        self.assertEqual(self.invoke(*(args+['--rollback-on-fail'])),2)
        self.assertEqual(self.host.get_affinity(100),set(plan['actions'][0]['target']))
        self.assertEqual(self.invoke(*(args+['--rollback-on-fail','--execute'])),3)
        outcome=json.loads((self.base/'verification.json').read_text())
        self.assertEqual(outcome['status'],'NO_GAIN')
        self.assertEqual(outcome['rollback']['status'],'RESTORED')
        self.assertEqual(self.host.get_affinity(100),set(range(16)))

    def test_stale_after_trace_does_not_pass_or_implicitly_restore(self):
        plan=self.plan();self.assertEqual(self.apply(),0)
        args=self.verify_args(plan)
        self.write('after-report.json',json.loads((self.base/'before-report.json').read_text()))
        calls=copy.deepcopy(self.host.calls)
        self.assertEqual(self.invoke(*args),3)
        self.assertEqual(json.loads((self.base/'verification.json').read_text())['status'],'INCONCLUSIVE')
        self.assertEqual(self.host.calls,calls)

    def test_unchecked_framework_does_not_apply(self):
        self.plan('unknown')
        self.assertEqual(self.apply(),2)
        self.assertFalse(self.host.calls)
        self.assertFalse((self.base/'state.json').exists())

    def test_successful_write_does_not_claim_readback_outside_target(self):
        plan=self.plan()
        actual_set=self.host.set_affinity
        changed=[False]
        def successful_then_external(tid,cpus):
            actual_set(tid,cpus)
            if tid==100 and not changed[0]:
                changed[0]=True
                # CPU 7 lies outside the requested pool, so this cannot be
                # an online/cpuset intersection silently applied by the kernel.
                self.host.thread(tid)['affinity']=[7]
        self.host.set_affinity=successful_then_external
        self.assertNotIn(7,plan['actions'][0]['target'])
        self.assertEqual(self.apply(),2)
        self.assertEqual(self.host.get_affinity(100),{7})
        self.assertEqual(json.loads((self.base/'state.json').read_text())['status'],'RESTORE_INCOMPLETE')


if __name__ == '__main__': unittest.main()
