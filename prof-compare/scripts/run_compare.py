#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_compare.py - 两张 Prof 数据直接比对的编排脚本

功能：
1. 校验基准 / 待比对 Prof 路径
2. 自动检测 msprof-analyze 是否安装，未安装则自动从 PyPI 安装
3. 调用 msprof-analyze compare 生成 performance_comparison_result_*.xlsx
4. 自动定位输出 xlsx，调用 main_analyzer.analyze() 完成后续分析
   （中间件 JSON + HTML 报告 + 中文 xlsx）

本脚本不依赖任何本地 msprof-analyze 项目文件；msprof-analyze 官方发布于
PyPI（pip install msprof-analyze），常规在线环境无需任何额外准备。

用法：
  # 最简：默认开启全部比对能力
  python run_compare.py -d ./ascend_pt -bp ./gpu_trace.json

  # 指定输出目录
  python run_compare.py -d ./ascend_pt -bp ./base_ascend_pt -o ./out

  # 透传 compare 的额外参数
  python run_compare.py -d ./ascend_pt -bp ./gpu_trace.json \
      --compare_args "--enable_operator_compare --use_input_shape"

  # 只生成 compare xlsx，不做后续分析
  python run_compare.py -d ./ascend_pt -bp ./gpu_trace.json --compare_only
