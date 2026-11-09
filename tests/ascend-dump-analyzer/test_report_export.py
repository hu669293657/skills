"""Exercise direct rendering from the shipped HTML templates."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


REFERENCES = Path(__file__).resolve().parents[2] / 'ascend-dump-analyzer' / 'references'
SCRIPT = REFERENCES / 'render_report.py'


class ReportExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='Ascend report ')
        self.addCleanup(tmp.cleanup)
        self.slots = Path(tmp.name) / 'slots.json'
        self.report = Path(tmp.name) / 'report.html'

    def fixture(self, mode):
        filename = 'template_single_dump.html' if mode == 'single' else 'template_comparison.html'
        template = (REFERENCES / filename).read_text(encoding='utf-8')
        fields = {key: '' for key in re.findall(r'[{][{]+([A-Z_][A-Z0-9_]*)[}][}]+', template)}
        for derived in ('SCORE_OFFSET', 'SIMILARITY_WIDTH'):
            fields.pop(derived, None)
        fields.update(SCORE_COLOR='var(--ink-muted)', NAV_ITEMS='<a class="nav-item" href="#hero">概览</a>',
                      ASSESSMENT_BANNER='<div class="banner info">证据不足</div>')
        if mode == 'single':
            fields.update(HOSTNAME='<img src=x onerror=alert(1)>', HEALTH_SCORE=None,
                          META_DATA_ROWS='<tr><td>输入</td><td>snapshot.json</td><td>UNKNOWN</td></tr>',
                          ISSUE_ITEMS='<li>需要补采</li>', MISSING_ITEMS='<div>需要补采</div>')
        else:
            fields.update(SIMILARITY=None, FILE_COUNT=0, DIFF_COUNT=None, IDENTICAL_COUNT=None,
                          SCOPE_COMPARISON_ROWS='<tr><td>请求输入</td><td>未知</td><td>未知</td><td>读取失败</td></tr>',
                          FINDING_BANNERS='<div class="banner info">证据不足</div>')
        return fields

    def run_cli(self, mode, slots, *args):
        self.slots.write_text(json.dumps(slots, ensure_ascii=False), encoding='utf-8')
        return subprocess.run(
            [sys.executable, '-X', 'utf8', '-B', str(SCRIPT), '--template', mode,
             '--slots', str(self.slots), '--output', str(self.report), *args],
            capture_output=True, text=True, encoding='utf-8', timeout=10,
        )

    def test_single_uses_shipped_dashboard_and_escapes_text(self):
        slots = self.fixture('single')
        result = self.run_cli('single', slots)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = self.report.read_text(encoding='utf-8')
        self.assertIn('Single Dump Analyzer', html)
        self.assertIn('<nav class="sidebar"', html)
        self.assertIn('<td>snapshot.json</td>', html)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img ', html)
        self.assertNotIn('fonts.googleapis.com', html)
        self.assertFalse(list(self.report.parent.glob('*.md')))
        self.assertEqual(self.run_cli('single', slots, '--check').returncode, 0)

    def test_blocked_comparison_keeps_coverage_and_unknown_similarity(self):
        slots = self.fixture('comparison')
        result = self.run_cli('comparison', slots)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = self.report.read_text(encoding='utf-8')
        self.assertIn('Environment Diff Analyzer', html)
        self.assertIn('id="scope-comparison"', html)
        self.assertIn('读取失败', html)
        self.assertIn('相似度 —', html)
        self.assertNotIn('相似度 0%', html)
        self.assertNotIn('相似度 —%', html)

    def test_missing_template_slot_leaves_previous_report_intact(self):
        self.report.write_text('previous report', encoding='utf-8')
        slots = self.fixture('single')
        del slots['WORKLOAD_ROWS']
        result = self.run_cli('single', slots)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WORKLOAD_ROWS', result.stderr)
        self.assertEqual(self.report.read_text(encoding='utf-8'), 'previous report')

    def test_fragment_and_style_slots_reject_active_content(self):
        for key, value in (('ASSESSMENT_BANNER', '<script>alert(1)</script>'),
                           ('NAV_ITEMS', '<a href="javascript:alert(1)">link</a>'),
                           ('ISSUE_ITEMS', '<li onclick="alert(1)">text</li>'),
                           ('SCORE_COLOR', 'red; background:url(https://example.org)')):
            with self.subTest(slot=key):
                slots = self.fixture('single')
                slots[key] = value
                self.assertNotEqual(self.run_cli('single', slots).returncode, 0)
                self.assertFalse(self.report.exists())

    def test_check_rejects_modified_report_without_rewriting(self):
        slots = self.fixture('single')
        result = self.run_cli('single', slots)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.report.write_text('different report', encoding='utf-8')
        self.assertNotEqual(self.run_cli('single', slots, '--check').returncode, 0)
        self.assertEqual(self.report.read_text(encoding='utf-8'), 'different report')


if __name__ == '__main__':
    unittest.main()
