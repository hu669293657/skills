"""Exercise real archive loading, manifest reconciliation, analysis and report output."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from examples.generate_synthetic import generate
from analyzer.analyze_cpu_trace import run_analysis


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.output = cls.base / 'tar-report'
        cls.archive = generate(cls.output, include_environment=False)
        cls.data = json.loads((cls.output / 'report_data.json').read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_raw_archive_to_manifest_to_json_and_merged_diagnosis_html(self):
        m = self.data['metrics']
        self.assertEqual(m['metadata']['data_origin'], 'SYNTHETIC_TEST_DATA')
        self.assertEqual(m['collection_integrity']['manifest_status'], 'COLLECTED')
        self.assertEqual(m['topology']['cpu_count'], 256)
        self.assertEqual(m['topology']['numa_count'], 8)
        self.assertEqual(len(m['npu_affinity']['rows']), 8)
        self.assertTrue(m['idle']['available'])
        self.assertEqual(len(m['frequency']['domains']), 8)
        self.assertTrue(any(t['confidence'] == 'HIGH' and t['npu_ids'] for t in m['thread_metrics']))
        self.assertTrue(any(c['npu_ids'] and c['causality'] == 'UNPROVEN' for c in m['contention']['candidates']))
        html = (self.output / 'hostbound_report.html').read_text(encoding='utf-8')
        self.assertIn('2.3 NPU', html)
        self.assertNotIn('<script src=', html)
        self.assertNotIn('最小补采集', html)
        self.assertIn('NUMA', html)
        self.assertNotIn('Idle Residency', html)
        self.assertEqual(len(m['per_cpu']), 256)
        self.assertNotIn("id='sec-13'", html)
        for n in range(1, 7):
            self.assertIn("<h2 id='sec-%d'>" % n, html)
        self.assertEqual(m['environment']['status'], 'UNKNOWN')
        self.assertIn('此采集包没有环境快照', html)
        visible = html.split("<h2 id='sec-2'>", 1)[0]
        self.assertIn('核心问题：调度延迟严重', visible)
        self.assertIn('同一个 CPU 核', visible)
        self.assertIn('限流', visible)
        self.assertIn('0.021–0.060', visible)
        self.assertNotIn('Top CPU Runtime', html)
        self.assertIn('忙核所在的 CPU 组报告过低频', visible)
        self.assertIn('业务任务的 CPU 运行范围需要核对', visible)

    def test_real_evidence_binding_targets_stay_disjoint_in_cpuset(self):
        rows = self.data['metrics']['npu_affinity']['rows']
        from analyzer.npu_affinity import cpus
        used = set()
        executable = [r for r in rows if r.get('target') and not r.get('blockers') and r['state'] != 'LOCAL']
        self.assertTrue(executable, 'the fixture must exercise guarded executable recommendations')
        for row in executable:
            self.assertIn(row['pid'], range(4000, 4008))
            target = cpus(row['target'])
            self.assertTrue(target <= cpus(row['affinity']))
            self.assertFalse(used & target)
            used.update(target)
            self.assertIn('taskset -apc %s %d' % (row['target'], row['pid']), self.data['metrics']['npu_affinity']['commands'])
        self.assertTrue(any(r['state'] == 'LOCAL' for r in rows))

    def test_zip_and_directory_preserve_evidence_and_results(self):
        capture = self.output / 'synthetic_capture'
        archive = self.base / 'case.zip'
        with zipfile.ZipFile(str(archive), 'w') as zf:
            for path in capture.rglob('*'):
                if path.is_file():
                    zf.write(str(path), path.relative_to(capture).as_posix())
        for source in (capture, archive):
            out = self.base / ('zip' if source == archive else 'dir')
            run_analysis(str(source), str(out), quiet=True)
            metrics = json.loads((out / 'report_data.json').read_text(encoding='utf-8'))['metrics']
            self.assertEqual(metrics['topology'], self.data['metrics']['topology'])
            self.assertEqual(metrics['npu_affinity']['commands'], self.data['metrics']['npu_affinity']['commands'])
            self.assertEqual(metrics['collection_integrity']['manifest_status'], 'COLLECTED')
            self.assertFalse((out / 'report.md').exists())
            self.assertTrue((out / 'bind_cpu.sh').is_file())


if __name__ == '__main__':
    unittest.main()
