#!/usr/bin/env python3
"""Resolve SLO constraints from deploy-config.md (## SLO约束) with defaults."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _parse_duration_to_ms(raw: str) -> float | None:
    s = (raw or "").strip().lower()
    if not s or s in ("-", "n/a", "none", "不限", "无", "null"):
        return None
    s = s.replace("<", "").replace("≤", "").replace("<=", "").strip()
    m = re.match(r"([\d.]+)\s*(ms|s|秒|毫秒)?", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "ms").lower()
    if unit in ("s", "秒"):
        return val * 1000.0
    return val


def parse_slo_section(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ttft_ms": None,
        "tpot_ms": None,
        "other": None,
        "section_present": False,
        "fields_present": [],
        "needs_user_input": False,
    }
    section = re.search(r"##\s*SLO约束\s*(.*?)(?=\n##\s|\Z)", text, re.S | re.I)
    if not section:
        out["needs_user_input"] = True
        return out
    out["section_present"] = True
    body = section.group(1)

    for label, key in (("TTFT", "ttft_ms"), ("TPOT", "tpot_ms"), ("其他约束", "other")):
        m = re.search(rf"[-*]\s*{label}\s*[:：]\s*(.*)", body)
        if not m:
            continue
        raw = m.group(1).strip()
        # strip trailing markdown comments
        raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        if not raw:
            continue
        out["fields_present"].append(key)
        if key == "other":
            out["other"] = raw
        else:
            out[key] = _parse_duration_to_ms(raw)

    # If section exists but no TPOT and no TTFT values → still allow defaults, but flag if completely empty
    if not out["fields_present"]:
        out["needs_user_input"] = True
    return out


def apply_defaults(slo: dict[str, Any], interactive_provided: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge interactive overrides and apply defaults: TPOT=50ms, TTFT unlimited."""
    merged = dict(slo)
    if interactive_provided:
        for k in ("ttft_ms", "tpot_ms", "other"):
            if k in interactive_provided and interactive_provided[k] is not None:
                merged[k] = interactive_provided[k]
                if k not in merged.get("fields_present", []):
                    merged.setdefault("fields_present", []).append(k)

    defaults_applied = []
    if merged.get("tpot_ms") is None:
        merged["tpot_ms"] = 50.0
        defaults_applied.append("tpot_ms=50")
    # ttft stays None = unlimited
    if merged.get("ttft_ms") is None:
        defaults_applied.append("ttft_ms=null(unlimited)")

    merged["defaults_applied"] = defaults_applied
    merged["needs_user_input"] = False
    return merged


def resolve_from_config(
    config_path: str | Path | None,
    interactive_json: str | Path | None = None,
    apply_default: bool = True,
) -> dict[str, Any]:
    text = ""
    if config_path and Path(config_path).exists():
        text = Path(config_path).read_text(encoding="utf-8")
    slo = parse_slo_section(text) if text else {
        "ttft_ms": None,
        "tpot_ms": None,
        "other": None,
        "section_present": False,
        "fields_present": [],
        "needs_user_input": True,
    }

    interactive = None
    if interactive_json and Path(interactive_json).exists():
        interactive = json.loads(Path(interactive_json).read_text(encoding="utf-8"))

    if apply_default:
        # Agent should ask user when needs_user_input before calling with apply_default;
        # scripts still apply defaults for offline/automation.
        return apply_defaults(slo, interactive)
    return slo


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="deploy-config.md")
    ap.add_argument("--interactive-json", default=None, help="Optional user-answered SLO JSON")
    ap.add_argument("--no-defaults", action="store_true", help="Do not apply TPOT=50 default")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = resolve_from_config(
        args.config,
        args.interactive_json,
        apply_default=not args.no_defaults,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
