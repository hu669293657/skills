"""Regression fixtures are synthetic and contain no user profiling payload."""
import csv
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import venv
from zipfile import ZipFile
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import advisor_fallback as advisor
import cluster_fallback as cluster
import compare_fallback as compare
import detect_prof as detect
import recipe_fallback as recipes
import report_generator as report
from prof_compare.parsers import operator
from prof_compare.xlsx import Sheet, open_workbook



def csv_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def card(root, rank, db=False):
    p = root / f'host_{rank}_12345678901234567_ascend_pt'
    apo = p / 'ASCEND_PROFILER_OUTPUT'
    apo.mkdir(parents=True)
    (p / f'profiler_info_{rank}.json').write_text(json.dumps({'rank_id': str(rank), 'config': {'experimental_config': {'_export_type': ['text']}}}), encoding='utf-8')
    (p / 'profiler_metadata.json').write_text(json.dumps({'distributed_args': {'world_size': 4}}), encoding='utf-8')
    csv_file(apo / 'step_trace_time.csv', [{'Step': str(s), 'Computing': 1000, 'Communication(Not Overlapped)': 100 + rank * 100, 'Communication': 200, 'Free': 200, 'Stage': 2000, 'Preparing': 0} for s in range(5)])
    csv_file(apo / 'op_statistic.csv', [{'OP Type': 'Mul', 'Total Time(us)': 1000, 'Count': 5, 'Avg Time(us)': 200, 'Max Time(us)': 200}])
    csv_file(apo / 'kernel_details.csv', [{'Name': 'kernel', 'Duration(us)': 100, 'Accelerator Core': 'AI_CORE', 'Input Shapes': '1,4', 'Input Data Types': 'float16', 'Block Dim': 48}])
    if db:
        with closing(sqlite3.connect(apo / f'ascend_pytorch_profiler_{rank}.db')) as conn:
            conn.execute('CREATE TABLE sample (x INTEGER)')
            conn.commit()
    return p


