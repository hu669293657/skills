#!/usr/bin/env python3
"""Analyze vllm-ascend source to discover attention/MoE code path for a model.

Generalized across model families (Qwen / GLM / DeepSeek / MiniMax / Llama / …).
Backend selection mirrors vllm_ascend.platform.NPUPlatform.get_attn_backend_cls:
  (use_mla, use_sparse, use_compress) → ASCEND / MLA / SFA / DSA.

Requires a local clone of https://github.com/vllm-project/vllm-ascend
(GitCode fallback: https://gitcode.com/Ascend/vllm-ascend).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


ATTENTION_BACKEND_FILES = {
    "ASCEND_MLA": "attention/mla_v1.py",
    "ASCEND_SFA": "attention/sfa_v1.py",
    "ASCEND_DSA": "attention/dsa_v1.py",
    "ASCEND_FA": "attention/fa3_v1.py",
    "ASCEND": "attention/attention_v1.py",
    "ASCEND_SFA_INDEXER": "attention/indexer.py",
}

# Mirrors platform.py backend_map[(use_mla, use_sparse, use_compress)]
BACKEND_MAP = {
    (True, False, False): "ASCEND_MLA",
    (False, False, False): "ASCEND",
    (True, True, False): "ASCEND_SFA",
    (True, False, True): "ASCEND_DSA",
}

BACKEND_TO_KERNELS: dict[str, dict[str, Any]] = {
    "ASCEND_SFA": {
        "tc_ops": [
            "tensor_cast.mla_sparse_attention.default",
            "tensor_cast.mla_sparse_attention_quant.default",
        ],
        "primary_kernels": ["SparseFlashAttention", "MatMulV2", "TransposeBatchMatMul"],
        "indexer_kernels": ["Index", "LinearIndex", "MatMulV2"],
        "notes": "AscendSFABackend; HF index_topk → use_sparse",
    },
    "ASCEND_DSA": {
        "tc_ops": ["tensor_cast.mla_sparse_attention.default"],
        "primary_kernels": ["SparseFlashAttention", "MatMulV2"],
        "indexer_kernels": ["Index", "MatMulV2"],
        "notes": "AscendDSABackend (use_compress)",
    },
    "ASCEND_MLA": {
        "tc_ops": [
            "tensor_cast.multihead_latent_attention.default",
            "tensor_cast.multihead_latent_attention_quant.default",
            "tensor_cast.mlapo.default",
        ],
        "primary_kernels": [
            "FusedInferAttentionScore",
            "RINGMLAPrefillBF16Kernel",
            "TransposeBatchMatMul",
            "MatMulV2",
            "KvRmsNormRopeCache",
        ],
        "indexer_kernels": [],
        "notes": "AscendMLABackend",
    },
    "ASCEND_FA": {
        "tc_ops": ["tensor_cast.attention.default"],
        "primary_kernels": ["FusedInferAttentionScore", "MatMulV2"],
        "indexer_kernels": [],
        "notes": "AscendFABackend",
    },
    "ASCEND": {
        "tc_ops": ["tensor_cast.attention.default"],
        "primary_kernels": ["FusedInferAttentionScore", "MatMulV2"],
        "indexer_kernels": [],
        "notes": "AscendAttentionBackend (dense GQA/MHA, e.g. Qwen)",
    },
}

COMM_KERNELS = ["hcom_allReduce_", "hcom_allGather_", "hcom_reduceScatter_"]
MOE_KERNELS = ["DispatchFFNCombine", "MoeGatingTopK", "GroupedMatmul"]

# Family → default architecture when HF config / profile is absent.
# Parameter size (27B/122B/397B…) does NOT change attention backend for Qwen dense/GQA.
FAMILY_ARCH_DEFAULTS: dict[str, dict[str, Any]] = {
    "qwen": {"use_mla": False, "use_sparse": False, "is_moe": False, "backend_hint": "ASCEND"},
    "qwen_moe": {"use_mla": False, "use_sparse": False, "is_moe": True, "backend_hint": "ASCEND"},
    "glm": {"use_mla": True, "use_sparse": True, "is_moe": True, "backend_hint": "ASCEND_SFA"},
    "glm_mla": {"use_mla": True, "use_sparse": False, "is_moe": True, "backend_hint": "ASCEND_MLA"},
    "deepseek_sparse": {"use_mla": True, "use_sparse": True, "is_moe": True, "backend_hint": "ASCEND_SFA"},
    "deepseek_mla": {"use_mla": True, "use_sparse": False, "is_moe": True, "backend_hint": "ASCEND_MLA"},
    "minimax": {"use_mla": False, "use_sparse": False, "is_moe": True, "backend_hint": "ASCEND"},
    "llama": {"use_mla": False, "use_sparse": False, "is_moe": False, "backend_hint": "ASCEND"},
    "unknown": {"use_mla": False, "use_sparse": False, "is_moe": False, "backend_hint": "ASCEND"},
}


@dataclass
class ModelArch:
    family: str
    use_mla: bool
    use_sparse: bool
    use_compress: bool
    is_moe: bool
    sources: list[str] = field(default_factory=list)


@dataclass
class CodePathAnalysis:
    model: str
    repo: str
    family: str = "unknown"
    use_mla: bool = False
    use_sparse: bool = False
    use_compress: bool = False
    is_moe: bool = False
    matched_model_files: list[str] = field(default_factory=list)
    registered_models: list[str] = field(default_factory=list)
    attention_backends: list[str] = field(default_factory=list)
    selected_backend: str | None = None
    core_ops: list[str] = field(default_factory=list)
    tc_ops: list[str] = field(default_factory=list)
    moe_kernels: list[str] = field(default_factory=list)
    comm_kernels: list[str] = field(default_factory=list)
    evidence_files: list[str] = field(default_factory=list)
    dispatch_hints: list[str] = field(default_factory=list)
    arch_sources: list[str] = field(default_factory=list)
    analysis_mode: str = "vllm_ascend_code"


def _require_repo(repo: str | Path) -> Path:
    root = Path(repo)
    if not root.exists():
        raise FileNotFoundError(
            f"vllm-ascend repo required but missing: {root}. "
            "Clone https://github.com/vllm-project/vllm-ascend "
            "(fallback https://gitcode.com/Ascend/vllm-ascend)."
        )
    pkg = root / "vllm_ascend"
    if not pkg.is_dir():
        raise FileNotFoundError(f"Not a vllm-ascend tree (no vllm_ascend/): {root}")
    return root


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def classify_family(model: str) -> str:
    """Map model name → family bucket (size-agnostic for Qwen)."""
    key = (model or "").lower().replace("_", "-")
    n = _norm(model)

    # DeepSeek sparse (DSA/SFA): V3.2, V4-flash, etc.
    if "deepseek" in key or n.startswith("deepseek"):
        if any(x in key for x in ("v3.2", "v3-2", "v32", "v4-flash", "v4flash", "flash")):
            return "deepseek_sparse"
        if any(x in key for x in ("v3", "v2", "r1")):
            return "deepseek_mla"
        return "deepseek_mla"

    # GLM family — GLM-5.x typically sparse MLA; older may be MLA-only
    if "glm" in key or n.startswith("glm") or "chatglm" in key:
        if any(x in key for x in ("glm-5", "glm5", "5.1", "5.2")):
            return "glm"
        return "glm_mla"

    # Qwen — any parameter size (0.8B/7B/27B/32B/72B/122B/397B…)
    if "qwen" in key or n.startswith("qwen"):
        # MoE variants often marked AxxB or MoE / 397B-A17B
        if re.search(r"moe|a\d+b|397b", key) and ("397" in key or "moe" in key or re.search(r"a\d+b", key)):
            return "qwen_moe"
        return "qwen"

    if "minimax" in key:
        return "minimax"
    if "llama" in key or "eagle" in key:
        return "llama"
    return "unknown"


def _load_json_maybe(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "config" in data and isinstance(data["config"], dict):
        return data["config"]
    if isinstance(data, dict) and "profile" in data and isinstance(data["profile"], dict):
        # allow wrapping parallel-strategies.json
        return data
    return data if isinstance(data, dict) else {}


def _flatten_hf(cfg: dict[str, Any]) -> dict[str, Any]:
    for nest in ("text_config", "llm_config", "language_config", "profile"):
        if isinstance(cfg.get(nest), dict):
            merged = dict(cfg)
            merged.update(cfg[nest])
            return merged
    return cfg


def arch_from_hf_config(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Derive use_mla / use_sparse / moe from HF-style config fields."""
    if not cfg:
        return None
    c = _flatten_hf(cfg)
    flags: dict[str, Any] = {"sources": ["hf_or_profile_config"]}

    # sparse: platform uses hasattr(hf_text_config, "index_topk")
    if "index_topk" in c or c.get("use_sparse") is True:
        flags["use_sparse"] = True
    if c.get("use_sparse") is False:
        flags["use_sparse"] = False

    # MLA indicators
    mla_keys = (
        "kv_lora_rank",
        "qk_nope_head_dim",
        "qk_rope_head_dim",
        "v_head_dim",
        "q_lora_rank",
    )
    if any(c.get(k) is not None for k in mla_keys) or c.get("use_mla") is True:
        flags["use_mla"] = True
    if c.get("use_mla") is False:
        flags["use_mla"] = False
    if str(c.get("attention_type") or "").lower() == "mla":
        flags["use_mla"] = True
    if str(c.get("attention_type") or "").lower() in ("mha", "gqa"):
        flags.setdefault("use_mla", False)

    # MoE
    if any(
        c.get(k)
        for k in (
            "num_experts",
            "n_routed_experts",
            "num_local_experts",
            "num_experts_per_tok",
        )
    ) or c.get("is_moe") is True:
        flags["is_moe"] = True

    if c.get("use_compress") is True:
        flags["use_compress"] = True

    if not any(k in flags for k in ("use_mla", "use_sparse", "is_moe", "use_compress")):
        return None
    return flags


