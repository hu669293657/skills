import json
import unittest
from analyzer.parser import TraceData, Event
from analyzer.metrics import compute_metrics


def data(events=(), start=None, samples=(), cap=None):
    td = TraceData()
    td.events = [Event(0, 0, 'sched_switch', fields={'prev_pid': 0, 'next_pid': 1}), Event(10, 0, 'sched_switch', fields={'prev_pid': 1, 'next_pid': 0})] + list(events)
    td.snapshot = {'hostbound/frequency_start.json': json.dumps(start or {'policies': []}),
                   'hostbound/frequency_samples.jsonl': '\n'.join(json.dumps(x) for x in samples),
                   'hostbound/capabilities.json': json.dumps({'events': {'power/cpu_frequency': cap or {}}})}
    return compute_metrics(td)


def policy(**kw):
    p = {'id': 'policy0', 'cpus': [0, 1], 'unit': 'kHz', 'cpuinfo_max_freq': 2600000, 'scaling_governor': 'ondemand'}
    p.update(kw)
    return {'policies': [p]}


class FrequencyTests(unittest.TestCase):
    def test_trace_uses_cpu_id_and_time_weighting(self):
        m = data([Event(1, 7, 'cpu_frequency', fields={'cpu_id': 0, 'state': 1000000}), Event(9, 7, 'cpu_frequency', fields={'cpu_id': 0, 'state': 2000000})], policy())
        f = m['frequency']
        self.assertEqual(f['status'], 'TRACE_AVAILABLE')
        self.assertEqual(len(f['domains']), 1)
        self.assertAlmostEqual(f['domains'][0]['average_mhz'], 10000/9)
        self.assertNotIn(7, m['freq']['per_cpu'])

    def test_enabled_without_changes_is_not_unavailable(self):
        self.assertEqual(data(cap={'exists': True, 'enabled': True})['frequency']['status'], 'NO_TRANSITION')

    def test_unsupported(self):
        self.assertEqual(data(cap={'exists': False})['frequency']['status'], 'UNSUPPORTED')

    def test_minimal_not_requested(self):
        f = data(cap={'exists': True, 'requested': False, 'enabled': False})['frequency']
        self.assertEqual(f['event_status'], 'NOT_REQUESTED')

    def test_enable_failed(self):
        self.assertEqual(data(cap={'exists': True, 'requested': True, 'enabled': False, 'error': 'permission denied'})['frequency']['status'], 'COLLECTION_FAILED')

    def test_sysfs_policy_not_duplicated(self):
        samples = [{'ts': t, 'policies': [{'id': 'policy0', 'scaling_cur_freq': v}]} for t,v in [(0,2000000),(5,2400000),(10,2400000)]]
        f = data(start=policy(), samples=samples)['frequency']
        self.assertEqual(f['status'], 'SYSFS_AVAILABLE')
        self.assertEqual(f['sample_count'], 3)
        self.assertEqual(len(f['domains']), 1)
        self.assertAlmostEqual(f['domains'][0]['average_mhz'], 2200)

    def test_static_only_and_missing_not_zero(self):
        f = data(start=policy())['frequency']
        self.assertEqual(f['status'], 'STATIC_ONLY')
        self.assertIsNone(f['domains'][0]['current_mhz'])
        self.assertIsNone(f['domains'][0]['average_mhz'])

    def test_explicit_units(self):
        from analyzer.frequency_analysis import to_mhz
        self.assertEqual(to_mhz(2600000000, 'Hz'), 2600)
        self.assertEqual(to_mhz(2600000, 'kHz'), 2600)
        self.assertEqual(to_mhz(2600, 'MHz'), 2600)
        self.assertIsNone(to_mhz(None, 'kHz'))
        self.assertIsNone(to_mhz(123, 'unknown'))
        from analyzer.metrics import freq_to_mhz
        self.assertEqual(freq_to_mhz(2600000000,'Hz'),2600)
        self.assertIsNone(freq_to_mhz(None))

    def test_lost_trace_is_not_no_transition(self):
        td = TraceData()
        td.events = [Event(0,0,'sched_switch'),Event(1,0,'sched_switch')]
        td.snapshot = {'trace_overrun.txt': '12', 'hostbound/capabilities.json': json.dumps({'events': {'power/cpu_frequency': {'exists': True, 'enabled': True}}})}
        self.assertEqual(compute_metrics(td)['frequency']['status'], 'COLLECTION_FAILED')
    def test_invalid_frequency_event_is_collection_failure(self):
        f=data([Event(1,0,'cpu_frequency',fields={'cpu_id':0,'state':'bad'})],cap={'exists':True,'enabled':True})['frequency']
        self.assertEqual(f['event_status'],'COLLECTION_FAILED')

    def test_unreadable_sysfs_failure_is_retained(self):
        f=data(start={'policies':[{'id':'policy0','cpus':[0],'errors':{'scaling_cur_freq':'permission denied'}}]},cap={'exists':False})['frequency']
        self.assertEqual(f['status'],'UNSUPPORTED')
        self.assertIn('scaling_cur_freq',str(f['domains'][0].get('errors')))

    def test_malformed_static_and_poll_shapes_do_not_crash(self):
        from analyzer.frequency_analysis import analyze_frequency
        for raw in ({'policies':123},{'policies':[{}]},[],{'policies':[{'id':'p','cpus':[None]}]}):
            with self.subTest(raw=raw):
                m={'snapshot':{'hostbound/frequency_start.json':json.dumps(raw),'hostbound/frequency_samples.jsonl':'[]\n{"policies":123}\n{"policies":[{}]}'},'t_start':0,'t_end':10,'events_ok':True}
                f=analyze_frequency(m,[])
                self.assertGreater(f['invalid_sidecar_records'],0)
                self.assertTrue(f['reasons'])

    def test_sysfs_weighting_is_bounded_to_trace_window(self):
        from analyzer.frequency_analysis import analyze_frequency
        m={'snapshot':{'hostbound/frequency_start.json':json.dumps(policy()),'hostbound/frequency_samples.jsonl':'\n'.join(json.dumps({'ts':t,'policies':[{'id':'policy0','scaling_cur_freq':v}]}) for t,v in [(-5,1000000),(2,2000000),(12,3000000)])},'t_start':0,'t_end':10,'events_ok':True}
        f=analyze_frequency(m,[])
        p=f['domains'][0]
        self.assertEqual(p['observed_sec'],10)
        self.assertEqual(p['average_mhz'],1800)
        self.assertEqual(p['highest_mhz'],2000)
        self.assertTrue(all(0<=t<=10 for t,_ in p['series']))
        self.assertEqual(f['clock_alignment'],'UNVERIFIED')

    def test_aliased_policies_do_not_double_count(self):
        p=policy();p['policies'].append(dict(p['policies'][0],id='policy-alias'))
        f=data(start=p,samples=[{'ts':0,'policies':[{'id':'policy0','scaling_cur_freq':2000000},{'id':'policy-alias','scaling_cur_freq':2000000}]},{'ts':10,'policies':[{'id':'policy0','scaling_cur_freq':2000000},{'id':'policy-alias','scaling_cur_freq':2000000}]}])['frequency']
        self.assertEqual(len(f['domains']),1)
        self.assertEqual(f['sample_count'],2)

    def test_incompatible_trace_clock_disables_sysfs_overlay(self):
        from analyzer.frequency_analysis import analyze_frequency
        m={'snapshot':{'hostbound/frequency_start.json':json.dumps(policy()),'hostbound/frequency_samples.jsonl':json.dumps({'ts':2,'policies':[{'id':'policy0','scaling_cur_freq':2000000}]}),'hostbound/trace_clock.txt':'global'},'t_start':0,'t_end':10,'events_ok':True}
        f=analyze_frequency(m,[])
        self.assertEqual(f['clock_alignment'],'INCOMPATIBLE')
        self.assertIsNone(f['domains'][0]['average_mhz'])
        self.assertEqual(f['domains'][0]['series'],[])

    def test_entirely_invalid_trace_falls_back_to_healthy_sysfs_per_domain(self):
        from analyzer.frequency_analysis import analyze_frequency
        m={'snapshot':{'hostbound/frequency_start.json':json.dumps(policy(cpuinfo_max_freq=3000000)),
                       'hostbound/trace_clock.txt':'mono',
                       'hostbound/frequency_samples.jsonl':'\n'.join(json.dumps({'ts':t,'policies':[{'id':'policy0','scaling_cur_freq':v}]}) for t,v in [(0,2000000),(5,2500000),(10,2500000)])},
           't_start':0,'t_end':10,'events_ok':True}
        f=analyze_frequency(m,[Event(1,0,'cpu_frequency',fields={'cpu_id':0,'state':'bad'})])
        self.assertEqual(f['status'],'SYSFS_AVAILABLE')
        self.assertEqual(f['event_status'],'COLLECTION_FAILED')
        self.assertEqual(f['invalid_trace_samples'],1)
        p=f['domains'][0]
        self.assertEqual(p['source'],'sysfs')
        self.assertEqual(p['clock_alignment'],'MONOTONIC')
        self.assertEqual(p['reliability'],'PARTIAL')
        self.assertEqual(p['sample_count'],3)
        self.assertEqual(p['average_mhz'],2250)
        self.assertEqual(p['raw_trace_errors'][0]['state'],'bad')
