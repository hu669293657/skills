#!/usr/bin/env python3
"""Orchestrate parallel-strategy tuning: SLO → clone → find → kv → slo concurrency."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_SKILLS_ROOT = _SCRIPTS.parent.parent  # configuration-tuning-skills/


def _run_py(script: Path, args: list[str]) -> None:
    cmd = [sys.executable, str(script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def _extract_field(text: str, cn_key: str) -> str | None:
    m = re.search(rf"[-*]\s*{re.escape(cn_key)}\s*[:：]\s*(.+)", text)
    if not m:
        # also try plain "key: value" lines in tables
        m = re.search(rf"\|\s*{re.escape(cn_key)}\s*\|\s*([^|]+)\|", text)
    if not m:
        return None
    return m.group(1).strip()


def load_baseline_summary(path: Path) -> dict[str, Any]:
    """Best-effort extract of fields from baseline-summary.md."""
    text = path.read_text(encoding="utf-8")
    out: dict[str, Any] = {"path": str(path)}
    mapping = {
        "model_name": ["模型名称", "model_name"],
        "quantization": ["量化格式", "quantization"],
        "num_npus": ["NPU卡数", "num_npus", "总卡数"],
        "device_type": ["设备类型", "device_type"],
        "deploy_strategy": ["部署策略", "deploy_strategy"],
        "input_seq_len": ["输入长度", "input_seq_len", "平均输入"],
        "output_seq_len": ["输出长度", "output_seq_len", "平均输出"],
    }
    for key, aliases in mapping.items():
        for a in aliases:
            val = _extract_field(text, a)
            if val:
                # strip markdown bold / code
                val = re.sub(r"[`*]", "", val).strip()
                if key in ("num_npus", "input_seq_len", "output_seq_len"):
                    m = re.search(r"\d+", val)
                    out[key] = int(m.group()) if m else val
                else:
                    out[key] = val
                break

    # DP/TP from launch-ish mentions
    m_tp = re.search(r"--tensor-parallel-size\s+(\d+)", text)
    m_dp = re.search(r"--data-parallel-size\s+(\d+)", text)
    if m_tp:
        out["tp"] = int(m_tp.group(1))
    if m_dp:
        out["dp"] = int(m_dp.group(1))
    return out


def write_tuning_process_report(
    out_md: Path,
    out_json: Path,
    case_dir: str,
    slo: dict[str, Any],
    parallel: dict[str, Any],
    kv: dict[str, Any],
    slo_conc: dict[str, Any],
    clone_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write intermediate process artifact with concrete calculation values.

    Deliverables:
      - tuning-process.json  (machine-readable process values)
      - tuning-process.md    (human-readable process report)
    """
    profile = parallel.get("profile") or {}
    input_len = parallel.get("input_len")
    output_len = parallel.get("output_len")
    budget = parallel.get("per_npu_budget_gb")
    weight_gb = profile.get("weight_gb")
    quant_bytes = profile.get("quant_bytes")
    params_b = profile.get("params_b")
    min_tp = parallel.get("min_tp")

    # Align KV rows by label
    kv_by_label = {
        (c.get("label") or f"DP{c.get('dp')}TP{c.get('tp')}"): c
        for c in (kv.get("combinations") or [])
    }
    slo_by_label = {
        (c.get("label") or f"DP{c.get('dp')}TP{c.get('tp')}"): c
        for c in (slo_conc.get("combinations") or [])
    }

    world_size = parallel.get("world_size", parallel.get("num_npus"))
    parallel_process = {
        "formula": {
            "quant_bytes": "BF16=2, W8A8=1, W4A8=0.5",
            "weight_gb": "params_b × quant_bytes",
            "min_tp": "ceil_pow2(weight_gb / 57.6), 57.6=64×0.95−3",
            "world_size": "num_cards × dies_per_card (A3=2, A2=1)",
            "dp": "world_size / tp",
            "ep": "dp × tp",
        },
        "inputs": {
            "model": parallel.get("model"),
            "quantization": parallel.get("quantization"),
            "device_type": parallel.get("device_type"),
            "num_npus": parallel.get("num_npus"),
            "num_cards": parallel.get("num_cards", parallel.get("num_npus")),
            "dies_per_card": parallel.get("dies_per_card", 1),
            "world_size": world_size,
            "input_len": input_len,
            "output_len": output_len,
            "params_b": params_b,
            "params_source": profile.get("params_source"),
            "quant_bytes": quant_bytes,
            "per_npu_budget_gb": budget,
            "current_dp_tp": parallel.get("current"),
        },
        "computed": {
            "weight_gb": weight_gb,
            "min_tp": min_tp,
            "min_tp_formula": parallel.get("min_tp_formula"),
            "min_tp_note": parallel.get("min_tp_note"),
            "topology_note": parallel.get("topology_note"),
            "world_size": world_size,
        },
        "combinations": [
            {
                "label": c.get("label"),
                "dp": c.get("dp"),
                "tp": c.get("tp"),
                "ep": c.get("ep"),
                "weight_per_npu_gb": c.get("weight_per_npu_gb"),
                "fits_weight_budget": c.get("fits_weight_budget"),
                "process": (
                    f"TP={c.get('tp')}, DP={world_size}/{c.get('tp')}={c.get('dp')}, "
                    f"EP={c.get('ep')}, weight_per_npu={weight_gb}/{c.get('tp')}="
                    f"{c.get('weight_per_npu_gb')}GB"
                ),
            }
            for c in (parallel.get("combinations") or [])
        ],
    }

    mem_defaults = kv.get("memory_defaults") or {}
    kv_process = {
        "formula": {
            "kv_bytes_per_token": (
                "2×layers×kv_heads×head_dim×dtype 或 MLA override 表"
            ),
            "workspace_gb": "workspace_base + context_k × per_1k",
            "budget_per_npu_gb": (
                "npu_hbm − weight_per_npu − workspace − hccl − system_reserved"
            ),
            "available_kv_gb": "max(0, budget_per_npu) × TP",
            "per_request_kv_gb": "(input_len + output_len) × kv_bytes_per_token / 1e9",
            "max_concurrency_memory": "floor(available_kv / per_request_kv) × DP",
        },
        "inputs": {
            "input_len": kv.get("input_len", input_len),
            "output_len": kv.get("output_len", output_len),
            "kv_bytes_per_token": kv.get("kv_bytes_per_token"),
            "kv_bytes_source": kv.get("kv_bytes_source"),
            "memory_defaults": mem_defaults,
            "seq_tokens_for_request": (
                None
                if input_len is None or output_len is None
                else int(input_len) + int(output_len)
            ),
        },
        "combinations": [],
    }
    for c in parallel.get("combinations") or []:
        label = c.get("label") or f"DP{c.get('dp')}TP{c.get('tp')}"
        k = kv_by_label.get(label, {})
        kv_process["combinations"].append(
            {
                "label": label,
                "dp": k.get("dp", c.get("dp")),
                "tp": k.get("tp", c.get("tp")),
                "weight_per_npu_gb": k.get("weight_per_npu_gb"),
                "workspace_gb": k.get("workspace_gb"),
                "hccl_gb": k.get("hccl_gb"),
                "budget_per_npu_gb": k.get("budget_per_npu_gb"),
                "available_kv_gb": k.get("available_kv_gb"),
                "per_request_kv_gb": k.get("per_request_kv_gb"),
                "max_concurrency_memory": k.get("max_concurrency_memory"),
                "process": (
                    f"budget={k.get('budget_per_npu_gb')}GB/NPU, "
                    f"available_kv={k.get('available_kv_gb')}GB, "
                    f"per_req={k.get('per_request_kv_gb')}GB, "
                    f"max_conc_mem=floor({k.get('available_kv_gb')}/"
                    f"{k.get('per_request_kv_gb')})×{k.get('dp', c.get('dp'))}="
                    f"{k.get('max_concurrency_memory')}"
                ),
            }
        )

    code_path = slo_conc.get("code_path") or {}
    recommended = slo_conc.get("recommended") or {}
    rec_label = recommended.get("label")
    rec_combo = slo_by_label.get(rec_label or "", {}) if rec_label else {}
    if not rec_combo and recommended:
        # fallback: match by dp/tp
        for c in slo_conc.get("combinations") or []:
            if c.get("dp") == recommended.get("dp") and c.get("tp") == recommended.get("tp"):
                rec_combo = c
                break
    rec_at = rec_combo.get("at_max_slo") or recommended.get("at_max_slo") or {}

    operator_core_path = {
        "model": code_path.get("model") or parallel.get("model"),
        "repo": code_path.get("repo"),
        "family": code_path.get("family"),
        "use_mla": code_path.get("use_mla"),
        "use_sparse": code_path.get("use_sparse"),
        "use_compress": code_path.get("use_compress"),
        "is_moe": code_path.get("is_moe"),
        "selected_backend": code_path.get("selected_backend") or slo_conc.get("selected_backend"),
        "core_ops": code_path.get("core_ops") or slo_conc.get("core_ops") or [],
        "tc_ops": code_path.get("tc_ops") or [],
        "moe_kernels": code_path.get("moe_kernels") or [],
        "comm_kernels": code_path.get("comm_kernels") or [],
        "evidence_files": code_path.get("evidence_files") or [],
        "dispatch_hints": code_path.get("dispatch_hints") or [],
        "arch_sources": code_path.get("arch_sources") or [],
        "matched_model_files": code_path.get("matched_model_files") or [],
        "analysis_mode": code_path.get("analysis_mode"),
    }

    op_rows: list[dict[str, Any]] = []
    for d in rec_at.get("op_details") or []:
        details = d.get("details") or []
        kernel_types = d.get("kernel_types") or [x.get("kernel_type") for x in details if x.get("kernel_type")]
        reasons = d.get("fallback_reasons") or []
        detail_reasons = [x.get("fallback_reason") for x in details if x.get("fallback_reason")]
        reason = "; ".join([str(x) for x in (reasons or detail_reasons) if x]) or None
        csv_sources = [
            str(x.get("source"))
            for x in details
            if x.get("source") and str(x.get("source")).startswith("msmodeling_csv")
        ]
        op_rows.append(
            {
                "op": d.get("op"),
                "phase": d.get("phase"),
                "tokens": d.get("tokens"),
                "latency_us": d.get("latency_us"),
                "scaled_latency_us": d.get("scaled_latency_us"),
                "source": d.get("source"),
                "used_real_csv": d.get("used_real_csv"),
                "kernel_types": kernel_types,
                "csv_sources": csv_sources,
                "fallback_reason": reason,
                "details": details,
            }
        )

    operator_latency = {
        "context": {
            "label": rec_combo.get("label") or recommended.get("label"),
            "dp": rec_combo.get("dp", recommended.get("dp")),
            "tp": rec_combo.get("tp", recommended.get("tp")),
            "ep": rec_combo.get("ep", recommended.get("ep")),
            "concurrency": rec_combo.get("max_concurrency_slo", recommended.get("max_concurrency_slo")),
            "perf_db_source": rec_at.get("perf_db_source") or slo_conc.get("perf_db_source"),
            "used_real_csv": rec_at.get("used_real_csv")
            if rec_at.get("used_real_csv") is not None
            else slo_conc.get("used_real_csv"),
            "perf_db_fallback_reasons": rec_at.get("perf_db_fallback_reasons")
            or slo_conc.get("perf_db_fallback_reasons")
            or [],
            "ttft_ms": rec_at.get("ttft_ms"),
            "itl_ms": rec_at.get("itl_ms"),
            "tpot_ms": rec_at.get("tpot_ms"),
        },
        "ops": op_rows,
        "shapes": rec_at.get("shapes") or [],
    }

    slo_process = {
        "formula": {
            "backend_dispatch": "(use_mla, use_sparse, use_compress) → ASCEND/MLA/SFA/DSA",
            "itl_ms": (
                "(Σ main_op_decode_latency_us × num_hidden_layers) / main_op_ratio "
                "(main ops = non-comm core_ops; ratio default 0.7 placeholder)"
            ),
            "tpot_ms": "ITL / (1 + mtp_accept_rate)",
            "ttft_ms": "sum(layer_scaled op latency) for prefill × prefill_scale",
            "max_concurrency_slo": (
                "binary search in [1, max_concurrency_memory] s.t. TPOT/TTFT constraints"
            ),
        },
        "inputs": {
            "ttft_ms": slo.get("ttft_ms"),
            "tpot_ms": slo.get("tpot_ms"),
            "other": slo.get("other"),
            "defaults_applied": slo.get("defaults_applied"),
            "mtp_accept_rate": slo_conc.get("mtp_accept_rate"),
            "main_op_ratio": slo_conc.get("main_op_ratio"),
            "num_hidden_layers": slo_conc.get("num_hidden_layers"),
            "num_hidden_layers_source": slo_conc.get("num_hidden_layers_source"),
            "selected_backend": slo_conc.get("selected_backend"),
            "family": code_path.get("family"),
            "core_ops": slo_conc.get("core_ops") or code_path.get("core_ops"),
            "perf_db_source": slo_conc.get("perf_db_source"),
            "used_real_csv": slo_conc.get("used_real_csv"),
            "perf_db_fallback_reasons": slo_conc.get("perf_db_fallback_reasons"),
            "msmodeling_csv_ensure": slo_conc.get("msmodeling_csv_ensure"),
            "use_mla": code_path.get("use_mla"),
            "use_sparse": code_path.get("use_sparse"),
        },
        "operator_core_path": operator_core_path,
        "operator_latency": operator_latency,
        "combinations": [],
        "recommended": recommended,
    }
    for c in parallel.get("combinations") or []:
        label = c.get("label") or f"DP{c.get('dp')}TP{c.get('tp')}"
        s = slo_by_label.get(label, {})
        at = s.get("at_max_slo") or {}
        mem_cap = (kv_by_label.get(label) or {}).get("max_concurrency_memory")
        slo_process["combinations"].append(
            {
                "label": label,
                "dp": s.get("dp", c.get("dp")),
                "tp": s.get("tp", c.get("tp")),
                "ep": s.get("ep", c.get("ep")),
                "max_concurrency_memory": s.get("max_concurrency_memory", mem_cap),
                "max_concurrency_slo": s.get("max_concurrency_slo"),
                "feasible": s.get("feasible"),
                "at_max_slo": {
                    "ttft_ms": at.get("ttft_ms"),
                    "itl_ms": at.get("itl_ms"),
                    "tpot_ms": at.get("tpot_ms"),
                    "perf_db_source": at.get("perf_db_source"),
                    "used_real_csv": at.get("used_real_csv"),
                },
                "process": (
                    f"search concurrency≤{s.get('max_concurrency_memory', mem_cap)} "
                    f"under TPOT≤{slo.get('tpot_ms')}ms"
                    + (
                        f"/TTFT≤{slo.get('ttft_ms')}ms"
                        if slo.get("ttft_ms") is not None
                        else "/TTFT=unlimited"
                    )
                    + f" → max_conc_slo={s.get('max_concurrency_slo')}, "
                    f"at_max tpot={at.get('tpot_ms')}ms itl={at.get('itl_ms')}ms "
                    f"ttft={at.get('ttft_ms')}ms, feasible={s.get('feasible')}"
                ),
            }
        )

    report = {
        "producer": "serving-tuning-subagent",
        "phase": 2,
        "artifact": "tuning-process",
        "case_dir": case_dir,
        "clone": clone_info,
        "parallel_strategy_process": parallel_process,
        "kv_memory_concurrency_process": kv_process,
        "slo_concurrency_process": slo_process,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Markdown process report
    md: list[str] = [
        "---",
        "producer: serving-tuning-subagent",
        "phase: 2",
        "artifact: tuning-process",
        "---",
        "",
        "# Phase 2 中间过程报告（并行策略 / KV / SLO）",
        "",
        f"- case_dir: `{case_dir}`",
        f"- 机器可读副本: `tuning/tuning-process.json`",
        "",
        "## A. 并行策略过程值",
        "",
        "### A.1 公式",
        "",
        f"- quant_bytes: `{parallel_process['formula']['quant_bytes']}`",
        f"- weight_gb: `{parallel_process['formula']['weight_gb']}`",
        f"- min_tp: `{parallel_process['formula']['min_tp']}`",
        f"- world_size: `{parallel_process['formula']['world_size']}`",
        f"- DP/EP: `{parallel_process['formula']['dp']}` / `{parallel_process['formula']['ep']}`",
        "",
        "### A.2 输入",
        "",
    ]
    for k, v in parallel_process["inputs"].items():
        md.append(f"- {k}: `{v}`")
    md += [
        "",
        "### A.3 计算结果",
        "",
        f"- weight_gb: `{parallel_process['computed']['weight_gb']}`",
        f"- min_tp: `{parallel_process['computed']['min_tp']}`",
        f"- world_size: `{parallel_process['computed'].get('world_size')}`",
        f"- note: {parallel_process['computed'].get('min_tp_note')}",
        f"- topology: {parallel_process['computed'].get('topology_note')}",
        "",
        "### A.4 合法组合过程",
        "",
        "| label | DP | TP | EP | weight_per_npu_gb | fits | process |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in parallel_process["combinations"]:
        md.append(
            f"| {c['label']} | {c['dp']} | {c['tp']} | {c['ep']} | "
            f"{c['weight_per_npu_gb']} | {c['fits_weight_budget']} | {c['process']} |"
        )

    md += [
        "",
        "## B. KV 内存并发上限过程值",
        "",
        "### B.1 公式",
        "",
    ]
    for k, v in kv_process["formula"].items():
        md.append(f"- {k}: `{v}`")
    md += ["", "### B.2 输入", ""]
    for k, v in kv_process["inputs"].items():
        md.append(f"- {k}: `{v}`")
    md += [
        "",
        "### B.3 各并行策略过程",
        "",
        "| label | available_kv_gb | per_request_kv_gb | max_concurrency_memory | process |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in kv_process["combinations"]:
        md.append(
            f"| {c['label']} | {c.get('available_kv_gb')} | {c.get('per_request_kv_gb')} | "
            f"{c.get('max_concurrency_memory')} | {c.get('process')} |"
        )

    md += [
        "",
        "## C. SLO 并发过程值",
        "",
        "### C.1 公式",
        "",
    ]
    for k, v in slo_process["formula"].items():
        md.append(f"- {k}: `{v}`")
    md += ["", "### C.2 输入 / 数据源", ""]
    for k, v in slo_process["inputs"].items():
        # keep ensure blob compact in markdown
        if k == "msmodeling_csv_ensure" and isinstance(v, dict):
            md.append(
                f"- msmodeling_csv_ensure.real_csv_ready: `{v.get('real_csv_ready')}`"
            )
            md.append(
                f"- msmodeling_csv_ensure.fallback_reasons: `{v.get('fallback_reasons')}`"
            )
            continue
        md.append(f"- {k}: `{v}`")

    ocp = slo_process.get("operator_core_path") or {}
    md += [
        "",
        "### C.3 算子核心路径（vllm-ascend）",
        "",
        f"- model: `{ocp.get('model')}`",
        f"- repo: `{ocp.get('repo')}`",
        f"- family: `{ocp.get('family')}`",
        f"- use_mla / use_sparse / use_compress: "
        f"`{ocp.get('use_mla')}` / `{ocp.get('use_sparse')}` / `{ocp.get('use_compress')}`",
        f"- is_moe: `{ocp.get('is_moe')}`",
        f"- selected_backend: `{ocp.get('selected_backend')}`",
        f"- analysis_mode: `{ocp.get('analysis_mode')}`",
        f"- core_ops: `{ocp.get('core_ops')}`",
        f"- tc_ops: `{ocp.get('tc_ops')}`",
        f"- moe_kernels: `{ocp.get('moe_kernels')}`",
        f"- comm_kernels: `{ocp.get('comm_kernels')}`",
        f"- evidence_files: `{ocp.get('evidence_files')}`",
        f"- dispatch_hints: `{ocp.get('dispatch_hints')}`",
        f"- arch_sources: `{ocp.get('arch_sources')}`",
        f"- matched_model_files: `{ocp.get('matched_model_files')}`",
    ]

    olat = slo_process.get("operator_latency") or {}
    olat_ctx = olat.get("context") or {}
    md += [
        "",
        "### C.4 算子耗时数据（msmodeling @ 推荐组合）",
        "",
        f"- label: `{olat_ctx.get('label')}`",
        f"- DP/TP/EP: `{olat_ctx.get('dp')}/{olat_ctx.get('tp')}/{olat_ctx.get('ep')}`",
        f"- concurrency: `{olat_ctx.get('concurrency')}`",
        f"- perf_db_source: `{olat_ctx.get('perf_db_source')}`",
        f"- used_real_csv: `{olat_ctx.get('used_real_csv')}`",
        f"- perf_db_fallback_reasons: `{olat_ctx.get('perf_db_fallback_reasons')}`",
        f"- at_max ttft/itl/tpot (ms): "
        f"`{olat_ctx.get('ttft_ms')}` / `{olat_ctx.get('itl_ms')}` / `{olat_ctx.get('tpot_ms')}`",
        "",
        "| op | phase | tokens | latency_us | scaled_latency_us | source | used_real_csv | kernel_types | fallback_reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    ops = olat.get("ops") or []
    if not ops:
        md.append("| _(no op_details)_ | | | | | | | | |")
    for op in ops:
        md.append(
            f"| {op.get('op')} | {op.get('phase')} | {op.get('tokens')} | "
            f"{op.get('latency_us')} | {op.get('scaled_latency_us')} | "
            f"{op.get('source')} | {op.get('used_real_csv')} | "
            f"{op.get('kernel_types')} | {op.get('fallback_reason') or ''} |"
        )
    # Expand per-kernel details when present
    detail_rows: list[str] = []
    for op in ops:
        for det in op.get("details") or []:
            detail_rows.append(
                f"| {op.get('op')} | {det.get('kernel_type')} | {det.get('latency_us')} | "
                f"{det.get('source')} | {det.get('fallback_reason') or ''} |"
            )
    if detail_rows:
        md += [
            "",
            "#### C.4.1 kernel detail",
            "",
            "| op | kernel_type | latency_us | source | fallback_reason |",
            "| --- | --- | --- | --- | --- |",
            *detail_rows,
        ]

    md += [
        "",
        "### C.5 各并行策略过程",
        "",
        "| label | mem_cap | max_concurrency_slo | tpot@max | itl@max | ttft@max | feasible | process |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in slo_process["combinations"]:
        at = c.get("at_max_slo") or {}
        md.append(
            f"| {c['label']} | {c.get('max_concurrency_memory')} | {c.get('max_concurrency_slo')} | "
            f"{at.get('tpot_ms')} | {at.get('itl_ms')} | {at.get('ttft_ms')} | "
            f"{c.get('feasible')} | {c.get('process')} |"
        )

    rec = slo_process.get("recommended")
    md += ["", "## D. 推荐（由上述过程得出）", ""]
    if rec:
        at = rec.get("at_max_slo") or {}
        # Avoid dumping huge op_details into D; point to C.4
        at_summary = {
            "ttft_ms": at.get("ttft_ms"),
            "itl_ms": at.get("itl_ms"),
            "tpot_ms": at.get("tpot_ms"),
            "perf_db_source": at.get("perf_db_source"),
            "used_real_csv": at.get("used_real_csv"),
        }
        md += [
            f"- label: `{rec.get('label')}`",
            f"- DP/TP/EP: `{rec.get('dp')}/{rec.get('tp')}/{rec.get('ep')}`",
            f"- max_concurrency_slo: `{rec.get('max_concurrency_slo')}`",
            f"- at_max_slo: `{at_summary}`",
            "- operator_core_path / operator_latency: 见 §C.3 / §C.4",
        ]
    else:
        md.append("- 无可行推荐")
    md += [
        "",
        "> 本文件为 Phase 2 **中间过程产物**；最终摘要见 `tuning-status.md`。",
        "",
    ]
    out_md.write_text("\n".join(md), encoding="utf-8")
    return report


def write_tuning_status(
    out_path: Path,
    case_dir: str,
    slo: dict[str, Any],
    parallel: dict[str, Any],
    kv: dict[str, Any],
    slo_conc: dict[str, Any],
    clone_info: dict[str, Any],
) -> None:
    rec = slo_conc.get("recommended")
    lines = [
        "---",
        "producer: serving-tuning-subagent",
        "phase: 2",
        "status: completed",
        "skill: serving-parallel-strategy-tuning",
        "---",
        "",
        "# 服务化调优状态（并行策略）",
        "",
        f"- case_dir: `{case_dir}`",
        f"- perf_db_source: `{slo_conc.get('perf_db_source')}`",
        f"- used_real_csv: `{slo_conc.get('used_real_csv')}`",
        f"- selected_backend: `{slo_conc.get('selected_backend')}`",
        f"- process_report: `tuning/tuning-process.md`",
        "",
        "## 0. 算子耗时数据源（必须优先真实 CSV）",
        "",
        f"- required: `msmodeling_csv` (real Duration(us) from profiling_database)",
        f"- actual: `{slo_conc.get('perf_db_source')}`",
        f"- used_real_csv: `{slo_conc.get('used_real_csv')}`",
    ]
    reasons = slo_conc.get("perf_db_fallback_reasons") or []
    if slo_conc.get("used_real_csv"):
        lines.append("- fallback_reasons: _(none — real CSV used)_")
    elif reasons:
        lines.append("- fallback_reasons:")
        for r in reasons:
            lines.append(f"  - {r}")
    else:
        lines.append(
            "- fallback_reasons: _(missing — non-real source without reason; treat as bug)_"
        )
    lines += [
        "",
        "## 1. SLO 约束",
        "",
        f"- TTFT (ms): {slo.get('ttft_ms')}",
        f"- TPOT (ms): {slo.get('tpot_ms')}",
        f"- 其他: {slo.get('other')}",
        f"- defaults_applied: {slo.get('defaults_applied')}",
        "",
        "## 2. 源码仓库与代码路径",
        "",
        f"- vllm-ascend: `{clone_info.get('vllm_ascend')}`",
        f"- msmodeling: `{clone_info.get('msmodeling')}`",
        f"- selected_backend: `{(slo_conc.get('code_path') or {}).get('selected_backend') or slo_conc.get('selected_backend')}`",
        f"- core_ops: `{slo_conc.get('core_ops') or (slo_conc.get('code_path') or {}).get('core_ops')}`",
        f"- evidence_files: `{(slo_conc.get('code_path') or {}).get('evidence_files')}`",
        "- detail: `tuning/tuning-process.md` §C.3 / §C.4",
        "",
        "## 3. 并行策略组合",
        "",
        f"- model: {parallel.get('model')}",
        f"- device_type: {parallel.get('device_type')}",
        f"- num_cards / dies_per_card / world_size: "
        f"{parallel.get('num_cards', parallel.get('num_npus'))} / "
        f"{parallel.get('dies_per_card', 1)} / {parallel.get('world_size', parallel.get('num_npus'))}",
        f"- weight_gb: {(parallel.get('profile') or {}).get('weight_gb')}",
        f"- min_tp: {parallel.get('min_tp')}",
        f"- combinations: {len(parallel.get('combinations') or [])}",
        "",
    ]
    for c in parallel.get("combinations") or []:
        lines.append(f"  - {c.get('label')}: weight_per_npu={c.get('weight_per_npu_gb')}GB")
    lines += [
        "",
        "## 4. KV Cache 容量",
        "",
        f"- kv_bytes_per_token: {kv.get('kv_bytes_per_token')} ({kv.get('kv_bytes_source')})",
        "",
    ]
    for c in kv.get("combinations") or []:
        lines.append(
            f"  - {c.get('label')}: available_kv={c.get('available_kv_gb')}GB, "
            f"max_conc_mem={c.get('max_concurrency_memory')}"
        )
    lines += [
        "",
        "## 5. SLO 并发",
        "",
    ]
    for c in slo_conc.get("combinations") or []:
        lines.append(
            f"  - {c.get('label')}: max_conc_slo={c.get('max_concurrency_slo')}, "
            f"feasible={c.get('feasible')}"
        )
    lines += ["", "## 6. 推荐配置", ""]
    if rec:
        lines += [
            f"- label: `{rec.get('label')}`",
            f"- DP/TP/EP: {rec.get('dp')}/{rec.get('tp')}/{rec.get('ep')}",
            f"- max_concurrency_slo: {rec.get('max_concurrency_slo')}",
            f"- at_max_slo: {rec.get('at_max_slo')}",
        ]
    else:
        lines.append("- 无可行组合（内存或 SLO 均不满足）")
    lines += [
        "",
        "## 7. 产物路径",
        "",
        "- `tuning/slo-constraints.json`",
        "- `tuning/parallel-strategies.json`",
        "- `tuning/kv-capacity.json`",
        "- `tuning/slo-concurrency.json`",
        "- `tuning/tuning-process.md` / `tuning/tuning-process.json`（中间过程值）",
        "",
        "> 本阶段为离线估算，未部署服务，未修改 baseline-launch.sh。",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _ensure_repos_linked(repos: Path, probe_root: Path | None = None) -> None:
    """If skip-clone and repos missing, optionally link from a probe checkout."""
    if probe_root is None:
        # default: repo .tmp/repo_probe next to configuration-tuning-skills
        probe_root = _SKILLS_ROOT.parent / ".tmp" / "repo_probe"
    for name in ("vllm-ascend", "msmodeling"):
        dest = repos / name
        src = probe_root / name
        if dest.exists() or not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.symlink_to(src.resolve())
        except OSError:
            # fallback copy is too heavy; leave missing
            pass


def resolve_workdir(case_dir: Path, workdir: Path | None = None) -> Path:
    """Resolve pipeline workdir. Default: parent of cases/, else parent of case_dir."""
    if workdir is not None:
        return workdir.resolve()
    case_dir = case_dir.resolve()
    if case_dir.parent.name == "cases":
        return case_dir.parent.parent
    return case_dir.parent


def resolve_repos_dir(
    case_dir: Path,
    workdir: Path | None = None,
    repos_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Repos live under workdir in a dedicated directory: {workdir}/repos/."""
    wd = resolve_workdir(case_dir, workdir)
    if repos_dir is not None:
        return wd, repos_dir.resolve()
    return wd, (wd / "repos").resolve()


def orchestrate(
    case_dir: Path,
    config_md: Path,
    baseline_summary: Path | None,
    skip_clone: bool = False,
    interactive_slo: Path | None = None,
    model_config: Path | None = None,
    no_msmodeling: bool = False,
    allow_without_repos: bool = False,
    workdir: Path | None = None,
    repos_dir: Path | None = None,
) -> dict[str, Any]:
    tuning = case_dir / "tuning"
    tuning.mkdir(parents=True, exist_ok=True)
    workdir, repos = resolve_repos_dir(case_dir, workdir=workdir, repos_dir=repos_dir)
    repos.mkdir(parents=True, exist_ok=True)

    # 1) SLO
    slo_script = _SCRIPTS / "resolve_slo_constraints.py"
    slo_out = tuning / "slo-constraints.json"
    slo_args = ["--config", str(config_md), "--out", str(slo_out), "--json"]
    if interactive_slo:
        slo_args += ["--interactive-json", str(interactive_slo)]
    _run_py(slo_script, slo_args)
    slo = json.loads(slo_out.read_text(encoding="utf-8"))

    # 2) Clone into {workdir}/repos/ (shared across cases; not under case tuning/)
    clone_script = _SCRIPTS / "clone_repos.py"
    clone_out = tuning / "clone-repos.json"
    # Always --allow-missing so JSON+warning are written; we enforce hard-fail below.
    clone_args = [
        "--repos-dir",
        str(repos),
        "--out",
        str(clone_out),
        "--json",
        "--allow-missing",
    ]
    if skip_clone:
        clone_args.append("--skip-clone")
    if no_msmodeling:
        clone_args.append("--no-msmodeling")
    _run_py(clone_script, clone_args)
    clone_info = json.loads(clone_out.read_text(encoding="utf-8"))
    clone_info["workdir"] = str(workdir)
    clone_info["repos_dir"] = str(repos)
    clone_out.write_text(json.dumps(clone_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if clone_info.get("lfs_warning"):
        print(clone_info["lfs_warning"], file=sys.stderr)

    if not clone_info.get("ok") and not allow_without_repos:
        warning = clone_info.get("warning") or (
            "【警告】vllm-ascend / msmodeling 下载或准备失败；"
            f"请手动放到 {repos}/ 后重新发起。"
        )
        # Also write a dedicated warning file under workdir for the user.
        warn_path = workdir / "repos-clone.warning.md"
        warn_path.write_text(warning + "\n", encoding="utf-8")
        raise RuntimeError(warning + f"\n\n(详见 `{warn_path}` / `{clone_out}`)")

    # Resolve model params from config + optional baseline
    # Prefer deploy-config via find script; enrich from baseline if needed
    find_script = (
        _SKILLS_ROOT / "find-possible-parallel-strategy" / "scripts" / "find_parallel_strategies.py"
    )
    parallel_out = tuning / "parallel-strategies.json"
    find_args = ["--config", str(config_md), "--out", str(parallel_out), "--json"]
    # Prefer Phase-0 ModelScope download at {workdir}/model_config.json
    resolved_model_config: Path | None = None
    for cand in (
        model_config,
        workdir / "model_config.json",
        case_dir / "model_config.json",
    ):
        if cand is not None and Path(cand).exists():
            resolved_model_config = Path(cand)
            break
    if resolved_model_config is not None:
        find_args += ["--model-config", str(resolved_model_config)]

    # If config missing fields, fill from baseline summary
    if baseline_summary and baseline_summary.exists():
        base = load_baseline_summary(baseline_summary)
        # re-parse config; if model missing pass CLI overrides
        from importlib.util import module_from_spec, spec_from_file_location

        dep_path = _SKILLS_ROOT / "find-possible-parallel-strategy" / "scripts" / "deploy_config.py"
        spec = spec_from_file_location("deploy_config_orch", dep_path)
        assert spec and spec.loader
        mod = module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg = mod.parse_deploy_config_md(config_md)
        if not cfg.get("model") and base.get("model_name"):
            find_args += ["--model", str(base["model_name"])]
        if not cfg.get("quantization") and base.get("quantization"):
            find_args += ["--quantization", str(base["quantization"])]
        if not cfg.get("num_npus") and base.get("num_npus"):
            find_args += ["--num-npus", str(base["num_npus"])]
        if not cfg.get("device_type") and base.get("device_type"):
            find_args += ["--device-type", str(base["device_type"])]
        if cfg.get("input_len") is None and base.get("input_seq_len"):
            find_args += ["--input-len", str(base["input_seq_len"])]
        if cfg.get("output_len") is None and base.get("output_seq_len"):
            find_args += ["--output-len", str(base["output_seq_len"])]
        if base.get("dp") is not None:
            find_args += ["--dp", str(base["dp"])]
        if base.get("tp") is not None:
            find_args += ["--tp", str(base["tp"])]

    _run_py(find_script, find_args)
    parallel = json.loads(parallel_out.read_text(encoding="utf-8"))

    # 3) KV
    kv_script = _SKILLS_ROOT / "serving-kv-cache-capacity" / "scripts" / "evaluate_kv_capacity.py"
    kv_out = tuning / "kv-capacity.json"
    _run_py(kv_script, ["--parallel-json", str(parallel_out), "--out", str(kv_out), "--json"])
    kv = json.loads(kv_out.read_text(encoding="utf-8"))

    # 4) SLO concurrency (strong repo dependency)
    if skip_clone:
        _ensure_repos_linked(repos)

    vllm_repo = repos / "vllm-ascend"
    ms_repo = repos / "msmodeling"
    code_path_out = tuning / "code-path.json"
    have_vllm = vllm_repo.exists() and (vllm_repo / "vllm_ascend").is_dir()
    have_ms = ms_repo.exists() and (
        ms_repo / "tensor_cast" / "performance_model" / "profiling_database" / "data"
    ).is_dir()

    if not allow_without_repos and (not have_vllm or not have_ms):
        warning = clone_info.get("warning") or (
            "【警告】serving-slo-concurrency 需要本地 vllm-ascend 与 msmodeling。\n"
            f"- vllm_ok={have_vllm} path={vllm_repo}\n"
            f"- ms_ok={have_ms} path={ms_repo}\n"
            f"请重新运行 clone（去掉 --skip-clone），或手动放置到 `{repos}/` 后重试。"
        )
        warn_path = workdir / "repos-clone.warning.md"
        warn_path.write_text(warning + "\n", encoding="utf-8")
        raise RuntimeError(warning + f"\n\n(详见 `{warn_path}`)")

    # Analyze code path first
    code_path: dict[str, Any] = {}
    if have_vllm:
        analyze_script = (
            _SKILLS_ROOT / "serving-slo-concurrency" / "scripts" / "analyze_vllm_ascend_path.py"
        )
        model_name = str(parallel.get("model") or "unknown")
        _run_py(
            analyze_script,
            [
                "--model",
                model_name,
                "--vllm-ascend-repo",
                str(vllm_repo),
                "--profile-json",
                str(parallel_out),
                "--out",
                str(code_path_out),
                "--json",
            ],
        )
        if code_path_out.exists():
            code_path = json.loads(code_path_out.read_text(encoding="utf-8"))

    # Mandatory: materialize real msmodeling CSV before latency lookup.
    ensure_info: dict[str, Any] | None = None
    if have_ms:
        ensure_script = (
            _SKILLS_ROOT / "serving-slo-concurrency" / "scripts" / "ensure_msmodeling_csv.py"
        )
        ensure_out = tuning / "msmodeling-csv-ensure.json"
        core_ops = code_path.get("core_ops") or []
        ensure_args = [
            "--msmodeling-repo",
            str(ms_repo),
            "--out",
            str(ensure_out),
            "--json",
        ]
        if core_ops:
            ensure_args += ["--kernels", ",".join(str(x) for x in core_ops)]
        try:
            _run_py(ensure_script, ensure_args)
            ensure_info = json.loads(ensure_out.read_text(encoding="utf-8"))
        except RuntimeError as e:
            ensure_info = {
                "real_csv_ready": False,
                "fallback_reasons": [f"ensure_msmodeling_csv failed: {e}"],
            }
            ensure_out.write_text(json.dumps(ensure_info, ensure_ascii=False, indent=2) + "\n")

    slo_script2 = _SKILLS_ROOT / "serving-slo-concurrency" / "scripts" / "estimate_slo_concurrency.py"
    slo_conc_out = tuning / "slo-concurrency.json"
    slo_args2 = [
        "--parallel-json",
        str(parallel_out),
        "--kv-json",
        str(kv_out),
        "--slo-json",
        str(slo_out),
        "--out",
        str(slo_conc_out),
        "--json",
    ]
    if have_vllm:
        slo_args2 += ["--vllm-ascend-repo", str(vllm_repo)]
    if have_ms:
        slo_args2 += ["--msmodeling-repo", str(ms_repo)]
    if allow_without_repos and (not have_vllm or not have_ms):
        slo_args2.append("--allow-without-repos")
    _run_py(slo_script2, slo_args2)
    slo_conc = json.loads(slo_conc_out.read_text(encoding="utf-8"))
    if ensure_info is not None:
        slo_conc["msmodeling_csv_ensure"] = ensure_info
        # Merge ensure-level reasons when real CSV still unused.
        if not slo_conc.get("used_real_csv"):
            merged = list(slo_conc.get("perf_db_fallback_reasons") or [])
            for r in ensure_info.get("fallback_reasons") or []:
                if r not in merged:
                    merged.append(r)
            if not ensure_info.get("real_csv_ready") and not merged:
                merged.append(
                    "ensure_msmodeling_csv did not materialize real CSVs "
                    "(git-lfs unavailable and/or GitCode LFS fetch failed)"
                )
            slo_conc["perf_db_fallback_reasons"] = merged
        slo_conc_out.write_text(
            json.dumps(slo_conc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    status_path = tuning / "tuning-status.md"
    write_tuning_status(
        status_path,
        str(case_dir),
        slo,
        parallel,
        kv,
        slo_conc,
        clone_info,
    )
    process_md = tuning / "tuning-process.md"
    process_json = tuning / "tuning-process.json"
    write_tuning_process_report(
        process_md,
        process_json,
        str(case_dir),
        slo,
        parallel,
        kv,
        slo_conc,
        clone_info,
    )

    return {
        "case_dir": str(case_dir),
        "workdir": str(workdir),
        "repos_dir": str(repos),
        "slo_constraints": str(slo_out),
        "parallel_strategies": str(parallel_out),
        "kv_capacity": str(kv_out),
        "slo_concurrency": str(slo_conc_out),
        "code_path": str(code_path_out) if code_path_out.exists() else None,
        "tuning_process_md": str(process_md),
        "tuning_process_json": str(process_json),
        "tuning_status": str(status_path),
        "clone": clone_info,
        "recommended": slo_conc.get("recommended"),
        "selected_backend": slo_conc.get("selected_backend"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--config", required=True, help="deploy-config.md")
    ap.add_argument(
        "--workdir",
        default=None,
        help="Pipeline workdir; clones go to {workdir}/repos/ (default: infer from case-dir)",
    )
    ap.add_argument(
        "--repos-dir",
        default=None,
        help="Override clone destination (default: {workdir}/repos)",
    )
    ap.add_argument("--baseline-summary", default=None)
    ap.add_argument("--model-config", default=None)
    ap.add_argument("--interactive-slo", default=None)
    ap.add_argument("--skip-clone", action="store_true")
    ap.add_argument("--no-msmodeling", action="store_true")
    ap.add_argument(
        "--allow-without-repos",
        action="store_true",
        help="Test-only: allow SLO step without vllm-ascend/msmodeling clones",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = orchestrate(
        case_dir=Path(args.case_dir),
        config_md=Path(args.config),
        baseline_summary=Path(args.baseline_summary) if args.baseline_summary else None,
        skip_clone=args.skip_clone,
        interactive_slo=Path(args.interactive_slo) if args.interactive_slo else None,
        model_config=Path(args.model_config) if args.model_config else None,
        no_msmodeling=args.no_msmodeling,
        allow_without_repos=args.allow_without_repos,
        workdir=Path(args.workdir) if args.workdir else None,
        repos_dir=Path(args.repos_dir) if args.repos_dir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