def resolve_model_arch(
    model: str,
    profile: dict[str, Any] | None = None,
    model_config: dict[str, Any] | None = None,
) -> ModelArch:
    family = classify_family(model)
    defaults = dict(FAMILY_ARCH_DEFAULTS.get(family, FAMILY_ARCH_DEFAULTS["unknown"]))
    sources = [f"family_default:{family}"]

    use_mla = bool(defaults["use_mla"])
    use_sparse = bool(defaults["use_sparse"])
    is_moe = bool(defaults["is_moe"])
    use_compress = False

    for label, cfg in (("profile", profile or {}), ("model_config", model_config or {})):
        derived = arch_from_hf_config(cfg)
        if not derived:
            continue
        if "use_mla" in derived:
            use_mla = bool(derived["use_mla"])
            sources.append(f"{label}:use_mla={use_mla}")
        if "use_sparse" in derived:
            use_sparse = bool(derived["use_sparse"])
            sources.append(f"{label}:use_sparse={use_sparse}")
        if "is_moe" in derived:
            is_moe = bool(derived["is_moe"])
            sources.append(f"{label}:is_moe={is_moe}")
        if "use_compress" in derived:
            use_compress = bool(derived["use_compress"])
            sources.append(f"{label}:use_compress={use_compress}")

    # Consistency: sparse implies MLA in Ascend backend map
    if use_sparse and not use_mla:
        use_mla = True
        sources.append("consistency:sparse_implies_mla")

    return ModelArch(
        family=family,
        use_mla=use_mla,
        use_sparse=use_sparse,
        use_compress=use_compress,
        is_moe=is_moe,
        sources=sources,
    )


