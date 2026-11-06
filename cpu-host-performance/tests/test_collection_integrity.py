"""Evidence reconciliation must not turn every failure into a recollection request."""
import json
import tempfile
import unittest
from pathlib import Path

from analyzer.parser import TraceData, Event, load_input, cleanup_temp
from analyzer.metrics import compute_metrics
from analyzer.analyze_cpu_trace import run_analysis


def result(entries, snapshots=None):
    td = TraceData()
    td.snapshot = dict(snapshots or {})
    td.snapshot['collection_manifest.json'] = json.dumps({'schema_version': 1, 'entries': entries})
    td.events = [Event(1, 0, 'sched_switch', fields={'prev_pid': '0', 'next_pid': '1'}),
                 Event(2, 0, 'sched_switch', fields={'prev_pid': '1', 'next_pid': '0'})]
    return compute_metrics(td)['collection_integrity']


def entry(category='npu_info', status='COLLECTED', **extra):
    row = {'category': category, 'requested': True, 'command': ['npu-smi', 'info'],
           'command_exists': True, 'returncode': 0, 'raw_path': 'hostbound/npu_smi_info.txt',
           'raw_exists': True, 'empty': False, 'parse_success': True, 'parsed_count': 1,
           'started_at': '2026-11-04T00:00:00Z', 'ended_at': '2026-11-04T00:00:01Z',
           'failure_reason': '', 'status': status}
    row.update(extra)
    return row


