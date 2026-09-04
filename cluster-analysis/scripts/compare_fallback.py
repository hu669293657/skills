#!/usr/bin/env python3
"""Phase 4 fallback: compare the max/min communication-ratio cards.

Selects baseline (min comm ratio) and compare (max comm ratio) cards from
step_trace_time.csv, then builds msprof-compare-equivalent results:
performance_comparison_result_{compare}_{base}.xlsx + compare_analysis_result.json
(prof-compare style middleware with insights).
"""
import argparse
import csv
import json
import os
import re
import sys

from lib.common import is_junk, read_csv_rows, read_json as _read_json, to_f
from lib.config import AGG_ROWS, COMM_NAME_PATTERN

CARD_DIR_PATTERN = re.compile(r'.*_ascend_(pt|ms)$')


def read_json(path):
    """历史行为：缺失/损坏返回 {}（其余脚本为 None），经 lib 显式 default 保持。"""
    return _read_json(path, default={})


def find_cards(root):
    cards = []
    for dirpath, dirnames, _ in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel.count(os.sep) >= 3:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not is_junk(d)]
        for d in dirnames:
            if CARD_DIR_PATTERN.match(d):
                cards.append(os.path.join(dirpath, d))
    out = []
    for cd in sorted(cards):
        apo = os.path.join(cd, 'ASCEND_PROFILER_OUTPUT')
        st = read_csv_rows(os.path.join(apo, 'step_trace_time.csv'))
        if not st:
            continue
        rank = None
        for fn in sorted(os.listdir(cd)):
            m = re.match(r'^profiler_info_(\d+)\.json$', fn)
            if m:
                info = read_json(os.path.join(cd, fn))
                rank = int(info.get('rank_id', m.group(1)))
                break
        agg = {k: sum(to_f(r.get(k)) for r in st) / len(st) for k in
               ('Computing', 'Communication(Not Overlapped)', 'Communication', 'Free', 'Stage')}
        out.append({'dir': cd, 'apo': apo, 'rank': rank if rank is not None else len(out),
                    'trace': agg, 'comm_ratio': agg['Communication(Not Overlapped)'] / agg['Stage']
                    if agg['Stage'] else 0, 'steps': len(st)})
    out.sort(key=lambda c: c['rank'])
    return out


def avg_step_trace(card):
    return card['trace']


def compare_overall(base, comp):
    dims = [('E2E Time', 'Stage'), ('Computing Time', 'Computing'),
            ('Uncovered Communication Time', 'Communication(Not Overlapped)'),
            ('Free Time', 'Free')]
    rows, insights = [], []
    for label, key in dims:
        b, c = base['trace'][key] / 1000, comp['trace'][key] / 1000
        ratio = (c - b) / b if b else None
        rows.append({'Dimension': label, f'Baseline(us)': round(b * 1000, 1),
                     f'Compare(us)': round(c * 1000, 1), 'Diff(ms)': round(c - b, 3),
                     'Diff Ratio': round(ratio, 4) if ratio is not None else 'N/A'})
        if ratio is not None and abs(ratio) > 0.01:
            insights.append({'dimension': label, 'diff_ms': round(c - b, 3),
                             'diff_ratio': round(ratio, 4),
                             'direction': 'degraded' if ratio > 0 else 'improved'})
    insights.sort(key=lambda x: -abs(x['diff_ms']))
    return rows, insights


def compare_op_statistic(base, comp):
    def agg(card):
        out = {}
        for r in read_csv_rows(os.path.join(card['apo'], 'op_statistic.csv')):
            out[r.get('OP Type', '')] = {'total': to_f(r.get('Total Time(us)')),
                                         'count': int(to_f(r.get('Count'))),
                                         'avg': to_f(r.get('Avg Time(us)')),
                                         'max': to_f(r.get('Max Time(us)'))}
        return out
    a, b = agg(base), agg(comp)
    rows = []
    for op in set(a) | set(b):
        va, vb = a.get(op), b.get(op)
        t_base = va['total'] if va else 0
        t_comp = vb['total'] if vb else 0
        diff = t_comp - t_base
        ratio = diff / t_base if t_base else None
        rows.append({'Op Type': op,
                     'Baseline Total Time(ms)': round(t_base / 1000, 3),
                     'Compare Total Time(ms)': round(t_comp / 1000, 3),
                     'Diff Duration(ms)': round(diff / 1000, 3),
                     'Diff Ratio': round(ratio, 4) if ratio is not None else 'NEW',
                     'Baseline Calls': va['count'] if va else 0,
                     'Compare Calls': vb['count'] if vb else 0,
                     'is_comm': bool(COMM_NAME_PATTERN.search(op))})
    rows.sort(key=lambda r: r['Diff Duration(ms)'], reverse=True)
    return rows


