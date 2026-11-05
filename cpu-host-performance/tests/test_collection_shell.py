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
        self.assertIn('--no-hostbound',r.stdout)
        self.assertIn('default: 30 s',r.stdout)

    def test_missing_option_value_fails_promptly(self):
        for flag in ('--duration', '--output', '--pid', '--frequency-interval'):
            with self.subTest(flag=flag):
                r = self.run_bash(REPO/'scripts/cpu_trace_collect.sh', flag)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('missing option value', r.stdout + r.stderr)

    def test_missing_tracefs_preserves_manifest_and_sidecars(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for name in ('host_snapshot.py', 'collection_evidence.py'):
                self.assertTrue((REPO/'scripts'/name).exists())
                shutil.copyfile(REPO/'scripts'/name, base/name)
            text=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
            text=text.replace('for d in /sys/kernel/tracing /sys/kernel/debug/tracing;', 'for d in "'+(base/'absent_tracefs').as_posix()+'";')
            text=text.replace('"$EUID"','"0"')
            text=text.replace('command -v python3 >/dev/null 2>&1 && PYTHON=python3', 'PYTHON="'+Path(sys.executable).as_posix()+'"')
            script=base/'collect.sh';script.write_text(text,encoding='utf-8',newline='\n')
            r=self.run_bash(script, '--duration', '1', '--no-hostbound', '--output', (base/'out').as_posix())
            self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
            snap=base/'out/cpu_trace'
            self.assertTrue((snap/'hostbound/collection_manifest.json').exists(),r.stdout+r.stderr)
            manifest=json.loads((snap/'hostbound/collection_manifest.json').read_text())
            trace=[e for e in manifest['entries'] if e['category']=='trace'][-1]
            self.assertEqual(trace['status'],'UNSUPPORTED')
            self.assertTrue(any(e['category']=='cpu_topology' for e in manifest['entries']))
            self.assertTrue((snap/'hostbound/trace_error.txt').is_file())

    def test_no_helper_fallback_does_not_claim_artifacts_were_generated(self):
        import sys
        cases=(('no_python','COMMAND_NOT_FOUND','python3_not_found'),
               ('no_helper','COMMAND_NOT_FOUND','host_snapshot.py_not_found'),
               ('unusable_helper','UNSUPPORTED','python3_or_collection_helper_unusable'))
        for mode,status,reason in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                base=Path(d)
                if mode=='unusable_helper':
                    (base/'host_snapshot.py').write_text('invalid python syntax!')
                    shutil.copyfile(REPO/'scripts/collection_evidence.py',base/'collection_evidence.py')
                text=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
                text=text.replace('for d in /sys/kernel/tracing /sys/kernel/debug/tracing;', 'for d in "'+(base/'absent_tracefs').as_posix()+'";')
                text=text.replace('"$EUID"','"0"')
                python_selection=':' if mode=='no_python' else 'PYTHON="'+Path(sys.executable).as_posix()+'"'
                text=text.replace('command -v python3 >/dev/null 2>&1 && PYTHON=python3',python_selection)
                script=base/'collect.sh';script.write_text(text,encoding='utf-8',newline='\n')
                r=self.run_bash(script,'--no-hostbound','--output',(base/'out').as_posix())
                self.assertNotEqual(r.returncode,0)
                manifest=json.loads((base/'out/cpu_trace/hostbound/collection_manifest.json').read_text())
                absent=[e for e in manifest['entries'] if e['raw_path'] is None and e['requested']]
                self.assertTrue(absent)
                for row in absent:
                    self.assertEqual(row['status'],status)
                    self.assertEqual(row['failure_reason'],reason)
                    self.assertFalse(row['raw_exists']);self.assertIsNone(row['empty'])
                self.assertTrue(all(e['status']=='NOT_REQUESTED' for e in manifest['entries'] if not e['requested']))

    def test_existing_output_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);(out/'cpu_trace').mkdir();target=out/'cpu_trace/user.txt';target.write_text('keep')
            r=self.run_bash(REPO/'scripts/cpu_trace_collect.sh','--output',out.as_posix())
            self.assertNotEqual(r.returncode,0)
            self.assertEqual(target.read_text(),'keep')

    def test_failed_end_sidecar_is_ledgered_without_losing_start_evidence(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            base=Path(d); trace=base/'fake'; (trace/'instances').mkdir(parents=True)
            (trace/'tracing_on').write_text('0')
            template=base/'template'; (template/'events/sched/sched_switch').mkdir(parents=True)
            (template/'events/sched/sched_switch/enable').write_text('0')
            (template/'events/enable').write_text('0')
            for name,value in [('tracing_on','0'),('current_tracer','nop'),('buffer_size_kb','4096'),('trace_clock','[local] mono'),('tracing_cpumask','1'),('trace','')]:
                (template/name).write_text(value)
            cpu=template/'per_cpu/cpu0'; cpu.mkdir(parents=True)
            (cpu/'trace').write_text('t-1 [000] .... 1.000: sched_switch: prev_pid=0 next_pid=1\nt-1 [000] .... 2.000: sched_switch: prev_pid=1 next_pid=0\n')
            (cpu/'stats').write_text('entries: 2\noverrun: 0\ndropped events: 0\n')
            (base/'host_snapshot.py').write_text('# synthetic helper; exits without real interface access\n')
            (base/'collection_evidence.py').write_text('''import json,sys
from pathlib import Path
if '--help' in sys.argv: raise SystemExit(0)
phase=sys.argv[1]
root=Path(sys.argv[sys.argv.index('--root')+1])
if phase=='end': raise SystemExit(17)
if phase=='start':
    (root/'hostbound/process_start.json').write_text('{"processes":[],"threads":[]}')
    data={'schema_version':1,'entries':[{'category':'processes','status':'COLLECTED','requested':True,'raw_path':'hostbound/process_start.json','raw_exists':True}]}
    for name in ('collection_manifest.json','hostbound/collection_manifest.json'):
        (root/name).write_text(json.dumps(data))
''', encoding='utf-8')
            text=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
            text=text.replace('for d in /sys/kernel/tracing /sys/kernel/debug/tracing;', 'for d in "'+trace.as_posix()+'";')
            text=text.replace('"$EUID"','"0"')
            text=text.replace('command -v python3 >/dev/null 2>&1 && PYTHON=python3','PYTHON="'+Path(sys.executable).as_posix()+'"')
            setup='''
mkdir(){ command mkdir "$@" || return $?; if [[ "$1" == */instances/cpu_host_* ]]; then cp -R "TEMPLATE/." "$1/"; fi; }
rmdir(){ :; }
'''.replace('TEMPLATE',template.as_posix())
            script=base/'collect.sh'; script.write_text(text.replace('set +e',setup+'\nset +e',1),encoding='utf-8',newline='\n')
            r=self.run_bash(script,'--duration','1','--output',(base/'out').as_posix())
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            snap=base/'out/cpu_trace'
            manifest=json.loads((snap/'collection_manifest.json').read_text())
            failed=[e for e in manifest['entries'] if e['category']=='sidecar_end']
            self.assertEqual(len(failed),1)
            self.assertEqual(failed[0]['status'],'EXECUTION_FAILED')
            self.assertEqual(failed[0]['returncode'],17)
            self.assertTrue(any(e['raw_path']=='hostbound/process_start.json' for e in manifest['entries']))
            self.assertTrue((snap/'trace.txt').exists())
            from analyzer.analyze_cpu_trace import run_analysis
            run_analysis(str(snap), str(base/'report'), quiet=True)
            metrics=json.loads((base/'report/report_data.json').read_text(encoding='utf-8'))['metrics']
            self.assertTrue(metrics['collection_integrity']['ftrace']['parse_success'])
            self.assertFalse(metrics['collection_integrity']['hostbound']['complete'])
            self.assertIn('sidecar_end_failed', (base/'report/hostbound_report.html').read_text(encoding='utf-8'))

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
                manifest=json.loads((snap/'hostbound/collection_manifest.json').read_text())
                captured=[e for e in manifest['entries'] if e['category']=='trace'][-1]
                self.assertEqual(captured['status'],'COLLECTED')
                self.assertTrue(captured['raw_exists']); self.assertFalse(captured['empty'])
                self.assertEqual((trace/'tracing_on').read_text(),'0')
