# -*- coding: utf-8 -*-
"""进程级采集：目标进程发现（--pid / --auto-detect）、目标详情、进程树、ps 快照。

自动探测规则（对齐 DESIGN 5.1）：/proc 扫描 cmdline 命中关键词
python|torch|mindspore|vllm|train|infer 的进程，按 CPU 累计时间（utime+stime）
择优，cmdline 含 train/infer 时加权。
"""

import glob
import json
import os
import re

from host_bound.collector import common

AUTO_DETECT_KEYWORDS = ["python", "torch", "mindspore", "vllm", "train", "infer"]

# environ 只保留与性能相关的关键变量（第九条：线程相关环境变量必须保留）
ENV_KEEP_PREFIXES = (
    "OMP_", "MKL_", "KMP_", "NUMA_", "CUDA", "NCCL", "GLOO", "ASCEND",
    "HCCL", "PYTHON", "TORCH", "MINDSPORE", "VLLM", "DATALOADER", "BATCH",
    "WORLD", "RANK", "LD_LIBRARY_PATH", "LD_PRELOAD", "MOE", "GRAPH",
    "GE_", "TEPARALLEL", "TE_", "DVM_", "CPU_AFFIN", "SCHED_", "WORKERS",
)
ENV_SENSITIVE_RE = re.compile(
    r"(?i)(token|secret|password|passwd|authorization|credential|api[_-]?key|_ak$|_sk$)")


def auto_detect_pid(logger=None):
    """扫描 /proc 找目标训练/推理进程，返回 pid 或 None。"""
    best, best_score = None, -1.0
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(pid_dir)
        cmd = _read_cmdline(pid)
        if not cmd:
            continue
        joined = " ".join(cmd).lower()
        if not any(k in joined for k in AUTO_DETECT_KEYWORDS):
            continue
        stat = _parse_stat_fields(os.path.join(pid_dir, "stat"))
        if stat is None:
            continue
        cpu_time = float(stat.get("utime", 0)) + float(stat.get("stime", 0))
        score = cpu_time * (2.0 if any(k in joined for k in ("train", "infer")) else 1.0)
        if score > best_score:
            best_score, best = score, pid
    if best and logger:
        logger.info("auto-detect 目标进程 pid=%s", best)
    return int(best) if best else None


def read_cmdline(pid):
    return _read_cmdline(str(pid))


def _read_cmdline(pid):
    text = common.read_text("/proc/%s/cmdline" % pid)
    if text is None:
        return []
    parts = [p for p in text.split("\x00") if p]
    return parts or ([text] if text.strip() else [])


def _parse_stat_fields(path):
    """解析 /proc/<pid>/stat 的关键字段：comm 可能含空格与括号，取最后一个 ')' 切分。"""
    text = common.read_text(path)
    if not text:
        return None
    try:
        head, rest = text.rsplit(")", 1)
    except ValueError:
        return None
    fields = rest.split()
    if len(fields) < 24:
        return None
    # fields[0]=state(第3列)，fields[i] = 第(3+i)列
    return {
        "comm": head.split("(", 1)[-1],
        "state": fields[0],
        "ppid": int(fields[1]),
        "utime": int(fields[11]),
        "stime": int(fields[12]),
        "threads": int(fields[17]) if len(fields) > 17 else 0,
        "starttime": int(fields[19]) if len(fields) > 19 else 0,
    }


