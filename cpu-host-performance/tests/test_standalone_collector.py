"""Exercise the single-file collector on fake proc/sys trees, without Python helpers."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash') or 'C:/Program Files/Git/bin/bash.exe'


def put(root, path, text):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')


def fixture(base):
    proc, sys, out = base/'proc', base/'sys', base/'out'
    put(proc, 'uptime', '20.00 0.00\n')
    put(proc, 'self/mountinfo', '10 1 0:1 / %s rw - cgroup2 cgroup rw\n11 1 0:2 / %s rw - cgroup cgroup rw,cpuset\n' % ((sys/'fs/cgroup').as_posix(),(sys/'fs/cgroup/cpuset').as_posix()))
    for pid, ppid in [(4001, 1), (4002, 4001)]:
        fields = ['0'] * 40
        fields[0], fields[1], fields[19], fields[36] = 'S', str(ppid), '10', '32'
        text = '%d (worker with ) paren) ' % pid + ' '.join(fields)
        for path in [str(pid), '%d/task/%d' % (pid, pid)]:
            put(proc, path+'/stat', text)
            put(proc, path+'/status', 'Name:\tworker\\name\nTgid:\t%d\nPid:\t%d\nThreads:\t1\nNSpid:\t%d\nCpus_allowed_list:\t0-63\nMems_allowed_list:\t0\n' % (pid,pid,pid))
            put(proc, path+'/cgroup', '0::/job\n')
        put(proc, '%d/comm' % pid, 'worker\\name\n')
        put(proc, '%d/cmdline' % pid, 'python\x00train.py\x00')
        put(proc, '%d/numa_maps' % pid, '100 default N0=3 kernelpagesize_kB=64\n')
    put(sys, 'fs/cgroup/job/cpuset.cpus.effective', '32-63\n')
    put(sys, 'fs/cgroup/job/cpuset.mems.effective', '0\n')
    put(sys, 'fs/cgroup/job/cpu.max', '200000 100000\n')
    put(sys, 'devices/system/cpu/online', '0-63\n')
    put(sys, 'devices/system/node/online', '0\n')
    put(sys, 'devices/system/node/node0/cpulist', '0-63\n')
    put(sys, 'devices/system/node/node0/distance', '10\n')
    put(sys, 'devices/system/node/node0/meminfo', 'Node 0 MemTotal: 1000 kB\n')
    put(sys, 'devices/system/cpu/cpufreq/policy32/related_cpus', '32 33\n')
    put(sys, 'devices/system/cpu/cpufreq/policy32/scaling_cur_freq', '2400000\n')
    put(sys, 'devices/system/cpu/cpu32/cpuidle/state0/time', '2000\n')
    return proc, sys, out


class StandaloneTests(unittest.TestCase):
    def test_native_dump_is_exported_and_packaged_without_rewriting(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            proc, sys, out = fixture(base)
            collector = base / 'Ascend tool' / 'msprechecker_dump.py'
            put(base, 'Ascend tool/msprechecker_dump.py', 'synthetic collector path')
            native = b'{\n  "_meta": {"tool": "msprechecker_dump.py", "schema_version": "1.2"}\n}\n'
            (base / 'native.json').write_bytes(native)
            self.run_library(base, proc, sys, out, """
DURATION=3; MINIMAL=0; HOSTBOUND=1; DISCOVER=1; PID_LIST=4001,4001
OUTDIR="$SNAP/exports"; CAPTURE_NAME=cpu_trace_fixture
mkdir -p "$OUTDIR"
python3(){
  [[ "$#" -eq 8 && "$1" == -- && "$2" == '%s' &&
     "$3 $4 $5 $6 $7" == '--filter --no-probes --target pid-4001=4001 --output-path' ]] || return 41
  cp -- '%s' "$8"
}
hb_ascend_dump '%s'
hb_manifest
""" % (collector.as_posix(), (base / 'native.json').as_posix(), collector.as_posix()))
            packaged = out / 'hostbound/environment/msprechecker_dumped.json'
            standalone = out / 'exports/cpu_trace_fixture_msprechecker_dumped.json'
            self.assertEqual(packaged.read_bytes(), native)
            self.assertEqual(standalone.read_bytes(), native)
            rows = json.loads((out / 'collection_manifest.json').read_text(encoding='utf-8'))['entries']
            self.assertEqual(rows[0]['category'], 'environment_dump')
            self.assertEqual(rows[0]['status'], 'COLLECTED')

    def test_native_dump_failed_write_is_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            proc, sys, out = fixture(base)
            put(base, 'collector.py', 'synthetic collector path')
            self.run_library(base, proc, sys, out, """
