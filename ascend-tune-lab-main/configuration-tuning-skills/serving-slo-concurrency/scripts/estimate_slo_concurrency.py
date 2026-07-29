#!/usr/bin/env python3
"""Estimate max concurrency under TTFT/TPOT SLO using vllm-ascend + msmodeling.

Strongly depends on local clones:
  - https://github.com/vllm-project/vllm-ascend
  - https://gitcode.com/Ascend/msmodeling

Use --allow-without-repos only for offline unit tests.
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
from dataclasses import asdict  # noqa: E402
from infer_operator_shapes import infer_shapes  # noqa: E402
from lookup_op_latency import index_msmodeling, load_builtin, lookup_op_latency  # noqa: E402

# Placeholder until model/family-specific ratios are provided.
DEFAULT_MAIN_OP_RATIO = 0.7


def _is_comm_op(op_name: str) -> bool:
    n = (op_name or "").lower()
    return n.startswith("hcom_") or "allreduce" in n or "allgather" in n or "reducescatter" in n


def resolve_num_hidden_layers(profile: dict[str, Any]) -> tuple[int, str]:
    """Read layer count from model config / profile (HF-style nested configs)."""
    if not isinstance(profile, dict):
        return 40, "fallback_default:40"
    candidates: list[tuple[dict[str, Any], str]] = [(profile, "profile")]
    for nest in ("text_config", "llm_config", "language_config", "config"):
        inner = profile.get(nest)
        if isinstance(inner, dict):
            candidates.append((inner, f"profile.{nest}"))
    for cfg, src in candidates:
        val = cfg.get("num_hidden_layers")
        if val is None:
            continue
        try:
            layers = int(val)
        except (TypeError, ValueError):
            continue
        if layers > 0:
            return layers, src
    return 40, "fallback_default:40"


def estimate_latencies_ms(
    model: str,
    profile: dict[str, Any],
    dp: int,
    tp: int,
    ep: int,
    input_len: int,
    output_len: int,
    concurrency: int,
    msmodeling_repo: str | None,
    vllm_ascend_repo: str | None,
    mtp_accept_rate: float,
    builtin: dict[str, Any],
    allow_without_repos: bool = False,
    code_path: dict[str, Any] | None = None,
    ms_index: dict[str, Any] | None = None,
    main_op_ratio: float | None = None,
) -> dict[str, Any]:
    if not allow_without_repos:
        if not vllm_ascend_repo or not Path(vllm_ascend_repo).exists():
            raise FileNotFoundError("vllm-ascend repo required")
        if not msmodeling_repo or not Path(msmodeling_repo).exists():
            raise FileNotFoundError("msmodeling repo required")

    if code_path is None:
        if vllm_ascend_repo and Path(vllm_ascend_repo).exists():
            code_path = asdict(
                analyze_vllm_ascend(
                    model,
                    vllm_ascend_repo,
                    profile=profile,
                    model_config=profile,
                )
            )
        else:
            code_path = {
                "selected_backend": "ASCEND",
                "core_ops": ["FusedInferAttentionScore", "MatMulV2", "hcom_allReduce_"],
                "tc_ops": ["tensor_cast.attention.default"],
                "analysis_mode": "fallback_no_repo",
            }

    if vllm_ascend_repo and Path(vllm_ascend_repo).exists():
        shapes_doc = infer_shapes(
            model,
            profile,
            dp,
            tp,
            ep,
            input_len,
            output_len,
            concurrency,
            vllm_ascend_repo,
            code_path=code_path,
        )
    else:
        shapes_doc = {
            "shapes": [
                {"op": op, "phase": ph, "num_tokens": concurrency * (input_len if ph == "prefill" else 1), "tokens": concurrency * (input_len if ph == "prefill" else 1)}
                for op in code_path.get("core_ops", [])
                for ph in ("prefill", "decode")
            ],
            "selected_backend": code_path.get("selected_backend"),
            "core_ops": code_path.get("core_ops"),
            "code_path": code_path,
        }

    layers, layers_source = resolve_num_hidden_layers(profile)
    prefill_scale = float(builtin.get("prefill_scale", 1.5))
    if main_op_ratio is None:
        main_op_ratio = float(builtin.get("default_main_op_ratio", DEFAULT_MAIN_OP_RATIO))
    main_op_ratio = float(main_op_ratio)
    if main_op_ratio <= 0 or main_op_ratio > 1:
        raise ValueError(f"main_op_ratio must be in (0, 1], got {main_op_ratio}")

    prefill_us = 0.0
    # Decode: sum of main-op (non-comm) per-call latencies, then ITL = sum * layers / ratio
    main_ops_decode_us = 0.0
    op_details: list[dict[str, Any]] = []
    sources: set[str] = set()
    fallback_reasons: list[str] = []
    used_real_flags: list[bool] = []

    for sh in shapes_doc["shapes"]:
        tokens = int(sh.get("num_tokens") or sh.get("tokens") or 1)
        msg = sh.get("message_bytes")
        hit = lookup_op_latency(
            sh["op"],
            tokens,
            msmodeling_repo,
            message_bytes=int(msg) if msg is not None else None,
            allow_without_repos=allow_without_repos,
            _index_cache=ms_index,
        )
        src = str(hit.get("source", "unknown")).split(":")[0]
        sources.add(src)
        used_real_flags.append(bool(hit.get("used_real_csv")))
        for reason in hit.get("fallback_reasons") or []:
            if reason and reason not in fallback_reasons:
                fallback_reasons.append(reason)
        if src == "builtin_op_latency.json" or src == "builtin_fallback":
            reason = (
                f"op={sh['op']}: used builtin_op_latency.json "
                "(msmodeling unavailable or --allow-without-repos)"
            )
            if reason not in fallback_reasons:
                fallback_reasons.append(reason)
        lat = float(hit["latency_us"])
        phase = sh.get("phase")
        op_name = str(sh["op"])
        is_comm = _is_comm_op(op_name)
        is_main = not is_comm

        if phase == "prefill":
            # Prefill still aggregates layer-scaled compute (+ later prefill_scale)
            scaled = lat * layers if is_main else lat
            prefill_us += scaled
            detail = {
                **hit,
                "phase": phase,
                "is_main_op": is_main,
                "raw_latency_us": lat,
                "scaled_latency_us": scaled,
            }
        else:
            # Decode main ops: keep per-layer raw; ITL applies layers/ratio once on the sum
            if is_main:
                main_ops_decode_us += lat
                contrib = (lat * layers) / main_op_ratio
            else:
                contrib = 0.0
            detail = {
                **hit,
                "phase": phase,
                "is_main_op": is_main,
                "raw_latency_us": lat,
                "scaled_latency_us": contrib,
                "itl_contribution_us": contrib,
            }
        op_details.append(detail)

    prefill_us *= prefill_scale
    ttft_ms = prefill_us / 1000.0
    # ITL = (Σ main_op_decode_latency × num_layers) / main_op_ratio
    itl_us = (main_ops_decode_us * layers) / main_op_ratio
    itl_ms = itl_us / 1000.0
    tpot_ms = itl_ms / (1.0 + mtp_accept_rate) if mtp_accept_rate >= 0 else itl_ms

    # Real CSV is required path; only label msmodeling_csv when ALL ops hit real CSV.
    if used_real_flags and all(used_real_flags):
        perf_source = "msmodeling_csv"
    elif "msmodeling_mixed" in sources or (
        any(s.startswith("msmodeling_csv") for s in sources) and fallback_reasons
    ):
        perf_source = "msmodeling_mixed"
    elif "msmodeling_lfs_analytic" in sources:
        perf_source = "msmodeling_lfs_analytic"
    elif any("msmodeling" in s for s in sources):
        perf_source = "msmodeling_mapping"
    else:
        perf_source = "builtin_fallback"

    return {
        "concurrency": concurrency,
        "ttft_ms": round(ttft_ms, 4),
        "itl_ms": round(itl_ms, 4),
        "tpot_ms": round(tpot_ms, 4),
        "mtp_accept_rate": mtp_accept_rate,
        "num_hidden_layers": layers,
        "num_hidden_layers_source": layers_source,
        "main_op_ratio": main_op_ratio,
        "main_ops_decode_us": round(main_ops_decode_us, 4),
        "itl_formula": (
            "ITL = (Σ main_op_decode_latency_us × num_hidden_layers) / main_op_ratio; "
            "TPOT = ITL / (1 + mtp_accept_rate)"
        ),
        "perf_db_source": perf_source,
        "used_real_csv": bool(used_real_flags) and all(used_real_flags),
        "perf_db_fallback_reasons": fallback_reasons,
        "selected_backend": shapes_doc.get("selected_backend"),
        "shapes": shapes_doc,
        "op_details": op_details,
    }


def meets_slo(
    est: dict[str, Any],
    ttft_ms: float | None,
    tpot_ms: float | None,
) -> bool:
    if tpot_ms is not None and float(est["tpot_ms"]) > tpot_ms:
        return False
    if ttft_ms is not None and float(est["ttft_ms"]) > ttft_ms:
        return False
    return True


def max_concurrency_for_combo(
    model: str,
    profile: dict[str, Any],
    combo: dict[str, Any],
    input_len: int,
    output_len: int,
    mem_cap: int,
    ttft_ms: float | None,
    tpot_ms: float | None,
    mtp_accept_rate: float,
    msmodeling_repo: str | None,
    vllm_ascend_repo: str | None,
    builtin: dict[str, Any],
    allow_without_repos: bool,
    code_path: dict[str, Any] | None,
    ms_index: dict[str, Any] | None,
) -> dict[str, Any]:
    dp, tp, ep = int(combo["dp"]), int(combo["tp"]), int(combo.get("ep") or combo["dp"] * combo["tp"])
    hi = max(0, int(mem_cap))
    if hi <= 0:
        return {
            **{k: combo[k] for k in ("dp", "tp", "ep", "label") if k in combo},
            "max_concurrency_memory": 0,
            "max_concurrency_slo": 0,
            "feasible": False,
            "reason": "memory_cap_zero",
        }

    left, right = 1, hi
    best, best_est = 0, None
    while left <= right:
        mid = (left + right) // 2
        est = estimate_latencies_ms(
            model,
            profile,
            dp,
            tp,
            ep,
            input_len,
            output_len,
            mid,
            msmodeling_repo,
            vllm_ascend_repo,
            mtp_accept_rate,
            builtin,
            allow_without_repos=allow_without_repos,
            code_path=code_path,
            ms_index=ms_index,
        )
        if meets_slo(est, ttft_ms, tpot_ms):
            best = mid
            best_est = est
            left = mid + 1
        else:
            right = mid - 1

    if best_est is None and hi >= 1:
        best_est = estimate_latencies_ms(
            model,
            profile,
            dp,
            tp,
            ep,
            input_len,
            output_len,
            1,
            msmodeling_repo,
            vllm_ascend_repo,
            mtp_accept_rate,
            builtin,
            allow_without_repos=allow_without_repos,
            code_path=code_path,
            ms_index=ms_index,
        )

    return {
        "dp": dp,
        "tp": tp,
        "ep": ep,
        "label": combo.get("label", f"DP{dp}TP{tp}"),
        "max_concurrency_memory": hi,
        "max_concurrency_slo": best,
        "feasible": best > 0,
        "selected_backend": (best_est or {}).get("selected_backend"),
        "at_max_slo": {
            "ttft_ms": best_est.get("ttft_ms") if best_est else None,
            "itl_ms": best_est.get("itl_ms") if best_est else None,
            "tpot_ms": best_est.get("tpot_ms") if best_est else None,
            "num_hidden_layers": best_est.get("num_hidden_layers") if best_est else None,
            "num_hidden_layers_source": best_est.get("num_hidden_layers_source")
            if best_est
            else None,
            "main_op_ratio": best_est.get("main_op_ratio") if best_est else None,
            "main_ops_decode_us": best_est.get("main_ops_decode_us") if best_est else None,
            "itl_formula": best_est.get("itl_formula") if best_est else None,
            "perf_db_source": best_est.get("perf_db_source") if best_est else None,
            "used_real_csv": best_est.get("used_real_csv") if best_est else None,
            "perf_db_fallback_reasons": (
                best_est.get("perf_db_fallback_reasons") if best_est else None
            ),
            # Operator-level process values for tuning-process intermediate report
            "op_details": (best_est.get("op_details") if best_est else None) or [],
            "shapes": ((best_est.get("shapes") or {}).get("shapes") if best_est else None)
            or [],
        },
        "slo": {"ttft_ms": ttft_ms, "tpot_ms": tpot_ms},
    }


def estimate_all(
    parallel: dict[str, Any],
    kv: dict[str, Any],
    slo: dict[str, Any],
    msmodeling_repo: str | None = None,
    vllm_ascend_repo: str | None = None,
    mtp_accept_rate: float | None = None,
    main_op_ratio: float | None = None,
    allow_without_repos: bool = False,
) -> dict[str, Any]:
    if not allow_without_repos:
        if not vllm_ascend_repo or not Path(vllm_ascend_repo).exists():
            raise FileNotFoundError(
                "vllm-ascend repo required. Clone https://github.com/vllm-project/vllm-ascend"
            )
        if not msmodeling_repo or not Path(msmodeling_repo).exists():
            raise FileNotFoundError(
                "msmodeling repo required. Clone https://gitcode.com/Ascend/msmodeling"
            )

    builtin = load_builtin()
    if mtp_accept_rate is None:
        mtp_accept_rate = float(
            slo.get("mtp_accept_rate")
            if slo.get("mtp_accept_rate") is not None
            else builtin.get("default_mtp_accept_rate", 0.7)
        )
    if main_op_ratio is None:
        main_op_ratio = float(
            slo.get("main_op_ratio")
            if slo.get("main_op_ratio") is not None
            else builtin.get("default_main_op_ratio", DEFAULT_MAIN_OP_RATIO)
        )
    ttft_ms = slo.get("ttft_ms")
    tpot_ms = slo.get("tpot_ms")
    if tpot_ms is None:
        tpot_ms = 50.0

    profile = parallel.get("profile") or {}
    model = parallel.get("model") or ""
    input_len = int(parallel.get("input_len") or 0)
    output_len = int(parallel.get("output_len") or 0)
    layers, layers_source = resolve_num_hidden_layers(profile)

    code_path = None
    if vllm_ascend_repo and Path(vllm_ascend_repo).exists():
        code_path = asdict(
            analyze_vllm_ascend(
                model,
                vllm_ascend_repo,
                profile=profile,
                model_config=profile,
            )
        )

    ms_index = None
    if msmodeling_repo and Path(msmodeling_repo).exists():
        ms_index = index_msmodeling(msmodeling_repo)

    mem_by_key: dict[str, int] = {}
    for c in kv.get("combinations") or []:
        key = c.get("label") or f"DP{c['dp']}TP{c['tp']}"
        mem_by_key[key] = int(c.get("max_concurrency_memory") or 0)

    # Share calibrated / placeholder ratio across all binary-search estimates.
    builtin = dict(builtin)
    builtin["default_main_op_ratio"] = float(main_op_ratio)

    results = []
    for combo in parallel.get("combinations") or []:
        key = combo.get("label") or f"DP{combo['dp']}TP{combo['tp']}"
        mem_cap = mem_by_key.get(key, 0)
        results.append(
            max_concurrency_for_combo(
                model,
                profile,
                combo,
                input_len,
                output_len,
                mem_cap,
                float(ttft_ms) if ttft_ms is not None else None,
                float(tpot_ms) if tpot_ms is not None else None,
                float(mtp_accept_rate),
                msmodeling_repo,
                vllm_ascend_repo,
                builtin,
                allow_without_repos,
                code_path,
                ms_index,
            )
        )

    feasible = [r for r in results if r["feasible"]]
    recommended = None
    if feasible:
        recommended = max(feasible, key=lambda r: (r["max_concurrency_slo"], r["tp"]))

    # Aggregate latency source / fallback reasons across combos (prefer recommended).
    sample_est = None
    if recommended and recommended.get("at_max_slo"):
        sample_est = recommended["at_max_slo"]
    elif results:
        sample_est = (results[0].get("at_max_slo") or {})

    perf_source = (
        sample_est.get("perf_db_source")
        if sample_est
        else ("msmodeling_mapping" if ms_index else "builtin_fallback")
    )
    used_real = bool(sample_est.get("used_real_csv")) if sample_est else False
    fallback_reasons: list[str] = []
    if sample_est:
        for reason in sample_est.get("perf_db_fallback_reasons") or []:
            if reason not in fallback_reasons:
                fallback_reasons.append(reason)
    if not used_real and not fallback_reasons:
        if allow_without_repos:
            fallback_reasons.append(
                "allow_without_repos=true; skipped mandatory real CSV path (test-only)"
            )
        elif not ms_index:
            fallback_reasons.append("msmodeling index unavailable; cannot query real CSV")
        else:
            fallback_reasons.append(
                "real CSV latency not used; see op_details for per-kernel reasons"
            )

    return {
        "model": model,
        "code_path": code_path,
        "core_ops": (code_path or {}).get("core_ops"),
        "selected_backend": (code_path or {}).get("selected_backend"),
        "slo": {"ttft_ms": ttft_ms, "tpot_ms": tpot_ms, "other": slo.get("other")},
        "mtp_accept_rate": mtp_accept_rate,
        "main_op_ratio": float(main_op_ratio),
        "num_hidden_layers": layers,
        "num_hidden_layers_source": layers_source,
        "itl_formula": (
            "ITL = (Σ main_op_decode_latency_us × num_hidden_layers) / main_op_ratio; "
            "TPOT = ITL / (1 + mtp_accept_rate)"
        ),
        "combinations": results,
        "recommended": recommended,
        "perf_db_source": perf_source,
        "used_real_csv": used_real,
        "perf_db_fallback_reasons": fallback_reasons,
        "repos": {
            "vllm_ascend": vllm_ascend_repo,
            "msmodeling": msmodeling_repo,
            "msmodeling_device_dir": (ms_index or {}).get("device_dir"),
            "op_mapping_path": (ms_index or {}).get("op_mapping_path"),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parallel-json", required=True)
    ap.add_argument("--kv-json", required=True)
    ap.add_argument("--slo-json", required=True)
    ap.add_argument("--msmodeling-repo", default=None)
    ap.add_argument("--vllm-ascend-repo", default=None)
    ap.add_argument("--mtp-accept-rate", type=float, default=None)
    ap.add_argument(
        "--main-op-ratio",
        type=float,
        default=None,
        help="Main-operator time share in (0,1]; default 0.7 (placeholder until calibrated)",
    )
    ap.add_argument("--allow-without-repos", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    parallel = json.loads(Path(args.parallel_json).read_text(encoding="utf-8"))
    kv = json.loads(Path(args.kv_json).read_text(encoding="utf-8"))
    slo = json.loads(Path(args.slo_json).read_text(encoding="utf-8"))

    result = estimate_all(
        parallel,
        kv,
        slo,
        msmodeling_repo=args.msmodeling_repo,
        vllm_ascend_repo=args.vllm_ascend_repo,
        mtp_accept_rate=args.mtp_accept_rate,
        main_op_ratio=args.main_op_ratio,
        allow_without_repos=args.allow_without_repos,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
