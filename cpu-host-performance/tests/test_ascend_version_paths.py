"""Resolve Ascend version evidence through the standalone Bash collector."""
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from tests import test_standalone_collector as collector


COMPONENTS = ('driver', 'toolkit', 'runtime', 'compiler', 'hccl', 'opp', 'atb', 'mindie')


class AscendVersionPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.out = self.base / 'out'
        self.shell_root = self.base.as_posix()
        if self.base.drive.endswith(':'):
            self.shell_root = '/' + self.base.drive[0].lower() + self.shell_root[2:]

    def collect(self, setup=''):
        result = collector.StandaloneTests().run_library(
            self.base, self.base / 'proc', self.base / 'sys', self.out,
            f'''
DURATION=3; MINIMAL=0; HOSTBOUND=1; DISCOVER=1; PID_LIST=
test_root={shlex.quote(self.shell_root)}
unset "${{!ASCEND_@}}" "${{!ATB_@}}" "${{!MINDIE_@}}" CONDA_PREFIX
export HOME="$test_root/home"
{setup}
hb_environment "$test_root/etc" "$test_root/Ascend"
hb_manifest
''')
        manifest = self.out / 'hostbound/collection_manifest.json'
        entries = json.loads(manifest.read_text(encoding='utf-8'))['entries']
        rows = [row for row in entries if row['category'] == 'environment_ascend']
        self.assertEqual([row['raw_path'] for row in rows],
                         [f'hostbound/environment/ascend/{name}.txt' for name in COMPONENTS])
        return dict(zip(COMPONENTS, rows)), result.stderr

    def assert_versions(self, sources, unsupported=None, setup=''):
        for name, path in sources.items():
            if path.endswith('ascend_toolkit_install.info'):
                content = 'package_name=Ascend-cann-toolkit\nversion=8.0.fixture\narch=aarch64\n'
            else:
                content = f'Version={name}.fixture\n'
            collector.put(self.base, path, content)
        rows, warnings = self.collect(setup)
        for name, path in sources.items():
            with self.subTest(component=name):
                row = rows[name]
                self.assertEqual(row['source'], f'{self.shell_root}/{path}')
                self.assertEqual((row['status'], row['failure_reason']), ('COLLECTED', None))
                self.assertEqual((self.out / row['raw_path']).read_bytes(),
                                 (self.base / path).read_bytes())
                self.assertNotIn(row['raw_path'], warnings)
        for name, (path, reason) in (unsupported or {}).items():
            with self.subTest(component=name):
                row = rows[name]
                self.assertEqual(row['source'], f'{self.shell_root}/{path}')
                self.assertEqual((row['status'], row['failure_reason']), ('UNSUPPORTED', reason))
                self.assertFalse((self.out / row['raw_path']).exists())
                self.assertIn(row['raw_path'], warnings)

    def test_share_info_and_legacy_component_files_in_one_install(self):
        self.assert_versions({
            'driver': 'Ascend/driver/version.info',
            'toolkit': 'Ascend/cann/share/info/asc-devkit/version.info',
            'runtime': 'Ascend/cann/share/info/runtime/version.info',
            'hccl': 'Ascend/cann/share/info/hccl/version.info',
            'compiler': 'Ascend/cann/compiler/version.info',
            'opp': 'Ascend/cann/opp/version.info',
        }, {
            'atb': ('Ascend/nnal/atb/latest/version.info', 'interface_missing'),
            'mindie': ('Ascend/mindie/latest/version.info', 'interface_missing'),
        })

    def test_precedence_and_non_regular_candidates(self):
        for path in ('Ascend/cann/hccl/version.info', 'Ascend/nnal/atb/latest/version.info',
                     'Ascend/mindie/latest/version.info'):
            (self.base / path).mkdir(parents=True)
        for path in ('Ascend/cann/asc-devkit/version.info', 'Ascend/cann/share/info/runtime/version.info'):
            collector.put(self.base, path, 'Version=lower-priority\n')
        self.assert_versions({
            'toolkit': 'Ascend/cann/toolkit/version.info',
            'runtime': 'Ascend/cann/runtime/version.info',
            'compiler': 'Ascend/ascend-toolkit/latest/share/info/compiler/version.info',
            'hccl': 'Ascend/ascend-toolkit/latest/hccl/version.info',
            'opp': 'Ascend/cann/opp/version.info',
            'atb': 'Ascend/atb/latest/version.info',
        }, {
            'driver': ('Ascend/driver/version.info', 'interface_missing'),
            'mindie': ('Ascend/mindie/latest/version.info', 'version_source_not_regular'),
        })

    def test_active_cann_and_driver_install_record_override_default_files(self):
        collector.put(self.base, 'Ascend/cann/runtime/version.info', 'Version=inactive\n')
        collector.put(self.base, 'Ascend/driver/version.info', 'Version=inactive\n')
        collector.put(self.base, 'etc/ascend_install.info',
                      f'Driver_Install_Path_Param={self.shell_root}/driver-install\n'
                      'touch "$test_root/sourced-install-record"\n')
        self.assert_versions({
            'driver': 'driver-install/driver/version.info',
            'toolkit': 'custom install/share/info/asc-devkit/version.info',
            'runtime': 'custom install/share/info/runtime/version.info',
            'compiler': 'custom install/compiler/version.info',
            'hccl': 'custom install/hccl/version.info',
            'opp': 'custom install/opp/version.info',
            'atb': 'Ascend/nnal/atb/latest/version.info',
            'mindie': 'Ascend/mindie/latest/version.info',
        }, {}, setup='export ASCEND_HOME_PATH="$test_root/custom install"')
        self.assertFalse((self.base / 'sourced-install-record').exists())

    def test_system_install_record_and_native_arch_toolkit_metadata(self):
        latest = 'site/Ascend/ascend-toolkit/latest'
        collector.put(self.base, 'etc/Ascend/ascend_cann_install.info',
                      f'Install_Path={self.shell_root}/site/Ascend\n')
        collector.put(self.base, f'{latest}/share/info/asc-devkit/version.info', 'Version=devkit\n')
        collector.put(self.base, f'{latest}/x86_64-linux/ascend_toolkit_install.info',
                      'package_name=Ascend-cann-toolkit\nversion=other-arch\narch=x86_64\n')
        collector.put(self.base, 'Ascend/cann/runtime/version.info', 'Version=system-default\n')
        self.assert_versions({
            'toolkit': f'{latest}/aarch64-linux/ascend_toolkit_install.info',
            'runtime': f'{latest}/runtime/version.info',
        }, setup='uname(){ if [[ "$1" == -m ]]; then echo aarch64; else command uname "$@"; fi; }')

    def test_user_install_record_and_optional_component_environment(self):
        collector.put(self.base, 'home/Ascend/ascend_cann_install.info',
                      f'Install_Path={self.shell_root}/user-install\n')
        for stale in ('Ascend/nnal/atb/latest/version.info', 'Ascend/mindie/latest/version.info'):
            collector.put(self.base, stale, 'Version=inactive\n')
        self.assert_versions({
            'hccl': 'user-install/cann/share/info/hccl/version.info',
            'atb': 'nnal-install/atb/8.0/version.info',
            'mindie': 'mindie-install/2.0/version.info',
        }, setup='''
export ATB_HOME_PATH="$test_root/nnal-install/atb/8.0/atb/cxx_abi_1"
export MINDIE_LLM_HOME_PATH="$test_root/mindie-install/2.0/mindie-llm"
mkdir -p -- "$ATB_HOME_PATH" "$MINDIE_LLM_HOME_PATH"
''')

    def test_active_conda_install_precedes_system_install(self):
        collector.put(self.base, 'Ascend/cann/runtime/version.info', 'Version=system-default\n')
        self.assert_versions({
            'runtime': 'conda/envs/train/Ascend/cann/share/info/runtime/version.info',
        }, setup='export CONDA_PREFIX="$test_root/conda/envs/train"')

    def test_single_version_directory_without_an_active_alias(self):
        self.assert_versions({
            'runtime': 'Ascend/cann-9.0.0/share/info/runtime/version.info',
            'hccl': 'Ascend/cann-9.0.0/share/info/hccl/version.info',
        })

    def test_nnrt_without_toolkit_and_explicit_opp(self):
        latest = 'Ascend/nnrt/latest'
        collector.put(self.base, f'{latest}/opp/version.info', 'Version=bundled-opp\n')
        self.assert_versions({
            'runtime': f'{latest}/runtime/version.info',
            'opp': 'site/op-library/version.info',
        }, setup='export ASCEND_OPP_PATH="$test_root/site/op-library"')

    def test_nnae_without_toolkit_and_without_opp_component_evidence(self):
        latest = 'Ascend/nnae/latest'
        for arch in ('aarch64', 'x86_64'):
            collector.put(self.base, f'{latest}/{arch}-linux/ascend_ops_install.info',
                          'package_name=Ascend-cann-ops\nversion=ops-bundle\n')
        self.assert_versions({
            'runtime': f'{latest}/runtime/version.info',
        }, {'opp': (f'{latest}/opp/version.info', 'interface_missing')})

    def test_multiple_versions_without_an_active_alias(self):
        for version in ('8.5.0', '9.0.0'):
            collector.put(self.base, f'Ascend/cann-{version}/share/info/runtime/version.info',
                          f'Version={version}\n')
        self.assert_versions({
            'driver': 'Ascend/driver/version.info',
            'atb': 'Ascend/nnal/atb/latest/version.info',
            'mindie': 'Ascend/mindie/latest/version.info',
        }, {name: ('Ascend', 'ambiguous_cann_installation')
            for name in ('toolkit', 'runtime', 'compiler', 'hccl', 'opp')})
