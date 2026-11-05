"""Execute generated safeguards with fake proc/sys/taskset; never bind the host."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from analyzer.npu_affinity import _binding_script


BASH = shutil.which('bash')
if not BASH and os.name == 'nt':
    candidate = Path(r'C:\Program Files\Git\bin\bash.exe')
    BASH = str(candidate) if candidate.is_file() else None


def shell_path(path):
    value = str(path).replace('\\', '/')
    return '/' + value[0].lower() + value[2:] if os.name == 'nt' and len(value) > 1 and value[1] == ':' else value


@unittest.skipUnless(BASH, 'Bash required for isolated generated-script execution')
class BindingScriptExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.posix_root = shell_path(self.root)
        for tid, stamp in ((100, 1000), (101, 1001)):
            self.write('proc/%d/stat' % tid, '%d (worker) S ' % tid + ' '.join(['0'] * 18 + [str(stamp)]))
            self.write('proc/%d/cgroup' % tid, '0::/job\n')
            self.write('proc/100/task/%d/status' % tid, 'Cpus_allowed_list:\t0-7\n')
        self.write('proc/sys/kernel/random/boot_id', 'test-boot\n')
        self.write('sys/devices/system/cpu/online', '0-7\n')
        self.write('sys/fs/cgroup/job/cpuset.cpus.effective', '0-7\n')
        (self.root / 'backup').mkdir()
        self.write('bin/systemd-detect-virt', '#!/usr/bin/env bash\nexit 1\n')
        self.write('bin/taskset', '''#!/usr/bin/env bash
set -euo pipefail
if [ "$#" = 2 ]; then
  echo "query $*" >> "$FIXTURE_ROOT/calls.log"
  exit 0
fi
[ "$#" = 3 ] && [ "$1" = '-pc' ] || exit 98
mask=$2; tid=$3
echo "apply $tid $mask" >> "$FIXTURE_ROOT/calls.log"
if [ "$tid" = "${FAIL_TID:-}" ] && [ "$mask" = '2-3' ]; then exit 9; fi
printf 'Cpus_allowed_list:\\t%s\\n' "$mask" > "$FIXTURE_ROOT/proc/100/task/$tid/status"
if [ "$tid" = '101' ] && [ "$mask" = '2-3' ] && [ "${REUSE_THREAD:-0}" = 1 ]; then
  sed 's/1001$/9999/' "$FIXTURE_ROOT/proc/101/stat" > "$FIXTURE_ROOT/proc/101/stat.new"
  mv "$FIXTURE_ROOT/proc/101/stat.new" "$FIXTURE_ROOT/proc/101/stat"
fi
if [ "$tid" = '100' ] && [ "$mask" = '2-3' ] && [ "${CREATE_THREAD:-0}" = 1 ]; then
  mkdir -p "$FIXTURE_ROOT/proc/100/task/102"
  printf 'Cpus_allowed_list:\\t0-7\\n' > "$FIXTURE_ROOT/proc/100/task/102/status"
fi
''')
        for path in (self.root / 'bin').iterdir():
            path.chmod(0o755)
        action = {'pid': 100, 'target': '2-3', 'evidence': {
            'starttime': '1000', 'tids': {100, 101},
            'thread_starttime': {100: '1000', 101: '1001'},
            'allowed': {100: set(range(8)), 101: set(range(8))}}}
        script = _binding_script([action], {'boot_id': 'test-boot'})
        # Replace every host filesystem boundary, including container guards and backups.
        replacements = {'/proc/': self.posix_root + '/proc/', '/sys/': self.posix_root + '/sys/',
                        '/var/tmp/': self.posix_root + '/backup/', '/.dockerenv': self.posix_root + '/.dockerenv',
                        '/run/.containerenv': self.posix_root + '/run/.containerenv'}
        script = re.sub('|'.join(re.escape(key) for key in replacements), lambda match: replacements[match[0]], script)
        script = script.replace('sudo bash <<', 'bash <<')
        script = script.replace('python3 - ', shlex.quote(shell_path(sys.executable)) + ' - ')
        self.script = self.write('bind.sh', 'export FIXTURE_ROOT=%s\nexport PATH=%s:"$PATH"\n' %
                                 (shlex.quote(self.posix_root), shlex.quote(self.posix_root + '/bin')) + script)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        return path

    def run_script(self, extra=None, path=None):
        env = dict(os.environ, FIXTURE_ROOT=self.posix_root)
        env.update(extra or {})
        # Running the original script sets its own fake bin PATH; manual rollback needs it too.
        command = 'export PATH=%s:"$PATH"; bash %s' % (
            shlex.quote(self.posix_root + '/bin'), shlex.quote(shell_path(path or self.script)))
        return subprocess.run([BASH, '-c', command], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', timeout=30)

    def mask(self, tid):
        return (self.root / ('proc/100/task/%d/status' % tid)).read_text().split()[-1]

    def calls(self):
        path = self.root / 'calls.log'
        return path.read_text() if path.exists() else ''

    def test_success_saves_each_tid_and_manual_rollback_restores_masks(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([self.mask(tid) for tid in (100, 101)], ['2-3', '2-3'])
        backup = next((self.root / 'backup').glob('npu-affinity.*'))
        self.assertTrue((backup / 'before.txt').is_file())
        self.assertTrue((backup / 'after.txt').is_file())
        rollback = self.run_script(path=backup / 'rollback.sh')
        self.assertEqual(rollback.returncode, 0, rollback.stdout)
        self.assertEqual([self.mask(tid) for tid in (100, 101)], ['0-7', '0-7'])

    def test_changed_pid_is_blocked_before_any_affinity_write(self):
        text = (self.root / 'proc/100/stat').read_text().rsplit(' ', 1)[0] + ' 9999'
        self.write('proc/100/stat', text)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('apply ', self.calls())
        self.assertEqual([self.mask(tid) for tid in (100, 101)], ['0-7', '0-7'])

    def test_partial_taskset_failure_automatically_rolls_back_saved_threads(self):
        result = self.run_script({'FAIL_TID': '101'})
        self.assertEqual(result.returncode, 9, result.stdout)
        self.assertIn('apply 100 2-3', self.calls())
        self.assertIn('apply 100 0-7', self.calls())
        self.assertEqual([self.mask(tid) for tid in (100, 101)], ['0-7', '0-7'])

    def test_new_thread_is_not_modified_and_original_threads_roll_back(self):
        result = self.run_script({'CREATE_THREAD': '1'})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('apply 102 ', self.calls())
        self.assertEqual([self.mask(tid) for tid in (100, 101, 102)], ['0-7', '0-7', '0-7'])

    def test_rollback_skips_a_reused_tid(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        text = (self.root / 'proc/101/stat').read_text().rsplit(' ', 1)[0] + ' 9999'
        self.write('proc/101/stat', text)
        backup = next((self.root / 'backup').glob('npu-affinity.*'))
        rollback = self.run_script(path=backup / 'rollback.sh')
        self.assertEqual(rollback.returncode, 0, rollback.stdout)
        self.assertEqual(self.mask(100), '0-7')
        self.assertEqual(self.mask(101), '2-3')

    def test_reused_tid_during_apply_cannot_pass_final_verification(self):
        result = self.run_script({'REUSE_THREAD': '1'})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mask(100), '0-7')
        self.assertNotIn('apply 101 0-7', self.calls())


if __name__ == '__main__':
    unittest.main()
