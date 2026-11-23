"""配套检查的离线 CLI 回归。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / 'ascend-dump-analyzer/references/check_compatibility.py'


class CompatibilityTests(unittest.TestCase):
    def check(self, packages, cann, python='3.12.1', scene='auto', npu=None, hdk=None,
              scope=None, in_container=False, ascend=None):
        data = {'runtime': {'packages': {'watched': packages}, 'python_version': python},
                'ascend': {'toolkit': cann if isinstance(cann, dict) else {'version': cann}},
                'npu': npu or {}}
        data['ascend'].update(ascend or {})
        data['runtime']['container'] = {'in_container': in_container}
        if hdk:
            data['ascend']['hdk'] = {'version': hdk}
        return self.run_check(data, ['--scene', scene], scope=scope)

    def run_check(self, data, options, scope=None, inventory=False):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'dump.json'
            source.write_text(json.dumps(data), encoding='utf-8')
            supplied = ['--inventory', str(source)] if inventory else [str(source)]
            if scope is not None:
                scope_file = Path(folder) / 'scope.json'
                scope_file.write_text(json.dumps(scope), encoding='utf-8')
                options = [*options, '--scope', str(scope_file)]
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', '-B', str(SCRIPT), *supplied, *options],
                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_dump_checks_the_scoped_hdk_release_and_cann_subpackage(self):
        result = self.check({}, '9.2.1', hdk='26.2.RC1',
                            npu={'devices': [{'name': 'A2', 'compatibility': 'OK'}]},
                            ascend={'atb': {'version': '9.2.1'}},
                            scope={'cann_edition': 'community'})
        catalog = result['catalog']
        self.assertEqual(catalog['scope']['hardware'], 'A2')
        checks = {tuple(item['components']): item for item in catalog['checks']}
        self.assertEqual(checks['cann', 'hdk']['status'], 'confirmed')
        self.assertEqual(checks['cann', 'hdk']['evidence'][0]['kind'], 'release')
        self.assertEqual(checks['atb', 'cann']['status'], 'confirmed')

    def test_mixed_hardware_and_container_hdk_do_not_certify_host(self):
        output = self.check({}, '9.2.1', hdk='26.2.RC1', in_container=True,
                            npu={'devices': [{'name': 'A2'}, {'name': 'A3'}]})
        hdk = output['catalog']['checks'][0]
        self.assertEqual(hdk['status'], 'unknown')
        self.assertEqual(hdk['missing'], ['hardware', 'hdk'])

    def test_dump_checks_full_vllm_dependencies_separately_from_baseline(self):
        output = self.check({'vllm': '0.30.0', 'vllm_ascend': '0.30.0',
                             'torch': '2.10.0', 'torch_npu': '2.10.0.post7',
                             'triton_ascend': '3.6.0.post2', 'mooncake': '0.3.14-rc1',
                             'memfabric-hybrid': '1.3.0', 'memcache-hybrid': '1.3.0',
                             'flash-linear-attention-npu': '26.9.1-release'}, '9.2.1')
        catalog = output['catalog']
        self.assertEqual(catalog['workload'], 'vllm_ascend')
        self.assertEqual(catalog['checks'][0]['status'], 'confirmed')
        self.assertIn('mooncake', catalog['checks'][0]['components'])

    def test_dump_detects_explicit_mindspeed_platform_restriction(self):
        output = self.check({'mindspeed_llm': '26.2.0'}, '9.2.0',
                            scope={'hardware': 'A2', 'hardware_variant': '推理'})
        platform = next(item for item in output['catalog']['checks']
                        if item['components'] == ['mindspeed_llm'])
        self.assertEqual(platform['status'], 'incompatible')
        self.assertEqual(platform['reason'], 'explicit_conflict')

    def test_inventory_keeps_download_and_candidate_evidence_in_scope(self):
        candidate = self.run_check(
            {'hardware': 'A2', 'versions': {'cann': '9.2.1', 'hdk': '26.1.1'}}, [],
            inventory=True)
        self.assertEqual(candidate['status'], 'candidate')
        download = self.run_check(
            {'mode': 'download', 'hardware': '310B', 'architecture': 'arm64',
             'install_method': '离线安装 (Runfile)', 'versions': {'cann': '9.2.0', 'hdk': '26.2.RC1'}},
            [], inventory=True)
        self.assertEqual(download['mode'], 'download')
        self.assertEqual(download['checks'][0]['status'], 'confirmed')
        self.assertEqual(download['checks'][0]['evidence'][0]['kind'], 'download')

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
        self.assertEqual(result['recommendations'], [])

    def test_recommendation_can_change_release_to_keep_more_components(self):
        packages = {'vllm': '0.30.0', 'vllm_ascend': '0.30.0', 'torch': '2.10.0',
                    'torch_npu': '2.10.0.post7', 'triton_ascend': '3.2.2'}
        result = self.check(packages, '9.2.0')
        self.assertEqual(result['software']['checks']['cann']['status'], 'deviation')
        self.assertEqual(result['software']['checks']['triton_ascend']['status'], 'deviation')
        self.assertEqual(len(result['recommendations']), 1)
        self.assertEqual(result['recommendations'][0]['changes'], {
            'vllm_ascend': {'status': 'deviation', 'observed': '0.30.0', 'expected': ['0.30.0rc2']},
        })

    def test_recommendation_does_not_count_an_uncollected_version(self):
        packages = {'vllm_ascend': '0.30.0', 'vllm': '0.30.0',
                    'torch': '2.10.0', 'torch_npu': '2.10.0.post7'}
        result = self.check(packages, '9.2.0')
        self.assertEqual(set(result['recommendations'][0]['changes']), {'cann'})

    def test_recommendation_cannot_skip_a_confirmed_hdk_mismatch(self):
        packages = {'vllm_ascend': '0.30.0', 'vllm': '0.30.0',
                    'torch': '2.10.0', 'torch_npu': '2.10.0.post7'}
        result = self.check(packages, '9.2.0', hdk='26.0.0')
        self.assertEqual(result['host']['hdk']['status'], 'deviation')
        self.assertEqual(result['recommendations'][0]['release'], '0.30.0')
        self.assertEqual(set(result['recommendations'][0]['changes']), {'cann', 'hdk'})

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
