import json
import tempfile
import unittest
from pathlib import Path
from analyzer.analyze_cpu_trace import run_analysis
from analyzer.parser import TraceData, parse_chrome_json_text, load_input
from analyzer.metrics import compute_metrics


class InputCompatibilityTests(unittest.TestCase):
    def test_chrome_lifecycle_fields_preserved(self):
        td=TraceData();parse_chrome_json_text(json.dumps({'traceEvents':[{'name':'sched:sched_process_fork','ts':1000000,'args':{'cpu':0,'parent_pid':1,'child_pid':2,'child_comm':'new task'}}]}),td)
        self.assertEqual(td.events[0].fields['child_pid'],2)
        self.assertEqual(td.events[0].etype,'sched_process_fork')

    def test_jsonl_sidecar_never_parsed_as_trace(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'hostbound').mkdir();(p/'trace.txt').write_text('a-1 [000] .... 1.000: sched_switch: prev_pid=0 next_pid=1\na-1 [000] .... 2.000: sched_switch: prev_pid=1 next_pid=0')
            (p/'hostbound/frequency_samples.jsonl').write_text('{"ts":1,"policies":[]}\n')
            td=load_input(d)
            self.assertEqual(len(td.events),2)
            self.assertIn('hostbound/frequency_samples.jsonl',td.snapshot)

    def test_static_sidecar_only_cli_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'input/hostbound').mkdir(parents=True)
            (p/'input/hostbound/frequency_start.json').write_text(json.dumps({'policies':[{'id':'policy0','cpus':[0],'cpuinfo_max_freq':2600000}]}))
            diag,out=run_analysis(str(p/'input'),str(p/'report'),quiet=True)
            self.assertTrue((p/'report/hostbound_report.html').is_file())
            self.assertTrue((p/'report/report_data.json').is_file())
            self.assertEqual(json.loads((p/'report/report_data.json').read_text(encoding='utf-8'))['metrics']['frequency']['status'],'STATIC_ONLY')
            self.assertNotEqual(diag['host_status'],'HEALTHY')
    def test_chrome_malformed_numeric_event_is_skipped(self):
        td=TraceData();parse_chrome_json_text(json.dumps({'traceEvents':[{'name':'sched_switch','ts':0,'args':{'cpu':0,'prev_pid':'bad','next_pid':2}},{'name':'sched_switch','ts':1000000,'args':{'cpu':0,'prev_pid':0,'next_pid':2}}]}),td)
        self.assertEqual(td.parse_stats['failed'],1)
        self.assertEqual(len(td.events),1)
        compute_metrics(td)
