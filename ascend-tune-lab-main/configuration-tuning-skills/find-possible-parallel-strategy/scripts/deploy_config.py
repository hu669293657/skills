#!/usr/bin/env python3
"""Parse deploy-config.md / baseline config MD into a dict."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

FIELD_MAP = {
    "输入长度": "input_len",
    "输出长度": "output_len",
    "设备类型": "device_type",
    "模型名称": "model",
    "量化格式": "quantization",
    "NPU卡数": "num_npus",
    "部署策略": "deploy_strategy",
    "ModelScope模型ID": "modelscope_model_id",
    "ModelScope模型Id": "modelscope_model_id",
    "modelscope_model_id": "modelscope_model_id",
    "ModelScope ID": "modelscope_model_id",
}


def _parse_basic_params(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    section = re.search(r"##\s*基本参数\s*(.*?)(?=\n##\s|\Z)", text, re.S)
    if not section:
        return out
    body = section.group(1)
    for cn, key in FIELD_MAP.items():
        m = re.search(rf"[-*]\s*{re.escape(cn)}\s*[:：]\s*(.+)", body)
        if not m:
            continue
        val = m.group(1).strip()
        if not val:
            continue
        if key in ("input_len", "output_len", "num_npus"):
            try:
                out[key] = int(re.search(r"\d+", val).group())  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                out[key] = val
        else:
            out[key] = val
    return out


def _parse_serving_bash(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    section = re.search(r"##\s*服务化配置\s*(.*?)(?=\n##\s|\Z)", text, re.S)
    if not section:
        return out
    body = section.group(1)
    bash = re.search(r"```(?:bash|sh)?\s*(.*?)```", body, re.S)
    if not bash:
        return out
    block = bash.group(1)
    for flag, key in (
        ("--data-parallel-size", "dp"),
        ("--tensor-parallel-size", "tp"),
        ("--host", "host"),
        ("--port", "port"),
    ):
        m = re.search(rf"{re.escape(flag)}\s+(\S+)", block)
        if m:
            raw = m.group(1).strip().rstrip("\\")
            if key in ("dp", "tp", "port"):
                try:
                    out[key] = int(raw)
                except ValueError:
                    out[key] = raw
            else:
                out[key] = raw
    m_model = re.search(r"vllm\s+serve\s+(\S+)", block)
    if m_model:
        out["model_path"] = m_model.group(1)
    return out


def parse_deploy_config_md(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    result = _parse_basic_params(text)
    serving = _parse_serving_bash(text)
    if serving:
        result["serving"] = serving
        if "dp" in serving:
            result["dp"] = serving["dp"]
        if "tp" in serving:
            result["tp"] = serving["tp"]
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="Path to deploy-config.md")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = parse_deploy_config_md(args.config)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for k, v in data.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
