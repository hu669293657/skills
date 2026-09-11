#!/usr/bin/env python3
"""Phase 0: PROF data detection & triage.

Detects single/multi-card layout, data format (db/text), framework,
existing analysis outputs, and produces a workflow plan.

Modes:
  single              单卡数据
  cluster             多卡数据（有可用卡目录）
  cluster_output_only 输入仅含 cluster_analysis_output（无任何卡目录），
                      只生成独立报告，不走完整 workflow
"""
import argparse
import json
import os
import re
import sqlite3
import sys

from lib.common import is_junk

CARD_DIR_PATTERN = re.compile(r'.*_ascend_(pt|ms)$')
PROF_DIR_PATTERN = re.compile(r'^PROF_\d{6}_\d{17}_\w+$')


def limited_walk(root, max_depth=4):
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not is_junk(d)]
        yield dirpath, dirnames, filenames


def find_card_dirs(root):
    """Find card directories: *_ascend_pt / *_ascend_ms, or dirs containing
    ASCEND_PROFILER_OUTPUT directly."""
    cards = []
    for dirpath, dirnames, filenames in limited_walk(root, max_depth=4):
        for d in dirnames:
            if CARD_DIR_PATTERN.match(d):
                cards.append(os.path.join(dirpath, d))
    if cards:
        return cards
    # fallback: dirs that directly contain ASCEND_PROFILER_OUTPUT
    for dirpath, dirnames, filenames in limited_walk(root, max_depth=3):
        if 'ASCEND_PROFILER_OUTPUT' in dirnames:
            cards.append(dirpath)
    return cards


def read_rank(card_dir):
    for fn in os.listdir(card_dir):
        if is_junk(fn):
            continue
        m = re.match(r'^profiler_info_(\d+)\.json$', fn)
        if m:
            try:
                with open(os.path.join(card_dir, fn), encoding='utf-8') as f:
                    data = json.load(f)
                return data.get('rank_id', int(m.group(1)))
            except Exception:
                return int(m.group(1))
    # fallback: rank from profiler db filename
    apo = os.path.join(card_dir, 'ASCEND_PROFILER_OUTPUT')
    if os.path.isdir(apo):
        for fn in os.listdir(apo):
            m = re.match(r'^ascend_pytorch_profiler_(\d+)\.db$', fn)
            if m:
                return int(m.group(1))
            m = re.match(r'^ascend_mindspore_profiler_(\d+)\.db$', fn)
            if m:
                return int(m.group(1))
    return None


def read_metadata(card_dir):
    for fn in ('profiler_metadata.json',):
        p = os.path.join(card_dir, fn)
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def detect_card_format(card_dir):
    """Return (format, framework, details) for one card."""
    apo = os.path.join(card_dir, 'ASCEND_PROFILER_OUTPUT')
    files = {}
    if os.path.isdir(apo):
        for fn in os.listdir(apo):
            if is_junk(fn) or fn.endswith(('-shm', '-wal')):
                continue
            files[fn] = os.path.getsize(os.path.join(apo, fn))
    framework = 'pytorch' if any('pytorch' in f for f in files) else (
        'mindspore' if any('mindspore' in f for f in files) else None)
    if framework is None:
        framework = 'pytorch' if card_dir.endswith('_ascend_pt') else (
            'mindspore' if card_dir.endswith('_ascend_ms') else 'unknown')
    has_db = any(re.match(r'^(ascend_pytorch_profiler|ascend_mindspore_profiler|msprof_)', f)
                 and files[f] > 0 for f in files)
    has_insight_db = 'mindstudio_insight_data.db' in files and files.get('mindstudio_insight_data.db', 0) > 0
    text_keys = ('kernel_details.csv', 'op_statistic.csv', 'operator_details.csv',
                 'api_statistic.csv', 'step_trace_time.csv', 'trace_view.json')
    has_text = any(k in files for k in text_keys)
    empty_profiler_db = any(re.match(r'^ascend_(pytorch|mindspore)_profiler_\d+\.db$', f)
                            and files[f] == 0 for f in files)
    if has_db:
        fmt = 'db'
    elif has_text:
        fmt = 'text'
    elif has_insight_db:
        fmt = 'insight_db'
    else:
        fmt = 'none'
    return fmt, framework, {'apo_files': {k: v for k, v in files.items()},
                            'empty_profiler_db': empty_profiler_db,
                            'has_insight_db': has_insight_db}


def check_cluster_output(root):
    """Inspect existing cluster_analysis_output under root."""
    out = {'exists': False, 'path': None, 'has_db': False, 'tables': [],
           'text_files': [], 'recipes_done': False, 'recipe_files': []}
    p = os.path.join(root, 'cluster_analysis_output')
    if not os.path.isdir(p):
        return out
    out['exists'] = True
    out['path'] = p
    db = os.path.join(p, 'cluster_analysis.db')
    base_tables = {'ClusterBaseInfo', 'ClusterStepTraceTime', 'HostInfo', 'RankDeviceMap',
                   'CommunicationGroupMapping', 'ClusterCommunicationTime',
                   'ClusterCommunicationBandwidth', 'ClusterCommunicationMatrix'}
    if os.path.isfile(db) and os.path.getsize(db) > 0:
        out['has_db'] = True
        try:
            conn = sqlite3.connect(db)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            out['tables'] = tables
            conn.close()
            extra = set(tables) - base_tables - {'sqlite_sequence', 'status_info',
                                                 'ExpertHotspotInfo', 'ExpertDeploymentInfo'}
            if extra:
                out['recipes_done'] = True
        except Exception:
            pass
    for fn in os.listdir(p):
        if is_junk(fn):
            continue
        if fn.endswith(('.csv', '.json')):
            out['text_files'].append(fn)
    rdir = os.path.join(p, 'recipes')
    if os.path.isdir(rdir):
        out['recipe_files'] = [f for f in os.listdir(rdir) if not is_junk(f)]
        if out['recipe_files']:
            out['recipes_done'] = True
    if any(f.startswith('slow_rank') for f in out['text_files'] + out['recipe_files']):
        out['recipes_done'] = True
    return out


