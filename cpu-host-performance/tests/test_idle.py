import unittest
from analyzer.parser import Event


class IdleResidencyTests(unittest.TestCase):
    def analyze(self, events, **kw):
        from analyzer.idle_analysis import analyze_idle
        m = {'t_start': 0., 't_end': 10., 'duration': 10., 'per_cpu': {0: {}}, 'snapshot': {}, 'events_ok': True}
        m.update(kw)
        return analyze_idle(m, events)

    def test_residency_uses_duration_not_state_identifier_and_unsigned_exit(self):
        r = self.analyze([Event(0, 7, 'cpu_idle', fields={'cpu_id': 0, 'state': 2}), Event(4, 7, 'cpu_idle', fields={'cpu_id': 0, 'state': 4294967295})])
        cpu = r['cpus'][0]
        self.assertAlmostEqual(cpu['residency_percent'], 40.)
        self.assertAlmostEqual(cpu['states'][0]['residency_sec'], 4.)
        self.assertEqual(cpu['states'][0]['state'], 2)
        self.assertNotIn(7, r['per_cpu_ratio'])

    def test_signed_exit_and_initial_unknown_leave_null_gaps(self):
        r = self.analyze([Event(2, 0, 'cpu_idle', fields={'state': 1}), Event(4, 0, 'cpu_idle', fields={'state': -1})])
        cpu = r['cpus'][0]
        self.assertIsNone(cpu['residency_percent'])
        self.assertEqual(cpu['coverage_percent'], 80.)
        self.assertIsNone(cpu['series'][0][1])
        self.assertEqual(cpu['series'][2][1], 100.)
        self.assertEqual(cpu['series'][4][1], 0.)

    def test_missing_cpu_events_are_not_zero(self):
        r = self.analyze([], per_cpu={0: {}, 1: {}})
        self.assertFalse(r['available'])
        self.assertEqual(r['per_cpu_ratio'], {0: None, 1: None})
        self.assertTrue(all(all(v is None for _, v in p['series']) for p in r['cpus']))

    def test_malformed_or_repeated_entry_is_partial(self):
        r = self.analyze([Event(0, 0, 'cpu_idle', fields={'state': 1}), Event(2, 0, 'cpu_idle', fields={'state': 2}), Event(4, 0, 'cpu_idle', fields={'state': -1}), Event(5, 0, 'cpu_idle', fields={'state': 'bad'})])
        self.assertGreater(r['unpaired_events'], 0)
        self.assertEqual(r['reliability'], 'PARTIAL')

    def test_lost_events_do_not_produce_trusted_residency(self):
        r = self.analyze([Event(0, 0, 'cpu_idle', fields={'state': 1}), Event(5, 0, 'cpu_idle', fields={'state': -1})], buffer_overrun=2)
        self.assertEqual(r['reliability'], 'PARTIAL')
        self.assertIsNone(r['cpus'][0]['residency_percent'])

    def test_malformed_state_interrupts_timeline_until_next_transition(self):
        r = self.analyze([Event(0,0,'cpu_idle',fields={'state':1}),Event(2,0,'cpu_idle',fields={'state':'bad'}),Event(4,0,'cpu_idle',fields={'state':-1})])
        self.assertIsNone(r['cpus'][0]['series'][2][1])
        self.assertIsNone(r['cpus'][0]['series'][3][1])
        self.assertEqual(r['cpus'][0]['series'][4][1],0.)