DURATION=3; MINIMAL=0; HOSTBOUND=1; DISCOVER=1; PID_LIST=
OUTDIR="$SNAP/exports"; CAPTURE_NAME=cpu_trace_fixture
mkdir -p "$OUTDIR"
python3(){ printf '{' > "$6"; return 17; }
hb_ascend_dump '%s'
hb_manifest
""" % ((base / 'collector.py').as_posix(),))
            self.assertFalse(list(out.rglob('msprechecker_dumped.json*')))
            self.assertFalse(list((out / 'exports').iterdir()))
            row = json.loads((out / 'collection_manifest.json').read_text(encoding='utf-8'))['entries'][0]
            self.assertEqual((row['status'], row['returncode']), ('EXECUTION_FAILED', 17))

    def test_environment_snapshot_collects_versions_and_explicit_process(self):
        from analyzer.analyze_cpu_trace import run_analysis

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            proc, sys, out = fixture(base)
            put(base, 'etc/os-release', 'PRETTY_NAME="Fixture <Linux>"\n')
            put(proc, 'sys/kernel/numa_balancing', '1\n')
            put(base, 'Ascend/cann/runtime/version.info', 'version=8.0.fixture\n')
            put(proc, '4001/environ', 'OMP_NUM_THREADS=8\x00API_KEY=FIXTURE_SECRET\x00'
                'HCCL_CONNECT_TIMEOUT=unsafe://user:FIXTURE_SECRET@host\x00')
            put(proc, '4001/maps', '123-456 r-xp 0000 00:00 1 /opt/site lib/libhccl.so.1\n')
            put(proc, '4001/limits', 'Max open files            1024                 4096                 files\n')
            put(proc, '4002/environ', 'OMP_NUM_THREADS=99\x00')
            self.run_library(base, proc, sys, out,
                'DURATION=3; MINIMAL=0; HOSTBOUND=1; DISCOVER=1; PID_LIST=4001,4999\n'
                'hb_environment "%s" "%s"\nhb_manifest\n' % ((base/'etc').as_posix(), (base/'Ascend').as_posix()))
            report = base/'report'
            run_analysis(str(out), str(report), quiet=True)
            raw = (report/'report_data.json').read_text(encoding='utf-8')
            self.assertNotIn('FIXTURE_SECRET', raw)
            env = json.loads(raw)['metrics']['environment']
            host = {row['id']: row for row in env['host']}
            self.assertEqual(host['os_release']['value'], 'Fixture <Linux>')
            self.assertEqual(host['numa_balancing']['value'], '1')
            components = {row['id']: row for row in env['ascend']}
            self.assertEqual(components['runtime']['value'], '8.0.fixture')
            self.assertEqual(components['driver']['status'], 'UNSUPPORTED')
            targets = {row['pid']: row for row in env['targets']}
            self.assertEqual(set(targets), {4001, 4999})
            self.assertEqual(targets[4001]['starttime'], 10)
            self.assertEqual(targets[4001]['environment']['value'], {'OMP_NUM_THREADS': '8'})
            self.assertEqual(targets[4001]['libraries']['value'], ['/opt/site lib/libhccl.so.1'])
            self.assertEqual(targets[4001]['limits']['value']['Max open files']['hard'], '4096')
            self.assertEqual(targets[4999]['status'], 'EXECUTION_FAILED')
            html = (report/'hostbound_report.html').read_text(encoding='utf-8')
            self.assertIn("<h2 id='sec-6'>6. 采集环境与软件栈</h2>", html)
            npu_section = html.split("<h2 id='sec-2'>", 1)[1].split("<h2 id='sec-3'>", 1)[0]
            self.assertNotIn('environment_ascend', npu_section)
            self.assertIn('Fixture &lt;Linux&gt;', html)
            self.assertIn('/opt/site lib/libhccl.so.1', html)

            (out/'hostbound/environment/ascend/runtime.txt').unlink()
            put(out, 'hostbound/environment/pid_4001.json', '{"pid":4002}')
            run_analysis(str(out), str(report), quiet=True)
            metrics = json.loads((report/'report_data.json').read_text(encoding='utf-8'))['metrics']
            self.assertEqual(metrics['environment']['targets'][0]['status'], 'PARSE_FAILED')
            runtime = next(row for row in metrics['environment']['ascend'] if row['id'] == 'runtime')
            self.assertEqual(runtime['status'], 'NOT_INCLUDED')

    def test_environment_discards_snapshot_if_target_exits_mid_read(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            proc, sys, out = fixture(base)
            put(proc, '4001/environ', 'OMP_NUM_THREADS=8\x00')
            self.run_library(base, proc, sys, out, """
