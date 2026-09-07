# -*- coding: utf-8 -*-
"""CLI 子命令实现（argparse，仅标准库）。

命令（对齐 DESIGN.md 第 3 节）：
  collect      客户机采集（L1 默认 / L2 工具 / L3 显式 perf）
  analyze      collection(目录|tar.gz) -> analysis.json（含 diagnosis 全量数据）
  report       analysis.json -> 固定 18 章节 HTML
  run          analyze + report 一键（路径 B：分析既有采集包）
  run-collect  采集 + 分析 + 报告一键（路径 A：客户机本机）
  validate     仅数据质量检查，输出完整度分级
  version      打印四元版本号

退出码约定：0 成功；2 部分数据缺失仍出报告；1 致命错误。
"""

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile

from host_bound import VERSION, SCHEMA_VERSION, REPORT_VERSION, RULE_VERSION
from host_bound.analyzer import run_analysis
from host_bound.collector.main import CollectParams, CollectorOrchestrator
from host_bound.collector.sanitize import Sanitizer, current_identity
from host_bound.report import generate_html

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2


class _CliError(Exception):
    """预期内的用户输入错误（打印消息、退出码 1）。"""


# ---------------------------------------------------------------- 工具函数

def _write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
    return path


def _write_text(path, text):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _find_manifest_root(tmp):
    """tar 解包后定位分析根目录：优先含 manifest.json 的目录。"""
    if os.path.isfile(os.path.join(tmp, "manifest.json")):
        return tmp
    first_dir = None
    for name in sorted(os.listdir(tmp)):
        sub = os.path.join(tmp, name)
        if os.path.isdir(sub):
            if first_dir is None:
                first_dir = sub
            if os.path.isfile(os.path.join(sub, "manifest.json")):
                return sub
    return first_dir or tmp


def _resolve_collection(src):
    """collection 参数（目录或 tar.gz）-> (分析根目录, 待清理临时目录|None)。"""
    if os.path.isdir(src):
        return src, None
    if os.path.isfile(src):
        try:
            is_tar = src.endswith((".tar.gz", ".tgz")) or tarfile.is_tarfile(src)
        except OSError:
            is_tar = False
        if is_tar:
            tmp = tempfile.mkdtemp(prefix="hb_unpack_")
            try:
                with tarfile.open(src, "r:*") as tf:
                    try:
                        tf.extractall(tmp, filter="data")  # Python >= 3.12
                    except TypeError:
                        tf.extractall(tmp)
            except (tarfile.TarError, OSError) as e:
                shutil.rmtree(tmp, ignore_errors=True)
                raise _CliError("无法解包 %s: %r" % (src, e))
            return _find_manifest_root(tmp), tmp
    raise _CliError("collection 不存在或不是目录/tar.gz: %s" % src)


