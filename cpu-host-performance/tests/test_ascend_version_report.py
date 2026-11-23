"""Check which package evidence reaches the environment report."""
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from analyzer.analyze_cpu_trace import run_analysis
from analyzer.collection_integrity import analyze_collection_integrity
from analyzer.environment_analysis import NATIVE_DUMP_PATH, analyze_environment


class AscendVersionReportTests(unittest.TestCase):
    def test_prefer_parsed_component_files_and_fill_gaps_from_native_dump(self):
        prefix = 'hostbound/environment/ascend/'
        source = '/opt/Ascend/cann/share/info/runtime/version.info'
        native = {
            '_meta': {'tool': 'msprechecker_dump.py'},
            'system': {'release': 'native-kernel'},
            'ascend': {
                'runtime': {'version': '8.0', 'source': 'native/runtime'},
                'hccl': {'version': '8.1', 'source': 'native/hccl'},
                'driver': {'version': '25.3', 'source': 'native/driver'},
                'mindie': {'version': 'old', 'collection_status': 'failed'},
                'compiler': {'version': '8.2', 'source': 'native/compiler'},
                'opp_kernel': {'version': '8.2', 'source': 'native/opp_kernel'},
            },
        }
        versions = {'runtime': 'Version=9.2.0', 'hccl': 'unknown format',
                    'driver': None, 'atb': 'Ascend-cann-atb Version: 8.5', 'mindie': None, 'compiler': None}
        snapshot = {NATIVE_DUMP_PATH: json.dumps(native)}
        entries = []
        for key, content in versions.items():
            path = prefix + key + '.txt'
            entry = {'category': 'environment_ascend', 'raw_path': path,
                     'status': 'UNSUPPORTED' if content is None else 'COLLECTED'}
            if content is not None:
                snapshot[path] = content
            if key == 'runtime':
                entry['source'] = source
            if key == 'compiler':
                entry['failure_reason'] = 'ambiguous_cann_installation'
            entries.append(entry)
        snapshot['collection_manifest.json'] = json.dumps({'schema_version': 1, 'entries': entries})
        report = analyze_environment(snapshot, analyze_collection_integrity({'snapshot': snapshot}))
        components = {row['id']: row for row in report['ascend']}
        self.assertEqual(list(components), [*versions, 'opp_kernel'])
        self.assertEqual((components['runtime']['value'], components['runtime']['source']), ('9.2.0', source))
        self.assertEqual((components['hccl']['value'], components['hccl']['source']), ('8.1', 'native/hccl'))
        self.assertEqual((components['driver']['value'], components['driver']['source']), ('25.3', 'native/driver'))
        self.assertEqual(components['atb']['value'], '8.5')
        self.assertIsNone(components['mindie']['value'])
        self.assertEqual(components['mindie']['status'], 'UNSUPPORTED')
        self.assertIsNone(components['compiler']['value'])
        self.assertEqual(components['compiler']['reason'], 'ambiguous_cann_installation')
        self.assertEqual(components['opp_kernel']['source'], 'native/opp_kernel')
        original = {row['id']: row for row in report['bash_snapshot']['ascend']}
        self.assertEqual(original['hccl']['status'], 'PARSE_FAILED')
        self.assertEqual(original['driver']['status'], 'UNSUPPORTED')
        self.assertEqual(report['host'][0]['value'], 'native-kernel')
        self.assertEqual(report['native_dump']['capture_status'], 'COLLECTED')

    def test_native_host_only_preserves_legacy_version_file_without_manifest(self):
        path = 'hostbound/environment/ascend/runtime.txt'
        snapshot = {
            path: 'Version=8.0',
            NATIVE_DUMP_PATH: json.dumps({'_meta': {'tool': 'msprechecker_dump.py'},
                                         'system': {'release': 'native-kernel'}}),
        }
        report = analyze_environment(snapshot, analyze_collection_integrity({'snapshot': snapshot}))
        self.assertEqual([(row['id'], row['value'], row['source']) for row in report['ascend']],
                         [('runtime', '8.0', path)])

    def test_html_distinguishes_toolkit_package_and_asc_devkit_component(self):
        raw_path = 'hostbound/environment/ascend/toolkit.txt'
        source = '/opt/Ascend/cann/share/info/asc-devkit/version.info'
        manifest = {'schema_version': 1, 'entries': [
            {'category': 'environment_ascend', 'status': 'COLLECTED', 'source': source, 'raw_path': raw_path},
        ]}
        package_info = '/opt/Ascend/cann/aarch64-linux/ascend_toolkit_install.info'
        for native_source, version, selected in (
                (None, None, source),
                (source, 'different', source),
                (package_info, ' ', source),
                ('/usr/local/Ascend/cann/aarch64-linux/ascend_toolkit_install.info', '9.0.0', source),
                (package_info, '9.0.0', package_info)):
            with self.subTest(native_source=native_source, version=version), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                with ZipFile(base / 'capture.zip', 'w') as archive:
                    archive.writestr('cpu_trace/' + raw_path, 'Version=9.2.0\n')
                    archive.writestr('cpu_trace/collection_manifest.json', json.dumps(manifest))
                    archive.writestr('cpu_trace/trace.txt',
                                     'worker-1 [000] .... 1.000: sched_switch: prev_pid=1 next_pid=0\n')
                    if native_source:
                        data = {'_meta': {'tool': 'msprechecker_dump.py'},
                                'ascend': {'toolkit': {'source': native_source, 'version': version}}}
                        archive.writestr('cpu_trace/' + NATIVE_DUMP_PATH, json.dumps(data))
                run_analysis(str(base / 'capture.zip'), str(base / 'report'), quiet=True)
                html = (base / 'report/hostbound_report.html').read_text(encoding='utf-8')
                metrics = json.loads((base / 'report/report_data.json').read_text(encoding='utf-8'))['metrics']
                row, = metrics['environment']['ascend']
                self.assertEqual(row['source'], selected)
                label = 'asc-devkit 子包' if selected == source else 'CANN Toolkit'
                value = '9.2.0' if selected == source else '9.0.0'
                self.assertEqual(row['value'], value)
                self.assertIn(f'<td>{label}</td><td>{value}</td><td>{selected}</td>', html)