def backend_from_arch(arch: ModelArch) -> str:
    key = (arch.use_mla, arch.use_sparse, arch.use_compress)
    if key in BACKEND_MAP:
        return BACKEND_MAP[key]
    # Unknown compress/sparse combo → prefer SFA if sparse else MLA/dense
    if arch.use_sparse:
        return "ASCEND_SFA"
    if arch.use_mla:
        return "ASCEND_MLA"
    return "ASCEND"


def list_attention_backends(root: Path) -> list[dict[str, str]]:
    attn = root / "vllm_ascend" / "attention"
    found: list[dict[str, str]] = []
    if not attn.is_dir():
        return found
    for py in sorted(attn.glob("*.py")):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(
            r"def get_name\(.*?\).*?return\s+[\"']([A-Z0-9_]+)[\"']",
            text,
            re.S,
        ):
            found.append({"backend": m.group(1), "file": str(py.relative_to(root))})
        for m in re.finditer(r"class\s+(Ascend\w+Backend)\b", text):
            found.append({"backend_class": m.group(1), "file": str(py.relative_to(root))})
    return found


def list_registered_models(root: Path) -> list[str]:
    init = root / "vllm_ascend" / "models" / "__init__.py"
    if not init.exists():
        return []
    text = init.read_text(encoding="utf-8", errors="ignore")
    return re.findall(r'register_model\(\s*["\']([^"\']+)["\']', text)