def _identity_from_manifest(root):
    """从 manifest.json 读取原始身份；缺失则回退本机身份。"""
    path = os.path.join(root, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        hostname = m.get("hostname")
        username = m.get("username")
        if hostname or username:
            return hostname, username
    except (OSError, ValueError):
        pass
    return current_identity()


def _sanitize_tree_for(root):
    """分析前对采集副本脱敏（hostname/用户名/路径/密钥/IP）。"""
    hostname, username = _identity_from_manifest(root)
    stats = Sanitizer(hostname, username).sanitize_tree(root)
    print("脱敏完成: %d 个文件被替换, 共 %d 处" % (stats["files"],
                                                stats["replacements"]))


def _print_diag_summary(diag, dq_grade):
    print("  数据质量: %s" % dq_grade)
    print("  Host Bound Score: %.1f (%s)" % (diag.host_bound_score,
                                             diag.score_label))
    print("  状态: %s  分类: %s  置信度: %.2f"
          % (diag.status, diag.classification, diag.confidence))
    print("  命中规则: %d 条" % len(diag.findings))
    for f in diag.findings[:5]:
        print("    - [%s] %s" % (f.severity, f.rule_id))


# ---------------------------------------------------------------- 子命令

def cmd_collect(args):
    params = CollectParams(duration=args.duration, interval=args.interval,
                           pid=args.pid, auto_detect=args.auto_detect,
                           level=args.level, output=args.output,
                           sanitize=args.sanitize, no_tar=args.no_tar)
    result = CollectorOrchestrator(params).run()
    if result["status"] == "error":
        sys.stderr.write("采集致命错误: %s\n" % result.get("error", ""))
        for note in result.get("notes", []):
            sys.stderr.write("  note: %s\n" % note)
        return EXIT_FATAL
    print("采集完成: status=%s" % result["status"])
    print("  采集目录: %s" % result["outdir"])
    if result.get("tar"):
        print("  采集包:   %s" % result["tar"])
    print("  case_id:  %s" % result.get("case_id", "-"))
    print("  数据质量: %s" % result.get("dq_grade", "-"))
    for note in result.get("notes", []):
        print("  note: %s" % note)
    return EXIT_OK if result["status"] == "ok" else EXIT_PARTIAL


def cmd_analyze(args):
    root, tmp = _resolve_collection(args.collection)
    try:
        if args.sanitize:
            _sanitize_tree_for(root)
        bundle = run_analysis(root)
        payload = bundle.to_dict()
        payload["_versions"] = {
            "tool_version": VERSION, "schema_version": SCHEMA_VERSION,
            "report_version": REPORT_VERSION, "rule_version": RULE_VERSION}
        out = os.path.abspath(args.output)
        _write_json(out, payload)
        diag = bundle.diag
        print("分析完成: %s" % out)
        _print_diag_summary(diag, bundle.dq.grade())
        return EXIT_OK if bundle.dq.grade() == "complete" else EXIT_PARTIAL
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_report(args):
    if not os.path.isfile(args.analysis):
        raise _CliError("analysis 文件不存在: %s" % args.analysis)
    try:
        with open(args.analysis, "r", encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise _CliError("analysis 文件不是合法 JSON: %r" % e)
    html = generate_html(data if isinstance(data, dict) else {})
    out = os.path.abspath(args.output)
    _write_text(out, html)
    print("报告已生成: %s" % out)
    return EXIT_OK


def cmd_run(args):
    root, tmp = _resolve_collection(args.collection)
    try:
        if args.sanitize:
            _sanitize_tree_for(root)
        bundle = run_analysis(root)
        payload = bundle.to_dict()
        payload["_versions"] = {
            "tool_version": VERSION, "schema_version": SCHEMA_VERSION,
            "report_version": REPORT_VERSION, "rule_version": RULE_VERSION}
        html = generate_html(payload)
        out = os.path.abspath(args.output)
        _write_text(out, html)
        print("报告已生成: %s" % out)
        _print_diag_summary(bundle.diag, bundle.dq.grade())
        if tmp:
            print("提示: 输入为 tar 包；如需保留 analysis.json 供离线重渲染，"
                  "请改用 analyze 命令")
        return EXIT_OK if bundle.dq.grade() == "complete" else EXIT_PARTIAL
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_run_collect(args):
    params = CollectParams(duration=args.duration, interval=args.interval,
                           pid=args.pid, auto_detect=args.auto_detect,
                           level=args.level, output=None,
                           sanitize=args.sanitize, no_tar=True)
    result = CollectorOrchestrator(params).run()
    if result["status"] == "error":
        sys.stderr.write("采集致命错误: %s\n" % result.get("error", ""))
        return EXIT_FATAL
    print("采集完成: %s (数据质量: %s)" % (result["outdir"],
                                          result["dq_grade"]))
    bundle = run_analysis(result["outdir"])
    payload = bundle.to_dict()
    payload["_versions"] = {
        "tool_version": VERSION, "schema_version": SCHEMA_VERSION,
        "report_version": REPORT_VERSION, "rule_version": RULE_VERSION}
    html = generate_html(payload)
    out = os.path.abspath(args.output)
    _write_text(out, html)
    print("报告已生成: %s" % out)
    _print_diag_summary(bundle.diag, bundle.dq.grade())
    return EXIT_OK if bundle.dq.grade() == "complete" else EXIT_PARTIAL


def cmd_validate(args):
    root, tmp = _resolve_collection(args.collection)
    try:
        bundle = run_analysis(root)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    dq = bundle.dq
    grade = dq.grade()
    print("数据质量分级: %s" % grade)
    for name in sorted(dq.sources):
        info = dq.sources[name]
        print("  %-12s %-8s %s" % (name, info.get("status", "-"),
                                   info.get("note", "") or ""))
    ps = bundle.parse_stats
    print("产物解析: 总数=%d 成功=%d 未知兜底=%d 失败=%d"
          % (ps.get("total", 0), ps.get("ok", 0), ps.get("unknown", 0),
             ps.get("failed", 0)))
    limits = dq.limitations()
    if limits:
        print("局限与警告:")
        for line in limits:
            print("  - %s" % line)
    return EXIT_OK if grade == "complete" else EXIT_PARTIAL


def cmd_version(_args):
    print("host-bound %s (schema %s / report %s / rules %s)"
          % (VERSION, SCHEMA_VERSION, REPORT_VERSION, RULE_VERSION))
    return EXIT_OK


# ---------------------------------------------------------------- 参数装配

def _add_collect_args(p):
    p.add_argument("--duration", type=float, default=60.0,
                   help="采样窗口时长（秒），默认 60")
    p.add_argument("--interval", type=float, default=1.0,
                   help="采样间隔（秒），默认 1")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pid", type=int, default=None,
                   help="目标进程 PID（与 --auto-detect 互斥）")
    g.add_argument("--auto-detect", action="store_true",
                   help="按 cmdline 关键词自动识别训练/推理进程")
    p.add_argument("--level", type=int, choices=(1, 2, 3), default=1,
                   help="采集级别：1 默认 / 2 工具 / 3 perf（显式授权）")
    p.add_argument("-o", "--output", default=None,
                   help="输出目录（默认当前目录）")
    p.add_argument("--sanitize", action="store_true",
                   help="打包/分析前对采集数据脱敏")
    return p


def build_parser():
    ap = argparse.ArgumentParser(
        prog="host-bound",
        description="CPU / 深度学习 Host Bound 性能诊断"
                    "（零依赖，离线可用，全部标准库实现）")
    sub = ap.add_subparsers(dest="command")

    c = sub.add_parser("collect", help="客户机采集（产出 collection/ 或 tar.gz）")
    _add_collect_args(c)
    c.add_argument("--no-tar", action="store_true", help="只留目录不打包")
    c.set_defaults(fn=cmd_collect)

    a = sub.add_parser("analyze",
                       help="解析 -> 特征 -> 诊断，输出 analysis.json")
    a.add_argument("collection", help="collection 目录或 tar.gz")
    a.add_argument("-o", "--output", default="analysis.json",
                   help="输出 JSON 路径，默认 analysis.json")
    a.add_argument("--sanitize", action="store_true",
                   help="分析前对采集副本脱敏")
    a.set_defaults(fn=cmd_analyze)

    r = sub.add_parser("report", help="analysis.json -> 固定 18 章节 HTML")
    r.add_argument("analysis", help="analyze 产出的 JSON 文件")
    r.add_argument("-o", "--output", default="report.html",
                   help="输出 HTML 路径，默认 report.html")
    r.set_defaults(fn=cmd_report)

    rn = sub.add_parser("run", help="analyze + report 一键（路径 B）")
    rn.add_argument("collection", help="collection 目录或 tar.gz")
    rn.add_argument("-o", "--output", default="report.html",
                    help="输出 HTML 路径，默认 report.html")
    rn.add_argument("--sanitize", action="store_true",
                    help="分析前对采集副本脱敏")
    rn.set_defaults(fn=cmd_run)

    rc = sub.add_parser("run-collect",
                        help="采集 + 分析 + 报告一键（路径 A，客户机）")
    _add_collect_args(rc)
    rc.set_defaults(fn=cmd_run_collect)

    v = sub.add_parser("validate", help="仅数据质量检查，输出完整度分级")
    v.add_argument("collection", help="collection 目录或 tar.gz")
    v.set_defaults(fn=cmd_validate)

    ver = sub.add_parser("version", help="打印四元版本号")
    ver.set_defaults(fn=cmd_version)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        return EXIT_FATAL
    try:
        return fn(args)
    except _CliError as e:
        sys.stderr.write("错误: %s\n" % e)
        return EXIT_FATAL
    except Exception as e:  # 兜底：致命错误统一退出码 1
        sys.stderr.write("致命错误: %r\n" % e)
        return EXIT_FATAL
