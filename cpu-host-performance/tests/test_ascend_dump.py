"""Interchange with the native msprechecker dump format."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from analyzer.analyze_cpu_trace import run_analysis
from analyzer.parser import load_input


class AscendDumpTests(unittest.TestCase):
    def test_standalone_and_packaged_dump_have_the_same_environment_evidence(self):
        dump = {
            '_meta': {'tool': 'msprechecker_dump.py', 'schema_version': '1.2',
                      'status': 'partial', 'collection': {'npu': {'status': 'failed'}}},
            'system': {'os_release': {'PRETTY_NAME': '"Fixture Linux"'}},
            'ascend': {'runtime': {'version': '8.0.fixture', 'collection_status': 'success'},
                       'driver': {'collection_status': 'unavailable', 'reason': 'fixture missing file'}},
            'workload': {'scope': 'explicit_pid', 'targets': {'worker-0': {
                'pid': 4001, 'start_time_ticks': 10, 'identity': {'exe': '/opt/venv/bin/python'},
                'environment': {'source': 'proc_exec_environment', 'values': {'OMP_NUM_THREADS': '8'}},
                'libraries': {'source': 'proc_maps', 'paths': ['/opt/ascend/libhccl.so']},
                'process_limits': 'Max open files            1024                 4096                 files',
            }}},
        }
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = json.dumps(dump, indent=4).encode('utf-8')
            standalone = base / 'custom-name.json'
            standalone.write_bytes(raw)
            archive = base / 'capture.zip'
            with zipfile.ZipFile(archive, 'w') as package:
                package.writestr('cpu_trace/hostbound/environment/msprechecker_dumped.json', raw)
                package.writestr('cpu_trace/trace.txt',
                                 'worker-4001 [000] .... 1.000000: sched_switch: prev_pid=4001 next_pid=0\n')

            td = load_input(str(standalone))
            self.assertEqual(td.format_name, 'Ascend Dump JSON')
            self.assertEqual(td.parse_stats['total'], 0)
            for source in (standalone, archive):
                with self.subTest(source=source.name):
                    output = base / (source.stem + '-report')
                    run_analysis(str(source), str(output), quiet=True)
                    metrics = json.loads((output / 'report_data.json').read_text(encoding='utf-8'))['metrics']
                    env = metrics['environment']
                    self.assertEqual(env['native_dump']['status'], 'partial')
                    self.assertEqual(env['native_dump']['collection']['npu']['status'], 'failed')
                    self.assertEqual(env['host'][0]['value'], 'Fixture Linux')
                    self.assertEqual(env['ascend'][0]['value'], '8.0.fixture')
                    self.assertEqual(env['targets'][0]['role'], 'worker-0')
                    self.assertEqual(env['targets'][0]['limits']['value']['Max open files']['hard'], '4096')
                    html = (output / 'hostbound_report.html').read_text(encoding='utf-8')
                    self.assertIn('/opt/ascend/libhccl.so', html)
                    self.assertIn('worker-0', html)
                    self.assertEqual(metrics['parse_stats']['parsed'], 0 if source == standalone else 1)
                    self.assertFalse(metrics['binding_plan']['commands'])
            self.assertEqual(standalone.read_bytes(), raw)
