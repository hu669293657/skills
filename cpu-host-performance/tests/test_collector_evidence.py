"""Read-only collector regression tests with synthetic files and child commands."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import host_snapshot as snapshot


def put(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def stat(pid, start=10):
    fields = ['0'] * 40
    fields[0] = 'S'; fields[1] = '1'; fields[19] = str(start)
    return '%d (worker) ' % pid + ' '.join(fields)


class CollectionEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_path = SCRIPTS / 'collection_evidence.py'

    def evidence(self):
        self.assertTrue(self.module_path.exists(), 'manifest collector is missing')
        spec = importlib.util.spec_from_file_location('collection_evidence', self.module_path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_command_failure_states_and_raw_evidence(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            missing = module.capture_command(d, 'npu_info', ['unavailable-cpu-collector-command'], 'missing.txt')
            self.assertEqual(missing['status'], 'COMMAND_NOT_FOUND')
            self.assertFalse(missing['command_exists'])
            denied = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'import sys;sys.stderr.write("Permission denied");sys.exit(1)'], 'denied.txt')
            self.assertEqual(denied['status'], 'PERMISSION_DENIED')
            unsupported = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'import sys;sys.stderr.write("unsupported option");sys.exit(2)'], 'unsupported.txt')
            self.assertEqual(unsupported['status'], 'UNSUPPORTED')
            failed = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'import sys;sys.stderr.write("device communication failed");sys.exit(4)'], 'failed.txt')
            self.assertEqual(failed['status'], 'EXECUTION_FAILED')
            self.assertTrue(failed['raw_exists']); self.assertFalse(failed['empty'])
            self.assertEqual(failed['returncode'], 4)
            self.assertLessEqual(failed['started_at'], failed['ended_at'])
            timed_out = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'import time;print("partial",flush=True);time.sleep(2)'], 'timeout.txt', timeout=.1)
            self.assertEqual(timed_out['status'], 'EXECUTION_FAILED')
            self.assertIn('timed out', timed_out['failure_reason'])
            self.assertTrue(timed_out['raw_exists'])

    def test_parser_results_empty_process_and_unparseable_are_distinct(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            parser = lambda text: json.loads(text)
            empty = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'print("[]")'], 'empty.txt', parser=parser, empty_status='NO_ACTIVE_PROCESS')
            self.assertEqual(empty['status'], 'NO_ACTIVE_PROCESS'); self.assertTrue(empty['parse_success'])
            parsed = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'print("[1,2]")'], 'parsed.txt', parser=parser)
            self.assertEqual(parsed['parsed_count'], 2); self.assertEqual(parsed['status'], 'COLLECTED')
            bad = module.capture_command(d, 'npu_info', [sys.executable, '-c', 'print("not a table")'], 'bad.txt', parser=parser)
            self.assertEqual(bad['status'], 'PARSE_FAILED'); self.assertFalse(bad['parse_success'])
            skipped = module.capture_command(d, 'npu_info', ['anything'], 'skip.txt', requested=False)
            self.assertEqual(skipped['status'], 'NOT_REQUESTED'); self.assertFalse(skipped['raw_exists'])

    def test_manifest_has_explicit_not_requested_tools_and_reconciled_paths(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            module.collect_phase(d, 'start', hostbound=False, discover=False, proc_root=d, sys_root=d)
            manifest = json.loads((Path(d) / 'hostbound/collection_manifest.json').read_text())
            self.assertEqual(manifest['schema_version'], 1)
            entries = manifest['entries']
            tools = [e for e in entries if e['category'] == 'external_tools']
            self.assertTrue(tools); self.assertTrue(all(e['status'] == 'NOT_REQUESTED' for e in tools))
            self.assertTrue(any(e['category'] == 'npu_info' and e['status'] == 'NOT_REQUESTED' for e in entries))
            self.assertTrue(any(e['category'] == 'numa_process' and e['status'] == 'NOT_REQUESTED' for e in entries))
            for entry in entries:
                for key in ('category', 'requested', 'command', 'command_exists', 'returncode', 'raw_path', 'raw_exists', 'empty', 'parse_success', 'parsed_count', 'started_at', 'ended_at', 'failure_reason', 'status'):
                    self.assertIn(key, entry)
                if entry['raw_exists']:
                    self.assertTrue((Path(d) / entry['raw_path']).is_file())

    def test_topology_keeps_numa_distances_and_memory(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, 'devices/system/cpu/online', '0-1,4')
            put(d, 'devices/system/node/online', '0,2')
            put(d, 'devices/system/node/node2/cpulist', '4')
            put(d, 'devices/system/node/node2/distance', '30 10')
            put(d, 'devices/system/node/node2/meminfo', 'Node 2 MemTotal: 4096 kB\nNode 2 MemFree: 2048 kB')
            data = snapshot.topology_snapshot(d)
            self.assertEqual(data['online_cpus'], [0, 1, 4])
            self.assertEqual(data['online_nodes'], [0, 2])
            node = data['nodes'][0]
            self.assertEqual(node['node'], 2); self.assertEqual(node['distance'], [30, 10])
            self.assertEqual(node['memory_kb']['MemTotal'], 4096)

    def test_v1_cpu_and_cpuset_ancestors_include_mems(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, 'fs/cgroup/cpu/jobs/task/cpu.cfs_quota_us', '200000')
            put(d, 'fs/cgroup/cpu/jobs/cpu.cfs_quota_us', '100000')
            put(d, 'fs/cgroup/cpuset/jobs/task/cpuset.cpus', '')
            put(d, 'fs/cgroup/cpuset/jobs/cpuset.cpus', '2-3')
            put(d, 'fs/cgroup/cpuset/jobs/cpuset.mems', '1')
            rows = snapshot.cgroup_snapshot('2:cpu,cpuacct:/jobs/task\n3:cpuset:/jobs/task', d)
            self.assertTrue(any(r.get('cpuset.cpus') == '2-3' and r.get('cpuset.mems') == '1' for r in rows))
            self.assertTrue(any(r.get('cpu.cfs_quota_us') == '100000' for r in rows))

    def test_process_identity_namespace_mems_and_numa_maps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); proc = root / 'proc'; sysroot = root / 'sys'
            put(proc, '100/stat', stat(100)); put(proc, '100/comm', 'worker')
            put(proc, '100/status', 'Name:\tworker\nPid:\t100\nTgid:\t100\nNSpid:\t100 7\nCpus_allowed_list:\t0-3\nMems_allowed_list:\t1')
            put(proc, '100/numa_maps', '7f default anon=4 dirty=4 N1=4 kernelpagesize_kB=64\n8f bind:1 mapped=2 N1=2 kernelpagesize_kB=4')
            put(proc, '100/task/100/stat', stat(100))
            put(proc, '100/task/100/status', 'Name:\tworker\nPid:\t100\nTgid:\t100\nNSpid:\t100 7\nCpus_allowed_list:\t0-3\nMems_allowed_list:\t1')
            data = snapshot.process_snapshot(str(proc), str(sysroot))
            process = data['processes'][0]; thread = data['threads'][0]
            self.assertEqual(process['namespace_pids'], [100, 7]); self.assertEqual(thread['namespace_pids'], [100, 7])
            self.assertEqual(process['numa_maps']['node_kb'], {'1': 264})
            self.assertEqual(thread['mems_allowed_list'], '1')
            self.assertIn('identity_started_at', process); self.assertIn('identity_verified_at', process)

    def test_process_reuse_during_snapshot_rejects_parent_and_threads(self):
        with tempfile.TemporaryDirectory() as d:
            put(d, '100/stat', stat(100)); put(d, '100/task/100/stat', stat(100))
            put(d, '100/task/100/status', 'Tgid:\t100\nPid:\t100')
            original = snapshot.read; calls = [0]
            def racing_read(path):
                if Path(path) == Path(d) / '100/stat':
                    calls[0] += 1
                    return stat(100, 10 if calls[0] == 1 else 11)
                return original(path)
            with mock.patch.object(snapshot, 'read', side_effect=racing_read):
                data = snapshot.process_snapshot(d, d)
            self.assertEqual(data['processes'], []); self.assertEqual(data['threads'], [])
            self.assertTrue(any(e['reason'] == 'identity_changed_during_snapshot' for e in data['errors']))

    def test_thread_specific_cgroup_and_effective_mems_override_leader(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); proc = root/'proc'; sysroot = root/'sys'
            put(proc, '100/stat', stat(100)); put(proc, '100/cgroup', '0::/leader')
            put(proc, '100/task/101/stat', stat(101))
            put(proc, '100/task/101/status', 'Tgid:\t100\nPid:\t101\nCpus_allowed_list:\t4-5')
            put(proc, '100/task/101/cgroup', '0::/thread')
            put(sysroot, 'fs/cgroup/leader/cpuset.cpus.effective', '0-1')
            put(sysroot, 'fs/cgroup/thread/cpuset.cpus.effective', '4-5')
            put(sysroot, 'fs/cgroup/thread/cpuset.mems.effective', '2')
            put(sysroot, 'fs/cgroup/thread/cpu.max', '50000 100000')
            data = snapshot.process_snapshot(str(proc), str(sysroot))
            thread = data['threads'][0]
            self.assertEqual(thread['cpuset_cpus_effective'], [4, 5])
            self.assertEqual(thread['cgroup'], '0::/thread')
            self.assertEqual(thread['cgroup_cpu'][0]['cpu.max'], '50000 100000')
            self.assertEqual(thread['cpuset_mems_effective'], [2])
            self.assertEqual(data['processes'][0]['pid'], 100)
            self.assertIn('cpu_time_sec', data['processes'][0])

    def test_proc_permission_error_is_not_reported_as_no_active_process(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); proc = root/'proc'; put(proc, '100/stat', stat(100))
            original = Path.read_text
            def denied(path, *args, **kwargs):
                if path == proc/'100/stat':
                    raise PermissionError('Permission denied')
                return original(path, *args, **kwargs)
            with mock.patch.object(Path, 'read_text', denied):
                row, data = module.capture_data(root, 'processes', 'hostbound/process_start.json', lambda: snapshot.process_snapshot(str(proc), d), 'processes', empty_status='NO_ACTIVE_PROCESS')
            self.assertEqual(row['status'], 'PERMISSION_DENIED')
            self.assertEqual(data['errors'][0]['status'], 'PERMISSION_DENIED')

    def test_trace_finalization_ledgers_poll_samples_and_event_capabilities(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            put(d, 'trace.txt', 'worker-1 [000] .... 1.0: sched_switch: prev_pid=0 next_pid=1\n')
            put(d, 'hostbound/frequency_samples.jsonl', '{"ts":1,"policies":[]}\n{"ts":2,"policies":[]}\n')
            put(d, 'hostbound/capabilities.tsv', 'event\texists\twritable\trequested\tenabled\treason\nsched/sched_switch\t1\t1\t1\t1\tok\n')
            module.mark_trace(d, 'COLLECTED')
            entries=json.loads((Path(d)/'collection_manifest.json').read_text())['entries']
            poll=[e for e in entries if e['raw_path']=='hostbound/frequency_samples.jsonl']
            self.assertEqual(len(poll),1)
            self.assertEqual(poll[0]['parsed_count'],2); self.assertTrue(poll[0]['parse_success'])
            self.assertTrue(any(e['category']=='trace_capabilities' and e['parse_success'] for e in entries))

    def test_namespace_unknown_npu_pid_is_not_assumed_to_be_local(self):
        module = self.evidence()
        self.assertTrue(hasattr(module, 'resolve_target_pids'), 'namespace-aware target resolution is missing')
        observed=[{'pid':100,'pid_namespace':'unknown'}, {'pid':200,'pid_namespace':'host'}]
        targets, unresolved=module.resolve_target_pids([300], observed, 'container')
        self.assertEqual(targets,{300})
        self.assertEqual({p['pid'] for p in unresolved},{100,200})
        targets, unresolved=module.resolve_target_pids([], observed, 'host')
        self.assertEqual(targets,{100,200}); self.assertEqual(unresolved,[])

    def test_real_subprocess_npu_formats_keep_raw_and_normalized_cpu_mapping(self):
        module = self.evidence(); info, mapping, topo = module.npu_parsers()
        self.assertIsNotNone(info)
        cases=[(info, 'NPU ID | Chip ID | Process ID | Process name | Process Memory(MB)\n0 | 1 | 100 | worker | 32', 'COLLECTED'),
               (info, 'No running processes found', 'NO_ACTIVE_PROCESS'),
               (mapping, 'NPU ID | Chip ID | CPU Affinity | NUMA ID\n0 | 1 | 8-11 | 2', 'COLLECTED'),
               (topo, 'NPU0 NPU1\nNPU0 X PHB\nNPU1 PHB X', 'COLLECTED')]
        with tempfile.TemporaryDirectory() as d:
            for number,(parser,raw,status) in enumerate(cases):
                row=module.capture_command(d,'npu_info',[sys.executable,'-c','import sys;sys.stdout.write('+repr(raw)+')'],'npu%d.txt'%number,parser=parser,empty_status='NO_ACTIVE_PROCESS')
                self.assertEqual(row['status'],status)
                self.assertEqual((Path(d)/row['raw_path']).read_text(),raw)
            self.assertEqual(mapping(cases[2][1])['devices'][0]['cpu_affinity'],'8-11')
            self.assertTrue(all(not x['cpu_affinity'] for x in topo(cases[3][1])['devices']))

    def test_end_missing_npu_command_does_not_reuse_start_no_process_status(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            manifest=module.load_manifest(d)
            previous=module.entry('npu_info',True,['npu-smi','info'],None)
            previous['status']='NO_ACTIVE_PROCESS'; manifest['entries'].append(previous)
            module.save_manifest(d,manifest)
            with mock.patch.object(module.shutil,'which',return_value=None):
                result=module.collect_phase(d,'end',hostbound=True,discover=False,proc_root=d,sys_root=d)
            outcome=[e for e in result['entries'] if e['category']=='numa_process'][-1]
            self.assertEqual(outcome['status'],'NOT_INCLUDED')

    def test_unreadable_leaf_cpuset_does_not_invent_effective_ancestor(self):
        rows=[{'version':1,'controllers':'cpuset','path':'/cg/job'},
              {'version':1,'controllers':'cpuset','path':'/cg','cpuset.cpus':'0-7'}]
        self.assertIsNone(snapshot.effective_cpuset(rows))
        rows[0]['cpuset.cpus']=''
        self.assertEqual(snapshot.effective_cpuset(rows),list(range(8)))
        rows[0].update(version=2,controllers='unified',**{'cpuset.cpus':'2-3'})
        self.assertIsNone(snapshot.effective_cpuset(rows))

    def test_unreadable_thread_cgroup_does_not_assert_leader_cpuset(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);proc=root/'proc';sysroot=root/'sys'
            put(proc,'100/stat',stat(100));put(proc,'100/cgroup','0::/leader')
            put(proc,'100/task/101/stat',stat(101))
            put(proc,'100/task/101/status','Tgid:\t100\nPid:\t101\nCpus_allowed_list:\t0-1')
            put(sysroot,'fs/cgroup/leader/cpuset.cpus.effective','0-1')
            data=snapshot.process_snapshot(str(proc),str(sysroot))
            self.assertIsNone(data['threads'][0]['cpuset_cpus_effective'])
            self.assertEqual(data['threads'][0]['cgroup_source'],'leader_fallback')

    def test_bare_host_detector_expected_rc_one_is_successful_negative_probe(self):
        module = self.evidence()
        with tempfile.TemporaryDirectory() as d:
            put(d,'hostbound/container_detector.txt','none\n')
            probe=module.entry('host_context',True,['systemd-detect-virt','--container'],'hostbound/container_detector.txt')
            probe.update(returncode=1,status='EXECUTION_FAILED',failure_reason='none',command_exists=True)
            entries=[]
            with mock.patch.object(module,'capture_command',return_value=probe):
                module.collect_context(Path(d),d,entries)
            self.assertEqual(entries[0]['status'],'COLLECTED')
            self.assertEqual(entries[0]['returncode'],1)
            self.assertTrue(entries[0]['parse_success'])
            self.assertEqual(entries[0]['parsed_count'],0)

    def test_trace_preflight_failure_does_not_claim_poll_artifacts_generated(self):
        module=self.evidence()
        for status in ('UNSUPPORTED','PERMISSION_DENIED'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as d:
                module.mark_trace(d,status,'preflight_trace_unavailable')
                entries=json.loads((Path(d)/'collection_manifest.json').read_text())['entries']
                artifacts=[e for e in entries if e['category'] in ('frequency','trace_capabilities')]
                self.assertEqual(len(artifacts),2)
                for row in artifacts:
                    self.assertEqual(row['status'],status)
                    self.assertIn('not_started',row['failure_reason'])
                    self.assertFalse(row['raw_exists']);self.assertIsNone(row['empty'])

    def test_completed_trace_missing_acquisition_artifacts_is_execution_failure(self):
        module=self.evidence()
        with tempfile.TemporaryDirectory() as d:
            put(d,'trace.txt','worker-1 [000] .... 1.0: sched_switch: prev_pid=0 next_pid=1\n')
            module.mark_trace(d,'COLLECTED')
            entries=json.loads((Path(d)/'collection_manifest.json').read_text())['entries']
            artifacts=[e for e in entries if e['category'] in ('frequency','trace_capabilities')]
            for row in artifacts:
                self.assertEqual(row['status'],'EXECUTION_FAILED')
                self.assertIn('not_generated',row['failure_reason'])

    def test_previously_generated_poll_artifact_then_missing_is_not_included(self):
        module=self.evidence()
        with tempfile.TemporaryDirectory() as d:
            put(d,'hostbound/frequency_samples.jsonl','{"ts":1,"policies":[]}\n')
            module.mark_trace(d,'EXECUTION_FAILED','interrupted_after_poll')
            (Path(d)/'hostbound/frequency_samples.jsonl').unlink()
            module.mark_trace(d,'EXECUTION_FAILED','resumed_finalization')
            entries=json.loads((Path(d)/'collection_manifest.json').read_text())['entries']
            row=[e for e in entries if e['raw_path']=='hostbound/frequency_samples.jsonl'][-1]
            self.assertEqual(row['status'],'NOT_INCLUDED')
            self.assertIn('previously_generated',row['failure_reason'])


if __name__ == '__main__':
    unittest.main()