DURATION=3; MINIMAL=0; HOSTBOUND=1; DISCOVER=1
mkdir -p "$SNAP/hostbound/environment"
hb_environment_libraries(){ rm -- "$HB_PROC/4001/stat"; }
hb_environment_pid 4001
hb_manifest
""")
            self.assertFalse((out/'hostbound/environment/pid_4001.json').exists())
            rows = json.loads((out/'collection_manifest.json').read_text(encoding='utf-8'))['entries']
            self.assertEqual(rows[0]['status'], 'EXECUTION_FAILED')
            self.assertFalse(rows[0]['raw_exists'])

    def test_rank_environment_is_whitelisted_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            put(proc,'4001/environ','LOCAL_RANK=3\x00RANK=11\x00ASCEND_RT_VISIBLE_DEVICES=7,2\x00API_KEY=SECRET\x00')
            self.run_library(base,proc,sys,out,'hb_snapshot process "$SNAP/start.json"\n')
            raw=(out/'start.json').read_text();data=json.loads(raw)
            env=next(p for p in data['processes'] if p['tgid']==4001)['rank_env']
            self.assertEqual(env,{'LOCAL_RANK':'3','RANK':'11','ASCEND_RT_VISIBLE_DEVICES':'7,2'})
            self.assertNotIn('SECRET',raw)

    def run_library(self, base, proc, sys, out, commands):
        code=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
        library=code.split('# BEGIN standalone evidence')[1].split('# END standalone evidence')[0]
        script=base/'library.sh'
        script.write_text(library+'\nHB_PROC="%s"; HB_SYS="%s"; SNAP="%s"\nmkdir -p "$SNAP/hostbound"\n' % (proc.as_posix(),sys.as_posix(),out.as_posix())+commands,encoding='utf-8',newline='\n')
        result=subprocess.run([BASH,str(script)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result

    def test_v1_empty_leaf_inherits_but_missing_leaf_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            for path in ('4001/cgroup','4001/task/4001/cgroup'):
                put(proc,path,'2:cpuset:/job\n')
            put(sys,'fs/cgroup/cpuset/job/cpuset.cpus','\n')
            put(sys,'fs/cgroup/cpuset/cpuset.cpus','32-35\n')
            self.run_library(base,proc,sys,out,'hb_snapshot process "$SNAP/start.json"\n')
            data=json.loads((out/'start.json').read_text())
            self.assertEqual(data['threads'][0]['cpuset_cpus_effective'],[32,33,34,35])
            (sys/'fs/cgroup/cpuset/job/cpuset.cpus').unlink()
            self.run_library(base,proc,sys,out,'hb_snapshot process "$SNAP/end.json"\n')
            self.assertIsNone(json.loads((out/'end.json').read_text())['threads'][0]['cpuset_cpus_effective'])

    def test_resources_keep_raw_values_for_offline_python(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            put(proc,'meminfo','MemTotal: 1000000 kB\nMemAvailable: 40000 kB\n')
            put(proc,'diskstats','8 0 sda 1 0 10 1 1 0 20 1 0 2 3\n')
            self.run_library(base,proc,sys,out,'hb_snapshot resources "$SNAP/resource.json"\n')
            data=json.loads((out/'resource.json').read_text())
            self.assertEqual(data['clock'],'boottime')
            self.assertIn('MemAvailable: 40000 kB',data['files']['/proc/meminfo']['text'])
            self.assertEqual(data['files']['/proc/diskstats']['status'],'COLLECTED')
            self.assertIsNone(data['files']['/proc/pressure/io']['text'])

    def test_v1_memory_and_pids_controller_limits_are_collected(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            cg='2:cpuset:/job\n3:memory:/job\n4:pids:/job\n'
            for path in ('4001/cgroup','4001/task/4001/cgroup'):put(proc,path,cg)
            put(proc,'self/mountinfo','11 1 0:2 / %s rw - cgroup cgroup rw,cpuset\n12 1 0:3 / %s rw - cgroup cgroup rw,memory\n13 1 0:4 / %s rw - cgroup cgroup rw,pids\n' % tuple((sys/('fs/cgroup/'+x)).as_posix() for x in ('cpuset','memory','pids')))
            for path,value in [('memory/job/memory.usage_in_bytes','990'),('memory/job/memory.limit_in_bytes','1000'),('pids/job/pids.current','99'),('pids/job/pids.max','100')]:put(sys,'fs/cgroup/'+path,value)
            self.run_library(base,proc,sys,out,'hb_snapshot process "$SNAP/process.json"\n')
            rows=json.loads((out/'process.json').read_text())['processes'][0]['cgroup_cpu']
            self.assertTrue(any(r.get('memory.usage_in_bytes')=='990' for r in rows))
            self.assertTrue(any(r.get('pids.max')=='100' for r in rows))

    def test_custom_mount_root_maps_to_correct_cgroup(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            put(proc,'self/mountinfo','10 1 0:1 /tenant %s rw - cgroup2 cgroup rw\n' % (sys/'fs/cgroup').as_posix())
            for path in ('4001/cgroup','4001/task/4001/cgroup'):
                put(proc,path,'0::/tenant/job\n')
            put(sys,'fs/cgroup/tenant/job/cpuset.cpus.effective','0-31\n')
            self.run_library(base,proc,sys,out,'hb_snapshot process "$SNAP/start.json"\n')
            self.assertEqual(json.loads((out/'start.json').read_text())['threads'][0]['cpuset_cpus_effective'],list(range(32,64)))

    def test_v2_known_disabled_cpuset_inherits_and_timestamp_keeps_precision(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            put(proc,'uptime','1234567.89 0\n')
            (sys/'fs/cgroup/job/cpuset.cpus.effective').unlink()
            put(sys,'fs/cgroup/job/cgroup.controllers','cpu memory\n')
            put(sys,'fs/cgroup/cpuset.cpus.effective','32-63\n')
            self.run_library(base,proc,sys,out,'hb_snapshot process "$SNAP/start.json"\nhb_snapshot frequency "$SNAP/freq.json"\n')
            self.assertEqual(json.loads((out/'start.json').read_text())['threads'][0]['cpuset_cpus_effective'],list(range(32,64)))
            self.assertAlmostEqual(json.loads((out/'freq.json').read_text())['ts'],1234567.89)

    def test_unreadable_identity_is_an_error_not_no_active_process(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            (proc/'4001/stat').unlink();(proc/'4002/stat').unlink()
            self.run_library(base,proc,sys,out,'DURATION=1; MINIMAL=0; HOSTBOUND=1; DISCOVER=1\nhb_data process processes hostbound/process_start.json\nhb_manifest\n')
            manifest=json.loads((out/'collection_manifest.json').read_text())
            self.assertEqual(manifest['entries'][0]['status'],'EXECUTION_FAILED')
            self.assertTrue(json.loads((out/'hostbound/process_start.json').read_text())['errors'])

    def test_command_failures_keep_raw_returncode_and_missing_tool(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            commands='''DURATION=1; MINIMAL=0; HOSTBOUND=1; DISCOVER=1
hb_command npu_info hostbound/missing.txt cpu_fixture_tool_not_installed
hb_command npu_info hostbound/denied.txt bash -c 'echo "Permission denied"; exit 13'
hb_command npu_topology hostbound/unsupported.txt bash -c 'echo "not supported"; exit 2'
hb_manifest
'''
            result = self.run_library(base,proc,sys,out,commands)
            rows=json.loads((out/'collection_manifest.json').read_text())['entries']
            self.assertEqual([r['status'] for r in rows],['COMMAND_NOT_FOUND','PERMISSION_DENIED','UNSUPPORTED'])
            self.assertIn('hostbound/missing.txt', result.stderr)
            self.assertIn('COMMAND_NOT_FOUND', result.stderr)
            self.assertIn('executable_not_found:cpu_fixture_tool_not_installed', result.stderr)
            self.assertFalse(rows[0]['raw_exists'])
            self.assertEqual(rows[1]['returncode'],13)
            self.assertIn('Permission denied',(out/'hostbound/denied.txt').read_text())

    def test_single_file_snapshots_keep_identity_cpuset_and_topology(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            proc, sys, out = fixture(base)
            code = (REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
            library = code.split('# BEGIN standalone evidence')[1].split('# END standalone evidence')[0] if '# BEGIN standalone evidence' in code else ''
            script = base/'test.sh'
            script.write_text(library + '\nHB_PROC="%s"; HB_SYS="%s"; SNAP="%s"\nmkdir -p "$SNAP/hostbound"\nhb_snapshot process "$SNAP/hostbound/process_start.json"\nhb_snapshot topology "$SNAP/hostbound/numa_topology.json"\nhb_snapshot frequency "$SNAP/hostbound/frequency_start.json"\nhb_snapshot idle "$SNAP/hostbound/idle_start.json"\n' % (proc.as_posix(),sys.as_posix(),out.as_posix()),encoding='utf-8',newline='\n')
            result = subprocess.run([BASH, str(script)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            batch = json.loads((out/'hostbound/process_start.json').read_text())
            self.assertEqual([p['ppid'] for p in batch['processes']], [1,4001])
            self.assertEqual(batch['processes'][0]['numa_maps']['node_kb'], {'0':192})
            self.assertEqual(batch['threads'][0]['cpuset_cpus_effective'], list(range(32,64)))
            self.assertEqual(batch['threads'][0]['starttime'], 10)
            self.assertEqual(batch['processes'][0]['cmdline'], 'python train.py ')
            self.assertEqual(json.loads((out/'hostbound/numa_topology.json').read_text())['nodes'][0]['cpus'], list(range(64)))
            self.assertEqual(json.loads((out/'hostbound/frequency_start.json').read_text())['policies'][0]['scaling_cur_freq'],2400000)
            self.assertEqual(json.loads((out/'hostbound/idle_start.json').read_text())['states'][0]['time'],2000)

    def test_only_sh_file_full_capture_and_offline_report(self):
        import tarfile
        from analyzer.analyze_cpu_trace import run_analysis
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc,sys,out=fixture(base)
            trace=base/'tracefs';(trace/'instances').mkdir(parents=True)
            put(trace,'tracing_on','0')
            template=base/'template'
            for name,value in [('tracing_on','0'),('current_tracer','nop'),('buffer_size_kb','1'),('trace_clock','[local] boot'),('trace',''),('events/enable','0'),('events/sched/sched_switch/enable','0')]:
                put(template,name,value)
            put(template,'per_cpu/cpu32/trace','a-4001 [032] d..2 20.000000: sched_switch: prev_comm=swapper prev_pid=0 prev_state=S next_comm=worker next_pid=4001\na-4001 [032] d..2 21.000000: sched_switch: prev_comm=worker prev_pid=4001 prev_state=S next_comm=swapper next_pid=0\n')
            put(template,'per_cpu/cpu32/stats','entries: 2\noverrun: 0\ndropped events: 0\n')
            put(proc,'sys/kernel/random/boot_id','fixture-boot')
            tools=base/'bin'
            for name in ('python','python3','jq'):
                put(tools,name,'#!/bin/bash\ntouch "%s"\nexit 99\n' % (base/'forbidden-runtime').as_posix())
            put(tools,'npu-smi','''#!/bin/bash
if [[ "$*" == 'info -t topo' ]]; then printf 'NPU CPU Affinity\nNPU0 X 32-63\n'
elif [[ "$*" == 'info -m' ]]; then printf 'NPU ID Physical ID\n0 0\n'
else printf '| NPU Chip | Process id | Process name | Process memory(MB) |\n| 0 0 | 4001 | worker | 100 |\n';fi
''')
            put(tools,'lscpu','#!/bin/bash\nif [[ "$1" == -p=* ]]; then\n'+''.join('echo %d,%d,0,0,Y\n' % (c,c) for c in range(64))+'fi\n')
            put(tools,'systemd-detect-virt','#!/bin/bash\necho none\nexit 1\n')
            for name in ('numactl','numastat','pstree','ps','df','lsblk'):
                put(tools,name,'#!/bin/bash\necho fixture-optional-command\n')
            put(tools,'taskset','#!/bin/bash\n[[ "$#" == 2 && "$1" == -pc ]] || exit 98\necho "query only"\n')
            code=(REPO/'scripts/cpu_trace_collect.sh').read_text(encoding='utf-8')
            code=code.replace('HB_PROC=/proc; HB_SYS=/sys','HB_PROC="%s"; HB_SYS="%s"' % (proc.as_posix(),sys.as_posix()))
            code=code.replace('for d in /sys/kernel/tracing /sys/kernel/debug/tracing;','for d in "%s";' % trace.as_posix())
            code=code.replace('"$EUID"','"0"').replace('[[ -e /.dockerenv || -e /run/.containerenv ]]','false')
            setup='''
chmod +x "TOOLS"/*
export PATH="TOOLS:$PATH"
mkdir(){ command mkdir "$@" || return $?; if [[ "$1" == */instances/cpu_host_* ]]; then cp -R "TEMPLATE/." "$1/"; fi; }
rmdir(){ :; }
'''.replace('TOOLS',('/'+tools.drive[0].lower()+tools.as_posix()[2:]) if os.name=='nt' else tools.as_posix()).replace('TEMPLATE',template.as_posix())
            script=base/'cpu_trace_collect.sh'
            script.write_text(code.replace('set +e',setup+'\nset +e',1),encoding='utf-8',newline='\n')
            outarg=('/'+out.drive[0].lower()+out.as_posix()[2:]) if os.name=='nt' else out.as_posix()
            result=subprocess.run([BASH,str(script),'--duration','1','--output',outarg],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse((base/'forbidden-runtime').exists())
            self.assertFalse(list(base.glob('*.py')))
            capture=next(p for p in out.glob('cpu_trace_*') if p.is_dir())
            manifest=json.loads((capture/'collection_manifest.json').read_text())
            self.assertEqual([r for r in manifest['entries'] if r['category']=='trace'][-1]['status'],'COLLECTED')
            self.assertTrue((capture/'hostbound/process_end.json').exists())
            self.assertTrue((capture/'hostbound/resource_end.json').exists())
            self.assertTrue((capture/'hostbound/resource_samples.jsonl').exists())
            archives=list(out.glob('cpu_trace_*.tar.gz'))
            self.assertTrue(archives,result.stdout+result.stderr)
            archive=archives[0]
            with tarfile.open(archive) as package:
                self.assertIn(capture.name+'/hostbound/frequency_samples.jsonl',package.getnames())
                self.assertIn(capture.name+'/hostbound/environment/host/kernel.txt',package.getnames())
            run_analysis(str(archive),str(base/'report'),quiet=True)
            metrics=json.loads((base/'report/report_data.json').read_text(encoding='utf-8'))['metrics']
            self.assertEqual(metrics['environment']['target_scope'], 'NOT_REQUESTED')
            self.assertIn('taskset -apc 32-63 4001',metrics['npu_affinity']['commands'],str(metrics['npu_affinity']))
            self.assertNotIn(' 4002',metrics['npu_affinity']['commands'])
            self.assertEqual(metrics['binding_plan']['commands'],'taskset -apc 32-63 4001')