def capture_target_details(outdir, pid):
    """目标进程一次性详情：cmdline/environ(过滤)/status/limits/smaps_rollup/io/fd数。"""
    result = {}
    base = "/proc/%s" % pid
    if not os.path.isdir(base):
        return {"found": False}
    result["found"] = True
    # exe（readlink 降级）
    try:
        result["exe"] = os.readlink(os.path.join(base, "exe"))
    except OSError:
        result["exe"] = ""
    cmdline = _read_cmdline(str(pid))
    result["cmdline"] = cmdline
    common.write_text(os.path.join(outdir, "process", "cmdline.txt"),
                      "\n".join(cmdline) + "\n" if cmdline else "unavailable\n")
    result["cmdline_ok"] = bool(cmdline)
    # environ：关键变量白名单 + 敏感值掩码（脱敏前先行处理）
    env_lines = _read_environ_filtered(base)
    common.write_text(os.path.join(outdir, "process", "environ.txt"),
                      "\n".join(env_lines) + "\n" if env_lines else "unavailable\n")
    result["environ_kept"] = len(env_lines)
    for name, src in (("status.txt", "status"), ("limits.txt", "limits"),
                      ("smaps_rollup.txt", "smaps_rollup"), ("io.txt", "io")):
        text = common.read_text(os.path.join(base, src))
        common.write_text(os.path.join(outdir, "process", name), text or "unavailable\n")
        result[name] = text is not None
    try:
        nfd = len(os.listdir(os.path.join(base, "fd")))
    except OSError:
        nfd = -1
    common.write_text(os.path.join(outdir, "process", "fd_count.txt"),
                      "open_fds=%d\n" % nfd)
    result["fd_count"] = nfd
    stat = _parse_stat_fields(os.path.join(base, "stat"))
    if stat:
        result["stat"] = stat
    common.write_text(os.path.join(outdir, "process", "target_summary.json"),
                      json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return result


def _read_environ_filtered(base):
    raw = common.read_text(os.path.join(base, "environ"))
    if not raw:
        return []
    lines = []
    for item in raw.split("\x00"):
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        if ENV_SENSITIVE_RE.search(key):
            lines.append("%s=***" % key)
        elif key.startswith(ENV_KEEP_PREFIXES):
            lines.append("%s=%s" % (key, val))
    return sorted(lines)


def capture_process_tree(outdir):
    """全量进程树：/proc 扫描 → 父子 JSON + 缩进文本。两次采集（窗口前后）便于对比。"""
    nodes = {}
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(pid_dir)
        stat = _parse_stat_fields(os.path.join(pid_dir, "stat"))
        if not stat:
            continue
        nodes[pid] = {"pid": pid, "comm": stat["comm"], "ppid": stat["ppid"],
                      "threads": stat["threads"],
                      "cpu_ticks": stat["utime"] + stat["stime"]}
    children = {}
    for pid, node in nodes.items():
        children.setdefault(str(node["ppid"]), []).append(pid)
    roots = [pid for pid in nodes if nodes[pid]["ppid"] not in
             {int(p) for p in nodes if p.isdigit()}]
    roots = [pid for pid in roots if pid != "0"] or (["1"] if "1" in nodes else [])
    tree = {"name": "root", "children": []}

    def build(pid, depth, out, lines):
        node = nodes.get(pid, {})
        out_node = {"pid": pid, "comm": node.get("comm", "?"),
                    "threads": node.get("threads", 0),
                    "cpu_ticks": node.get("cpu_ticks", 0)}
        out.append(out_node)
        lines.append("%s%s (%s) threads=%s cpu_ticks=%s"
                     % ("  " * depth, pid, node.get("comm", "?"),
                        node.get("threads", "?"), node.get("cpu_ticks", "?")))
        if depth < 24:  # 防环
            for c in sorted(children.get(pid, []), key=lambda x: int(x)):
                kid_children = []
                out_node.setdefault("children", kid_children)
                build(c, depth + 1, kid_children, lines)

    lines = []
    for r in sorted(roots, key=lambda x: int(x)):
        build(r, 0, tree["children"], lines)
    common.write_text(os.path.join(outdir, "process", "process_tree.json"),
                      json.dumps(tree, indent=2, ensure_ascii=False, default=str))
    common.write_text(os.path.join(outdir, "process", "process_tree.txt"),
                      "\n".join(lines) + "\n")
    return {"processes": len(nodes)}


def capture_ps(outdir, tools, pid=None):
    """ps/pstree/top 快照（工具存在才执行；缺工具记 unavailable）。"""
    result = {}
    jobs = [
        ("ps_aux.txt", ["ps", "auxf"] if tools.get("ps") else None),
        ("ps_threads.txt", ["ps", "-T", "-p", str(pid),
                            "-o", "pid,tid,psr,pcpu,stat,comm,wchan:32"]
         if (tools.get("ps") and pid) else None),
        ("pstree.txt", ["pstree", "-p", "-a", str(pid)]
         if (tools.get("pstree") and pid) else None),
        ("top_batch.txt",
         (["top", "-b", "-n", "1", "-p", str(pid)] if pid else ["top", "-b", "-n", "1"])
         if tools.get("top") else None),
    ]
    for fname, cmd in jobs:
        if not cmd:
            common.write_text(os.path.join(outdir, "process", fname), "unavailable\n")
            result[fname] = False
            continue
        rc, out, err, _ = common.run_command(cmd, timeout=30)
        if rc == 0 and out.strip():
            common.write_text(os.path.join(outdir, "process", fname), out)
            result[fname] = True
        else:
            common.write_text(os.path.join(outdir, "process", fname),
                              "unavailable\n# %s\n" % (err or "empty"))
            result[fname] = False
    return result
