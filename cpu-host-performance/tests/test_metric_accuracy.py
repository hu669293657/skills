import unittest
from analyzer.parser import TraceData, Event
from analyzer.metrics import compute_metrics


class MetricAccuracyTests(unittest.TestCase):
    def test_irq_denominator_observed_cpu_time(self):
        td=TraceData()
        for c in range(2):
            td.events.extend([Event(0,c,'sched_switch',fields={'prev_pid':0,'next_pid':1+c}),Event(10,c,'sched_switch',fields={'prev_pid':1+c,'next_pid':0}),Event(1,c,'irq_handler_entry',fields={'irq':1}),Event(2,c,'irq_handler_exit',fields={'irq':1})])
        m=compute_metrics(td)
        self.assertEqual(m['irq']['total_ratio'],10)
        self.assertEqual(m['irq']['max_cpu_ratio'],10)
        self.assertEqual(m['irq']['observed_cpu_sec'],20)

    def test_irq_hot_core_has_its_own_source(self):
        td = TraceData()
        td.snapshot = {'trace_overrun.txt': '0'}
        for cpu, name, span in [(0, 'nic-global', 2.5), (1, 'nvme-local', 6),
                                (2, 'nic-global', 2.5), (3, 'nic-global', 2.5)]:
            td.events.extend([
                Event(0, cpu, 'sched_switch', fields={'prev_pid': 0, 'next_pid': cpu + 1}),
                Event(10, cpu, 'sched_switch', fields={'prev_pid': cpu + 1, 'next_pid': 0}),
                Event(0.1, cpu, 'irq_handler_entry', fields={'irq': 10 + cpu, 'name': name}),
                Event(0.1 + span, cpu, 'irq_handler_exit', fields={'irq': 10 + cpu}),
            ])
        m = compute_metrics(td)
        self.assertEqual(m['irq']['top_names'][0][0], 'nic-global')
        self.assertEqual(m['irq']['peak_cpu'], 1)
        self.assertEqual(m['irq']['peak_sources'][0]['name'], 'nvme-local')
        from tests.test_reports import html
        verdict = html(m).split("<h2 id='sec-2'>", 1)[0]
        self.assertIn('nvme-local', verdict)
        self.assertNotIn('nic-global', verdict)
        self.assertIn('IRQ 11', verdict)

    def test_irq_hot_core_excludes_sources_outside_observed_time(self):
        td = TraceData()
        td.snapshot = {'trace_overrun.txt': '0'}
        td.events = [
            Event(0, 0, 'irq_handler_entry', fields={'irq': 1, 'name': 'outside'}),
            Event(5, 0, 'irq_handler_exit', fields={'irq': 1}),
            Event(6, 0, 'sched_switch', fields={'prev_pid': 0, 'next_pid': 1}),
            Event(7, 0, 'irq_handler_entry', fields={'irq': 2, 'name': 'inside'}),
            Event(8, 0, 'irq_handler_exit', fields={'irq': 2}),
            Event(9, 0, 'sched_switch', fields={'prev_pid': 1, 'next_pid': 0}),
        ]
        metrics = compute_metrics(td)['irq']
        self.assertEqual(metrics['top_names'][0][0], 'outside')
        self.assertEqual(metrics['peak_sources'], [{'id': '2', 'name': 'inside', 'seconds': 1}])

    def test_missing_irq_not_zero(self):
        td=TraceData();td.events=[Event(0,0,'sched_switch',fields={'prev_pid':0,'next_pid':1}),Event(10,0,'sched_switch',fields={'prev_pid':1,'next_pid':0})]
        m=compute_metrics(td)
        self.assertIsNone(m['irq']['total_ratio'])
        self.assertIsNone(m['per_cpu'][0]['irq_ratio'])
        self.assertFalse(m['buffer_overrun_known'])

    def test_idle_switch_direction(self):
        td=TraceData();td.events=[Event(0,0,'sched_switch',fields={'prev_pid':1,'next_pid':0}),Event(5,0,'sched_switch',fields={'prev_pid':0,'next_pid':1}),Event(10,0,'sched_switch',fields={'prev_pid':1,'next_pid':0})]
        self.assertEqual(compute_metrics(td)['per_cpu'][0]['util'],50)

    def test_all_online_cpus_visible_unknown_not_busy(self):
        td=TraceData();td.events=[Event(0,0,'sched_switch',fields={'prev_pid':0,'next_pid':1}),Event(10,0,'sched_switch',fields={'prev_pid':1,'next_pid':0})];td.snapshot={'cpu_online.txt':'0-255'}
        m=compute_metrics(td)
        self.assertEqual(len(m['per_cpu']),256)
        self.assertIsNone(m['per_cpu'][255]['util'])
    def test_malformed_numeric_event_is_counted_not_fatal(self):
        from analyzer.parser import parse_ftrace_text
        td=TraceData()
        parse_ftrace_text('a-1 [000] .... 0.000: sched_switch: prev_pid=0 next_pid=1\na-1 [000] .... 1.000: sched_switch: prev_pid=bad next_pid=2\na-1 [000] .... 2.000: sched_switch: prev_pid=1 next_pid=0',td)
        m=compute_metrics(td)
        self.assertEqual(td.parse_stats['failed'],1)
        self.assertFalse(m['events_ok'])
    def test_idle_only_trace_preserves_utilization(self):
        td=TraceData();td.events=[Event(0,0,'cpu_idle',fields={'state':'0','cpu_id':'0','idle_exit':False}),Event(5,0,'cpu_idle',fields={'state':'4294967295','cpu_id':'0','idle_exit':True}),Event(10,0,'cpu_idle',fields={'state':'0','cpu_id':'0','idle_exit':False})]
        m=compute_metrics(td)
        self.assertEqual(m['per_cpu'][0]['util'],50)
        self.assertIsNone(m['per_cpu'][0]['cs_rate'])

    def test_missing_irq_exit_marks_quality_partial(self):
        td=TraceData();td.events=[Event(0,0,'sched_switch',fields={'prev_pid':0,'next_pid':1}),Event(1,0,'irq_handler_entry',fields={'irq':1}),Event(10,0,'sched_switch',fields={'prev_pid':1,'next_pid':0})]
        m=compute_metrics(td)
        self.assertIsNone(m['irq']['total_ratio'])
        self.assertEqual(m['irq']['unpaired_events'],1)

    def test_unsigned_64_idle_exit_does_not_become_an_idle_state(self):
        td = TraceData()
        td.events = [Event(0, 0, 'cpu_idle', fields={'state': '1', 'cpu_id': '0'}),
                     Event(5, 0, 'cpu_idle', fields={'state': '18446744073709551615', 'cpu_id': '0'}),
                     Event(10, 0, 'cpu_idle', fields={'state': '1', 'cpu_id': '0'})]
        m = compute_metrics(td)
        self.assertEqual(m['per_cpu'][0]['util'], 50)
        self.assertNotIn('18446744073709551615', m['cstate_dist'])
        self.assertEqual(m['idle']['per_cpu_ratio'][0], 50)

    def test_missing_idle_transition_keeps_gap_out_of_core_observation(self):
        td = TraceData()
        td.events = [Event(0, 0, 'cpu_idle', fields={'state': '1', 'cpu_id': '0'}),
                     Event(5, 0, 'cpu_idle', fields={'state': '2', 'cpu_id': '0'}),
                     Event(10, 0, 'cpu_idle', fields={'state': '-1', 'cpu_id': '0'})]
        m = compute_metrics(td)
        self.assertEqual(m['per_cpu'][0]['observed_sec'], 5)
        self.assertIsNone(m['idle']['per_cpu_ratio'][0])
        self.assertNotIn('1', m['cstate_dist'])

    def test_irq_sources_beyond_top_ten_remain_in_report_data(self):
        td = TraceData()
        td.events = [Event(0, 0, 'sched_switch', fields={'prev_pid': 0, 'next_pid': 1}),
                     Event(10, 0, 'sched_switch', fields={'prev_pid': 1, 'next_pid': 0})]
        for irq in range(15):
            td.events.extend([Event(1+irq*.02, 0, 'irq_handler_entry', fields={'irq': irq, 'name': 'device%d' % irq}),
                              Event(1.01+irq*.02, 0, 'irq_handler_exit', fields={'irq': irq})])
        m = compute_metrics(td)
        self.assertEqual(len(m['irq']['top_names']), 10)
        self.assertEqual(len(m['irq']['all_names']), 15)
