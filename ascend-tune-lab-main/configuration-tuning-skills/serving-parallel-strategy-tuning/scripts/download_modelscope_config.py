#!/usr/bin/env python3
"""Download HuggingFace-style config.json for a model from ModelScope into workdir.

Pipeline hard gate (Phase 0): after ## 基本参数 is valid, fetch config to
`{workdir}/model_config.json`. On failure, exit non-zero with a user-facing
warning asking for a manual config file.

Does NOT download model weights — only config.json (and optionally
configuration.json as fallback).
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPTS.parent
_REF = _SKILL_ROOT / "references" / "modelscope_model_ids.json"

# Common org prefixes when model name has no slash.
_ORG_BY_PREFIX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^qwen", re.I), "Qwen"),
    (re.compile(r"^(glm|chatglm)", re.I), "ZhipuAI"),
    (re.compile(r"^deepseek", re.I), "deepseek-ai"),
    (re.compile(r"^minimax", re.I), "MiniMax"),
    (re.compile(r"^llama", re.I), "LLM-Research"),
    (re.compile(r"^internlm", re.I), "Shanghai_AI_Laboratory"),
    (re.compile(r"^yi", re.I), "01ai"),
]

_USER_AGENT = "ascend-tune-lab/download_modelscope_config"


def _load_id_map() -> dict[str, str]:
    if not _REF.exists():
        return {}
    try:
        data = json.loads(_REF.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in (data.get("ids") or data or {}).items() if k and v}


def candidate_model_ids(model_name: str, explicit_id: str | None = None) -> list[str]:
    """Build ordered ModelScope model_id candidates."""
    seen: list[str] = []
    def add(x: str | None) -> None:
        x = (x or "").strip().strip("/")
        if x and x not in seen:
            seen.append(x)

    name = (model_name or "").strip()
    id_map = _load_id_map()

    # Prefer explicit / curated map before raw name heuristics
    add(explicit_id)
    add(id_map.get(name))
    lower = name.lower()
    for k, v in id_map.items():
        if k.lower() == lower:
            add(v)
    norm = re.sub(r"[^a-z0-9]+", "", lower)
    for k, v in id_map.items():
        if re.sub(r"[^a-z0-9]+", "", k.lower()) == norm:
            add(v)

    add(name)
    if "/" not in name:
        for pat, org in _ORG_BY_PREFIX:
            if pat.search(name):
                add(f"{org}/{name}")
                # common variants
                add(f"{org}/{name}-Instruct")
                add(f"{org}/{name}-Base")
                break
    return seen


def _http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def _try_http_config(model_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Try ModelScope HTTP endpoints for config.json."""
    errors: list[str] = []
    enc = urllib.parse.quote(model_id, safe="/")
    urls = [
        f"https://www.modelscope.cn/models/{enc}/resolve/master/config.json",
        f"https://www.modelscope.cn/models/{enc}/resolve/main/config.json",
        (
            "https://www.modelscope.cn/api/v1/models/"
            f"{enc}/repo?Revision=master&FilePath=config.json"
        ),
        (
            "https://www.modelscope.cn/api/v1/models/"
            f"{enc}/repo?Revision=main&FilePath=config.json"
        ),
        f"https://www.modelscope.cn/models/{enc}/resolve/master/configuration.json",
        f"https://www.modelscope.cn/models/{enc}/resolve/main/configuration.json",
    ]
    for url in urls:
        try:
            raw = _http_get(url)
            # API may wrap JSON; accept only object configs
            text = raw.decode("utf-8", errors="replace").strip()
            if not text.startswith("{"):
                errors.append(f"{url}: not JSON object")
                continue
            data = json.loads(text)
            if not isinstance(data, dict):
                errors.append(f"{url}: JSON root not object")
                continue
            # ModelScope API error payload
            if "Code" in data and "Data" not in data and "model_type" not in data:
                errors.append(f"{url}: API error {data.get('Code')} {data.get('Message')}")
                continue
            if "Data" in data and isinstance(data["Data"], (bytes, str)):
                errors.append(f"{url}: unexpected Data wrapper")
                continue
            if not _looks_like_model_config(data):
                errors.append(f"{url}: JSON lacks model-config fields")
                continue
            return data, errors
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            errors.append(f"{url}: {e}")
    return None, errors


def _looks_like_model_config(data: dict[str, Any]) -> bool:
    keys = set(data.keys())
    markers = {
        "model_type",
        "architectures",
        "hidden_size",
        "num_hidden_layers",
        "num_attention_heads",
        "vocab_size",
        "text_config",
        "llm_config",
        "torch_dtype",
        "transformers_version",
    }
    return bool(keys & markers)