"""
import argparse
import glob
import gzip
import json
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

COMPARE_RESULT_PATTERN = "performance_comparison_result_*.xlsx"

# 透传给 compare 的合法参数前缀，用于校验 --compare_args 内容
KNOWN_COMPARE_FLAGS = {
    "--enable_profiling_compare", "--enable_operator_compare", "--enable_memory_compare",
    "--enable_communication_compare", "--enable_api_compare", "--enable_kernel_compare",
    "--disable_details", "--disable_module", "--max_kernel_num", "--op_name_map",
    "--use_input_shape", "--gpu_flow_cat", "--base_step", "--comparison_step",
    "--force", "--use_kernel_type", "--debug",
}


def log(msg: str) -> None:
    print(f"[run_compare] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"错误：{msg}")
    sys.exit(code)


def run_cmd(cmd, desc: str, timeout: int = None) -> int:
    """执行外部命令并实时转发输出，返回退出码。"""
    log(f"{desc}：{' '.join(str(c) for c in cmd)}")
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        log(f"命令启动失败：{e}")
        return -1
    try:
        for line in proc.stdout or []:
            print(line.rstrip(), flush=True)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log(f"命令超时（>{timeout}s），已终止")
        return -2
    return proc.returncode


# ---------------------------------------------------------------------------
# msprof-analyze 检测与安装
# ---------------------------------------------------------------------------

def detect_msprof() -> str:
    """检测 msprof-analyze 是否可用。

    返回：'cli'（msprof-analyze 命令可用）/ 'module'（包已安装但命令不在 PATH）/ ''（未安装）
    """
    if shutil.which("msprof-analyze"):
        return "cli"
    try:
        if importlib.util.find_spec("msprof_analyze") is not None:
            return "module"
    except (ImportError, ValueError):
        pass
    return ""


def pip_install(target: str) -> bool:
    log(f"正在安装 {target}（首次安装需下载依赖，可能需要几分钟）...")
    code = run_cmd([sys.executable, "-m", "pip", "install", target], "pip 安装")
    return code == 0


def ensure_msprof(allow_install: bool = True) -> str:
    """确保 msprof-analyze 可用，返回执行模式：'cli' / 'module'。

    安装策略（按序尝试，任一成功即止）：
    1. 已安装 → 直接使用
    2. 未安装 → 在线 PyPI 安装：pip install msprof-analyze（官方发布渠道）
    """
    mode = detect_msprof()
    if mode:
        log(f"检测到 msprof-analyze 已安装（模式：{mode}），跳过安装")
        return mode
    if not allow_install:
        die("msprof-analyze 未安装，且已通过 --skip_install 跳过自动安装。"
            "请先执行 pip install msprof-analyze 后重试。")

    if pip_install("msprof-analyze"):
        return detect_msprof() or "cli"
    die("msprof-analyze 自动安装失败。请检查网络后手动执行 pip install msprof-analyze，"
        "安装完成后重试。")


# ---------------------------------------------------------------------------
# 执行 compare
# ---------------------------------------------------------------------------

def validate_extra_args(extra: str) -> list:
    """解析并校验透传给 compare 的额外参数。"""
    if not extra or not extra.strip():
        return []
    try:
        tokens = shlex.split(extra)
    except ValueError as e:
        die(f"--compare_args 解析失败：{e}")
    unknown = [t for t in tokens if t.startswith("-") and t not in KNOWN_COMPARE_FLAGS]
    if unknown:
        log(f"警告：以下参数不在 compare 已知参数列表中，仍将透传：{' '.join(unknown)}")
    return tokens


def build_compare_cmd(mode: str, base: str, comp: str,
                      output: str, extra: list) -> list:
    """构造 compare 命令：msprof-analyze compare -d <待比对> -bp <基准> -o <输出>。"""
    if mode == "cli":
        head = [shutil.which("msprof-analyze")]
    else:
        head = [sys.executable, "-c",
                "from msprof_analyze.cli.entrance import msprof_analyze_cli; msprof_analyze_cli()"]
    return [*head, "compare", "-d", comp, "-bp", base, "-o", output, *extra]


def find_result_xlsx(output_dir: str) -> str:
    """在输出目录中定位最新生成的比对结果 xlsx（含一层子目录）。"""
    candidates = glob.glob(os.path.join(output_dir, COMPARE_RESULT_PATTERN))
    for sub in glob.glob(os.path.join(output_dir, "*")):
        if os.path.isdir(sub):
            candidates.extend(glob.glob(os.path.join(sub, COMPARE_RESULT_PATTERN)))
    if not candidates:
        return ""
    return max(candidates, key=os.path.getmtime)


def run_compare(mode: str, base: str, comp: str,
                output: str, extra: list) -> str:
    """执行 compare 并返回结果 xlsx 路径。"""
    os.makedirs(output, exist_ok=True)
    cmd = build_compare_cmd(mode, base, comp, output, extra)
    log("开始执行性能比对（算子/内存比对较耗时，请耐心等待）...")
    code = run_cmd(cmd, "执行 msprof-analyze compare")
    if code != 0:
        die(f"compare 执行失败（退出码 {code}）。"
            f"请检查 Prof 数据格式（NPU 目录需含 ASCEND_PROFILER_OUTPUT 或 PROF_*；"
            f"GPU 需为 torch profiler 导出的 *.pt.trace.json）。")
    xlsx = find_result_xlsx(output)
    if not xlsx:
        die(f"compare 退出码为 0，但在输出目录中未找到 {COMPARE_RESULT_PATTERN}：{output}")
    log(f"compare 完成，结果文件：{xlsx}")
    return xlsx


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def msprof_input(path, stack):
    path = Path(path)
    if path.name == "ASCEND_PROFILER_OUTPUT" and path.is_dir():
        return str(path.parent)
    if path.is_file() and path.name.endswith(".json.gz"):
        folder = stack.enter_context(TemporaryDirectory(prefix="prof-compare-"))
        plain = Path(folder) / path.stem
        with gzip.open(path, "rb") as source, plain.open("wb") as target:
            shutil.copyfileobj(source, target)
        return str(plain)
    return str(path)


def input_pair(comp, base):
    from cross_trace import ascend_dir, gpu_file
    def is_npu(path):
        folder = ascend_dir(path)
        return all((folder / file).is_file() for file in ("kernel_details.csv", "operator_details.csv"))
    current, baseline = is_npu(comp), is_npu(base)
    if current and baseline:
        return "npu_npu", comp, base
    if current or baseline:
        npu, other = (comp, base) if current else (base, comp)
        trace = gpu_file(other)
        if trace:
            return "npu_gpu", npu, str(trace)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="两张 Prof 数据直接比对：自动安装 msprof-analyze → 执行 compare → 生成分析报告")
    parser.add_argument("-d", "--profiling_path", required=True,
                        help="待比对 Prof 路径（通常是 NPU 数据 / 优化后数据 / 新版本数据）")
    parser.add_argument("-bp", "--benchmark_profiling_path", required=True,
                        help="基准 Prof 路径（通常是 GPU 数据 / 优化前数据 / 旧版本数据）")
    parser.add_argument("-o", "--output", default=None,
                        help="输出目录（默认 ./compare_output_<时间戳>）")
    parser.add_argument("--compare_args", default="",
                        help='透传给 compare 的额外参数，如 "--enable_operator_compare --use_input_shape"')
    parser.add_argument("--compare_only", action="store_true",
                        help="只执行 compare 生成 xlsx，不进行后续分析")
    parser.add_argument("--skip_install", action="store_true",
                        help="跳过 msprof-analyze 自动安装检测")
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--host-kernel", action="store_true",
                       help="要求在综合报告中加入原始 host 与 device Top 15（NPU/GPU 或 NPU/NPU）")
    route.add_argument("--msprof", action="store_true", help="显式选择综合报告流程（默认）")
    parser.add_argument("--npu-top-csv", help="核对按 opType、accCore 分组的 NPU 排名 CSV")
    parser.add_argument("--host-aliases", help="host 原名映射 JSON（排名侧到对照侧）")
    args = parser.parse_args()

    base = os.path.abspath(args.benchmark_profiling_path)
    comp = os.path.abspath(args.profiling_path)
    for label, path in (("基准 Prof (-bp)", base), ("待比对 Prof (-d)", comp)):
        if not os.path.exists(path):
            die(f"{label} 路径不存在：{path}")
        log(f"{label}：{path}")

    output = os.path.abspath(args.output) if args.output else os.path.abspath(
        f"compare_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    log(f"输出目录：{output}")

    extra = validate_extra_args(args.compare_args)

    required = args.host_kernel or args.npu_top_csv or args.host_aliases
    if required and args.compare_only:
        parser.error("--compare_only 只生成原始 xlsx；生成含 host/kernel 的报告时请去掉该开关")
    detail = None
    try:
        pair = input_pair(comp, base) if not args.compare_only else None
        if pair:
            from host_kernel import compare_gpu, compare_npus
            kind, first, second = pair
            if kind == "npu_gpu":
                # The raw section always ranks the NPU; keep the caller's direction for msprof.
                if first == comp:
                    base = second
                else:
                    comp = second
                detail = compare_gpu(first, second, args.npu_top_csv, args.host_aliases)
            else:
                detail = compare_npus(first, second, args.npu_top_csv, args.host_aliases)
            if any(t.split("=")[0] in ("--base_step", "--comparison_step") for t in extra):
                detail["msprof_step_filter"] = True
        elif required:
            die("需要两份单卡 NPU 文本导出，或一份 NPU 文本导出和 GPU trace")
    except (OSError, ValueError) as exc:
        if required:
            die(str(exc))
        log(f"原始 host/kernel 明细无法生成：{exc}。继续生成原有综合报告。")

    mode = ensure_msprof(allow_install=not args.skip_install)
    with ExitStack() as stack:
        xlsx = run_compare(mode, msprof_input(base, stack), msprof_input(comp, stack), output, extra)
    if args.compare_only:
        log(f"--compare_only 已设置，跳过分析。结果文件：{xlsx}")
        return

    log("开始解析 xlsx 并生成分析报告...")
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    from main_analyzer import analyze
    analyze(xlsx, output, detail)
    if detail:
        # Older bookmarks and consumers used these names for the raw comparison.
        report = Path(output) / "compare_analysis_report.html"
        shutil.copyfile(report, report.with_name("host_kernel_compare.html"))
        report.with_name("host_kernel_compare.json").write_text(
            json.dumps(detail, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    log("全流程完成！")


if __name__ == "__main__":
    main()
