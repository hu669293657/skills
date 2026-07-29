#!/usr/bin/env python3
"""Clone vllm-ascend and msmodeling into a dedicated repos directory.

Typical destination (pipeline): `{workdir}/repos/` — shared across cases,
not under `{case_dir}/tuning/`.

Priority:
  vllm-ascend: GitHub → GitCode
  msmodeling:  GitCode → GitHub  (user-specified primary: gitcode.com/Ascend/msmodeling)

On failure: populate `warning` + `user_action_required`, print to stderr, exit 1
(unless --allow-missing). Caller / Agent must surface the warning to the user.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

VLLM_ASCEND_URLS = [
    "https://github.com/vllm-project/vllm-ascend.git",
    "https://gitcode.com/Ascend/vllm-ascend.git",
]

MSMODELING_URLS = [
    "https://gitcode.com/Ascend/msmodeling.git",
    "https://github.com/Ascend/msmodeling.git",
]


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def shallow_clone(urls: list[str], dest: Path) -> dict[str, Any]:
    if dest.exists() and (dest / ".git").exists():
        return {
            "ok": True,
            "path": str(dest),
            "skipped": True,
            "url": None,
            "tried_urls": list(urls),
            "error": None,
        }

    if dest.exists():
        shutil.rmtree(dest)

    last_err = None
    errors: list[str] = []
    for url in urls:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1", url, str(dest)]
        proc = _run(cmd, timeout=900)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            last_err = err
            errors.append(f"{url}: {err or f'exit={proc.returncode}'}")
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            continue
        return {
            "ok": True,
            "path": str(dest),
            "skipped": False,
            "url": url,
            "tried_urls": list(urls),
            "error": None,
            "errors": errors,
        }

    return {
        "ok": False,
        "path": str(dest),
        "skipped": False,
        "url": None,
        "tried_urls": list(urls),
        "error": last_err,
        "errors": errors,
    }


def try_git_lfs_pull(repo: Path, include_glob: str) -> dict[str, Any]:
    """Best-effort LFS pull for profiling CSVs."""
    if not (repo / ".git").exists():
        return {"ok": False, "error": "not a git repo"}
    proc = _run(["git", "lfs", "version"], cwd=repo, timeout=30)
    if proc.returncode != 0:
        return {"ok": False, "error": "git-lfs unavailable", "detail": (proc.stderr or "")[:200]}
    proc2 = _run(["git", "lfs", "pull", f"--include={include_glob}"], cwd=repo, timeout=1800)
    return {
        "ok": proc2.returncode == 0,
        "error": None if proc2.returncode == 0 else (proc2.stderr or proc2.stdout or "")[:500],
    }


def _repo_warning_block(
    name: str,
    info: dict[str, Any],
    urls: list[str],
) -> str:
    path = info.get("path") or ""
    err = info.get("error") or "unknown"
    tried = info.get("tried_urls") or urls
    lines = [
        f"### {name}",
        f"- 目标路径: `{path}`",
        f"- 错误: {err}",
        "- 尝试过的 URL:",
    ]
    for u in tried:
        lines.append(f"  - {u}")
    lines += [
        "- 请手动 clone 后重新发起：",
        f"  ```bash",
        f"  git clone --depth 1 {urls[0]} {path}",
        f"  # 若主源失败，可改用备源：{urls[1] if len(urls) > 1 else '(无)'}",
        f"  ```",
    ]
    return "\n".join(lines)


def enrich_clone_result(
    result: dict[str, Any],
    *,
    require_msmodeling: bool = True,
) -> dict[str, Any]:
    """Add ok / warning / user_action_required for Agent-facing failure handling."""
    vllm = result.get("vllm_ascend") or {}
    ms = result.get("msmodeling") or {}
    vllm_ok = bool(vllm.get("ok"))
    ms_ok = bool(ms.get("ok")) if require_msmodeling else True

    result["ok"] = vllm_ok and ms_ok
    result["user_action_required"] = not result["ok"]
    result["warning"] = None
    result["lfs_warning"] = None

    failed: list[str] = []
    if not vllm_ok:
        failed.append(_repo_warning_block("vllm-ascend", vllm, VLLM_ASCEND_URLS))
    if require_msmodeling and not ms_ok:
        failed.append(_repo_warning_block("msmodeling", ms, MSMODELING_URLS))

    if failed:
        repos_dir = result.get("repos_dir") or ""
        result["warning"] = (
            "【警告】无法自动下载 / 准备 vllm-ascend 与 msmodeling 源码仓。\n"
            "Phase 2 SLO 分析强依赖这两个仓库；请按下方说明手动放置后重新发起。\n\n"
            f"- repos 目录: `{repos_dir}`\n"
            f"- skip_clone: `{result.get('skip_clone')}`\n\n"
            + "\n\n".join(failed)
            + "\n\n放置完成后目录应满足：\n"
            f"- `{repos_dir}/vllm-ascend/vllm_ascend/` 存在\n"
            f"- `{repos_dir}/msmodeling/tensor_cast/performance_model/profiling_database/data/` 存在\n"
        )

    lfs = result.get("msmodeling_lfs")
    if isinstance(lfs, dict) and ms_ok and not lfs.get("ok"):
        result["lfs_warning"] = (
            "【警告】msmodeling 已 clone，但 git-lfs 拉取 profiling CSV 失败"
            f"（{lfs.get('error') or lfs.get('detail') or 'unknown'}）。\n"
            "后续可能无法使用真实 `msmodeling_csv` 耗时，将回退并在产物中写明原因。\n"
            "建议安装 git-lfs 后执行：\n"
            f"  `cd {ms.get('path')} && git lfs pull "
            "--include='tensor_cast/performance_model/profiling_database/data/**'`"
        )

    return result


def clone_repos(
    repos_dir: Path,
    skip_clone: bool = False,
    clone_msmodeling: bool = True,
    try_lfs: bool = True,
) -> dict[str, Any]:
    repos_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"repos_dir": str(repos_dir), "skip_clone": skip_clone}

    if skip_clone:
        vllm = repos_dir / "vllm-ascend"
        ms = repos_dir / "msmodeling"
        result["vllm_ascend"] = {
            "ok": vllm.exists() and (vllm / "vllm_ascend").is_dir(),
            "path": str(vllm),
            "skipped": True,
            "url": None,
            "tried_urls": list(VLLM_ASCEND_URLS),
            "error": None
            if (vllm.exists() and (vllm / "vllm_ascend").is_dir())
            else "missing (skip_clone)",
        }
        result["msmodeling"] = {
            "ok": ms.exists()
            and (ms / "tensor_cast" / "performance_model" / "profiling_database" / "data").is_dir(),
            "path": str(ms),
            "skipped": True,
            "url": None,
            "tried_urls": list(MSMODELING_URLS),
            "error": None
            if (
                ms.exists()
                and (ms / "tensor_cast" / "performance_model" / "profiling_database" / "data").is_dir()
            )
            else "missing (skip_clone)",
        }
        return enrich_clone_result(result, require_msmodeling=clone_msmodeling)

    result["vllm_ascend"] = shallow_clone(VLLM_ASCEND_URLS, repos_dir / "vllm-ascend")
    if clone_msmodeling:
        result["msmodeling"] = shallow_clone(MSMODELING_URLS, repos_dir / "msmodeling")
        if result["msmodeling"]["ok"] and try_lfs:
            result["msmodeling_lfs"] = try_git_lfs_pull(
                repos_dir / "msmodeling",
                "tensor_cast/performance_model/profiling_database/data/**",
            )
    else:
        result["msmodeling"] = {
            "ok": True,
            "path": str(repos_dir / "msmodeling"),
            "skipped": True,
            "url": None,
            "tried_urls": list(MSMODELING_URLS),
            "error": "clone_msmodeling=false (not required)",
        }
    return enrich_clone_result(result, require_msmodeling=clone_msmodeling)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repos-dir", required=True)
    ap.add_argument("--skip-clone", action="store_true")
    ap.add_argument("--no-msmodeling", action="store_true")
    ap.add_argument("--no-lfs", action="store_true")
    ap.add_argument(
        "--allow-missing",
        action="store_true",
        help="Do not exit 1 when clone fails (still emit warning in JSON)",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = clone_repos(
        Path(args.repos_dir),
        skip_clone=args.skip_clone,
        clone_msmodeling=not args.no_msmodeling,
        try_lfs=not args.no_lfs,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json or not args.out:
        print(text)

    if result.get("lfs_warning"):
        print(result["lfs_warning"], file=sys.stderr)
    if not result.get("ok"):
        if result.get("warning"):
            print(result["warning"], file=sys.stderr)
        if not args.allow_missing:
            sys.exit(1)


if __name__ == "__main__":
    main()
