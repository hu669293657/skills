#!/usr/bin/env python3
"""Render the selected rank pair using the bundled prof-compare engine."""
import argparse
from pathlib import Path
import sys

from lib.common import read_json
from lib.config import PROF_COMPARE_REPORT, VENDOR_RC_SKIP


def cmd_compare(args):
    compare_dir = Path(args.compare_dir).resolve()
    reports_dir = Path(args.reports_dir).resolve()
    summary = read_json(compare_dir / 'compare_analysis_result.json') or {}
    cards = [summary.get(role + '_card') or {} for role in ('base', 'compare')]
    base, compare = cards
    ranks_known = all('rank' in card for card in cards)
    if ranks_known:
        workbook = compare_dir / f"performance_comparison_result_{compare['rank']}_{base['rank']}.xlsx"
    else:
        workbook = next(iter(sorted(compare_dir.glob('performance_comparison_result_*.xlsx'))), None)
    if workbook is None or not workbook.is_file():
        print('SKIP: 未找到本次比对的 performance_comparison_result_*.xlsx')
        return VENDOR_RC_SKIP
    from prof_compare.main_analyzer import analyze
    from prof_compare.host_kernel import compare_npus
    from prof_compare.npu_source import npu_source
    from prof_compare.profile_identity import comparison_identity, read_profile_identity

    identities = []
    sources = []
    for card in cards:
        path = card.get('dir')
        source = None
        if path:
            try:
                source = npu_source(path)
            except (OSError, ValueError) as exc:
                print(f'WARN: rank {card["rank"]} 原始明细不可用：{exc}')
        sources.append(source)
        identity = read_profile_identity(path, source, None) if source else {'input_path': path}
        if 'rank' in card:
            identity.update(rank=card['rank'], device_type='NPU')
        identities.append(identity)

    detail = None
    if all(sources):
        try:
            detail = compare_npus(compare['dir'], base['dir'])
            detail['side_roles'] = ['compare', 'base']
        except (OSError, ValueError) as exc:
            print(f'WARN: 原始 Host / Kernel 明细不可用，使用比对工作簿：{exc}')

    comparison = None
    if ranks_known:
        labels = tuple(f"Rank {card['rank']}" for card in cards)
        comparison = comparison_identity(*identities, labels=labels)
    analyze(str(workbook), str(reports_dir), host_kernel=detail, comparison=comparison)
    out_html = reports_dir / PROF_COMPARE_REPORT
    if not out_html.is_file():
        print(f'ERROR: prof-compare 未生成 {out_html}')
        return 1
    print(f'OK: {out_html}')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    compare = sub.add_parser('compare', help='生成 prof-compare 子报告')
    compare.add_argument('--compare-dir', required=True, help='P4 比对工作簿和中间件目录')
    compare.add_argument('--reports-dir', required=True, help='子报告输出目录')
    args = parser.parse_args()
    return cmd_compare(args)


if __name__ == '__main__':
    sys.exit(main())
