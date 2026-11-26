"""Regression coverage for report severity and full-Step Free evidence."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from insight_locator import free, severity
import report_generator as report


class CandidateEvidenceTests(unittest.TestCase):
    def test_main_sections_share_weights_and_keep_compute_details_collapsed(self):
        locator = {'compute': [{'issue': '跨卡差异', 'location': '400 / 2卡 / 3ms',
                                'impact': '命中', 'cause': '核查计算',
                                'evidence': {'rank': 2, 'step': '400', 'name': 'testKernel',
                                             'matching_rank_excess_ns': 20, 'stage_ns': 100,
                                             'instances': [{'rank': 2, 'pmu': {'aiv_vec_ratio': 0.2}},
                                                           {'rank': 4, 'pmu': {'aiv_vec_ratio': 0.1}}]}}]}
        locator['severity'] = severity(locator)
        overview = report.sec_overview({}, None, None, 'cluster', locator=locator)
        conclusions = report.sec_conclusions(None, None, None, 'cluster', locator=locator)
        self.assertIn('data-severity="compute">100.0%', overview)
        blocks = [
            '<h3>采集窗口与 Stage 时间构成</h3>', 'class="kpi-row"',
            '<h3>瓶颈严重度</h3>', '<h3>概览小结</h3>',
        ]
        offsets = [overview.index(block) for block in blocks]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn('data-severity="compute">100.0%', conclusions)
        self.assertIn('<details class="bw-compute-detail">', conclusions)
        self.assertIn('20.00%', conclusions)
        self.assertIn('10.00%', conclusions)

    def test_normalization_uses_one_peak_per_kind_and_totals_exactly_100(self):
        def row(amount):
            return {'evidence': {'stage_ns': 100, 'duration_ns': amount,
                                 'start_skew_ns': amount, 'matching_rank_excess_ns': amount}}
        result = severity({'free': [row(60), row(59)], 'compute': [row(10)],
                           'communication': [row(20)]})
        self.assertEqual({k: v['tenths'] for k, v in result.items()},
                         {'free': 667, 'compute': 111, 'communication': 222})

    def test_free_preserves_full_step_and_exact_candidate_gaps(self):
        class Card:
            rank = 3
            host = 'fixture'
            steps = [{'id': '7', 'start': 100, 'end': 15_000_100}]
            frees = {'7': [(200, 9_000_200), (10_000_100, 10_500_100),
                           (11_000_100, 11_500_100), (12_000_100, 12_500_100)]}

            def stage_ns(self, step):
                return 15_000_000

            def free_cause(self, start, end):
                return '查下发'

        continuous, fragmented = free([Card()], 100)
        initial = continuous['evidence']['timeline']
        tail = fragmented['evidence']['timeline']
        self.assertEqual((initial['start_ns'], initial['end_ns']), (0, 15_000_000))
        self.assertEqual(len(initial['free_intervals_ns']), 4)
        self.assertEqual(initial['selected_intervals_ns'], [[100, 9_000_100]])
        self.assertEqual(tail['selected_intervals_ns'],
                         [[10_000_000, 10_500_000], [11_000_000, 11_500_000], [12_000_000, 12_500_000]])