def find_model_files(root: Path, model: str, family: str) -> list[str]:
    models_dir = root / "vllm_ascend" / "models"
    hits: list[str] = []
    if not models_dir.is_dir():
        return hits

    family_tokens = {
        "qwen": ["qwen"],
        "qwen_moe": ["qwen"],
        "glm": ["glm", "chatglm"],
        "glm_mla": ["glm", "chatglm"],
        "deepseek_sparse": ["deepseek"],
        "deepseek_mla": ["deepseek"],
        "minimax": ["minimax"],
        "llama": ["llama", "eagle"],
    }.get(family, [])

    tokens = [
        t
        for t in re.split(r"[^a-z0-9]+", (model or "").lower())
        if t and t not in ("b", "for", "causal", "lm", "instruct", "chat")
    ]
    tokens = list(dict.fromkeys(family_tokens + tokens))

    for py in models_dir.rglob("*.py"):
        if py.name == "__init__.py":
            continue
        name = py.stem.lower().replace("_", "")
        rel = str(py.relative_to(root))
        if any(t in name for t in tokens if len(t) >= 3):
            hits.append(rel)
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")[:12000]
        except OSError:
            continue
        if any(t in text.lower() for t in tokens if len(t) >= 4):
            hits.append(rel)
    return sorted(set(hits))[:40]


def _scan_platform_dispatch(root: Path) -> list[str]:
    """Extract evidence that platform.py uses (use_mla, use_sparse) map."""
    plat = root / "vllm_ascend" / "platform.py"
    hints: list[str] = []
    if not plat.exists():
        return hints
    text = plat.read_text(encoding="utf-8", errors="ignore")
    if "get_attn_backend_cls" in text:
        hints.append("platform.get_attn_backend_cls")
    if "use_mla" in text and "use_sparse" in text:
        hints.append("dispatch_key=(use_mla,use_sparse,use_compress)")
    if "AscendSFABackend" in text:
        hints.append("maps_sparse_to_AscendSFABackend")
    if "AscendMLABackend" in text:
        hints.append("maps_mla_to_AscendMLABackend")
    if "AscendAttentionBackend" in text:
        hints.append("maps_dense_to_AscendAttentionBackend")
    if "index_topk" in text:
        hints.append("sparse_signal=hf_text_config.index_topk")
    return hints


