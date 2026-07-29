#!/usr/bin/env python3
"""Lookup operator latency from msmodeling profiling database.

Requires local clone of https://gitcode.com/Ascend/msmodeling
(path: tensor_cast/performance_model/profiling_database/data).

Lookup order:
1. op_mapping.yaml → kernel_type
2. matching CSV under vllm_ascend/ or hccl/ (nearest shape / message size)
3. if CSV is Git-LFS pointer: analytic estimate keyed by kernel_type from mapping
4. builtin table only when --allow-without-repos
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


def _refs() -> Path:
    return Path(__file__).resolve().parent.parent / "references"


def load_builtin() -> dict[str, Any]:
    return json.loads((_refs() / "builtin_op_latency.json").read_text(encoding="utf-8"))


def find_msmodeling_data_root(msmodeling_repo: str | Path | None) -> Path | None:
    if not msmodeling_repo:
        return None
    root = Path(msmodeling_repo)
    candidates = [
        root / "tensor_cast" / "performance_model" / "profiling_database" / "data",
        root / "profiling_database" / "data",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    for p in root.rglob("profiling_database"):
        data = p / "data"
        if data.is_dir():
            return data
    return None


def _is_lfs_pointer(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:120]
    except OSError:
        return False
    return head.startswith("version https://git-lfs.github.com/spec/v1")


def discover_device_dirs(data_root: Path) -> list[Path]:
    return sorted([p for p in data_root.iterdir() if p.is_dir()])


def find_op_mapping(device_dir: Path) -> Path | None:
    matches = list(device_dir.glob("vllm_ascend/*/op_mapping.yaml"))
    return matches[0] if matches else None


def find_kernel_csv(device_dir: Path, kernel_type: str) -> Path | None:
    # compute kernels
    for csv_path in device_dir.glob(f"vllm_ascend/*/{kernel_type}.csv"):
        return csv_path
    # hccl
    for csv_path in device_dir.glob(f"hccl/*/{kernel_type}.csv"):
        return csv_path
    # fuzzy
    for csv_path in device_dir.rglob(f"{kernel_type}.csv"):
        return csv_path
    return None


def load_op_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text) or {}
    # minimal fallback: extract kernel_type lines only
    mapping: dict[str, Any] = {"operator_mappings": {}}
    current = None
    for line in text.splitlines():
        m = re.match(r'\s*"([^"]+)":\s*$', line)
        if m:
            current = m.group(1)
            mapping["operator_mappings"][current] = {}
            continue
        if current:
            km = re.search(r"kernel_type:\s*(\S+)", line)
            if km:
                mapping["operator_mappings"][current]["kernel_type"] = km.group(1)
    return mapping


def resolve_kernel_types(op: str, mapping: dict[str, Any]) -> list[str]:
    """Map logical op / kernel name to concrete CSV kernel_type list."""
    ops = mapping.get("operator_mappings") or {}
    if op in ops:
        entry = ops[op] or {}
        if entry.get("composite") and entry.get("sub_kernels"):
            return list(entry["sub_kernels"])
        kt = entry.get("kernel_type")
        alts = entry.get("alternate_kernel_types") or []
        out = []
        if kt:
            out.append(kt)
        out.extend(alts)
        return out or [op]
    # already a kernel type
    return [op]


def _parse_latency_from_csv(path: Path, tokens: int) -> dict[str, Any] | None:
    if _is_lfs_pointer(path):
        return {
            "lfs_pointer": True,
            "path": str(path),
        }
    # real CSV
    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return None
    latency_cols = [
        c
        for c in (rows[0].keys() if rows else [])
        if c
        and (
            "duration" in c.lower()
            or "latency" in c.lower()
            or c.lower().endswith("(us)")
        )
    ]
    if not latency_cols:
        # try any numeric column
        latency_cols = [
            c
            for c in rows[0].keys()
            if c and re.search(r"us|time|duration", c, re.I)
        ]
    best = None
    best_dist = None
    for row in rows:
        # try find a token/M/batch-like dim
        dim = None
        for key, val in row.items():
            if key and re.search(r"token|batch|m\b|seq|shape", key, re.I):
                try:
                    nums = [int(x) for x in re.findall(r"\d+", str(val))]
                    if nums:
                        dim = nums[0]
                        break
                except ValueError:
                    continue
        lat = None
        for lc in latency_cols:
            try:
                lat = float(str(row[lc]).replace(",", ""))
                break
            except (TypeError, ValueError, KeyError):
                continue
        if lat is None:
            continue
        dist = abs((dim or tokens) - tokens)
        if best is None or best_dist is None or dist < best_dist:
            best = lat
            best_dist = dist
    if best is None:
        return None
    return {
        "latency_us": float(best),
        "source": f"msmodeling_csv:{path}",
        "lfs_pointer": False,
    }


def analytic_latency_us(kernel: str, tokens: int, message_bytes: int | None = None) -> float:
    """Shape-aware stand-in when CSV is LFS-unavailable; keyed by msmodeling kernel name."""
    k = kernel.lower()
    t = max(1, tokens)
    if message_bytes is not None and (k.startswith("hcom_") or "allreduce" in k or "allgather" in k):
        # ~0.02 us per KB + base
        return 30.0 + (message_bytes / 1024.0) * 0.05
    if "sparseflash" in k or "lightning" in k or "index" == k or k.endswith("index"):
        return 80.0 + 0.35 * t + 0.00002 * t * t
    if "ringmla" in k or "fusedinfer" in k or "attention" in k:
        return 100.0 + 0.4 * t + 0.00003 * t * t
    if "dispatchffn" in k or "groupedmatmul" in k:
        return 120.0 + 0.5 * t
    if "matmul" in k or "bmm" in k or "batchmatmul" in k:
        return 40.0 + 0.25 * t
    if "norm" in k or "rope" in k:
        return 20.0 + 0.05 * t
    return 60.0 + 0.3 * t


def _bucket_latency(op_spec: dict[str, Any], tokens: int) -> float:
    buckets = sorted(op_spec.get("buckets") or [], key=lambda b: int(b["max_tokens"]))
    if not buckets:
        return 1000.0
    for b in buckets:
        if tokens <= int(b["max_tokens"]):
            return float(b["latency_us"])
    return float(buckets[-1]["latency_us"])


def lookup_builtin(op: str, tokens: int, builtin: dict[str, Any] | None = None) -> dict[str, Any]:
    builtin = builtin or load_builtin()
    ops = builtin.get("ops") or {}
    key = op if op in ops else "GENERIC_ATTN"
    lat = _bucket_latency(ops[key], tokens)
    reason = (
        f"op={op}: msmodeling real CSV unavailable; "
        "fell back to builtin_op_latency.json (not production path)"
    )
    return {
        "op": op,
        "kernel_type": key,
        "tokens": tokens,
        "latency_us": lat,
        "source": "builtin_op_latency.json",
        "used_real_csv": False,
        "fallback_reasons": [reason],
        "details": [
            {
                "kernel_type": key,
                "latency_us": lat,
                "source": "builtin_op_latency.json",
                "fallback_reason": reason,
            }
        ],
    }


def index_msmodeling(msmodeling_repo: str | Path) -> dict[str, Any]:
    data_root = find_msmodeling_data_root(msmodeling_repo)
    if data_root is None:
        raise FileNotFoundError(
            f"msmodeling profiling_database/data not found under {msmodeling_repo}. "
            "Clone https://gitcode.com/Ascend/msmodeling"
        )
    devices = discover_device_dirs(data_root)
    if not devices:
        raise FileNotFoundError(f"No device dirs under {data_root}")
    device = devices[0]
    mapping_path = find_op_mapping(device)
    mapping = load_op_mapping(mapping_path) if mapping_path else {}
    return {
        "data_root": str(data_root),
        "device_dir": str(device),
        "op_mapping_path": str(mapping_path) if mapping_path else None,
        "mapping": mapping,
    }


def lookup_op_latency(
    op: str,
    tokens: int,
    msmodeling_repo: str | Path | None = None,
    message_bytes: int | None = None,
    allow_without_repos: bool = False,
    _index_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if msmodeling_repo is None:
        if allow_without_repos:
            return lookup_builtin(op, tokens)
        raise FileNotFoundError(
            "msmodeling_repo is required for serving-slo-concurrency. "
            "Clone https://gitcode.com/Ascend/msmodeling"
        )

    index = _index_cache or index_msmodeling(msmodeling_repo)
    mapping = index.get("mapping") or {}
    device_dir = Path(index["device_dir"])
    kernels = resolve_kernel_types(op, mapping)
    # If op itself is kernel, ensure listed
    if op not in kernels:
        kernels = [op] + kernels

    total = 0.0
    details = []
    any_csv = False
    any_non_csv = False
    fallback_reasons: list[str] = []
    for kt in kernels:
        csv_path = find_kernel_csv(device_dir, kt)
        if csv_path is None:
            any_non_csv = True
            lat = analytic_latency_us(kt, tokens, message_bytes)
            reason = (
                f"kernel={kt}: no CSV under {device_dir} "
                "(missing profiling file); used analytic mapping"
            )
            fallback_reasons.append(reason)
            details.append(
                {
                    "kernel_type": kt,
                    "latency_us": lat,
                    "source": "msmodeling_mapping_analytic(no_csv)",
                    "fallback_reason": reason,
                }
            )
            total += lat
            continue
        parsed = _parse_latency_from_csv(csv_path, tokens)
        if parsed and parsed.get("lfs_pointer"):
            any_non_csv = True
            lat = analytic_latency_us(kt, tokens, message_bytes)
            reason = (
                f"kernel={kt}: CSV is Git-LFS pointer ({csv_path.name}); "
                "git lfs pull / GitCode LFS fetch required for real Duration(us)"
            )
            fallback_reasons.append(reason)
            details.append(
                {
                    "kernel_type": kt,
                    "latency_us": lat,
                    "source": f"msmodeling_lfs_analytic:{csv_path.name}",
                    "fallback_reason": reason,
                }
            )
            total += lat
        elif parsed and "latency_us" in parsed:
            any_csv = True
            details.append(
                {
                    "kernel_type": kt,
                    "latency_us": parsed["latency_us"],
                    "source": parsed["source"],
                    "fallback_reason": None,
                }
            )
            total += float(parsed["latency_us"])
        else:
            any_non_csv = True
            lat = analytic_latency_us(kt, tokens, message_bytes)
            reason = (
                f"kernel={kt}: CSV present but no parseable Duration/latency column "
                f"({csv_path.name}); used analytic mapping"
            )
            fallback_reasons.append(reason)
            details.append(
                {
                    "kernel_type": kt,
                    "latency_us": lat,
                    "source": f"msmodeling_mapping_analytic:{csv_path.name}",
                    "fallback_reason": reason,
                }
            )
            total += lat

    # Prefer real CSV only when every kernel hit real data.
    if any_csv and not any_non_csv:
        source = "msmodeling_csv"
    elif any_csv and any_non_csv:
        source = "msmodeling_mixed"
    elif any(r.startswith("kernel=") and "LFS pointer" in r for r in fallback_reasons):
        source = "msmodeling_lfs_analytic"
    else:
        source = "msmodeling_mapping_analytic"

    return {
        "op": op,
        "kernel_types": kernels,
        "tokens": tokens,
        "latency_us": total,
        "source": source,
        "used_real_csv": bool(any_csv and not any_non_csv),
        "fallback_reasons": fallback_reasons,
        "details": details,
        "device_dir": index.get("device_dir"),
        "op_mapping_path": index.get("op_mapping_path"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--op", required=True)
    ap.add_argument("--tokens", type=int, required=True)
    ap.add_argument("--msmodeling-repo", default=None)
    ap.add_argument("--message-bytes", type=int, default=None)
    ap.add_argument("--allow-without-repos", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = lookup_op_latency(
        args.op,
        args.tokens,
        args.msmodeling_repo,
        message_bytes=args.message_bytes,
        allow_without_repos=args.allow_without_repos,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