class IntegrityTests(unittest.TestCase):
    def test_buffer_overrun_recollection_uses_larger_per_cpu_buffer(self):
        from analyzer.collection_integrity import analyze_collection_integrity
        result = analyze_collection_integrity({'buffer_overrun': 3990000, 'buffer_overrun_known': True}, {})
        action = next(row for row in result['actions'] if row['status'] == 'LOST_EVENTS')
        self.assertIn('--buffer-size 16384', action['command'])
        self.assertEqual(result['ftrace']['lost_events'], 3990000)

    def test_absent_declared_raw_is_not_included_and_not_blindly_recollected(self):
        data = result([entry()])
        self.assertEqual(data['entries'][0]['status'], 'NOT_INCLUDED')
        self.assertEqual(data['entries'][0]['collector_status'], 'COLLECTED')
        self.assertFalse(data['actions'][0]['recollect'])

    def test_garbage_raw_is_parse_failed_not_missing(self):
        data = result([entry()], {'hostbound/npu_smi_info.txt': 'unexpected new vendor table'})
        self.assertEqual(data['entries'][0]['status'], 'PARSE_FAILED')
        self.assertTrue(data['entries'][0]['raw_exists'])
        self.assertFalse(data['actions'][0]['recollect'])

    def test_no_process_is_success_without_recollection(self):
        data = result([entry()], {'hostbound/npu_smi_info.txt': 'No running processes found'})
        self.assertEqual(data['entries'][0]['status'], 'NO_ACTIVE_PROCESS')
        self.assertEqual(data['entries'][0]['parsed_count'], 0)
        self.assertFalse(any(a['recollect'] for a in data['actions']))

    def test_dependency_permission_and_not_requested_remain_distinct(self):
        for status in ('COMMAND_NOT_FOUND', 'PERMISSION_DENIED', 'UNSUPPORTED', 'NOT_REQUESTED'):
            with self.subTest(status=status):
                data = result([entry(status=status, raw_exists=False, requested=status != 'NOT_REQUESTED')])
                self.assertEqual(data['entries'][0]['status'], status)
                action = data['actions'][0]
                self.assertEqual(action['recollect'], status == 'NOT_REQUESTED')
                if action['recollect']:
                    self.assertIn('sudo bash scripts/cpu_trace_collect.sh', action['command'])
                    self.assertNotIn('--hostbound', action['command'])

    def test_npu_missing_does_not_invalidate_ftrace(self):
        data = result([entry(status='COMMAND_NOT_FOUND', raw_exists=False)])
        self.assertTrue(data['ftrace']['events_present'])
        self.assertTrue(data['ftrace']['parse_success'])
        self.assertEqual(data['ftrace']['loss_status'], 'UNKNOWN')
        self.assertFalse(data['hostbound']['complete'])

    def test_loaded_trace_export_failure_reconciles_its_manifest_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); (base/'hostbound').mkdir()
            (base/'trace.txt').write_text('t-1 [000] .... 1.000: sched_switch: prev_pid=0 next_pid=1\n'
                                         't-1 [000] .... 2.000: sched_switch: prev_pid=1 next_pid=0\n', encoding='utf-8')
            (base/'hostbound/export_errors.txt').write_text('cpu1 export_failed', encoding='utf-8')
            (base/'collection_manifest.json').write_text(json.dumps({'entries': [entry('trace', raw_path='trace.txt')]}), encoding='utf-8')
            td = load_input(str(base))
            try:
                data = compute_metrics(td)['collection_integrity']
                self.assertEqual(data['entries'][0]['status'], 'EXECUTION_FAILED')
                self.assertIn('cpu1 export_failed', data['entries'][0]['failure_reason'])
                self.assertFalse(data['entries'][0]['parse_success'])
                self.assertEqual(data['entries'][0]['collector_status'], 'COLLECTED')
                self.assertFalse(data['ftrace']['parse_success'])
            finally:
                cleanup_temp(td)

    def test_bad_manifest_retains_parse_failure(self):
        td = TraceData()
        td.snapshot = {'collection_manifest.json': '{bad'}
        data = compute_metrics(td)['collection_integrity']
        self.assertEqual(data['manifest_status'], 'PARSE_FAILED')
        self.assertFalse(any(a['recollect'] for a in data['actions']))

    def test_wrong_shape_manifest_and_capabilities_do_not_abort_analysis(self):
        td = TraceData()
        td.snapshot = {'collection_manifest.json': '[]', 'hostbound/capabilities.json': '{"events":123}'}
        data = compute_metrics(td)['collection_integrity']
        self.assertEqual(data['manifest_status'], 'PARSE_FAILED')

    def test_invalid_capability_contract_is_parse_failure(self):
        data = result([entry('capabilities', raw_path='hostbound/capabilities.json')],
                      {'hostbound/capabilities.json': '{"events":{"power/cpu_idle":123}}'})
        self.assertEqual(data['entries'][0]['status'], 'PARSE_FAILED')

    def test_old_package_is_inferred_without_claiming_manifest(self):
        td = TraceData()
        td.snapshot = {'hostbound/npu_smi_info.txt': 'No running processes found',
                       'hostbound/numactl.txt': 'node 0 cpus: 0 1'}
        data = compute_metrics(td)['collection_integrity']
        self.assertEqual(data['manifest_status'], 'LEGACY_NOT_PROVIDED')
        statuses = {e['category']: e['status'] for e in data['entries']}
        self.assertEqual(statuses['npu_info'], 'NO_ACTIVE_PROCESS')
        self.assertEqual(statuses['cpu_topology'], 'COLLECTED')

    def test_manifest_only_cli_reports_unavailable_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            capture = base / 'capture'; capture.mkdir()
            (capture / 'collection_manifest.json').write_text(json.dumps({'schema_version': 1,
                'entries': [entry('trace', 'UNSUPPORTED', raw_path='trace.txt', raw_exists=False,
                                  failure_reason='tracefs unavailable')]}), encoding='utf-8')
            run_analysis(str(capture), str(base / 'report'), quiet=True)
            data = json.loads((base / 'report/report_data.json').read_text(encoding='utf-8'))
            self.assertFalse(data['metrics']['collection_integrity']['ftrace']['events_present'])
            self.assertIn('tracefs unavailable', (base / 'report/hostbound_report.html').read_text(encoding='utf-8'))

    def test_arbitrary_manifest_raw_retained_without_becoming_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); hb = base / 'cpu_trace/hostbound'; hb.mkdir(parents=True)
            (hb / 'vendor.out').write_text('node 0 cpus: 0 1', encoding='utf-8')
            (base / 'cpu_trace/collection_manifest.json').write_text(json.dumps({'entries': [
                entry('cpu_topology', raw_path='hostbound/vendor.out')]}), encoding='utf-8')
            td = load_input(str(base))
            try:
                self.assertIn('hostbound/vendor.out', td.snapshot)
                self.assertEqual(td.parse_stats.get('parsed', 0), 0)
                self.assertIn('hostbound/vendor.out', td.source_files)
            finally:
                cleanup_temp(td)

    def test_trace_shared_as_idle_evidence_is_still_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'trace.txt').write_text('t-1 [000] .... 1.000: cpu_idle: state=1 cpu_id=0\n'
                                           't-1 [000] .... 2.000: cpu_idle: state=-1 cpu_id=0\n', encoding='utf-8')
            (base / 'collection_manifest.json').write_text(json.dumps({'entries': [
                entry('idle', raw_path='trace.txt'), entry('trace', raw_path='trace.txt')]}), encoding='utf-8')
            td = load_input(str(base))
            try:
                self.assertEqual(len(td.events), 2)
            finally:
                cleanup_temp(td)

    def test_malformed_declared_json_sidecar_is_not_silently_empty(self):
        data = result([entry('processes', raw_path='hostbound/process_start.json')],
                      {'hostbound/process_start.json': '{"processes":123,"threads":[]}'} )
        self.assertEqual(data['entries'][0]['status'], 'PARSE_FAILED')

    def test_one_valid_topology_source_cannot_mask_another_failed_parser(self):
        data = result([entry('cpu_topology', raw_path='hostbound/numactl.txt')],
                      {'hostbound/numactl.txt': 'unexpected output',
                       'hostbound/lscpu_parse.txt': '# CPU,Core,Socket,Node,Online\n0,0,0,0,Y'})
        self.assertEqual(data['entries'][0]['status'], 'PARSE_FAILED')


if __name__ == '__main__':
    unittest.main()