def analyze_vllm_ascend(
    model: str,
    repo: str | Path,
    profile: dict[str, Any] | None = None,
    model_config: dict[str, Any] | None = None,
) -> CodePathAnalysis:
    root = _require_repo(repo)
    arch = resolve_model_arch(model, profile=profile, model_config=model_config)

    analysis = CodePathAnalysis(
        model=model,
        repo=str(root),
        family=arch.family,
        use_mla=arch.use_mla,
        use_sparse=arch.use_sparse,
        use_compress=arch.use_compress,
        is_moe=arch.is_moe,
        arch_sources=list(arch.sources),
    )

    backends = list_attention_backends(root)
    analysis.registered_models = list_registered_models(root)
    analysis.matched_model_files = find_model_files(root, model, arch.family)
    analysis.attention_backends = sorted({b["backend"] for b in backends if "backend" in b})
    for name, rel in ATTENTION_BACKEND_FILES.items():
        if (root / "vllm_ascend" / rel).exists() and name not in analysis.attention_backends:
            analysis.attention_backends.append(name)

    selected = backend_from_arch(arch)
    analysis.selected_backend = selected
    analysis.dispatch_hints = [
        f"arch=({arch.use_mla},{arch.use_sparse},{arch.use_compress})→{selected}",
        f"family={arch.family}",
        *_scan_platform_dispatch(root),
    ]

    rel = ATTENTION_BACKEND_FILES.get(selected)
    if rel:
        analysis.evidence_files.append(f"vllm_ascend/{rel}")
    analysis.evidence_files.append("vllm_ascend/platform.py")
    analysis.evidence_files.extend(analysis.matched_model_files[:10])

    if selected in ("ASCEND_SFA", "ASCEND_DSA"):
        for csrc_rel in (
            "vllm_ascend/attention/indexer.py",
            "csrc/attention/sparse_flash_attention",
            "csrc/attention/lightning_indexer",
            "csrc/attention/kv_quant_sparse_flash_attention",
        ):
            if (root / csrc_rel).exists() or (root / csrc_rel.replace("vllm_ascend/", "vllm_ascend/")).exists():
                p = root / csrc_rel
                if p.exists():
                    analysis.evidence_files.append(csrc_rel)

    mapping = BACKEND_TO_KERNELS.get(selected, BACKEND_TO_KERNELS["ASCEND"])
    analysis.tc_ops = list(mapping["tc_ops"])
    cores = list(mapping["primary_kernels"])
    if mapping.get("indexer_kernels"):
        cores = list(mapping["indexer_kernels"]) + cores
    analysis.core_ops = cores
    analysis.comm_kernels = list(COMM_KERNELS)

    # MoE kernels: family default or evidence in matched files / profile
    moe = arch.is_moe
    for rel_f in analysis.matched_model_files:
        p = root / rel_f
        if p.exists() and re.search(
            r"FusedMoE|MoE|n_routed_experts|num_experts",
            p.read_text(encoding="utf-8", errors="ignore")[:16000],
        ):
            moe = True
            break
    analysis.is_moe = moe
    if moe:
        analysis.moe_kernels = list(MOE_KERNELS)
        analysis.core_ops = analysis.core_ops + [k for k in MOE_KERNELS if k not in analysis.core_ops]

    for k in COMM_KERNELS[:1]:
        if k not in analysis.core_ops:
            analysis.core_ops.append(k)

    return analysis


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--vllm-ascend-repo", required=True)
    ap.add_argument("--profile-json", default=None, help="parallel-strategies.json or profile dict")
    ap.add_argument("--model-config", default=None, help="HF model_config.json path")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    profile = _load_json_maybe(Path(args.profile_json) if args.profile_json else None)
    # If parallel-strategies wrapper, prefer .profile
    if "profile" in profile and isinstance(profile["profile"], dict) and "hidden_size" not in profile:
        profile = profile["profile"]
    model_cfg = _load_json_maybe(Path(args.model_config) if args.model_config else None)

    result = asdict(
        analyze_vllm_ascend(
            args.model,
            args.vllm_ascend_repo,
            profile=profile,
            model_config=model_cfg,
        )
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
