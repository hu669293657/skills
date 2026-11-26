"""Exercise the report's Python-to-browser chart data contract."""
from html.parser import HTMLParser
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import report_generator as report


class ChartMarkup(HTMLParser):
    payload = None

    def handle_starttag(self, tag, attributes):
        if tag == 'div' and 'data-spec' in dict(attributes):
            self.payload = dict(attributes)['data-spec']


class RecipeCoverageTests(unittest.TestCase):
    def test_category_with_outputs_but_no_curated_chart_exposes_coverage_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / 'cluster_analysis_output' / 'recipes' / 'export_summary'
            export.mkdir(parents=True)
            (export / 'summary.csv').write_text('rank\n0\n', encoding='utf-8')
            summary = {'categories': [{
                'key': 'other', 'title': '其他特性',
                'recipes': [{'name': 'export_summary', 'status': 'done', 'rows': 16},
                            {'name': 'p2p_pairing', 'status': 'unsupported', 'reason': '本数据未采集 P2P'}],
            }]}
            html = report.sec_recipes(summary, directory)
        self.assertIn('其他特性</h3>', html)
        self.assertIn('recipes/export_summary/', html)
        self.assertIn('16 行', html)
        self.assertIn('本数据未采集 P2P', html)


@unittest.skipUnless(shutil.which('node'), 'Node is needed to execute report JavaScript')
class ReportChartTests(unittest.TestCase):
    def test_offline_render_reads_chart_block_payload_and_shows_the_values(self):
        chart = report.chart_block({'kind': 'bar', 'cats': ['A & B < 2'],
                                    'series': [{'name': '耗时', 'data': [17.25]}]})
        parser = ChartMarkup()
        parser.feed(chart)
        self.assertIsNotNone(parser.payload)
        check = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const element = {
  dataset: {spec: input.payload}, style: {}, innerHTML: '',
  closest: () => ({open: true}),
};
const context = {
  document: {
    querySelectorAll: selector => selector === 'div.chart[data-spec]' ? [element] : [],
    querySelector: () => null,
    getElementById: () => null,
  },
  location: {hash: ''}, scrollY: 0,
  addEventListener: () => {},
};
context.window = context;
vm.runInNewContext(input.js, context);
process.stdout.write(element.innerHTML);
"""
        js = '\n'.join((report.CHART_RENDER_JS, report.PAGE_JS))
        result = subprocess.run(['node', '-e', check], input=json.dumps({'payload': parser.payload, 'js': js}),
                                capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertIn('<td>17.25</td>', result.stdout)
        self.assertIn('A &amp; B &lt; 2', result.stdout)
