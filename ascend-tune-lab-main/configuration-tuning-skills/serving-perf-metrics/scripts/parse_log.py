#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serving-perf-metrics: 从服务化日志运行阶段提取性能指标（权重容量、显存、吞吐、命中率等）并生成 CSV。

输出格式（转置模式）：
  每一行是一个指标（按 1~11 编号），
  该指标在日志中出现的所有值依次写入各列。

支持两种日志格式：
  格式A - 独立行：每个指标在单独一行出现
  格式B - vLLM 聚合行：多个指标聚合在 APIServer 的一行日志中

用法:
    python3 parse_log.py <日志文件路径> [输出CSV路径]

如果未指定输出路径，默认输出到 <日志文件名>_metrics.csv
"""

from __future__ import annotations
import re
import sys
import csv
from pathlib import Path


# ── 正则定义 ──────────────────────────────────────────────

# 权重加载（独立行）
RE_WEIGHT = re.compile(
    r"Loading model weights took\s*([\d.]+)\s*(GB|GiB|MB|MiB)"
)

# Available KV cache（独立行）
RE_KV_MEM = re.compile(
    r"Available KV cache memory:\s*([\d.]+)\s*(GB|GiB|MB|MiB)"
)

# APIServer 聚合日志行（vLLM 格式 B），一行包含多个指标
# 格式示例:
# 1 Engines Aggregated: Avg prompt throughput: X, Avg generation throughput: Y, ... GPU KV cache usage: Z%, Prefix cache hit rate: W%
RE_AGGREGATED = re.compile(
    r"(\d+)\s+Engines?\s+Aggregated:"
    r"\s*Avg prompt throughput:\s*([\d.]+)\s*tokens/s,?"
    r"\s*Avg generation throughput:\s*([\d.]+)\s*tokens/s,?"
    r"\s*Running:\s*\d+\s+reqs,?"
    r"\s*Waiting:\s*\d+\s+reqs,?"
    r"\s*GPU KV cache usage:\s*([\d.]+)%,?"
    r"\s*Prefix cache hit rate:\s*([\d.]+)%"
)

# 独立行指标正则（格式 A 用，顺序无关）
STANDALONE_PATTERNS = [
    (RE_KV_MEM, "可分配token显存容量",
     lambda m: f"{m.group(1)} {m.group(2)}"),
    (re.compile(r"GPU KV cache usage:\s*([\d.]+)\s*%"),
     "kv cache显存利用率", lambda m: f"{m.group(1)}%"),
    (re.compile(r"Prefix cache hit rate:\s*([\d.]+)\s*%"),
     "prefix cache命中率", lambda m: f"{m.group(1)}%"),
    (re.compile(r"Avg Draft acceptance rate\s*:\s*([\d.]+)\s*%"),
     "MTP命中率", lambda m: f"{m.group(1)}%"),
    (re.compile(r"Mean acceptance length\s*:\s*([\d.]+)"),
     "平均接受长度", lambda m: m.group(1)),
    (re.compile(r"Avg generation throughput:\s*([\d.]+)\s*tokens/s"),
     "平均输出吞吐", lambda m: f"{m.group(1)} tokens/s"),
    (re.compile(r"Accepted throughput:\s*([\d.]+)\s*tokens/s"),
     "草稿模型生成吞吐", lambda m: f"{m.group(1)} tokens/s"),
    (re.compile(r"Drafted throughput:\s*([\d.]+)\s*tokens/s"),
     "草稿模型有效生成吞吐", lambda m: f"{m.group(1)} tokens/s"),
    (re.compile(r"Accepted\s*:\s*(\d+)\s*tokens"),
     "接受的tokens", lambda m: f"{m.group(1)} tokens"),
    (re.compile(r"Drafted\s*:\s*(\d+)\s*tokens"),
     "草稿模型生成tokens", lambda m: f"{m.group(1)} tokens"),
]

# 指标顺序（1~11，CSV 行按此输出）
METRIC_NAMES = [
    "权重占用容量",
    "可分配token显存容量",
    "kv cache显存利用率",
    "prefix cache命中率",
    "MTP命中率",
    "平均接受长度",
    "平均输出吞吐",
    "草稿模型生成吞吐",
    "草稿模型有效生成吞吐",
    "接受的tokens",
    "草稿模型生成tokens",
]


# ── 核心数据模型 ──────────────────────────────────────
# 所有解析结果统一收集到一个 dict[str, list[str]]
# key = 指标名, value = 按出现顺序排列的所有值


def parse_format_a(lines: list[str]) -> dict[str, list[str]]:
    """
    格式 A：每行匹配对应指标，收集所有出现的值。
    权重加载每次出现都收集（可能有多个不同的值），
    其余指标每次匹配到都追加。
    """
    result: dict[str, list[str]] = {name: [] for name in METRIC_NAMES}
    found = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 权重加载
        wm = RE_WEIGHT.search(stripped)
        if wm:
            result["权重占用容量"].append(f"{wm.group(1)} {wm.group(2)}")
            found = True
            continue

        # 其他独立行指标
        for pat, colname, fmt_fn in STANDALONE_PATTERNS:
            m = pat.search(stripped)
            if m:
                result[colname].append(fmt_fn(m))
                found = True
                break

    return result, found


def parse_format_b(lines: list[str]) -> dict[str, list[str]]:
    """
    格式 B：解析 vLLM APIServer 聚合行。

    权重加载收集所有 worker 的不重复值。
    Available KV cache memory 取第一个值。
    聚合行中提取 kv cache usage、prefix hit rate、generation throughput。
    """
    result: dict[str, list[str]] = {name: [] for name in METRIC_NAMES}
    found = False

    run_weights: set[str] = set()
    run_kv_mem: str | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 权重加载 → 收集所有不重复值
        wm = RE_WEIGHT.search(stripped)
        if wm:
            val = f"{wm.group(1)} {wm.group(2)}"
            if val not in run_weights:
                run_weights.add(val)
                result["权重占用容量"].append(val)
            found = True
            continue

        # Available KV cache
        km = RE_KV_MEM.search(stripped)
        if km:
            val = f"{km.group(1)} {km.group(2)}"
            if run_kv_mem is None:
                run_kv_mem = val
                result["可分配token显存容量"].append(val)
            continue

        # APIServer 聚合行
        am = RE_AGGREGATED.search(stripped)
        if am:
            result["平均输出吞吐"].append(f"{am.group(3)} tokens/s")
            result["kv cache显存利用率"].append(f"{am.group(4)}%")
            result["prefix cache命中率"].append(f"{am.group(5)}%")
            found = True
            continue

    return result, found


# ── 自动检测格式 ──────────────────────────────────────

def detect_format(lines: list[str]) -> str:
    weight_count = 0
    agg_count = 0
    for line in lines:
        if RE_WEIGHT.search(line):
            weight_count += 1
        if RE_AGGREGATED.search(line):
            agg_count += 1
    if agg_count > 0:
        return "b"
    if weight_count > 0:
        return "a"
    return "unknown"


# ── CSV 写出（转置模式） ──────────────────────────────

def write_transposed_csv(metric_values: dict[str, list[str]], output_path: str):
    """
    按转置格式输出：
      每行 = 一个指标（按 METRIC_NAMES 顺序 1~11），
      各列 = 该指标在日志中出现的所有值。
    """
    # 确定最大列数（所有指标中最多的值个数）
    max_vals = max((len(v) for v in metric_values.values()), default=0)

    # 表头
    header = ["序号", "指标名称"]
    for i in range(1, max_vals + 1):
        header.append(f"值{i}")

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for idx, name in enumerate(METRIC_NAMES, 1):
            vals = metric_values.get(name, [])
            row = [idx, name] + vals
            writer.writerow(row)

    print(f"✅ CSV 已生成: {output_path}")
    print(f"   共 {len(METRIC_NAMES)} 行指标，最多 {max_vals} 列值")


# ── 主入口 ────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法: python3 parse_log.py <日志文件路径> [输出CSV路径]", file=sys.stderr)
        print()
        print("示例:")
        print("  python3 parse_log.py serving.log")
        print("  python3 parse_log.py /path/to/serving.log output.csv")
        sys.exit(1)

    log_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) >= 3 else f"{Path(log_path).stem}_metrics.csv"

    path = Path(log_path)
    if not path.exists():
        print(f"❌ 错误: 文件不存在: {log_path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    fmt = detect_format(lines)
    print(f"📋 检测到日志格式: {'格式B (vLLM 聚合行)' if fmt == 'b' else '格式A (独立行)' if fmt == 'a' else '未知'}")

    if fmt == "b":
        metric_values, found = parse_format_b(lines)
    elif fmt == "a":
        metric_values, found = parse_format_a(lines)
    else:
        metric_values, found = {}, False

    if not found:
        print("⚠️  警告: 未在日志中找到任何可解析的指标。", file=sys.stderr)
        print("   请确认日志包含以下关键字之一:")
        print("   - Loading model weights took")
        print("   - Engines Aggregated: ... GPU KV cache usage: ...")

    write_transposed_csv(metric_values, output_path)


if __name__ == "__main__":
    main()
