"""Collector shell tests run against a temporary fake tracefs, never real ftrace."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
BASH=shutil.which('bash') or ('C:/Program Files/Git/bin/bash.exe' if Path('C:/Program Files/Git/bin/bash.exe').exists() else None)


@unittest.skipUnless(BASH,'bash unavailable')
class CollectionShellTests(unittest.TestCase):
    def run_bash(self,script,*args):
        return subprocess.run([BASH,str(script)]+list(args),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)

    def test_bash_syntax(self):
        r=subprocess.run([BASH,'-n',str(REPO/'scripts/cpu_trace_collect.sh')],capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_help_and_configuration_without_root(self):
        r=self.run_bash(REPO/'scripts/cpu_trace_collect.sh','--help')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('--frequency-interval',r.stdout)
        self.assertIn('--discover',r.stdout)

    def test_existing_output_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);(out/'cpu_trace').mkdir();target=out/'cpu_trace/user.txt';target.write_text('keep')
            r=self.run_bash(REPO/'scripts/cpu_trace_collect.sh','--output',out.as_posix())
            self.assertNotEqual(r.returncode,0)
            self.assertEqual(target.read_text(),'keep')

    def test_fake_tracefs_full_and_minimal_capability(self):
        for minimal in (False,True):
            with self.subTest(minimal=minimal), tempfile.TemporaryDirectory() as d:
                base=Path(d);trace=base/'fake';trace.mkdir();(trace/'instances').mkdir()
                template=base/'template';template.mkdir()
                events=['sched/sched_switch','power/cpu_frequency']
                for e in events:
                    p=template/'events'/e/'enable';p.parent.mkdir(parents=True,exist_ok=True);p.write_text('0')
                (template/'events/enable').write_text('0')
                for name,value in [('tracing_on','0'),('current_tracer','nop'),('buffer_size_kb','4096'),('trace_clock','[local] mono'),('tracing_cpumask','1'),('trace','')]:
                    (template/name).write_text(value)
                cpu=template/'per_cpu/cpu0';cpu.mkdir(parents=True)
                (cpu/'trace').write_text('a-1 [000] .... 1.000: sched_switch: prev_pid=0 next_pid=1\na-1 [000] .... 2.000: sched_switch: prev_pid=1 next_pid=0\n')
                (cpu/'stats').write_text('entries: 2\noverrun: 3\ncommit overrun: 0\ndropped events: 1\n')
                # Only this temporary copy has the fake root/EUID and filesystem adapters.
                text=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
                text=text.replace('for d in /sys/kernel/tracing /sys/kernel/debug/tracing;', 'for d in "'+trace.as_posix()+'";')
                text=text.replace('"$EUID"','"0"')
                text=text.replace('SCRIPT_DIR=$(cd --', 'SCRIPT_DIR=$(cd --')
                text=text.replace('command -v python3 >/dev/null 2>&1 && PYTHON=python3',': # helper unavailable in fake-tracefs integration')
                setup='''
mkdir(){
  command mkdir "$@" || return $?
  if [[ "$1" == */instances/cpu_host_* ]]; then cp -R "TEMPLATE/." "$1/"; fi
}
rmdir(){ printf '%s\\n' "$1" >> "CLEANUP_LOG"; }
'''.replace('TEMPLATE',template.as_posix()).replace('CLEANUP_LOG',(base/'cleanup.log').as_posix())
                text=text.replace('set +e',setup+'\nset +e',1)
                script=base/'collect.sh';script.write_text(text,encoding='utf-8',newline='\n')
                args=['--duration','1','--output',(base/'out').as_posix()]
                if minimal:args.append('--minimal')
                r=self.run_bash(script,*args)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                snap=base/'out/cpu_trace'
                self.assertEqual((snap/'trace_overrun.txt').read_text().strip(),'4')
                caps=(snap/'hostbound/capabilities.tsv').read_text()
                self.assertIn('power/cpu_frequency\t1\t1\t%d\t%d'%(0 if minimal else 1,0 if minimal else 1),caps)
                self.assertTrue((base/'cleanup.log').exists())
                self.assertEqual((trace/'instances').is_dir(),True)
                self.assertTrue((snap/'trace.txt').exists())
