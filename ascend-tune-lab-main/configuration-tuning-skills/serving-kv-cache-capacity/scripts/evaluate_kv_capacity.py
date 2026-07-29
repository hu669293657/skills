#!/usr/bin/env python3
"""Evaluate KV cache capacity and memory-bound max concurrency per parallel combo."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


def _refs() -> Path:
    return Path(__file__).resolve().parent.parent / "references"


def load_memory_defaults() -> dict[str, Any]:
    path = _refs() / "memory_defaults.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_model_key(name: str) -> str:
    s = (name or "").strip().lower().replace("_", "-")
    s = re.sub(r"^[^/]+/", "", s)
    return s


def kv_bytes_per_token(
    profile: dict[str, Any],
    model: str,
    defaults: dict[str, Any],
) -> tuple[float, str]:
    """Return (bytes_per_token, source)."""
    overrides = defaults.get("mla_kv_bytes_per_token_overrides") or {}
    key = normalize_model_key(model)
    for alias, val in overrides.items():
        if alias in key or key.startswith(alias):
            return float(val), f"mla_override:{alias}"

    layers = profile.get("num_hidden_layers")
    kv_heads = profile.get("num_key_value_heads")
    head_dim = profile.get("head_dim")
    dtype_b = float(defaults.get("kv_dtype_bytes_default", 2))
    if layers and kv_heads and head_dim:
        bpt = 2 * int(layers) * int(kv_heads) * int(head_dim) * dtype_b
        return float(bpt), "mha_formula"

    # Fallback heuristic for missing dims (conservative ~128KiB/token)
    return 131072.0, "fallback_default_128KiB"


def workspace_gb(context_len: int, defaults: dict[str, Any]) -> float:
    base = float(defaults.get("workspace_gb_base", 2.0))
    per_1k = float(defaults.get("workspace_gb_per_1k_context", 0.05))
    return base + (context_len / 1000.0) * per_1k


def evaluate_combo(
    combo: dict[str, Any],
    weight_gb: float,
    kv_bpt: float,
    input_len: int,
    output_len: int,
    defaults: dict[str, Any],
    context_len: int | None = None,
) -> dict[str, Any]:
    tp = int(combo["tp"])
    dp = int(combo["dp"])
    ctx = context_len if context_len is not None else (input_len + output_len)
    ws = workspace_gb(ctx, defaults)
    hccl_gb = float(defaults.get("hccl_buffsize_mb_default", 512)) / 1024.0
    hbm = float(defaults.get("npu_hbm_gb", 64))
    sys_res = float(defaults.get("system_reserved_gb", 3))
    weight_per_npu = weight_gb / tp
    budget = hbm - weight_per_npu - ws - hccl_gb - sys_res
    available_kv_dp = max(0.0, budget) * tp
    per_req = (input_len + output_len) * kv_bpt / 1e9
    if per_req <= 0:
        max_conc = 0
    else:
        max_conc = int(math.floor(available_kv_dp / per_req)) * dp
    return {
        **combo,
        "context_len": ctx,
        "workspace_gb": round(ws, 4),
        "hccl_gb": round(hccl_gb, 4),
        "weight_per_npu_gb": round(weight_per_npu, 4),
        "budget_per_npu_gb": round(budget, 4),
        "available_kv_gb": round(available_kv_dp, 4),
        "kv_bytes_per_token": kv_bpt,
        "per_request_kv_gb": round(per_req, 6),
        "max_concurrency_memory": max(0, max_conc),
    }


def evaluate_from_parallel_json(
    parallel: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    context_len: int | None = None,
) -> dict[str, Any]:
    defaults = defaults or load_memory_defaults()
    profile = parallel.get("profile") or {}
    model = parallel.get("model") or ""
    weight_gb = float(profile.get("weight_gb") or 0)
    input_len = int(parallel.get("input_len") or 0)
    output_len = int(parallel.get("output_len") or 0)
    kv_bpt, kv_source = kv_bytes_per_token(profile, model, defaults)

    results = []
    for combo in parallel.get("combinations") or []:
        results.append(
            evaluate_combo(
                combo,
                weight_gb=weight_gb,
                kv_bpt=kv_bpt,
                input_len=input_len,
                output_len=output_len,
                defaults=defaults,
                context_len=context_len,
            )
        )

    return {
        "model": model,
        "quantization": parallel.get("quantization"),
        "input_len": input_len,
        "output_len": output_len,
        "kv_bytes_per_token": kv_bpt,
        "kv_bytes_source": kv_source,
        "memory_defaults": {
            "npu_hbm_gb": defaults.get("npu_hbm_gb"),
            "system_reserved_gb": defaults.get("system_reserved_gb"),
            "workspace_gb_base": defaults.get("workspace_gb_base"),
            "hccl_buffsize_mb_default": defaults.get("hccl_buffsize_mb_default"),
        },
        "combinations": results,
    }


def parse_available_kv_from_log(log_text: str) -> float | None:
    m = re.search(
        r"Available KV cache memory:\s*([\d.]+)\s*(GB|GiB|MB|MiB)",
        log_text,
        re.I,
    )
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("mb", "mib"):
        return val / 1024.0
    return val


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parallel-json", required=True, help="parallel-strategies.json")
    ap.add_argument("--context-len", type=int, default=None)
    ap.add_argument("--log-file", default=None, help="Optional serving log for KV calibrate")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    parallel = json.loads(Path(args.parallel_json).read_text(encoding="utf-8"))
    result = evaluate_from_parallel_json(parallel, context_len=args.context_len)

    if args.log_file and Path(args.log_file).exists():
        measured = parse_available_kv_from_log(Path(args.log_file).read_text(encoding="utf-8", errors="ignore"))
        result["log_available_kv_gb"] = measured
        result["log_calibrated"] = measured is not None

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
