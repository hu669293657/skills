"""Exercise the actual report-export CLI; no third-party dependencies required."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'references' / 'render_report.py'


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.links = []
        self.visible = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href')

    def handle_data(self, data):
        self.visible.append(data)


class ReportExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='Ascend 报告 ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.md = self.base / '环境 分析.md'
        self.html = self.md.with_suffix('.html')

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, '-X', 'utf8', '-B', str(SCRIPT), str(self.md), *args],
            capture_output=True, text=True, encoding='utf-8', timeout=10,
        )

    def test_default_export_keeps_markdown_and_creates_complete_offline_html(self):
        source = '# 环境分析\n\n结论：证据不足，UNKNOWN。\n\n| 项目 | 状态 |\n| --- | --- |\n| driver | UNKNOWN |\n\n- 补采网络\n\n```bash\npython3 collect.py --filter\n```\n'
        self.md.write_text(source, encoding='utf-8-sig')
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.md.read_text(encoding='utf-8-sig'), source)
        html = self.html.read_text(encoding='utf-8')
        doc = Document(html)
        self.assertTrue({'html', 'title', 'h1', 'table', 'th', 'td', 'ul', 'li', 'pre', 'code'}.issubset(doc.tags))
        self.assertIn('结论：证据不足，UNKNOWN。', ''.join(doc.visible))
        self.assertNotIn('script', doc.tags)
        self.assertNotIn('link', doc.tags)
        self.assertIn(str(self.md), result.stdout)
        self.assertIn(str(self.html), result.stdout)
        self.assertEqual(self.run_cli('--check').returncode, 0)

    def test_untrusted_html_and_links_remain_inert(self):
        self.md.write_text('# <script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n[危险](javascript:alert) [文档](https://example.org/docs)\n\n```html\n<svg onload=alert(3)>\n```\n', encoding='utf-8')
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = Document(self.html.read_text(encoding='utf-8'))
        self.assertNotIn('script', doc.tags)
        self.assertNotIn('img', doc.tags)
        self.assertNotIn('svg', doc.tags)
        self.assertEqual(doc.links, ['https://example.org/docs'])
        self.assertIn('<img src=x onerror=alert(2)>', ''.join(doc.visible))

    def test_check_rejects_missing_stale_and_modified_html_without_rewriting(self):
        self.md.write_text('# 报告\n\n原结论。\n', encoding='utf-8')
        self.assertNotEqual(self.run_cli('--check').returncode, 0)
        self.assertFalse(self.html.exists())
        self.assertEqual(self.run_cli().returncode, 0)
        generated = self.html.read_bytes()
        self.md.write_text('# 报告\n\n新结论。\n', encoding='utf-8')
        self.assertNotEqual(self.run_cli('--check').returncode, 0)
        self.assertEqual(self.html.read_bytes(), generated)
        self.assertEqual(self.run_cli().returncode, 0)
        self.html.write_text(self.html.read_text(encoding='utf-8').replace('新结论。', '错误结论。'), encoding='utf-8')
        self.assertNotEqual(self.run_cli('--check').returncode, 0)

    def test_missing_or_empty_markdown_is_failure_and_existing_html_survives(self):
        self.html.write_text('previous report', encoding='utf-8')
        for source in (None, '', ' \n\t'):
            with self.subTest(source=source):
                if source is not None:
                    self.md.write_text(source, encoding='utf-8')
                result = self.run_cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.html.read_text(encoding='utf-8'), 'previous report')

    def test_same_source_supports_literal_pipes_and_template_like_evidence(self):
        self.md.write_text('# 报告\n\n| 字段 | 值 |\n| --- | --- |\n| `a|b` | a\\|b |\n\n原始配置键 `{{HOSTNAME}}` 待核验。\n', encoding='utf-8')
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = Document(self.html.read_text(encoding='utf-8'))
        self.assertEqual(doc.tags.count('td'), 2)
        self.assertIn('a|b', ''.join(doc.visible))
        self.assertIn('{{HOSTNAME}}', ''.join(doc.visible))


if __name__ == '__main__':
    unittest.main()
