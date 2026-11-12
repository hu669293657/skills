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
        for derived in ('SCORE_OFFSET', 'SIMILARITY_WIDTH', 'COMPATIBILITY_LOOKUP'):
            fields.pop(derived, None)
        fields.update(SCORE_COLOR='var(--ink-muted)', NAV_ITEMS='<a class="nav-item" href="#hero">概览</a>',
                      ASSESSMENT_BANNER='<div class="banner info">暂无可判定结论</div>',
                      COMPATIBILITY_ROWS='<tr><td colspan="5">待核验</td></tr>')
        if mode == 'single':
            fields.update(HOSTNAME='<img src=x onerror=alert(1)>', HEALTH_SCORE=None,
                          META_DATA_ROWS='<tr><td>输入</td><td>snapshot.json</td><td>UNKNOWN</td></tr>',
                          ISSUE_ITEMS='<li>已确认的发现</li>', MISSING_ITEMS='<div>网络探测未采集</div>')
        else:
            fields.update(SIMILARITY=None, FILE_COUNT=None, DIFF_COUNT=None, IDENTICAL_COUNT=None,
                          SCOPE_COMPARISON_ROWS='',
                          MISSING_SECTION='<section id="missing"><h2>未采集项</h2><p>输入读取失败</p></section>',
                          COMPATIBILITY_ROWS='<tr><td colspan="6">待核验</td></tr>',
                          FINDING_BANNERS='')
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
        self.assertIn('单份分析', html)
        self.assertIn('状态总览', html)
        self.assertIn('版本配套检查', html)
        self.assertIn('待核验', html)
        self.assertEqual(html.count('网络探测未采集'), 1)
        self.assertLess(html.index('id="missing"'), html.index('网络探测未采集'))
        self.assertNotIn('id="diagnosis"', html)
        self.assertIn('<nav class="sidebar"', html)
        self.assertIn('<td>snapshot.json</td>', html)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img ', html)
        self.assertNotIn('fonts.googleapis.com', html)
        self.assertFalse(list(self.report.parent.glob('*.md')))
        self.assertEqual(self.run_cli('single', slots, '--check').returncode, 0)

    def test_blocked_comparison_keeps_read_failures_in_missing_and_hides_similarity(self):
        slots = self.fixture('comparison')
        result = self.run_cli('comparison', slots)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = self.report.read_text(encoding='utf-8')
        self.assertIn('多份比对', html)
        self.assertIn('状态总览', html)
        self.assertIn('id="scope-comparison"', html)
        self.assertEqual(html.count('读取失败'), 1)
        self.assertLess(html.index('id="missing"'), html.index('读取失败'))
        self.assertIn('class="score-ring" data-collected=""', html)
        self.assertIn('class="sim-bar" data-collected=""', html)
        self.assertNotIn('相似度 —', html)
        self.assertNotIn('相似度 0%', html)

    def test_collected_component_card_opens_lookup_without_a_matrix_link(self):
        for mode in ('single', 'comparison'):
            with self.subTest(mode=mode):
                slots = self.fixture(mode)
                slots.pop('VERSION_MISMATCH_BANNER', None)
                result = self.run_cli(mode, slots)
                self.assertEqual(result.returncode, 0, result.stderr)
                html = self.report.read_text(encoding='utf-8')
                self.assertIn('id="ascend"', html)
                self.assertNotIn('id="compatibility-lookup"', html)

                slots['COMPATIBILITY_ENVIRONMENTS'] = [{
                    'name': '示例节点', 'scene': 'vllm', 'versions': {
                        'vllm_ascend': '0.30.0', 'cann': '9.2.0', 'vllm': '0.30.0',
                        'torch': '2.10.0', 'torch_npu': '2.10.0.post7', 'python': '3.11.9',
                    },
                }]
                slots['COMPATIBILITY_ROWS'] = (
                    '<tr><td><a href="https://example.org/matrix">内置发布基线</a></td></tr>'
                )
                result = self.run_cli(mode, slots)
                self.assertEqual(result.returncode, 0, result.stderr)
                html = self.report.read_text(encoding='utf-8')
                ascend = html.split('id="ascend"', 1)[1].split('</section>', 1)[0]
                self.assertIn('版本配套检查', ascend)
                self.assertIn('id="compatibility-lookup"', ascend)
                self.assertIn('data-compatibility-template=', ascend)
                self.assertIn('vLLM Ascend 0.30.0', ascend)
                vllm_panel = ascend.split('<template id="compatibility-card-0-vllm_ascend">', 1)[1]
                vllm_panel = vllm_panel.split('</template>', 1)[0]
                self.assertIn('<th scope="col">推荐版本</th>', vllm_panel)
                self.assertRegex(vllm_panel, r'<th scope="row">CANN</th><td>9\.2\.0</td><td>.*9\.2\.1')
                self.assertNotIn('<th scope="row">vLLM Ascend</th>', vllm_panel)
                cann_panel = ascend.split('<template id="compatibility-card-0-cann">', 1)[1]
                cann_panel = cann_panel.split('</template>', 1)[0]
                self.assertRegex(cann_panel, r'<th scope="row">vLLM Ascend</th><td>0\.30\.0</td><td>.*0\.30\.0rc2')
                self.assertNotIn('<th scope="row">CANN</th>', cann_panel)
                self.assertIn('推荐改动 1 项', ascend)
                self.assertNotIn('https://', ascend)
                self.assertEqual(self.run_cli(mode, slots, '--check').returncode, 0)

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
