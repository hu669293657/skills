"""Independent forward tests of documented CLI flow; all affinity writes are fake."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
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


class GeneratedShellForwardTests(unittest.TestCase):
    def test_apply_and_restore_delegate_without_running_taskset(self):
        from analyzer.binding_plan import render_binding_script
        bash=shutil.which('bash') or 'C:/Program Files/Git/bin/bash.exe'
        if not Path(bash).exists():self.skipTest('Bash unavailable')
        action=dict(pid=100,tid=100,target='0-3',scope='process',
                    process_starttime='1000',thread_starttime='1000',command='taskset -apc 0-3 100')
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);binary=base/'bin';binary.mkdir()
            log=base/'calls.txt'
            runner=binary/'python3'
            runner.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$BIND_FORWARD_LOG"\n',
                              encoding='utf-8',newline='\n')
            runner.chmod(0o755)
            script=base/'bind_cpu.sh'
            script.write_text(render_binding_script([action],{'boot_id':'synthetic-boot'}),
                              encoding='utf-8',newline='\n')
            (base/'report_data.json').write_text('{}')
            # Invoke via a Bash wrapper so Git Bash's native PATH conversion is explicit.
            bindir=binary.as_posix()
            if os.name=='nt':bindir='/'+binary.drive[0].lower()+bindir[2:]
            wrapper=base/'run.sh'
            wrapper.write_text('export PATH="'+bindir+':$PATH"\nexport BIND_FORWARD_LOG="'+
                log.as_posix()+'"\nchmod +x "'+bindir+'/python3"\nbash "'+script.as_posix()+
                '" "$@"\n',encoding='utf-8',newline='\n')
            result=subprocess.run([bash,str(wrapper),'--apply','/synthetic-skill','/synthetic-state','disabled'],
                capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            calls=log.read_text().splitlines()
            self.assertEqual(len(calls),2)
            self.assertIn('/scripts/ascend_bind.py plan --report-data',calls[0])
            self.assertIn('--main-pid 100 --framework-binding disabled',calls[0])
            self.assertIn('apply --plan /synthetic-state.plan.json --state /synthetic-state --execute',calls[1])
            result=subprocess.run([bash,str(wrapper),'--rollback','/synthetic-skill','/synthetic-state'],
                capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('rollback --state /synthetic-state --execute',log.read_text().splitlines()[-1])


if __name__=='__main__':unittest.main()
