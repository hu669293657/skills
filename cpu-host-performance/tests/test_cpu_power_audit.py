import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runpy import run_path

audit = run_path(str(Path(__file__).resolve().parents[1] / 'scripts/cpu_power_audit.py'))['audit']


class PowerAuditTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'online').write_text('0-1')

    def policy(self, index, **values):
        path = self.root / 'cpufreq' / ('policy%d' % index)
        path.mkdir(parents=True)
        for field, value in dict(affected_cpus=str(index), **values).items():
            (path / field).write_text(str(value))

    def test_mixed_governors_and_capped_target(self):
        self.policy(0, scaling_governor='performance', scaling_max_freq=2000,
                    cpuinfo_max_freq=3000, cpuinfo_cur_freq=2000)
        self.policy(1, scaling_governor='schedutil', scaling_max_freq=3000, scaling_cur_freq=3000)
        result = audit(self.root, 2, 0)
        self.assertEqual(result['online_governor'], 'MIXED')
        first, second = result['policies']
        self.assertEqual(first['policy_max_observation'], 'REACHED_IN_SAMPLES')
        self.assertEqual(first['hardware_max_observation'], 'NOT_SEEN_IN_SAMPLES')
        self.assertEqual(first['hardware_samples'], 2)
        self.assertEqual(first['policy_max_hits'], 2)
        self.assertEqual(first['hardware_max_hits'], 0)
        self.assertEqual(second['policy_max_observation'], 'UNKNOWN')

    def test_coverage_and_missing_files(self):
        self.assertEqual(audit(self.root, 1, 0)['online_governor'], 'UNKNOWN')
        self.policy(0, scaling_governor='performance')
        self.assertEqual(audit(self.root, 1, 0)['online_governor'], 'UNKNOWN')
        self.policy(1, scaling_governor='performance', cpuinfo_cur_freq='unavailable')
        result = audit(self.root, 1, 0)
        self.assertEqual(result['online_governor'], 'ALL_PERFORMANCE')
        self.assertEqual(result['policies'][1]['hardware_samples'], 0)
