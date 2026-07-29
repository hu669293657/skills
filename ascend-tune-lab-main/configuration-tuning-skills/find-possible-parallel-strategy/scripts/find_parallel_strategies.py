#!/usr/bin/env python3
"""Enumerate legal DP×TP×EP combinations for single-node mixed deployment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from deploy_config import parse_deploy_config_md  # noqa: E402
from device_topology import resolve_world_size  # noqa: E402
from model_profile import (  # noqa: E402
    PER_NPU_WEIGHT_GB_DOC,
    build_model_profile,
    min_tp_from_weight,
)


def power_of_two_tps(min_tp: int, max_tp: int) -> list[int]:
    tps: list[int] = []
    t = 1
    while t <= max_tp:
        if t >= min_tp:
            tps.append(t)
        t *= 2
    return tps


def enumerate_parallel_combinations(
    total_npus: int,
    min_tp: int,
    weight_gb: float,
) -> list[dict[str, Any]]:
    combos: list[dict[str, Any]] = []
    for tp in power_of_two_tps(min_tp, total_npus):
        if total_npus % tp != 0:
            continue
        dp = total_npus // tp
        ep = dp * tp  # = total_npus for single-node mix with EP=world
        weight_per_npu = weight_gb / tp
        combos.append(
            {
                "dp": dp,
                "tp": tp,
                "ep": ep,
                "label": f"DP{dp}TP{tp}EP{ep}",
                "weight_per_npu_gb": round(weight_per_npu, 4),
                "fits_weight_budget": weight_per_npu <= PER_NPU_WEIGHT_GB_DOC,
            }
        )
    return combos


def analyze(
    model: str,
    quantization: str,
    num_npus: int,
    model_config_path: str | None = None,
    current_dp: int | None = None,
    current_tp: int | None = None,
    input_len: int | None = None,
    output_len: int | None = None,
    device_type: str | None = None,
) -> dict[str, Any]:
    """Enumerate DP/TP/EP. ``num_npus`` is physical card count (总卡数 / NPU卡数)."""
    profile = build_model_profile(model, quantization, model_config_path)
    min_tp = min_tp_from_weight(profile.weight_gb)
    topo = resolve_world_size(num_npus, device_type)
    world_size = int(topo["world_size"])
    combos = enumerate_parallel_combinations(world_size, min_tp, profile.weight_gb)
    return {
        "scope": "single_node_mix",
        "model": model,
        "quantization": quantization,
        "device_type": device_type,
        "device_family": topo.get("device_family"),
        "num_npus": num_npus,
        "num_cards": topo["num_cards"],
        "dies_per_card": topo["dies_per_card"],
        "world_size": world_size,
        "input_len": input_len,
        "output_len": output_len,
        "profile": {
            "params_b": profile.params_b,
            "params_source": profile.params_source,
            "quant_bytes": profile.quant_bytes,
            "weight_gb": profile.weight_gb,
            "num_hidden_layers": profile.num_hidden_layers,
            "num_key_value_heads": profile.num_key_value_heads,
            "head_dim": profile.head_dim,
            "hidden_size": profile.hidden_size,
            "is_moe": profile.is_moe,
            "attention_type": profile.attention_type,
        },
        "per_npu_budget_gb": PER_NPU_WEIGHT_GB_DOC,
        "min_tp": min_tp,
        "min_tp_formula": "ceil_pow2(weight_gb / 57.6)",
        "min_tp_note": (
            "ceil_pow2(ratio): e.g. 284/57.6≈4.93 → min_tp=8 "
            "(not Ceiling(ratio)=5 then snap-down to 4)."
        ),
        "topology_note": (
            "NPU卡数=总卡数(物理卡); A3 world_size=num_cards×2 (dual-die); "
            "A2 world_size=num_cards. DP/TP/EP 按 world_size 枚举."
        ),
        "current": {"dp": current_dp, "tp": current_tp},
        "combinations": combos,
        "feasible": len(combos) > 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="deploy-config.md path")
    ap.add_argument("--model", default=None)
    ap.add_argument("--quantization", default=None)
    ap.add_argument(
        "--num-npus",
        type=int,
        default=None,
        help="Physical card count (总卡数 / NPU卡数), not die count",
    )
    ap.add_argument(
        "--device-type",
        default=None,
        help="Device type (e.g. A3 / Atlas 800I A3); A3 uses 2 dies per card",
    )
    ap.add_argument("--model-config", default=None)
    ap.add_argument("--input-len", type=int, default=None)
    ap.add_argument("--output-len", type=int, default=None)
    ap.add_argument("--dp", type=int, default=None)
    ap.add_argument("--tp", type=int, default=None)
    ap.add_argument("--out", default=None, help="Write JSON to this path")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    model = args.model
    quantization = args.quantization
    num_npus = args.num_npus
    device_type = args.device_type
    input_len = args.input_len
    output_len = args.output_len
    dp = args.dp
    tp = args.tp

    if args.config:
        cfg = parse_deploy_config_md(args.config)
        model = model or cfg.get("model")
        quantization = quantization or cfg.get("quantization")
        num_npus = num_npus or cfg.get("num_npus")
        device_type = device_type or cfg.get("device_type")
        input_len = input_len if input_len is not None else cfg.get("input_len")
        output_len = output_len if output_len is not None else cfg.get("output_len")
        dp = dp if dp is not None else cfg.get("dp")
        tp = tp if tp is not None else cfg.get("tp")

    if not model or not quantization or not num_npus:
        ap.error("require --model, --quantization, --num-npus (or --config)")

    result = analyze(
        model=str(model),
        quantization=str(quantization),
        num_npus=int(num_npus),
        model_config_path=args.model_config,
        current_dp=dp,
        current_tp=tp,
        input_len=input_len,
        output_len=output_len,
        device_type=str(device_type) if device_type else None,
    )

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
