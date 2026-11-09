"""配套检查的离线 CLI 回归。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / 'ascend-dump-analyzer/references/check_compatibility.py'


class CompatibilityTests(unittest.TestCase):
    def check(self, packages, cann, python='3.12.1', scene='auto', npu=None):
        data = {'runtime': {'packages': {'watched': packages}, 'python_version': python},
                'ascend': {'toolkit': cann if isinstance(cann, dict) else {'version': cann}},
                'npu': npu or {}}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'dump.json'
            source.write_text(json.dumps(data), encoding='utf-8')
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(SCRIPT), str(source),
                                     '--scene', scene], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_vllm_release_and_rc_keep_distinct_cann_baselines(self):
        packages = {'vllm': '0.30.0', 'vllm_ascend': '0.30.0',
                    'torch': '2.10.0+cpu', 'torch_npu': '2.10.0.post7'}
        release = self.check(packages, '9.2.1')
        self.assertEqual(release['software']['status'], 'matched')
        packages['vllm_ascend'] = '0.30.0rc2'
        rc = self.check(packages, '9.2.1')
        self.assertEqual(rc['software']['status'], 'deviation')
        self.assertEqual(rc['software']['checks']['cann']['expected'], ['9.2.0'])

    def test_unlisted_wheel_is_unknown_and_board_nok_is_explicit(self):
        result = self.check({'torch': '2.14.0', 'torch_npu': '2.14.0.post999'}, '9.2.1',
                            npu={'devices': [{'device_id': 3, 'compatibility': 'NOK'}]})
        self.assertEqual(result['software']['status'], 'unknown')
        self.assertEqual(result['boards'][0]['status'], 'incompatible')

    def test_legacy_cann_edition_and_python_patch_floor(self):
        packages = {'torch': '1.11.0', 'torch_npu': '1.11.0.post14'}
        good = self.check(packages, '8.0.RC2.beta1', '3.7.5')
        self.assertEqual(good['software']['status'], 'matched')
        old_python = self.check(packages, '8.0.RC2.beta1', '3.7.4')
        self.assertEqual(old_python['software']['checks']['python']['status'], 'deviation')

    def test_multiple_cann_candidates_do_not_prove_runtime_mismatch(self):
        packages = {'vllm': '0.30.0', 'vllm_ascend': '0.30.0',
                    'torch': '2.10.0', 'torch_npu': '2.10.0.post7'}
        result = self.check(packages, {'version': '9.2.0', 'installed_candidates':
                            [{'version': '9.2.0'}, {'version': '9.2.1'}]})
        self.assertEqual(result['software']['status'], 'unknown')
        self.assertEqual(result['software']['checks']['cann']['status'], 'unknown')

    def test_generic_torch_cann_range_preserves_exact_package_baseline(self):
        result = self.check({'torch': '2.14.0', 'torch_npu': '2.14.0'}, '9.2.1', '3.14')
        self.assertEqual(result['software']['status'], 'matched')
        self.assertEqual(result['software']['checks']['cann']['expected'], ['9.2.*'])
        self.assertEqual(result['software']['package_baseline']['cann'], '9.2.0')

    def test_mindspeed_pairwise_support_does_not_certify_entire_stack(self):
        result = self.check({'mindspeed_llm': '26.2.0', 'torch': '2.10.0',
                             'torch_npu': '2.10.0.post7'}, '9.1.0')
        self.assertEqual(result['software']['status'], 'deviation')
        self.assertEqual(result['component_support']['cann']['status'], 'matched')
        self.assertEqual(result['component_support']['torch_npu_product']['status'], 'matched')

    def test_failed_board_query_is_unknown_even_if_raw_says_nok(self):
        result = self.check({}, '9.2.1', npu={'devices': [{
            'device_id': 0, 'queries': {'board': {'returncode': 1, 'status': 'failed'}},
            'raw': {'board': 'Compatibility : NOK'}}]})
        self.assertEqual(result['boards'][0]['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
