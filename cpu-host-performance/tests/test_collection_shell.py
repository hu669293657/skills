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
        return subprocess.run([BASH,str(script)]+list(args),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)  # Git Bash process startup dominates these Windows fixtures

    def test_bash_syntax(self):
        r=subprocess.run([BASH,'-n',str(REPO/'scripts/cpu_trace_collect.sh')],capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_help_and_configuration_without_root(self):
        r=self.run_bash(REPO/'scripts/cpu_trace_collect.sh','--help')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('--frequency-interval',r.stdout)
        self.assertIn('--discover',r.stdout)
        self.assertIn('--no-hostbound',r.stdout)
        self.assertIn('default: 3 s',r.stdout)
        self.assertIn('默认 16384',r.stdout)

    def test_missing_option_value_fails_promptly(self):
        for flag in ('--duration', '--output', '--pid', '--frequency-interval'):
            with self.subTest(flag=flag):
                r = self.run_bash(REPO/'scripts/cpu_trace_collect.sh', flag)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('missing option value', r.stdout + r.stderr)

    def test_repeated_collection_uses_unique_timestamp_directories(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);(out/'cpu_trace').mkdir();target=out/'cpu_trace/user.txt';target.write_text('keep')
            text=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
            # Stop after directory allocation, without reading host interfaces.
            text=text.replace('hb_phase start', 'printf "%s\\n" "$SNAP"; exit 0',1)
            script=out/'test.sh';script.write_text(text,encoding='utf-8',newline='\n')
            for _ in range(2):
                result=self.run_bash(script,'--output',out.as_posix())
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertNotIn('拒绝覆盖',result.stdout+result.stderr)
            captures=[p for p in out.glob('cpu_trace_*') if p.is_dir()]
            self.assertEqual(len(captures),2)
            self.assertTrue(all(__import__('re').match(r'cpu_trace_\d{8}_\d{6}_',p.name) for p in captures))
            self.assertEqual(target.read_text(),'keep')

    def test_fake_tracefs_full_and_minimal_capability(self):
        for minimal in (False,True):
            with self.subTest(minimal=minimal), tempfile.TemporaryDirectory() as d:
                base=Path(d);trace=base/'fake';trace.mkdir();(trace/'instances').mkdir()
                (trace/'tracing_on').write_text('0')
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
                text=text.replace('HB_PROC=/proc; HB_SYS=/sys', 'HB_PROC="'+(base/'proc').as_posix()+'"; HB_SYS="'+(base/'sys').as_posix()+'"')
                text=text.replace('for name in cpuinfo loadavg meminfo vmstat cmdline;', 'for name in cpuinfo loadavg meminfo vmstat cmdline;')
                setup='''
mkdir(){
  command mkdir "$@" || return $?
  if [[ "$1" == */instances/cpu_host_* ]]; then cp -R "TEMPLATE/." "$1/"; fi
}
rmdir(){ printf '%s\\n' "$1" >> "CLEANUP_LOG"; }
'''.replace('TEMPLATE',template.as_posix()).replace('CLEANUP_LOG',(base/'cleanup.log').as_posix())
                text=text.replace('set +e',setup+'\nset +e',1)
                # These tests exercise tracefs lifecycle/capabilities, not host
                # df/lsblk probes. Keep them independent of Windows drive latency.
                fake_commands='''
hb_command(){
  printf 'synthetic optional tool output\\n' > "$SNAP/$2"
  hb_entry "$1" COLLECTED "$2" "fixture_command" 0
}
'''
                text=text.replace('# END standalone evidence','# END standalone evidence\n'+fake_commands,1)
                script=base/'collect.sh';script.write_text(text,encoding='utf-8',newline='\n')
                args=['--no-hostbound','--duration','1','--output',(base/'out').as_posix()]
                if minimal:args.extend(['--minimal','--buffer-size','8192'])
                r=self.run_bash(script,*args)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                snap=next(p for p in (base/'out').glob('cpu_trace_*') if p.is_dir())
                self.assertEqual((snap/'trace_overrun.txt').read_text().strip(),'4')
                requested = 8192 if minimal else 16384
                metadata=(snap/'metadata.txt').read_text(encoding='utf-8')
                self.assertIn('buffer_size_requested_kb: %d'%requested,metadata)
                self.assertIn('buffer_size_effective_kb: %d'%requested,metadata)
                caps=(snap/'hostbound/capabilities.tsv').read_text()
                self.assertIn('power/cpu_frequency\t1\t1\t%d\t%d'%(0 if minimal else 1,0 if minimal else 1),caps)
                self.assertTrue((base/'cleanup.log').exists())
                self.assertEqual((trace/'instances').is_dir(),True)
                self.assertTrue((snap/'trace.txt').exists())
                manifest=json.loads((snap/'hostbound/collection_manifest.json').read_text())
                captured=[e for e in manifest['entries'] if e['category']=='trace'][-1]
                self.assertEqual(captured['status'],'COLLECTED')
                self.assertTrue(captured['raw_exists']); self.assertFalse(captured['empty'])
                self.assertEqual((trace/'tracing_on').read_text(),'0')
