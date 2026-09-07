# -*- coding: utf-8 -*-
"""打包：文件清单（sha256）+ manifest.json + tar.gz。

manifest 内容（对齐 DESIGN 4.4 / 5.1）：四元版本、采集参数、目标进程、
能力探测结果、逐文件清单、数据质量分级、脱敏标记。tar 包内根目录为 case_id。
"""

import hashlib
import json
import os
import re
import tarfile
import time

from host_bound import VERSION, SCHEMA_VERSION, REPORT_VERSION, RULE_VERSION
from host_bound.collector import common

VERSION_BLOCK = {
    "tool_version": VERSION,
    "schema_version": SCHEMA_VERSION,
    "report_version": REPORT_VERSION,
    "rule_version": RULE_VERSION,
}

HASH_SKIP_SUFFIX = (".tar.gz", ".tgz")


def hash_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except OSError:
        return ""


def list_files(outdir):
    files = []
    for dirpath, _dirs, filenames in os.walk(outdir):
        for fn in filenames:
            if fn.endswith(HASH_SKIP_SUFFIX):
                continue  # tar 包自身与嵌套包不进清单
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, outdir).replace("\\", "/")
            try:
                size = os.path.getsize(p)
            except OSError:
                size = -1
            files.append({"path": rel, "size": size, "sha256": hash_file(p)})
    files.sort(key=lambda x: x["path"])
    return files


def make_case_id(hostname):
    host = re.sub(r"[^A-Za-z0-9-]", "", hostname)[:16] if hostname else "host"
    return "hb-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), (host or "host").lower())


def build_manifest(outdir, capabilities, params, target, dq, sanitized,
                   hostname, username):
    manifest = {
        "manifest_version": "1.0",
        "case_id": make_case_id(hostname),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "version": VERSION_BLOCK,
        "params": {
            "duration": params.duration,
            "interval": params.interval,
            "level": params.level,
            "pid": params.pid,
            "auto_detect": params.auto_detect,
            "sanitize": params.sanitize,
        },
        "hostname": hostname,
        "username": username,
        "target": target,
        "capabilities": capabilities,
        "data_quality": dq,
        "sanitized": bool(sanitized),
        "files": [],
    }
    manifest["files"] = list_files(outdir)
    return manifest


def write_manifest(outdir, manifest):
    path = os.path.join(outdir, "manifest.json")
    common.write_json(path, manifest)
    # 写入后把 manifest 自身补进 files 清单（保持逐文件可追溯）
    manifest["files"] = [f for f in manifest["files"]
                         if f["path"] != "manifest.json"]
    manifest["files"].append({
        "path": "manifest.json", "size": os.path.getsize(path),
        "sha256": hash_file(path)})
    manifest["files"].sort(key=lambda x: x["path"])
    common.write_json(path, manifest)
    return path


def make_tar(outdir, case_id):
    """打 tar.gz，包内根目录 = case_id。返回 tar 路径。"""
    parent = os.path.dirname(os.path.abspath(outdir))
    tar_path = os.path.join(parent, case_id + ".tar.gz")
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(outdir, arcname=case_id)
    return tar_path


def compute_dq(system_avail, target_found, thread_rows, tools_results,
               device_static, perf_results, notes):
    """采集侧数据质量分级（分析端还会按文件实际可解析性复核）。

    核心组：system / process / threads；外围组：tools / device / perf。
    全核心 + ≥1 外围 → complete；部分核心 → partial；全缺 → insufficient。
    """
    groups = {
        "system": "present" if system_avail else "missing",
        "process": "present" if target_found else "missing",
        "threads": "present" if thread_rows > 0 else "missing",
        "tools": "present" if any(v.get("ok") for v in (tools_results or {}).values())
                  else "missing",
        "device": "present" if any((device_static or {}).values()) else "missing",
        "perf": "present" if (perf_results or {}).get("perf_stat", {}).get("ok")
                 else "missing",
    }
    core_ok = sum(1 for k in ("system", "process", "threads")
                  if groups[k] == "present")
    extra_ok = sum(1 for k in ("tools", "device", "perf")
                   if groups[k] == "present")
    if core_ok == 3 and extra_ok >= 1:
        grade = "complete"
    elif core_ok >= 1:
        grade = "partial"
    else:
        grade = "insufficient"
    return {"grade": grade, "groups": groups, "notes": notes or []}


def manifest_to_json_bytes(manifest):
    return json.dumps(manifest, indent=2, ensure_ascii=False, default=str).encode("utf-8")
