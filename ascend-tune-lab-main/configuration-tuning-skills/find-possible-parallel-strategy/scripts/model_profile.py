#!/usr/bin/env python3
"""Resolve model parameter count and weight size for parallel strategy planning."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

QUANT_BYTES = {
    "bf16": 2.0,
    "bfloat16": 2.0,
    "fp16": 2.0,
    "w8a8": 1.0,
    "w8a8c16": 1.0,
    "w8a8c8": 1.0,
    "w4a8": 0.5,
    "w4a8c16": 0.5,
    "w4a8c8": 0.5,
}

# 64 * 0.95 - 3 = 57.8; plan uses 57.6 for documentation alignment with user formula.
PER_NPU_WEIGHT_GB = 64 * 0.95 - 3  # 57.8
PER_NPU_WEIGHT_GB_DOC = 57.6


def normalize_quant_key(q: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", (q or "").lower())
    aliases = {
        "bf16": "bf16",
        "bfloat16": "bf16",
        "fp16": "bf16",
        "w8a8": "w8a8",
        "w8a8c16": "w8a8",
        "w8a8c8": "w8a8",
        "w4a8": "w4a8",
        "w4a8c16": "w4a8",
        "w4a8c8": "w4a8",
    }
    return aliases.get(key, key)


def quant_bytes(q: str) -> float:
    key = normalize_quant_key(q)
    if key not in QUANT_BYTES and key not in ("bf16", "w8a8", "w4a8"):
        # try prefix match
        for k, v in QUANT_BYTES.items():
            if key.startswith(k.replace(".", "")):
                return v
    return QUANT_BYTES.get(key, QUANT_BYTES.get(normalize_quant_key(q), 1.0))


def _refs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "references"


def load_params_table() -> dict[str, float]:
    path = _refs_dir() / "model_params_b.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k).lower(): float(v) for k, v in data.get("models", {}).items()}


def normalize_model_key(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"^zhipuai/", "", s)
    s = re.sub(r"^deepseek-ai/", "", s)
    s = re.sub(r"^qwen/", "", s)
    return s


def params_from_name(name: str) -> float | None:
    table = load_params_table()
    key = normalize_model_key(name)
    if key in table:
        return table[key]
    # try without trailing quant suffix
    for alias, val in table.items():
        if key.startswith(alias) or alias in key:
            return val
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", key)
    if m:
        return float(m.group(1))
    return None


def estimate_params_from_hf_config(cfg: dict[str, Any]) -> float | None:
    """Rough dense/MoE estimate from HF-style config (billions)."""
    inner = cfg
    for nest in ("text_config", "llm_config", "language_config"):
        if isinstance(cfg.get(nest), dict):
            inner = cfg[nest]
            break
    hidden = int(inner.get("hidden_size") or 0)
    layers = int(inner.get("num_hidden_layers") or 0)
    intermediate = int(inner.get("intermediate_size") or hidden * 4)
    vocab = int(inner.get("vocab_size") or 0)
    if not (hidden and layers):
        return None
    # approx transformer params
    dense = layers * (4 * hidden * hidden + 3 * hidden * intermediate) + vocab * hidden
    n_experts = int(
        inner.get("num_local_experts")
        or inner.get("n_routed_experts")
        or inner.get("num_experts")
        or 0
    )
    if n_experts > 0:
        moe_inter = int(
            inner.get("moe_intermediate_size")
            or inner.get("ffn_hidden_size")
            or intermediate // 4
            or 1
        )
        dense = layers * (4 * hidden * hidden) + vocab * hidden
        dense += layers * n_experts * (3 * hidden * moe_inter)
    return dense / 1e9


def load_model_config_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "config" in data and isinstance(data["config"], dict):
        return data["config"]
    return data


@dataclass
class ModelProfile:
    model: str
    quantization: str
    quant_bytes: float
    params_b: float
    params_source: str
    weight_gb: float
    num_hidden_layers: int | None = None
    num_key_value_heads: int | None = None
    head_dim: int | None = None
    hidden_size: int | None = None
    is_moe: bool = False
    attention_type: str = "mha"


def build_model_profile(
    model: str,
    quantization: str,
    model_config_path: str | Path | None = None,
) -> ModelProfile:
    qbytes = quant_bytes(quantization)
    params_b: float | None = params_from_name(model)
    source = "model_params_b.json" if params_b is not None else ""
    hf: dict[str, Any] = {}
    if model_config_path and Path(model_config_path).exists():
        hf = load_model_config_file(Path(model_config_path))
        if params_b is None:
            est = estimate_params_from_hf_config(hf)
            if est is not None:
                params_b = est
                source = "hf_config_estimate"
    if params_b is None:
        raise ValueError(
            f"Cannot resolve params_b for model={model!r}; "
            "add to references/model_params_b.json or provide model_config.json"
        )
    if not source:
        source = "name_suffix"

    inner = hf
    for nest in ("text_config", "llm_config", "language_config"):
        if isinstance(hf.get(nest), dict):
            inner = hf[nest]
            break

    layers = int(inner.get("num_hidden_layers") or 0) or None
    kv_heads = int(inner.get("num_key_value_heads") or inner.get("num_attention_heads") or 0) or None
    hidden = int(inner.get("hidden_size") or 0) or None
    head_dim = None
    if hidden and kv_heads:
        n_heads = int(inner.get("num_attention_heads") or kv_heads)
        head_dim = hidden // n_heads if n_heads else None
    is_moe = bool(
        inner.get("num_local_experts")
        or inner.get("n_routed_experts")
        or inner.get("num_experts")
    )
    attn = "mla" if (inner.get("kv_lora_rank") or inner.get("qk_nope_head_dim")) else "mha"

    return ModelProfile(
        model=model,
        quantization=quantization,
        quant_bytes=qbytes,
        params_b=float(params_b),
        params_source=source,
        weight_gb=float(params_b) * qbytes,
        num_hidden_layers=layers,
        num_key_value_heads=kv_heads,
        head_dim=head_dim,
        hidden_size=hidden,
        is_moe=is_moe,
        attention_type=attn,
    )


def ceil_pow2(x: float) -> int:
    """Ceiling to next power of 2. For x<=1 returns 1."""
    if x <= 1:
        return 1
    return 1 << math.ceil(math.log2(x))


def min_tp_from_weight(weight_gb: float, budget_gb: float = PER_NPU_WEIGHT_GB_DOC) -> int:
    """min_tp = ceil_pow2(weight_gb / budget_gb).

    Note: user verbal example for 284GB used Ceiling(284/57.6)=5→4 then listed
    DP4TP4; this implementation follows ceil_pow2(ratio) so 284/57.6≈4.93 → 8.
    """
    ratio = weight_gb / budget_gb
    return ceil_pow2(ratio)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--quantization", required=True)
    ap.add_argument("--model-config", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    profile = build_model_profile(args.model, args.quantization, args.model_config)
    data = asdict(profile)
    data["min_tp"] = min_tp_from_weight(profile.weight_gb)
    data["per_npu_budget_gb"] = PER_NPU_WEIGHT_GB_DOC
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data)


if __name__ == "__main__":
    main()
