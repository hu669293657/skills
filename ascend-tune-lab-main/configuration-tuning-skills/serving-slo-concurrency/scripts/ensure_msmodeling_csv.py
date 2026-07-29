#!/usr/bin/env python3
"""Ensure msmodeling profiling CSVs are real data (not Git-LFS pointers).

Priority:
1. ``git lfs pull`` when git-lfs is available
2. GitCode LFS batch API download for remaining pointer files
3. Report unresolved pointers with reasons (caller must surface them)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lookup_op_latency import (  # noqa: E402
    _is_lfs_pointer,
    find_msmodeling_data_root,
)

GITCODE_LFS_BATCH = "https://gitcode.com/Ascend/msmodeling.git/info/lfs/objects/batch"


def _parse_lfs_pointer(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("version https://git-lfs.github.com/spec/v1"):
        return None
    oid_m = re.search(r"oid sha256:([0-9a-f]+)", text)
    size_m = re.search(r"size (\d+)", text)
    if not oid_m or not size_m:
        return None
    return {"oid": oid_m.group(1), "size": int(size_m.group(1)), "path": path}


def list_lfs_pointers(msmodeling_repo: Path, names: set[str] | None = None) -> list[dict[str, Any]]:
    data_root = find_msmodeling_data_root(msmodeling_repo)
    if data_root is None:
        return []
    stems = None
    if names is not None:
        stems = {n[:-4] if n.endswith(".csv") else n for n in names}
    out: list[dict[str, Any]] = []
    for p in data_root.rglob("*.csv"):
        if not p.is_file():
            continue
        if stems is not None and p.stem not in stems:
            continue
        if _is_lfs_pointer(p):
            meta = _parse_lfs_pointer(p)
            if meta:
                out.append(meta)
    return out


def try_git_lfs_pull(repo: Path) -> dict[str, Any]:
    if not (repo / ".git").exists():
        return {"ok": False, "method": "git_lfs_pull", "error": "not a git repo"}
    ver = subprocess.run(
        ["git", "lfs", "version"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if ver.returncode != 0:
        return {
            "ok": False,
            "method": "git_lfs_pull",
            "error": "git-lfs unavailable",
            "detail": (ver.stderr or ver.stdout or "")[:300],
        }
    proc = subprocess.run(
        [
            "git",
            "lfs",
            "pull",
            "--include=tensor_cast/performance_model/profiling_database/data/**",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "method": "git_lfs_pull",
        "error": None if proc.returncode == 0 else (proc.stderr or proc.stdout or "")[:500],
    }


def download_via_gitcode_lfs(pointers: list[dict[str, Any]]) -> dict[str, Any]:
    downloaded = 0
    failures: list[str] = []
    for i in range(0, len(pointers), 40):
        chunk = pointers[i : i + 40]
        body = json.dumps(
            {
                "operation": "download",
                "transfers": ["basic"],
                "objects": [{"oid": p["oid"], "size": p["size"]} for p in chunk],
            }
        ).encode()
        req = urllib.request.Request(
            GITCODE_LFS_BATCH,
            data=body,
            headers={
                "Accept": "application/vnd.git-lfs+json",
                "Content-Type": "application/vnd.git-lfs+json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            failures.append(f"batch_api: {e}")
            continue
        by_oid = {o.get("oid"): o for o in data.get("objects") or []}
        for p in chunk:
            obj = by_oid.get(p["oid"])
            path: Path = p["path"]
            if not obj:
                failures.append(f"{path.name}: missing oid in batch response")
                continue
            if obj.get("error"):
                failures.append(f"{path.name}: {obj['error']}")
                continue
            href = ((obj.get("actions") or {}).get("download") or {}).get("href")
            if not href:
                failures.append(f"{path.name}: no download href")
                continue
            try:
                with urllib.request.urlopen(href, timeout=180) as r:
                    content = r.read()
            except (urllib.error.URLError, TimeoutError) as e:
                failures.append(f"{path.name}: download failed: {e}")
                continue
            if content.startswith(b"version https://git-lfs"):
                failures.append(f"{path.name}: still LFS pointer after download")
                continue
            path.write_bytes(content)
            downloaded += 1
    return {
        "ok": downloaded > 0 and not failures,
        "method": "gitcode_lfs_api",
        "downloaded": downloaded,
        "requested": len(pointers),
        "failures": failures[:20],
    }


def ensure_msmodeling_csv(
    msmodeling_repo: str | Path,
    kernel_names: list[str] | None = None,
) -> dict[str, Any]:
    """Make profiling CSVs real. Returns status + unresolved reasons."""
    repo = Path(msmodeling_repo)
    result: dict[str, Any] = {
        "msmodeling_repo": str(repo),
        "required_source": "msmodeling_csv",
        "attempts": [],
        "real_csv_ready": False,
        "unresolved_pointers": [],
        "fallback_reasons": [],
    }
    if not repo.exists():
        result["fallback_reasons"].append("msmodeling repo missing; cannot query real CSV latency")
        return result

    names = set(kernel_names) if kernel_names else None
    if names:
        # also accept .csv filenames
        names |= {f"{n}.csv" if not n.endswith(".csv") else n for n in list(names)}

    pointers = list_lfs_pointers(repo, names=None if kernel_names is None else names)
    # If filter yielded nothing but repo still has pointers for core names, widen once
    if kernel_names and not pointers:
        pointers = list_lfs_pointers(repo, names=None)
        # keep only requested stems
        stems = {n[:-4] if n.endswith(".csv") else n for n in kernel_names}
        pointers = [p for p in pointers if Path(p["path"]).stem in stems]

    if not pointers:
        # no pointers for scoped files — check whether CSVs exist as real files
        data_root = find_msmodeling_data_root(repo)
        if data_root is None:
            result["fallback_reasons"].append(
                "msmodeling profiling_database/data not found"
            )
            return result
        result["real_csv_ready"] = True
        result["attempts"].append({"method": "noop", "ok": True, "note": "no LFS pointers in scope"})
        return result

    result["initial_pointer_count"] = len(pointers)
    lfs = try_git_lfs_pull(repo)
    result["attempts"].append(lfs)

    pointers = list_lfs_pointers(repo, names=None if kernel_names is None else names)
    if kernel_names and pointers:
        stems = {n[:-4] if n.endswith(".csv") else n for n in kernel_names}
        pointers = [p for p in pointers if Path(p["path"]).stem in stems]

    if pointers:
        api = download_via_gitcode_lfs(pointers)
        result["attempts"].append(api)
        pointers = list_lfs_pointers(repo, names=None if kernel_names is None else names)
        if kernel_names and pointers:
            stems = {n[:-4] if n.endswith(".csv") else n for n in kernel_names}
            pointers = [p for p in pointers if Path(p["path"]).stem in stems]

    if pointers:
        result["real_csv_ready"] = False
        result["unresolved_pointers"] = [str(p["path"]) for p in pointers[:30]]
        reasons = []
        for a in result["attempts"]:
            if not a.get("ok") and a.get("error"):
                reasons.append(f"{a.get('method')}: {a.get('error')}")
        if not reasons:
            reasons.append("LFS pointer files remain after pull/API attempts")
        reasons.append(
            "CSV files are still Git-LFS pointers; latency falls back to analytic mapping"
        )
        result["fallback_reasons"] = reasons
    else:
        result["real_csv_ready"] = True
        if not any(a.get("method") == "git_lfs_pull" and a.get("ok") for a in result["attempts"]):
            # recovered via API after git-lfs failed — still success
            pass
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--msmodeling-repo", required=True)
    ap.add_argument(
        "--kernels",
        default=None,
        help="Comma-separated kernel/op names to materialize (default: all pointers under data/)",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    kernels = [x.strip() for x in args.kernels.split(",") if x.strip()] if args.kernels else None
    result = ensure_msmodeling_csv(args.msmodeling_repo, kernel_names=kernels)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)


if __name__ == "__main__":
    main()