def check_existing_outputs(root):
    res = {'advisor': [], 'compare': []}
    for dirpath, dirnames, filenames in limited_walk(root, max_depth=3):
        for fn in filenames:
            if is_junk(fn):
                continue
            if fn.startswith('mstt_advisor_'):
                res['advisor'].append(os.path.join(dirpath, fn))
            elif fn.startswith('performance_comparison_result_'):
                res['compare'].append(os.path.join(dirpath, fn))
    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', required=True, help='PROF input path (card dir or multi-card root)')
    parser.add_argument('--output', required=True, help='output json path')
    parser.add_argument('--skill-dir', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = parser.parse_args()

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f'ERROR: path not found: {root}', file=sys.stderr)
        sys.exit(1)

    card_dirs = find_card_dirs(root)
    cards = []
    for cd in card_dirs:
        rank = read_rank(cd)
        fmt, fw, detail = detect_card_format(cd)
        meta = read_metadata(cd)
        dist = meta.get('distributed_args', {}) if isinstance(meta, dict) else {}
        cards.append({
            'rank': rank if rank is not None else len(cards),
            'dir': cd,
            'format': fmt,
            'framework': fw,
            'usable': fmt != 'none',
            'world_size': dist.get('world_size'),
            'empty_profiler_db': detail['empty_profiler_db'],
            'has_insight_db': detail['has_insight_db'],
        })
    cards.sort(key=lambda c: c['rank'])

    frameworks = {c['framework'] for c in cards if c['framework']}
    mixed = len([f for f in frameworks if f in ('pytorch', 'mindspore')]) > 1
    usable_n = len([c for c in cards if c['usable']])
    mode = 'cluster' if usable_n > 1 else 'single'

    cluster = None
    cluster_root = None
    if mode == 'cluster' and cards:
        # multi-card root = common parent of card dirs
        parents = {os.path.dirname(c['dir']) for c in cards}
        cluster_root = parents.pop() if len(parents) == 1 else root
        cluster = check_cluster_output(cluster_root)
        cluster['expected_path'] = os.path.join(cluster_root, 'cluster_analysis_output')
    elif usable_n == 0:
        # 无可用卡目录：输入可能仅为 cluster_analysis_output 文件夹
        # （兼容 db 新格式/旧格式 cluster.db/TEXT 模式）
        cluster = check_cluster_output(root)
        cluster['expected_path'] = os.path.join(root, 'cluster_analysis_output')
        has_data = (cluster['has_db'] or cluster['text_files']
                    or os.path.isfile(os.path.join(root, 'cluster_analysis_output', 'cluster.db'))
                    or os.path.isfile(os.path.join(root, 'cluster.db')))
        if has_data:
            mode = 'cluster_output_only'
            cluster_root = root

    existing = check_existing_outputs(root)

    if mode == 'cluster_output_only':
        # 独立报告模式：只调用 cluster-output-analysis 出报告，不走完整 workflow
        steps = {
            'advisor': 'skip: no card dirs',
            'swimlane': 'skip: no card dirs',
            'cluster': 'skip: output exists',
            'recipes': 'skip: standalone report only',
            'compare': 'skip: no card dirs',
            'report_mode': 'standalone',
        }
    else:
        steps = {
            'advisor': 'run per card',
            'swimlane': 'run per card',
            'cluster': ('skip: output exists' if cluster and cluster['exists'] and (
                cluster['has_db'] or cluster['text_files']) else 'generate') if mode == 'cluster' else 'skip: single card',
            'recipes': ('check and run missing' if not (cluster and cluster['recipes_done'])
                        else 'skip: already done') if mode == 'cluster' else 'skip: single card',
            'compare': 'select max/min comm-ratio cards and compare' if mode == 'cluster' else 'skip: single card',
            'report_mode': mode,
        }

    result = {
        'input': root,
        'mode': mode,
        'mixed_framework': mixed,
        'cards': cards,
        'cluster_root': cluster_root,
        'cluster_output': cluster,
        'existing_outputs': existing,
        'steps': steps,
    }
    if mixed:
        result['error'] = 'PyTorch and MindSpore data mixed in one root; analyze separately.'

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"mode={mode} cards={len(cards)} formats={[c['format'] for c in cards]}")
    if mode == 'cluster_output_only':
        print('standalone: 输入仅含 cluster_analysis_output，将只生成独立报告')
    if cluster:
        print(f"cluster_output exists={cluster['exists']} recipes_done={cluster['recipes_done']}")
    for c in cards:
        print(f"  rank={c['rank']} fmt={c['format']} fw={c['framework']} usable={c['usable']}")
    print(f'written: {args.output}')


if __name__ == '__main__':
    main()
