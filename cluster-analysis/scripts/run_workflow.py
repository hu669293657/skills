#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cluster-analysis 端到端 workflow 编排器。

用法：
  python run_workflow.py --input <prof路径>            # 单卡目录或多卡根目录
  python run_workflow.py --input <prof根目录> --force  # 忽略已有输出件全部重跑
  python run_workflow.py --input <...> --msprof-cli    # 优先尝试原生 msprof-analyze CLI

阶段（依赖顺序执行；每阶段幂等，已有输出件自动跳过）：
  1 detect    探测输入：单卡/多卡、数据格式、已有输出件 -> detect_result.json
  2 advisor   专家建议 all 场景（单卡=单卡 advisor；多卡=对全部卡一次 advisor）
              -> advisor/advisor.md + advisor.json
  3 cluster   多卡且缺 cluster_analysis_output 时生成（位于 prof 根目录，
              与各卡目录同级）-> cluster_analysis_output/
  4 recipes   多卡且未做过进阶分析时全量执行 23 个 recipe（五大类）
              -> cluster_analysis_output/recipes/ + recipes_summary.{md,json}
  5 compare   多卡：自动选通信占比最大/最小两卡做性能比对
              -> compare/compare_analysis_result.json + performance_comparison_result_*.xlsx
  6 vendor    桥接 vendor 子技能生成子报告（供总报告 iframe 内嵌）
              -> reports/cluster_analysis_report.html（多卡）
                 reports/compare_analysis_report.html（有 xlsx 时）
  7 swimlane  每张卡泳道 TopN 最长算子 + 全算子 7 列统计；多卡生成横向比对
              -> swimlane/swimlane_rank{N}.{md,json} + swimlane_compare.md
  8 report    生成总 HTML 报告（信息概览优先、左侧固定导航、章节可折叠、
              vendor 子报告嵌入、无卡片清单）
              -> performance_report.html（与全部中间件同目录）