class RegressionTests(unittest.TestCase):
    def test_advisor_report_renders_each_communication_type(self):
        cards = []
        for rank in range(2):
            directory = card(self.path, rank)
            apo = directory / 'ASCEND_PROFILER_OUTPUT'
            ops = {f'hcom_allReduce__{i}@group': {'Communication Time Info': {'Elapse Time(ms)': 100 + i}}
                   for i in range(12)}
            (apo / 'communication.json').write_text(json.dumps({'step0': {
                'collective': ops,
                'p2p': {'hcom_send__9@group': {'Communication Time Info': {'Elapse Time(ms)': 2}}},
            }}), encoding='utf-8')
            cards.append({'rank': rank, 'dir': str(directory), 'apo': str(apo)})
        results = advisor.analyze(cards)
        advisor.write_report(results, cards, str(self.path / 'advisor'), str(self.path))
        metadata = report.load_advisor(str(self.path))
        groups = metadata['data']['top_comm_by_type']
        self.assertEqual(len(groups['AllReduce']), 10)
        self.assertEqual(groups['Send'][0]['elapse'], 4)
        document = report.sec_advisor(metadata, str(self.path))
        self.assertIn('AllReduce', document)
        self.assertIn('hcom_send__9', document)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='技能 regression ')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def workflow(self, root, output, *flags, skill_root=ROOT, python=sys.executable):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        env.pop('PYTHONPATH', None)
        return subprocess.run(
            [str(python), '-B', str(skill_root / 'scripts/run_workflow.py'),
             '--input', str(root), '--output-dir', str(output), '--no-native', '--skip-swimlane', *flags],
            cwd=skill_root, capture_output=True, text=True, encoding='utf-8', env=env, timeout=60)

    def test_text_config_with_db_is_text(self):
        c = card(self.path, 0, db=True)
        fmt, _, detail = detect.detect_card_format(str(c))
        self.assertEqual(fmt, 'text')
        self.assertTrue(detail['has_db'])
        self.assertTrue(detail['capabilities']['fallback_advisor'])

    def test_rank_is_integer(self):
        self.assertEqual(detect.read_rank(str(card(self.path, 0))), 0)

    def test_corrupt_db_is_not_usable(self):
        c = card(self.path, 0)
        apo = c / 'ASCEND_PROFILER_OUTPUT'
        for f in apo.iterdir():
            f.unlink()
        (apo / 'ascend_pytorch_profiler_0.db').write_bytes(b'not sqlite')
        self.assertEqual(detect.detect_card_format(str(c))[0], 'none')

    def test_corrupt_cluster_db_not_cached(self):
        p = self.path / 'cluster_analysis_output'
        p.mkdir()
        (p / 'cluster_analysis.db').write_bytes(b'not sqlite')
        self.assertFalse(detect.check_cluster_output(str(self.path))['has_db'])

    def test_recipe_directory_is_not_completion(self):
        (self.path / 'cluster_analysis_output/recipes/slow_rank').mkdir(parents=True)
        self.assertFalse(detect.check_cluster_output(str(self.path))['recipes_done'])

    def test_output_only_requires_rank_profs(self):
        p = self.path / 'cluster_analysis_output'
        p.mkdir()
        csv_file(p / 'cluster_step_trace_time.csv', [{'Step': '0', 'Type': 'rank', 'Index': 0, 'Computing': 1000, 'Communication': 100, 'Free': 100, 'Stage': 1200}])
        out = self.path / 'out'
        r = self.workflow(p, out)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        state = json.loads((out / 'workflow_status.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertIn('卡级 Prof', ' '.join(state['errors']))
        self.assertFalse((out / 'reports').exists())
        self.assertFalse((out / 'performance_report.html').exists())

    def test_shared_skill_compares_ranks_without_sibling_skill(self):
        shared = self.path / 'shared' / 'cluster-analysis'
        shutil.copytree(ROOT, shared, ignore=shutil.ignore_patterns('__pycache__'))
        venv_root = self.path / 'python'
        venv.EnvBuilder(with_pip=False).create(venv_root)
        python = venv_root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        root = self.path / 'input'
        for rank in (0, 1):
            apo = card(root, rank) / 'ASCEND_PROFILER_OUTPUT'
            csv_file(apo / 'kernel_details.csv', [
                {'Name': 'mul_kernel', 'Type': 'Mul', 'Duration(us)': 100 + rank,
                 'Start Time(us)': 5, 'Accelerator Core': 'AI_CORE', 'Input Shapes': '1,4',
                 'Input Data Types': 'float16', 'Block Dim': 48}])
            csv_file(apo / 'op_statistic.csv', [
                {'OP Type': 'Mul', 'Core Type': 'AI_CORE', 'Count': 1, 'Total Time(us)': 100 + rank}])
            csv_file(apo / 'operator_details.csv', [
                {'Name': 'aten::mul', 'Host Self Duration(us)': 5 + rank,
                 'Host Total Duration(us)': 7 + rank}])
        out = self.path / 'out'
        r = self.workflow(root, out, '--base-rank', '1', '--compare-rank', '0', skill_root=shared, python=python)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        reports = out / 'reports'
        self.assertTrue((out / 'compare/performance_comparison_result_0_1.xlsx').is_file(), r.stdout + r.stderr)
        self.assertTrue((reports / 'compare_analysis_report.html').is_file(), r.stdout + r.stderr)
        detail = json.loads((reports / 'compare_analysis_result.json').read_text(encoding='utf-8'))
        self.assertEqual(detail['metadata']['comparison']['base']['rank'], 1)
        self.assertEqual(detail['metadata']['comparison']['compare']['rank'], 0)
        self.assertIn('host_kernel', detail, r.stdout + r.stderr)
        self.assertEqual(detail['host_kernel']['device'][0]['op'], 'Mul')
        self.assertEqual(detail['host_kernel']['host'][0]['npu_name'], 'aten::mul')
        self.assertTrue(detail['host_kernel']['execution'])
        chinese_book = reports / 'chinese_xlsx/compare_chinese_result.xlsx'
        with open_workbook(chinese_book) as workbook:
            self.assertIn('总体性能比对', workbook)
            hot_rows = [list(sheet.iter_rows()) for name, sheet in workbook.items() if 'Top' in name]
            self.assertTrue(any(any('Mul' in row for row in rows) for rows in hot_rows), list(workbook))
        self.assertFalse((reports / 'cluster_analysis_report.html').exists())

        # A legacy artifact from a previous run must stay out of the new report.
        (reports / 'cluster_analysis_report.html').write_text('legacy report', encoding='utf-8')
        r = self.workflow(root, out, '--base-rank', '1', '--compare-rank', '0', skill_root=shared, python=python)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        html = (out / 'performance_report.html').read_text(encoding='utf-8')
        self.assertIn('集群总览', html)
        self.assertIn('src="reports/compare_analysis_report.html"', html)
        self.assertNotIn('cluster_analysis_report.html', html)

    def test_nested_root_closes_workflow(self):
        nested = self.path / 'input/inner'
        card(nested, 0)
        card(nested, 1)
        out = self.path / 'out'
        r = self.workflow(nested.parent, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((out / 'advisor/advisor.json').exists(), r.stdout)
        self.assertTrue((nested / 'cluster_analysis_output/recipes_summary.json').exists(), r.stdout)
        d = json.loads((out / 'detect_result.json').read_text(encoding='utf-8'))
        self.assertEqual(d['coverage']['missing_ranks'], [2, 3])
        self.assertEqual(d['coverage']['unique_step_count'], 5)

    def test_db_only_failure_is_nonzero_and_structured(self):
        c = card(self.path / 'input', 0, db=True)
        for f in (c / 'ASCEND_PROFILER_OUTPUT').glob('*.csv'):
            f.unlink()
        out = self.path / 'out'
        r = self.workflow(c.parent, out)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        s = json.loads((out / 'workflow_status.json').read_text(encoding='utf-8'))
        self.assertEqual(s['status'], 'failed')
        self.assertIn('DB', json.dumps(s))

    def test_empty_input_fails(self):
        p = self.path / 'empty'
        p.mkdir()
        self.assertNotEqual(self.workflow(p, self.path / 'out').returncode, 0)

    def test_cached_compare_respects_rank_parameters(self):
        root = self.path / 'input'
        card(root, 0)
        card(root, 1)
        out = self.path / 'out'
        self.assertEqual(self.workflow(root, out).returncode, 0)
        r = self.workflow(root, out, '--base-rank', '1', '--compare-rank', '0')
        self.assertEqual(r.returncode, 0, r.stdout)
        c = json.loads((out / 'compare/compare_analysis_result.json').read_text(encoding='utf-8'))
        self.assertEqual([c['base_card']['rank'], c['compare_card']['rank']], [1, 0])

    def test_advisor_aligns_cumulative_window(self):
        c = card(self.path, 0)
        res = advisor.analyze([{'rank': 0, 'apo': str(c / 'ASCEND_PROFILER_OUTPUT'), 'dir': str(c)}])
        texts = [s for _, s in advisor.build_advice(res) if 'Mul' in s]
        self.assertTrue(any('20.0%' in s for s in texts), texts)

    def test_missing_counters_do_not_become_no_bound(self):
        c = card(self.path, 0)
        findings, _, _ = advisor.load_kernel_findings(str(c / 'ASCEND_PROFILER_OUTPUT'))
        self.assertEqual(findings, [])

    def test_new_operator_does_not_crash_report(self):
        c = {'insights': {'operator': {'top_degraded': [{'Op Type': 'new', 'Diff Duration(ms)': 1, 'Diff Ratio': 'NEW'}]}}}
        rendered = report.sec_conclusions(None, None, c, 'cluster')
        self.assertIn('NEW', rendered)

    def test_compare_table_uses_ms(self):
        c = {'overall_metrics': [{'Dimension': 'E2E Time', 'Baseline(ms)': 527.76, 'Compare(ms)': 527.55, 'Diff(ms)': -.208, 'Diff Ratio': -.0004}]}
        rendered = report.sec_compare(c)
        self.assertIn('527.76', rendered)
        self.assertIn('基线(ms)', rendered)

    def test_communication_growth_does_not_mean_slower_e2e(self):
        rows = [{'Dimension': 'E2E Time', 'Baseline(ms)': 527.76, 'Compare(ms)': 527.55, 'Diff(ms)': -.208, 'Diff Ratio': -.0004}, {'Dimension': 'Uncovered Communication Time', 'Baseline(ms)': 137.6, 'Compare(ms)': 248.95, 'Diff(ms)': 111.355, 'Diff Ratio': .8093}]
        ins = compare.build_insights(rows, [], [], [], [])
        c = {'overall_metrics': rows, 'insights': ins, 'compare_card': {'rank': 3}}
        self.assertNotIn('整体更慢', report.sec_compare(c) + report.sec_conclusions(None, None, c, 'cluster'))

    def test_kernel_aggregation_preserves_shapes(self):
        a, b = card(self.path, 0), card(self.path, 1)
        csv_file(a / 'ASCEND_PROFILER_OUTPUT/kernel_details.csv', [{'Name': 'kernel', 'Duration(us)': 10, 'Input Shapes': s, 'Input Data Types': 'float16', 'Accelerator Core': 'AI_CORE'} for s in ('1,4', '2,4')])
        rows = compare.compare_kernel({'apo': str(a / 'ASCEND_PROFILER_OUTPUT')}, {'apo': str(b / 'ASCEND_PROFILER_OUTPUT')})
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['Input Shape'] for r in rows}, {'1,4', '2,4'})

    def test_missing_kernel_csv_allows_partial_compare(self):
        a, b = card(self.path, 0), card(self.path, 1)
        (a / 'ASCEND_PROFILER_OUTPUT/kernel_details.csv').unlink()
        rows = compare.compare_kernel({'apo': str(a / 'ASCEND_PROFILER_OUTPUT')}, {'apo': str(b / 'ASCEND_PROFILER_OUTPUT')})
        self.assertEqual(rows, [])

    def test_reads_existing_xlsx_shared_strings_and_cached_values(self):
        path = self.path / 'native_compare.xlsx'
        spreadsheet = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        office = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        package = 'http://schemas.openxmlformats.org/package/2006/relationships'
        with ZipFile(path, 'w') as archive:
            archive.writestr('_rels/.rels', f'<Relationships xmlns="{package}">'
                             f'<Relationship Id="rId1" Type="{office}/officeDocument" Target="xl/workbook.xml"/>'
                             '</Relationships>')
            archive.writestr('xl/workbook.xml', f'<workbook xmlns="{spreadsheet}" xmlns:r="{office}">'
                             '<sheets><sheet name="KernelCompare" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="{package}">'
                             f'<Relationship Id="rId1" Type="{office}/worksheet" Target="/xl/worksheets/sheet1.xml"/>'
                             f'<Relationship Id="rId2" Type="{office}/sharedStrings" Target="sharedStrings.xml"/>'
                             '</Relationships>')
            archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="{spreadsheet}">'
                             '<si><r><t>算子</t></r><r><t>&amp;Kernel</t></r></si></sst>')
            archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{spreadsheet}">'
                             '<dimension ref="A1:C3"/><sheetData>'
                             '<row r="1"><c r="B1" t="s"><v>0</v></c></row>'
                             '<row r="3"><c r="A3"><f>2+3.5</f><v>5.5</v></c>'
                             '<c r="C3" t="b"><v>0</v></c></row></sheetData></worksheet>')
        with open_workbook(path) as workbook:
            self.assertEqual(list(workbook['KernelCompare'].iter_rows()),
                             [(None, '算子&Kernel', None), (None, None, None), (5.5, None, False)])

    def test_operator_statistic_does_not_match_detail(self):
        ws = Sheet([['No', 'Op', 'Base', 'Count', 'Comp', 'Count', 'Diff', 'Ratio']])
        wb = {'OperatorCompareStatistic': ws}
        ws.rows.append([1, 'a', 10, 1, 20, 1, 10, 1])
        self.assertFalse(operator.parse_compare(wb)['available'])

    def test_concentration_uses_positive_increments(self):
        ws = Sheet([['No', 'Op', 'Base', 'Count', 'Comp', 'Count', 'Diff', 'Ratio']])
        wb = {'OperatorCompareStatistic': ws}
        for i, diff in enumerate([10] * 11 + [-100]):
            ws.rows.append([i, f'op{i}', 100, 1, 100 + diff, 1, diff, diff / 100])
        self.assertAlmostEqual(operator.parse_statistic(wb)['concentration'], 90.9, places=1)

    def test_fallback_communication_unit_and_base_info(self):
        cards = [cluster.collect_card(str(card(self.path, rank))) for rank in (0, 1)]
        cards[0]['comm'] = {'step0': {'collective': {'op@g': {'Communication Time Info': {'Elapse Time(ms)': 103.844799}}, 'Total Op Info': {'Communication Time Info': {'Elapse Time(ms)': 200}}}}}
        db = cluster.write_cluster_db(str(self.path), cards)
        with closing(sqlite3.connect(db)) as conn:
            info = dict(conn.execute('SELECT key, value FROM ClusterBaseInfo'))
            self.assertEqual(info['communication_time_unit'], 'ms')
            self.assertEqual(info['world_size'], '4')
            rows = list(conn.execute(
                "SELECT group_name, elapsed_time FROM ClusterCommunicationTime WHERE hccl_op_name = 'op'"))
        self.assertEqual(rows, [('g', 103.844799)])

    def test_slow_rank_does_not_vote_across_groups(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE ClusterCommunicationTime(step,rank_id,hccl_op_name,group_name,elapsed_time)')
        conn.executemany('INSERT INTO ClusterCommunicationTime VALUES(?,?,?,?,?)', [('0', 0, 'hcom_allReduce__1', 'g0', 1), ('0', 1, 'hcom_allReduce__2', 'g1', 2)])
        ctx = recipes.Ctx(conn.cursor(), {}, str(self.path), str(self.path / 'recipes'))
        rows, reason = recipes.recipe_slow_rank(ctx)
        self.assertFalse(rows)
        self.assertTrue(reason)

    def test_unknown_module_is_unavailable(self):
        c = card(self.path, 0)
        csv_file(c / 'ASCEND_PROFILER_OUTPUT/operator_details.csv', [{'Name': 'op', 'Device Total Duration(us)': 100, 'Call Stack': ''}])
        ctx = recipes.Ctx(None, {0: str(c)}, str(self.path), str(self.path))
        rows, reason = recipes.recipe_module_statistic(ctx)
        self.assertIsNone(rows)
        self.assertTrue(reason)

    def test_failed_rebuild_preserves_old_db(self):
        good = [cluster.collect_card(str(card(self.path, rank))) for rank in (0, 1)]
        target = Path(cluster.write_cluster_db(str(self.path), good))
        old = target.read_bytes()
        good[0]['comm'] = {'step0': {'collective': {'invalid': None}}}
        with self.assertRaises(AttributeError):
            cluster.write_cluster_db(str(self.path), good)
        self.assertEqual(target.read_bytes(), old)

    def test_cache_same_parameters_reuses(self):
        root = self.path / 'input'
        card(root, 0)
        card(root, 1)
        out = self.path / 'out'
        self.assertEqual(self.workflow(root, out).returncode, 0)
        self.assertEqual(self.workflow(root, out).returncode, 0)
        status = json.loads((out / 'workflow_status.json').read_text(encoding='utf-8'))
        self.assertTrue(status['cache_reused'])

    def test_compare_rejects_mismatched_steps(self):
        root = self.path / 'input'
        a, b = card(root, 0), card(root, 1)
        f = b / 'ASCEND_PROFILER_OUTPUT/step_trace_time.csv'
        rows = list(csv.DictReader(f.open(encoding='utf-8')))
        rows[-1]['Step'] = '99'
        csv_file(f, rows)
        r = self.workflow(root, self.path / 'out')
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_time_recipe_excludes_stage_aggregate(self):
        cards = [cluster.collect_card(str(card(self.path, rank))) for rank in (0, 1)]
        path = cluster.write_cluster_db(str(self.path), cards)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("INSERT INTO ClusterStepTraceTime SELECT step,'stage',99,computing,communication_not_overlapped,overlapped,communication,free,stage,bubble,communication_not_overlapped_and_exclude_receive,preparing,dp_index,pp_index,tp_index FROM ClusterStepTraceTime LIMIT 1")
            ctx = recipes.Ctx(conn.cursor(), {}, str(self.path), str(self.path))
            rows, _ = recipes.recipe_cluster_time_summary(ctx)
            self.assertEqual(len(rows), 10)

    def test_slow_rank_aligned_complete_group_votes(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE CommunicationGroupMapping(rank_set,group_name,pg_name)')
        conn.execute("INSERT INTO CommunicationGroupMapping VALUES('(0,1)','g','pg')")
        conn.execute('CREATE TABLE ClusterCommunicationTime(step,rank_id,hccl_op_name,group_name,elapsed_time)')
        conn.executemany('INSERT INTO ClusterCommunicationTime VALUES(?,?,?,?,?)', [('0', 0, 'hcom_allReduce__1', 'g', 1), ('0', 1, 'hcom_allReduce__1', 'g', 2)])
        ctx = recipes.Ctx(conn.cursor(), {}, str(self.path), str(self.path / 'recipes'))
        rows, reason = recipes.recipe_slow_rank(ctx)
        self.assertEqual(rows[0]['rank'], 0)
        self.assertIsNone(reason)

    def test_numeric_missing_and_nonfinite_stay_unknown(self):
        from lib.common import to_f
        for v in ('N/A', 'nan', 'inf', None):
            self.assertIsNone(to_f(v, None))

    def test_utf8_bom_csv_header(self):
        from lib.common import read_csv_rows
        p = self.path / 'bom.csv'
        p.write_text('Step,Stage\n0,100\n', encoding='utf-8-sig')
        self.assertEqual(read_csv_rows(str(p))[0]['Step'], '0')

    def test_subprocess_start_failure_is_caught(self):
        import run_workflow
        rc, message = run_workflow.sh([str(self.path / 'missing.exe')])
        self.assertNotEqual(rc, 0)
        self.assertIn('Error', message)

    def test_subprocess_timeout_is_bounded(self):
        import run_workflow
        previous = run_workflow.COMMAND_TIMEOUT
        self.addCleanup(setattr, run_workflow, 'COMMAND_TIMEOUT', previous)
        run_workflow.COMMAND_TIMEOUT = .05
        rc, message = run_workflow.sh([sys.executable, '-c', 'import time; time.sleep(2)'])
        self.assertNotEqual(rc, 0)
        self.assertIn('TimeoutExpired', message)

    def test_partial_workflow_has_visible_failure(self):
        c = card(self.path / 'input', 0)
        (c / 'ASCEND_PROFILER_OUTPUT/kernel_details.csv').unlink()
        # 损坏的 trace_view.json：swimlane 具备数据源（运行），解析失败后
        # CSV 降级源为空 -> 阶段真实失败，必须以 partial/退出码 2 可见
        (c / 'ASCEND_PROFILER_OUTPUT/trace_view.json').write_text('{not-json', encoding='utf-8')
        out = self.path / 'out'
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/run_workflow.py'), '--input', str(c), '--output-dir', str(out), '--no-native'], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(r.returncode, 2, r.stdout)
        status = json.loads((out / 'workflow_status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['status'], 'partial')
        self.assertIn('分析未完整完成', (out / 'performance_report.html').read_text(encoding='utf-8'))

    def test_swimlane_without_any_data_source_is_unsupported(self):
        """DB-only（无 trace_view/kernel_details/api_statistic/operator_details）
        的卡：泳道记 unsupported 并注明原因，不算失败。"""
        c = card(self.path / 'input', 0)
        apo = c / 'ASCEND_PROFILER_OUTPUT'
        (apo / 'kernel_details.csv').unlink()
        out = self.path / 'out'
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/run_workflow.py'), '--input', str(c), '--output-dir', str(out), '--no-native'], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout)
        status = json.loads((out / 'workflow_status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['stages']['swimlane_rank0']['status'], 'unsupported')

    def test_cache_invalidates_small_file_content_and_code(self):
        from lib.workflow_state import cache_key
        root = self.path / 'input'
        root.mkdir()
        p = root / 'metadata.json'
        p.write_text('{"a":1}', encoding='utf-8')
        stamp = p.stat()
        before = cache_key(str(root), str(self.path / 'out'), str(ROOT), {})
        p.write_text('{"a":2}', encoding='utf-8')
        os.utime(p, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertNotEqual(before, cache_key(str(root), str(self.path / 'out'), str(ROOT), {}))

    def test_timeout_stops_owned_child_process(self):
        import run_workflow
        import time
        marker = self.path / 'should_not_exist'
        payload = 'import time; from pathlib import Path; time.sleep(1); Path(' + repr(str(marker)) + ').write_text("unexpected")'
        parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(payload) + ']); time.sleep(5)'
        previous = run_workflow.COMMAND_TIMEOUT
        self.addCleanup(setattr, run_workflow, 'COMMAND_TIMEOUT', previous)
        run_workflow.COMMAND_TIMEOUT = .2
        rc, _ = run_workflow.sh([sys.executable, '-c', parent])
        self.assertNotEqual(rc, 0)
        time.sleep(1.2)
        self.assertFalse(marker.exists())

    def test_report_uses_normalized_cluster_root_before_original_input(self):
        outer = self.path / 'input'
        inner = outer / 'inner'
        correct = inner / 'cluster_analysis_output'
        correct.mkdir(parents=True)
        (outer / 'cluster_analysis_output').mkdir()
        mw = self.path / 'out'
        mw.mkdir()
        (mw / 'detect_result.json').write_text(json.dumps({'input': str(outer), 'cluster_root': str(inner), 'cluster_output': {'path': None, 'expected_path': str(correct)}}), encoding='utf-8')
        self.assertEqual(Path(report.cluster_dir(str(mw))), correct)


if __name__ == '__main__':
    unittest.main()