def _try_modelscope_sdk(model_id: str, cache_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        from modelscope.hub.snapshot_download import snapshot_download  # type: ignore
    except ImportError:
        return None, "modelscope SDK not installed"

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        local = snapshot_download(
            model_id,
            local_dir=str(cache_dir / model_id.replace("/", "__")),
            allow_patterns=["config.json", "configuration.json"],
        )
    except Exception as e:  # noqa: BLE001 — surface any SDK failure
        return None, f"modelscope snapshot_download failed: {e}"

    root = Path(local)
    for name in ("config.json", "configuration.json"):
        p = root / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return None, f"read {p}: {e}"
            if isinstance(data, dict) and _looks_like_model_config(data):
                return data, None
    return None, f"no config.json under {root}"


def parse_modelscope_id_from_config_md(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    for cn in ("ModelScope模型ID", "ModelScope模型Id", "modelscope_model_id", "ModelScope ID"):
        m = re.search(rf"[-*]\s*{re.escape(cn)}\s*[:：]\s*(\S+)", text)
        if m:
            return m.group(1).strip()
    return None


def parse_model_name_from_config_md(path: Path) -> str:
    # Prefer shared parser when available
    try:
        sys.path.insert(0, str(_SKILL_ROOT.parent / "find-possible-parallel-strategy" / "scripts"))
        from deploy_config import parse_deploy_config_md  # type: ignore

        cfg = parse_deploy_config_md(path)
        if cfg.get("model"):
            return str(cfg["model"])
    except Exception:  # noqa: BLE001
        pass
    text = path.read_text(encoding="utf-8")
    m = re.search(r"[-*]\s*模型名称\s*[:：]\s*(.+)", text)
    if not m:
        raise ValueError(f"模型名称 not found in {path}")
    return m.group(1).strip()


def download_model_config(
    model_name: str,
    out_path: Path,
    *,
    modelscope_id: str | None = None,
    cache_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download config; write out_path. Returns result metadata dict."""
    out_path = out_path.resolve()
    meta: dict[str, Any] = {
        "ok": False,
        "model_name": model_name,
        "modelscope_id_requested": modelscope_id,
        "out_path": str(out_path),
        "source": None,
        "resolved_model_id": None,
        "errors": [],
        "user_action_required": False,
        "warning": None,
    }

    if out_path.exists() and not force:
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and _looks_like_model_config(existing):
            meta.update(
                {
                    "ok": True,
                    "source": "existing_file",
                    "resolved_model_id": None,
                    "message": f"reused existing {out_path}",
                }
            )
            return meta

    candidates = candidate_model_ids(model_name, modelscope_id)
    meta["candidates"] = candidates
    if not candidates:
        meta["user_action_required"] = True
        meta["warning"] = (
            f"无法从模型名 {model_name!r} 推断 ModelScope model_id。"
            f"请手动将 HuggingFace/ModelScope 的 config.json 放到: {out_path}"
        )
        return meta

    cache = cache_dir or (out_path.parent / ".modelscope_cache")
    all_errors: list[str] = []

    for mid in candidates:
        data, errs = _try_http_config(mid)
        all_errors.extend(errs)
        if data is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            meta.update(
                {
                    "ok": True,
                    "source": "modelscope_http",
                    "resolved_model_id": mid,
                    "message": f"downloaded config.json for {mid} → {out_path}",
                }
            )
            meta["errors"] = all_errors
            return meta

        data, err = _try_modelscope_sdk(mid, cache)
        if err:
            all_errors.append(f"{mid} sdk: {err}")
        if data is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            meta.update(
                {
                    "ok": True,
                    "source": "modelscope_sdk",
                    "resolved_model_id": mid,
                    "message": f"downloaded config.json for {mid} → {out_path}",
                }
            )
            meta["errors"] = all_errors
            return meta

    meta["errors"] = all_errors
    meta["user_action_required"] = True
    meta["warning"] = (
        "【警告】无法从 ModelScope 下载模型 config.json。\n"
        f"- 模型名称: {model_name}\n"
        f"- 尝试过的 model_id: {', '.join(candidates)}\n"
        f"- 目标路径: {out_path}\n\n"
        "请手动提供 config 文件后重新发起流水线：\n"
        f"1. 从 ModelScope / HuggingFace 获取该模型的 `config.json`\n"
        f"2. 复制到工作目录: `{out_path}`\n"
        "3. （推荐）在 deploy-config.md 的 ## 基本参数 中增加一行：\n"
        "   - ModelScope模型ID: <org/model-name>\n"
        "4. 重新发起请求（已有 model_config.json 时会直接复用）"
    )
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="deploy-config.md (to read 模型名称)")
    ap.add_argument("--model", default=None, help="model name override")
    ap.add_argument("--modelscope-id", default=None, help="explicit ModelScope model id org/name")
    ap.add_argument("--workdir", required=True, help="pipeline workdir")
    ap.add_argument(
        "--out",
        default=None,
        help="output path (default: {workdir}/model_config.json)",
    )
    ap.add_argument("--force", action="store_true", help="re-download even if out exists")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    out = Path(args.out) if args.out else workdir / "model_config.json"

    model = args.model
    ms_id = args.modelscope_id
    if args.config:
        cfg_path = Path(args.config)
        if not model:
            model = parse_model_name_from_config_md(cfg_path)
        if not ms_id:
            ms_id = parse_modelscope_id_from_config_md(cfg_path)
    if not model:
        print("ERROR: --model or --config with 模型名称 is required", file=sys.stderr)
        sys.exit(2)

    result = download_model_config(
        model,
        out,
        modelscope_id=ms_id,
        cache_dir=workdir / ".modelscope_cache",
        force=args.force,
    )
    meta_path = workdir / "model_config.fetch.json"
    workdir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("ok"):
            print(result.get("message") or f"OK → {out}")
        else:
            print(result.get("warning") or "FAILED", file=sys.stderr)

    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