def compare_kernel(base, comp):
    def agg(card):
        out = {}
        with open(os.path.join(card['apo'], 'kernel_details.csv'), encoding='utf-8',
                  errors='replace') as f:
            for r in csv.DictReader(f):
                name = r.get('Name', '')
                d = to_f(r.get('Duration(us)'))
                e = out.setdefault(name, {'total': 0.0, 'count': 0, 'max': 0.0,
                                          'min': float('inf'), 'core': r.get('Accelerator Core', '')})
                e['total'] += d
                e['count'] += 1
                e['max'] = max(e['max'], d)
                e['min'] = min(e['min'], d)
        for e in out.values():
            if e['min'] == float('inf'):
                e['min'] = 0
        return out
    a, b = agg(base), agg(comp)
    rows = []
    for name in set(a) | set(b):
        va, vb = a.get(name), b.get(name)
        t_base = va['total'] if va else 0
        t_comp = vb['total'] if vb else 0
        diff = t_comp - t_base
        ratio = diff / t_base if t_base else None
        rows.append({'Kernel Name': name, 'Core Type': (va or vb)['core'],
                     'Baseline Total(us)': round(t_base, 1), 'Compare Total(us)': round(t_comp, 1),
                     'Diff Total(us)': round(diff, 1),
                     'Diff Total Ratio': round(ratio, 4) if ratio is not None else 'NEW',
                     'Baseline Calls': va['count'] if va else 0,
                     'Compare Calls': vb['count'] if vb else 0,
                     'Baseline Avg(us)': round(va['total'] / va['count'], 1) if va else 0,
                     'Compare Avg(us)': round(vb['total'] / vb['count'], 1) if vb else 0,
                     'Baseline Max(us)': round(va['max'], 1) if va else 0,
                     'Compare Max(us)': round(vb['max'], 1) if vb else 0,
                     'is_comm': bool(COMM_NAME_PATTERN.search(name))})
    rows.sort(key=lambda r: r['Diff Total(us)'], reverse=True)
    return rows


def compare_api(base, comp):
    def agg(card):
        out = {}
        for r in read_csv_rows(os.path.join(card['apo'], 'api_statistic.csv')):
            out[r.get('API Name', '')] = {'total': to_f(r.get('Time(us)')),
                                          'count': int(to_f(r.get('Count')))}
        return out
    a, b = agg(base), agg(comp)
    rows = []
    for api in set(a) | set(b):
        va, vb = a.get(api), b.get(api)
        t_base = va['total'] if va else 0
        t_comp = vb['total'] if vb else 0
        rows.append({'API Name': api,
                     'Baseline Total(ms)': round(t_base / 1000, 3),
                     'Compare Total(ms)': round(t_comp / 1000, 3),
                     'Diff(ms)': round((t_comp - t_base) / 1000, 3),
                     'Baseline Calls': va['count'] if va else 0,
                     'Compare Calls': vb['count'] if vb else 0})
    rows.sort(key=lambda r: r['Diff(ms)'], reverse=True)
    return rows