"""
import argparse
import os
import shutil
import subprocess
import sys

from lib.common import read_json
from lib.config import MW_NAME

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL_DIR, 'scripts')


def sh(cmd, cwd=None):
    print('[run] ' + ' '.join(os.path.relpath(c, SKILL_DIR)
                              if os.path.isabs(c) and c.startswith(SKILL_DIR) else c
                              for c in cmd))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='ignore')
    out = ((p.stdout or '') + (p.stderr or '')).strip()
    if out:
        for line in out.splitlines()[-25:]:
            print('  | ' + line)
    return p.returncode, out


def script(name):
    return os.path.join(SCRIPTS, name)


def detect_msprof_cli():
    exe = shutil.which('msprof-analyze')
    return exe


def main():
    ap = argparse.ArgumentParser(
        description='cluster-analysis 端到端性能分析 workflow',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('--input', required=True, help='prof 路径：单卡目录或多卡根目录')
    ap.add_argument('--output-dir', default=None,
                    help=f'中间件与报告目录（默认 <prof根>/{MW_NAME}）')
    ap.add_argument('--force', action='store_true', help='忽略已有输出件，全部重跑')
    ap.add_argument('--top-num', type=int, default=20, help='cluster/泳道 TopN（默认 20）')
    ap.add_argument('--base-rank', type=int, default=None, help='比对基准卡（默认自动选通信占比最小）')
    ap.add_argument('--compare-rank', type=int, default=None, help='比对目标卡（默认自动选通信占比最大）')
    ap.add_argument('--msprof-cli', action='store_true',
                    help='若安装了 msprof-analyze 则 advisor/cluster 阶段优先用原生 CLI（失败自动回退内置实现）')
    ap.add_argument('--skip-recipes', action='store_true')
    ap.add_argument('--skip-compare', action='store_true')
    ap.add_argument('--skip-swimlane', action='store_true')
    ap.add_argument('--skip-report', action='store_true')
    ap.add_argument('--report-mode', choices=['single', 'cluster', 'compare'], default=None,
                    help='覆盖报告模式（默认按探测结果）')
    args = ap.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        print(f'ERROR: 输入路径不存在: {root}')
        return 1
    mw = os.path.abspath(args.output_dir) if args.output_dir else os.path.join(root, MW_NAME)
    for sub in ('advisor', 'swimlane', 'compare', 'reports'):
        os.makedirs(os.path.join(mw, sub), exist_ok=True)
    cli = detect_msprof_cli() if args.msprof_cli else None
    if args.msprof_cli and not cli:
        print('[cli] msprof-analyze 未安装，使用内置实现')

    # ---------- 1 detect ----------
    detect_json = os.path.join(mw, 'detect_result.json')
    if args.force or not os.path.isfile(detect_json):
        rc, _ = sh([sys.executable, script('detect_prof.py'),
                    '--path', root, '--output', detect_json])
        if rc != 0:
            print('ERROR: 探测失败')
            return 1
    det = read_json(detect_json)
    if det is None and not args.force:
        # 已有 detect_result.json 缺失或损坏（JSON 解析失败）时重跑一次探测自愈
        print('[detect] detect_result.json 缺失或损坏，重跑探测')
        rc, _ = sh([sys.executable, script('detect_prof.py'),
                    '--path', root, '--output', detect_json])
        if rc != 0:
            print('ERROR: 探测失败')
            return 1
        det = read_json(detect_json)
    if not isinstance(det, dict):
        print('ERROR: detect_result.json 无法读取，无法继续（可尝试 --force 重跑）')
        return 1
    mode = det.get('mode', 'single')
    cards = [c for c in det.get('cards', []) if c.get('usable')]
    if det.get('mixed_framework'):
        print('ERROR: ' + det['error'])
        return 1
    print(f'[detect] mode={mode} cards={len(cards)}')

    # ---------- 2 advisor（单卡=单卡 advisor；多卡=对全部卡一次 advisor） ----------
    adv_md = os.path.join(mw, 'advisor', 'advisor.md')
    if args.force or not os.path.isfile(adv_md):
        target = root if mode == 'cluster' else (cards[0]['dir'] if cards else root)
        if cli:  # 原生 CLI 产出 mstt_advisor_* 文件，仅供留档
            sh([cli, 'advisor', 'all', target, '-o', os.path.join(mw, 'advisor')])
        # 始终用内置实现归一化出 advisor.md + advisor.json（总报告契约）
        rc, _ = sh([sys.executable, script('advisor_fallback.py'),
                    '--root', target, '--output', os.path.join(mw, 'advisor')])
        if rc != 0:
            print('WARN: advisor 阶段失败，报告将以缺省内容继续')
    else:
        print('[skip] advisor 已存在')

    # ---------- 3 cluster（多卡且缺失时生成） ----------
    cl = det.get('cluster_output') or {}
    cl_exists = mode == 'cluster' and cl.get('exists') and (cl.get('has_db') or cl.get('text_files'))
    if mode == 'cluster' and (args.force or not cl_exists):
        done = False
        if cli:
            rc, _ = sh([cli, 'cluster', 'analysis', '-d', root, '--top_num', str(args.top_num)],
                       cwd=root)
            done = rc == 0 and os.path.isdir(os.path.join(root, 'cluster_analysis_output'))
        if not done:
            rc, _ = sh([sys.executable, script('cluster_fallback.py'), '--root', root]
                       + (['--force'] if args.force else []))
            if rc != 0:
                print('WARN: cluster 阶段失败，报告将以缺省内容继续')
    else:
        print('[skip] cluster_analysis_output 已存在' if mode == 'cluster'
              else '[skip] 单卡无需 cluster 阶段')

    # ---------- 4 recipes（多卡且未做过进阶分析时全量执行） ----------
    cl_root = det.get('cluster_root') or root
    cl_path = (cl.get('path')
               or (cl.get('expected_path')
                   if mode == 'cluster' else None)
               or os.path.join(cl_root, 'cluster_analysis_output'))
    recipes_done = bool(cl.get('recipes_done'))
    if mode == 'cluster' and not args.skip_recipes and (args.force or not recipes_done):
        done = False
        if cli:
            for recipe in ('cluster_time_summary', 'communication_time_sum', 'slow_rank',
                           'slow_link', 'communication_bottleneck', 'free_analysis',
                           'compute_op_sum', 'hccl_sum'):
                sh([cli, 'cluster', 'analysis', '-d', root, recipe, '--top_num', str(args.top_num)])
            rdir = os.path.join(cl_path, 'recipes')
            done = os.path.isdir(rdir) and bool(os.listdir(rdir))
        if not done:
            rc, _ = sh([sys.executable, script('recipe_fallback.py'), '--root', cl_root,
                        '--top-num', str(args.top_num)])
            if rc != 0:
                print('WARN: recipes 阶段部分失败，报告将呈现已完成部分')
    else:
        print('[skip] recipes（已完成或单卡）')

    # ---------- 5 compare（多卡：通信占比最大/最小两卡） ----------
    cmp_json = os.path.join(mw, 'compare', 'compare_analysis_result.json')
    cmp_ok = False
    if mode == 'cluster' and not args.skip_compare:
        if args.force or not os.path.isfile(cmp_json):
            rc, _ = sh([sys.executable, script('compare_fallback.py'), '--root', root,
                        '--output', os.path.join(mw, 'compare')]
                       + (['--base-rank', str(args.base_rank)] if args.base_rank is not None else [])
                       + (['--compare-rank', str(args.compare_rank)]
                          if args.compare_rank is not None else []))
            cmp_ok = rc == 0 and os.path.isfile(cmp_json)
        else:
            cmp_ok = True
            print('[skip] compare 已存在')
    else:
        print('[skip] compare（单卡或已禁用）')

    # ---------- 6 vendor 子报告 ----------
    reports_dir = os.path.join(mw, 'reports')
    if mode == 'cluster' and os.path.isdir(cl_path):
        rc, _ = sh([sys.executable, script('vendor_bridge.py'), 'cluster',
                    '--data-dir', cl_path, '--reports-dir', reports_dir])
        if rc not in (0, 2):
            print('WARN: vendor cluster 子报告失败')
    if cmp_ok:
        rc, _ = sh([sys.executable, script('vendor_bridge.py'), 'compare',
                    '--compare-dir', os.path.join(mw, 'compare'),
                    '--reports-dir', reports_dir])
        if rc not in (0, 2):
            print('WARN: vendor compare 子报告失败')

    # ---------- 7 swimlane（每卡 TopN + 全算子统计；多卡横向比对） ----------
    if not args.skip_swimlane and cards:
        sw_dir = os.path.join(mw, 'swimlane')
        for c in cards:
            md = os.path.join(sw_dir, f"swimlane_rank{c['rank']}.md")
            if args.force or not os.path.isfile(md):
                rc, _ = sh([sys.executable, script('swimlane_analyzer.py'),
                            '--card', c['dir'], '--rank', str(c['rank']),
                            '--output', sw_dir])
                if rc != 0:
                    print(f"WARN: rank {c['rank']} 泳道分析失败")
            else:
                print(f"[skip] rank {c['rank']} 泳道已存在")
        if mode == 'cluster' and len(cards) > 1:
            sh([sys.executable, script('swimlane_analyzer.py'),
                '--compare-dir', sw_dir])

    # ---------- 8 report（概览优先 / 左侧导航 / 折叠 / 子报告嵌入） ----------
    if not args.skip_report:
        rmode = args.report_mode or mode
        out_html = os.path.join(mw, 'performance_report.html')
        rc, _ = sh([sys.executable, script('report_generator.py'),
                    '--middleware', mw, '--mode', rmode, '--output', out_html])
        if rc != 0:
            print('ERROR: 总报告生成失败')
            return 1
        print(f'\n完成：{out_html}')
        sub1 = os.path.join(reports_dir, 'cluster_analysis_report.html')
        sub2 = os.path.join(reports_dir, 'compare_analysis_report.html')
        for p in (sub1, sub2):
            if os.path.isfile(p):
                print(f'子报告：{p}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
