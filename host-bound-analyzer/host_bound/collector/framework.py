# -*- coding: utf-8 -*-
"""框架探测与线程归类：
- 通过目标进程 exe → site-packages 扫描 dist-info/egg-info 识别
  PyTorch / MindSpore / vLLM / TensorFlow / Paddle 等框架与版本
- 线程名归类：DataLoader worker / OMP / MKL / 主线程 / 通信线程
  （第九条：DataLoader 子进程与 OMP 线程池规模必须可识别）
"""

import glob
import os
import re

from host_bound.collector import common

FRAMEWORK_PATTERNS = [
    ("torch", "torch"), ("pytorch", "torch"),
    ("tensorflow", "tensorflow"), ("mindspore", "mindspore"),
    ("vllm", "vllm"), ("paddlepaddle", "paddle"),
    ("onnxruntime", "onnxruntime"), ("deepspeed", "deepspeed"),
    ("megatron", "megatron"), ("transformers", "transformers"),
]

# 线程名归类（小写匹配）
THREAD_CLASS_RULES = [
    ("dataloader", re.compile(r"dataloader|dl_worker", re.I)),
    ("python_worker", re.compile(r"^python3?$", re.I)),
    ("omp", re.compile(r"libgomp|openmp|omp_|gnuomp", re.I)),
    ("mkl", re.compile(r"mkl", re.I)),
    ("torch_main", re.compile(r"pt_main_thread|pt_autograd|torch", re.I)),
    ("mindspore", re.compile(r"mindspore|ms_run|ge_.*worker|davinci|rts", re.I)),
    ("comm", re.compile(r"nccl|hccl|gloo|mpi|hccl", re.I)),
    ("vllm", re.compile(r"vllm|apc_worker|engine", re.I)),
]


def find_site_dirs(exe_path):
    """从目标进程 exe 推导 site-packages 目录列表。"""
    dirs = []
    if not exe_path or not os.path.exists(exe_path):
        return dirs
    prefix = os.path.dirname(os.path.dirname(os.path.abspath(exe_path)))
    for pattern in ("lib/python*/site-packages", "lib64/python*/site-packages",
                    "local/lib/python*/dist-packages", "lib/python3/dist-packages"):
        dirs.extend(glob.glob(os.path.join(prefix, pattern)))
    return dirs


def scan_frameworks(site_dirs):
    found = {}
    for d in site_dirs:
        for entry in os.listdir(d):
            for fname, key in FRAMEWORK_PATTERNS:
                if entry.lower().startswith(fname + "-") and \
                        (entry.endswith(".dist-info") or entry.endswith(".egg-info")):
                    ver = entry[len(fname) + 1:].rsplit(".", 1)[0]
                    found.setdefault(key, ver)
    return [{"name": k, "version": v, "via": "site-packages"} for k, v in sorted(found.items())]


def classify_threads(thread_names):
    """输入线程名列表 → {类别: 计数} + 未归类样本。"""
    counts, unknown = {}, []
    for name in thread_names:
        for label, rx in THREAD_CLASS_RULES:
            if rx.search(name or ""):
                counts[label] = counts.get(label, 0) + 1
                break
        else:
            unknown.append(name)
            counts["other"] = counts.get("other", 0) + 1
    return {"counts": counts, "unknown_sample": unknown[:20]}


def collect(outdir, target_info, thread_names=None):
    """探测框架 + 归类线程，写 framework/ 产物。"""
    frameworks = scan_frameworks(find_site_dirs(target_info.get("exe", "")))
    # 进程 cmdline 中含脚本名提示（如 train.py / mindspore / vllm serve）
    cmdline_join = " ".join(target_info.get("cmdline", [])).lower()
    hints = [k for k, _ in FRAMEWORK_PATTERNS if k in cmdline_join]
    cls = classify_threads(thread_names or [])
    payload = {
        "frameworks": frameworks,
        "cmdline_hints": sorted(set(hints)),
        "thread_class": cls,
        "python_path": target_info.get("exe", ""),
    }
    common.write_json(os.path.join(outdir, "framework", "framework.json"), payload)
    lines = ["frameworks:"]
    for f in frameworks:
        lines.append("  %s==%s (site-packages)" % (f["name"], f["version"]))
    hints_str = ", ".join(sorted(set(hints))) or "none"
    lines.append("cmdline hints: %s" % hints_str)
    lines.append("thread classification: %s" % cls["counts"])
    common.write_text(os.path.join(outdir, "framework", "framework.txt"),
                      "\n".join(lines) + "\n")
    return payload