def compare_communication(base, comp):
    def agg(card):
        cj = read_json(os.path.join(card['apo'], 'communication.json'))
        out = {}
        for step, groups in cj.items():
            for gtype in ('collective', 'p2p'):
                for op_key, info in (groups.get(gtype) or {}).items():
                    name = op_key.split('@')[0]
                    if name in AGG_ROWS:
                        continue
                    ti = info.get('Communication Time Info', {})
                    e = out.setdefault(name, {'total': 0.0, 'wait': 0.0, 'transit': 0.0, 'count': 0})
                    e['total'] += to_f(ti.get('Elapse Time(ms)'))
                    e['wait'] += to_f(ti.get('Wait Time(ms)'))
                    e['transit'] += to_f(ti.get('Transit Time(ms)'))
                    e['count'] += 1
        return out
    a, b = agg(base), agg(comp)
    rows = []
    for op in set(a) | set(b):
        va, vb = a.get(op), b.get(op)
        t_base = va['total'] if va else 0
        t_comp = vb['total'] if vb else 0
        rows.append({'Comm Op': op,
                     'Baseline Total(ms)': round(t_base, 3), 'Compare Total(ms)': round(t_comp, 3),
                     'Diff(ms)': round(t_comp - t_base, 3),
                     'Diff Ratio': round((t_comp - t_base) / t_base, 4) if t_base else 'NEW',
                     'Baseline Wait(ms)': round(va['wait'], 3) if va else 0,
                     'Compare Wait(ms)': round(vb['wait'], 3) if vb else 0,
                     'Baseline Transit(ms)': round(va['transit'], 3) if va else 0,
                     'Compare Transit(ms)': round(vb['transit'], 3) if vb else 0})
    rows.sort(key=lambda r: r['Diff(ms)'], reverse=True)
    return rows


