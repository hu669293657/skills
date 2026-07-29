#!/usr/bin/env python3
"""Infer operator shapes from vllm-ascend code-path analysis + model profile.

Requires --vllm-ascend-repo (strong dependency). GLM5 LI/SFA is only one of
many paths discovered from AscendSFABackend / AscendMLABackend / etc.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from analyze_vllm_ascend_path import analyze_vllm_ascend  # noqa: E402


def _shape_dims(
    kernel: str,
    phase: str,
    concurrency: int,
    input_len: int,
    tokens: int,
    hidden_per_tp: int,
    head_dim: int,
    layers: int,
    tp: int,
) -> dict[str, Any]:
    """Heuristic shape descriptors aligned with msmodeling query modes."""
    base = {
        "kernel_type": kernel,
        "phase": phase,
        "concurrency": concurrency,
        "num_tokens": tokens,
        "hidden_per_tp": hidden_per_tp,
        "head_dim": head_dim,
        "tp": tp,
        "layers": layers,
    }
    k = kernel.lower()
    if "attention" in k or "mla" in k or "sparseflash" in k or "fusedinfer" in k:
        q_len = input_len if phase == "prefill" else 1
        base["query_mode"] = "attention_special"
        base["batch"] = concurrency
        base["seq"] = q_len
        base["heads_hint"] = max(1, hidden_per_tp // max(head_dim, 1))
        base["shape_key"] = [concurrency, q_len, base["heads_hint"], head_dim]
    elif k.startswith("hcom_") or "allreduce" in k or "allgather" in k:
        # message bytes ≈ tokens * hidden_per_tp * 2 (bf16)
        msg = tokens * hidden_per_tp * 2
        base["query_mode"] = "communication"
        base["message_bytes"] = msg
        base["shape_key"] = [msg]
    elif "dispatchffn" in k or "groupedmatmul" in k or "moegating" in k:
        base["query_mode"] = "moe_fused"
        base["shape_key"] = [tokens, hidden_per_tp]
    else:
        # MatMul-like: [M, K] x [K, N] with M=tokens
        base["query_mode"] = "compute"
        base["shape_key"] = [tokens, hidden_per_tp, hidden_per_tp]
    return base


def infer_shapes(
    model: str,
    profile: dict[str, Any],
    dp: int,
    tp: int,
    ep: int,
    input_len: int,
    output_len: int,
    concurrency: int,
    vllm_ascend_repo: str,
    code_path: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if code_path is None:
        code_path = analyze_vllm_ascend(model, vllm_ascend_repo).__dict__
        # dataclass might already be dict if passed
    if hasattr(code_path, "__dict__") and not isinstance(code_path, dict):
        from dataclasses import asdict

        code_path = asdict(code_path)

    layers = int(profile.get("num_hidden_layers") or 40)
    hidden = int(profile.get("hidden_size") or 4096)
    head_dim = int(profile.get("head_dim") or 128)
    hidden_per_tp = max(1, hidden // max(tp, 1))

    prefill_tokens = max(1, concurrency * input_len)
    decode_tokens = max(1, concurrency)

    core_ops = list(code_path.get("core_ops") or [])
    shapes = []
    for kernel in core_ops:
        for phase, tokens in (("prefill", prefill_tokens), ("decode", decode_tokens)):
            dims = _shape_dims(
                kernel, phase, concurrency, input_len, tokens, hidden_per_tp, head_dim, layers, tp
            )
            shapes.append(
                {
                    "op": kernel,  # use NPU/msmodeling kernel_type as op id
                    **dims,
                    "dp": dp,
                    "ep": ep,
                }
            )

    return {
        "model": model,
        "analysis_mode": "vllm_ascend_code",
        "selected_backend": code_path.get("selected_backend"),
        "tc_ops": code_path.get("tc_ops"),
        "core_ops": core_ops,
        "parallel": {"dp": dp, "tp": tp, "ep": ep},
        "input_len": input_len,
        "output_len": output_len,
        "concurrency": concurrency,
        "shapes": shapes,
        "code_path": code_path,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--profile-json", default=None)
    ap.add_argument("--dp", type=int, required=True)
    ap.add_argument("--tp", type=int, required=True)
    ap.add_argument("--ep", type=int, default=None)
    ap.add_argument("--input-len", type=int, required=True)
    ap.add_argument("--output-len", type=int, required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--vllm-ascend-repo", required=True)
    ap.add_argument("--code-path-json", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    profile: dict[str, Any] = {}
    if args.profile_json:
        profile = json.loads(Path(args.profile_json).read_text(encoding="utf-8"))
        if "profile" in profile and isinstance(profile["profile"], dict):
            profile = profile["profile"]

    code_path = None
    if args.code_path_json:
        code_path = json.loads(Path(args.code_path_json).read_text(encoding="utf-8"))

    ep = args.ep if args.ep is not None else args.dp * args.tp
    result = infer_shapes(
        model=args.model,
        profile=profile,
        dp=args.dp,
        tp=args.tp,
        ep=ep,
        input_len=args.input_len,
        output_len=args.output_len,
        concurrency=args.concurrency,
        vllm_ascend_repo=args.vllm_ascend_repo,
        code_path=code_path,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