def build_insights(overall_rows, op_rows, kernel_rows, api_rows, comm_rows):
    insights = {'overall': [], 'operator': {}, 'kernel': {}, 'api': {}, 'communication': {}}

    degraded = [r for r in overall_rows if isinstance(r['Diff Ratio'], float) and r['Diff Ratio'] > 0]
    improved = [r for r in overall_rows if isinstance(r['Diff Ratio'], float) and r['Diff Ratio'] < 0]
    def dim_insight(row):
        return {'dimension': row['Dimension'], 'diff_ms': row['Diff(ms)'],
                'diff_ratio': row['Diff Ratio']}

    insights['overall'] = {
        'degraded_max': dim_insight(degraded[0]) if degraded else None,
        'improved_max': dim_insight(improved[0]) if improved else None,
        'rows': overall_rows,
    }

    comp_ops = [r for r in op_rows if not r['is_comm']]
    top_degraded = [r for r in comp_ops if r['Diff Duration(ms)'] > 0][:10]
    total_degraded = sum(r['Diff Duration(ms)'] for r in comp_ops
                         if r['Diff Duration(ms)'] > 0)
    top10_share = (sum(r['Diff Duration(ms)'] for r in top_degraded) / total_degraded
                   if total_degraded > 0 else None)
    insights['operator'] = {
        'top_degraded': top_degraded,
        'top_improved': [r for r in comp_ops if r['Diff Duration(ms)'] < 0][:5],
        'degradation_concentration_top10': round(top10_share, 3) if top10_share is not None else None,
        'note': '劣化是否集中于少数算子' if top10_share is not None else '',
    }

    comp_kernels = [r for r in kernel_rows if not r['is_comm']]
    sig_degraded = [r for r in comp_kernels
                    if isinstance(r['Diff Total Ratio'], float) and r['Diff Total Ratio'] > 0.05
                    and r['Diff Total(us)'] > 0]
    comm_kernels = [r for r in kernel_rows if r['is_comm']]
    lb_rows = [r for r in comp_kernels
               if min(r['Baseline Calls'], r['Compare Calls']) > 10
               and r['Baseline Calls'] and r['Compare Calls']]
    load_imbalance = [r for r in lb_rows
                      if max(r['Baseline Calls'], r['Compare Calls']) /
                      min(r['Baseline Calls'], r['Compare Calls']) > 2]
    insights['kernel'] = {
        'significant_degraded_count': len(sig_degraded),
        'significant_degraded_top10': sig_degraded[:10],
        'comm_kernel_top10': comm_kernels[:10],
        'load_imbalance_kernels': load_imbalance[:10],
    }

    insights['api'] = {
        'top_degraded': [r for r in api_rows if r['Diff(ms)'] > 0][:10],
        'top_improved': [r for r in api_rows if r['Diff(ms)'] < 0][:5],
    }

    insights['communication'] = {
        'top_degraded': [r for r in comm_rows if r['Diff(ms)'] > 0][:10],
        'top_improved': [r for r in comm_rows if r['Diff(ms)'] < 0][:5],
        'wait_split_note': 'Wait 上升=到达时间差异；Transit 上升=数据量或带宽变化',
    }
    return insights


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--base-rank', type=int, default=None, help='override baseline rank')
    parser.add_argument('--compare-rank', type=int, default=None, help='override compare rank')
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    cards = find_cards(root)
    if len(cards) < 2:
        print('ERROR: need >=2 cards with step_trace_time.csv', file=sys.stderr)
        sys.exit(1)

    by_ratio = sorted(cards, key=lambda c: (c['comm_ratio'], c['trace']['Communication(Not Overlapped)']))
    min_card, max_card = by_ratio[0], by_ratio[-1]
    if args.base_rank is not None:
        cand = [c for c in cards if c['rank'] == args.base_rank]
        if not cand:
            print(f"ERROR: --base-rank {args.base_rank} not found; "
                  f"valid ranks: {[c['rank'] for c in cards]}", file=sys.stderr)
            sys.exit(1)
        min_card = cand[0]
    if args.compare_rank is not None:
        cand = [c for c in cards if c['rank'] == args.compare_rank]
        if not cand:
            print(f"ERROR: --compare-rank {args.compare_rank} not found; "
                  f"valid ranks: {[c['rank'] for c in cards]}", file=sys.stderr)
            sys.exit(1)
        max_card = cand[0]

    print(f"baseline(min comm): rank {min_card['rank']} comm_ratio={min_card['comm_ratio']:.4f}")
    print(f"compare(max comm):  rank {max_card['rank']} comm_ratio={max_card['comm_ratio']:.4f}")

    overall_rows, overall_insights = compare_overall(min_card, max_card)
    op_rows = compare_op_statistic(min_card, max_card)
    kernel_rows = compare_kernel(min_card, max_card)
    api_rows = compare_api(min_card, max_card)
    comm_rows = compare_communication(min_card, max_card)
    insights = build_insights(overall_rows, op_rows, kernel_rows, api_rows, comm_rows)

    os.makedirs(args.output, exist_ok=True)
    tag = f"{max_card['rank']}_{min_card['rank']}"

    # xlsx (msprof-compare style)
    try:
        import pandas as pd
        xlsx_path = os.path.join(args.output, f'performance_comparison_result_{tag}.xlsx')
        with pd.ExcelWriter(xlsx_path, engine='openpyxl') as w:
            pd.DataFrame(overall_rows).to_excel(w, sheet_name='OverallMetrics', index=False)
            pd.DataFrame(op_rows).to_excel(w, sheet_name='OperatorCompareStatistic', index=False)
            pd.DataFrame(kernel_rows).to_excel(w, sheet_name='KernelCompare', index=False)
            pd.DataFrame(api_rows).to_excel(w, sheet_name='ApiCompare', index=False)
            pd.DataFrame(comm_rows).to_excel(w, sheet_name='CommunicationCompare', index=False)
        print(f'xlsx: {xlsx_path}')
    except ImportError:
        xlsx_path = None
        print('openpyxl/pandas unavailable; skip xlsx')

    # CSV mirrors
    csv_dir = os.path.join(args.output, 'csv')
    os.makedirs(csv_dir, exist_ok=True)
    for name, rows in (('overall_metrics', overall_rows), ('operator_statistic', op_rows),
                       ('kernel_compare', kernel_rows), ('api_compare', api_rows),
                       ('communication_compare', comm_rows)):
        write_csv_file(os.path.join(csv_dir, f'{name}.csv'), rows)

    # middleware json (prof-compare style)
    middleware = {
        'base_card': {'rank': min_card['rank'], 'dir': min_card['dir'],
                      'comm_ratio': round(min_card['comm_ratio'], 4)},
        'compare_card': {'rank': max_card['rank'], 'dir': max_card['dir'],
                         'comm_ratio': round(max_card['comm_ratio'], 4)},
        'overall_metrics': overall_rows,
        'insights': insights,
    }
    json_path = os.path.join(args.output, 'compare_analysis_result.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(middleware, f, ensure_ascii=False, indent=2, default=str)

    print(f'middleware: {json_path}')
    if overall_insights:
        print('overall dimension diffs: ' + '; '.join(
            f"{i['dimension']} {i['direction']} {i['diff_ratio']*100:+.1f}%"
            for i in overall_insights[:4]))


def write_csv_file(path, rows):
    if not rows:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
        return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


if __name__ == '__main__':
    main()
