# -*- coding: utf-8 -*-
"""环境与能力探测 → capabilities.json。

探测项全部容错：任何一项失败只记 note，不影响其他项，绝不致命。
输出结构（对齐 DESIGN 5.2）：
{
  "os": {...}, "cpu": {...}, "numa": {...}, "container": {...},
  "tools": {"ps": "/usr/bin/ps", ...}, "device": {"npu": {...}, "gpu": {...}},
  "python": {...}, "perf_paranoid": {"value": 2, "hint": "..."},
  "probed_at": "...", "notes": [...]
}
"""

import glob
import os
import platform
import shutil
import socket

from host_bound.collector import common
from host_bound.util import fsio

# 需探测的外部工具（缺失记 None；L2/L3 采样按此决定是否启用）
TOOL_LIST = [
    "ps", "top", "vmstat", "pidstat", "iostat", "mpstat",
    "numastat", "numactl", "perf", "trace-cmd", "lscpu", "pstree",
]

PROC_CPUINFO = "/proc/cpuinfo"
SYS_NODES = "/sys/devices/system/node"


def probe_environment():
    cap = {"probed_at": fsio.now_str(), "notes": []}
    cap["os"] = _probe_os(cap["notes"])
    cap["cpu"] = _probe_cpu(cap["notes"])
    cap["numa"] = _probe_numa(cap["notes"])
    cap["container"] = _probe_container(cap["notes"])
    cap["tools"] = _probe_tools()
    cap["device"] = _probe_devices(cap["notes"])
    cap["python"] = _probe_python()
    cap["perf_paranoid"] = _probe_perf_paranoid()
    return cap


def _probe_os(notes):
    info = {
        "name": platform.system() or "unknown",
        "kernel": platform.release() or "",
        "arch": platform.machine() or "",
        "hostname": socket.gethostname(),
    }
    # 发行版
    text = common.read_text("/etc/os-release")
    if text:
        for line in text.splitlines():
            if line.startswith("PRETTY_NAME="):
                info["distro"] = line.split("=", 1)[1].strip().strip('"')
                break
    else:
        info["distro"] = ""
        notes.append("os-release 不可读（非 Linux 或精简容器）")
    return info


def _probe_cpu(notes):
    info = {"count_logical": os.cpu_count() or 0}
    text = common.read_text(PROC_CPUINFO)
    if text:
        models, sockets, cores = set(), set(), set()
        flags = ""
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            if key == "model name":
                models.add(val)
            elif key == "physical id":
                sockets.add(val)
            elif key == "core id":
                cores.add(val)
            elif key == "flags":
                flags = val
        info["model"] = sorted(models)[0] if models else ""
        info["sockets"] = len(sockets) or 1
        info["count_physical"] = len(cores) or info["count_logical"]
        info["avx512"] = "avx512" in flags
    else:
        info["model"] = ""
        info["sockets"] = 0
        info["count_physical"] = 0
        notes.append("/proc/cpuinfo 不可读：CPU 拓扑细节缺失")
    # 频率与 governor（/sys）
    govs = set()
    for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        g = common.read_text(p)
        if g:
            govs.add(g.strip())
    info["governors"] = sorted(govs)
    return info


def _probe_numa(notes):
    nodes = sorted(glob.glob(os.path.join(SYS_NODES, "node*")))
    if nodes:
        return {"available": True, "node_count": len(nodes), "via": "sysfs"}
    if shutil.which("numactl"):
        rc, out, err, _ = common.run_command(["numactl", "--hardware"], timeout=10)
        if rc == 0 and "node" in out.lower():
            m = [ln for ln in out.splitlines() if "available" in ln.lower()]
            try:
                n = int(m[0].split("cpus:")[0].split()[-2]) if m else 0
            except (IndexError, ValueError):
                n = 0
            return {"available": n > 0, "node_count": n, "via": "numactl"}
    return {"available": False, "node_count": 0, "via": "none"}


def _probe_container(notes):
    if os.path.exists("/.dockerenv"):
        return {"is_container": True, "kind": "docker", "evidence": "/.dockerenv"}
    if os.path.exists("/run/.containerenv"):
        return {"is_container": True, "kind": "podman", "evidence": "/run/.containerenv"}
    cgroup = common.read_text("/proc/1/cgroup")
    if cgroup:
        if "kubepods" in cgroup:
            return {"is_container": True, "kind": "kubernetes", "evidence": "/proc/1/cgroup"}
        if any(x in cgroup for x in ("docker", "containerd", "lxc")):
            return {"is_container": True, "kind": "container", "evidence": "/proc/1/cgroup"}
    if not cgroup:
        notes.append("/proc/1/cgroup 不可读：容器判定降级为路径探测")
    return {"is_container": False, "kind": "host", "evidence": ""}


def _probe_tools():
    tools = {}
    for name in TOOL_LIST:
        tools[name] = shutil.which(name)
    return tools


def _probe_devices(notes):
    dev = {"npu": {"available": False, "tool": "", "count": 0},
           "gpu": {"available": False, "tool": "", "count": 0}}
    if shutil.which("npu-smi"):
        dev["npu"] = {"available": True, "tool": "npu-smi",
                      "count": len(glob.glob("/dev/davinci*")) or None}
    elif glob.glob("/dev/davinci*"):
        dev["npu"] = {"available": True, "tool": "",
                      "count": len(glob.glob("/dev/davinci*"))}
        notes.append("检测到昇腾设备节点但 npu-smi 不可用，设备利用率将缺失")
    if shutil.which("nvidia-smi"):
        dev["gpu"] = {"available": True, "tool": "nvidia-smi", "count": None}
    elif glob.glob("/dev/nvidia[0-9]*"):
        dev["gpu"] = {"available": True, "tool": "",
                      "count": len(glob.glob("/dev/nvidia[0-9]*"))}
        notes.append("检测到 NVIDIA 设备节点但 nvidia-smi 不可用，设备利用率将缺失")
    return dev


def _probe_python():
    info = {}
    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            rc, out, _err, _ = common.run_command([path, "--version"], timeout=10)
            ver = out.strip().split()[-1] if out.strip() else ""
            info = {"path": path, "version": ver, "bin": name}
            break
    return info


def _probe_perf_paranoid():
    """perf_event_paranoid 值决定 L2/L3 可用性：>1 限制 per-pid 采样，>2 禁用。"""
    text = common.read_text("/proc/sys/kernel/perf_event_paranoid")
    val = None
    if text:
        try:
            val = int(text.strip())
        except ValueError:
            val = None
    hint = ""
    if val is not None:
        if val >= 3:
            hint = "perf_event_paranoid>=3：perf 采样被禁用，需 root 或调低该值"
        elif val == 2:
            hint = "perf_event_paranoid=2：仅允许用户态跟踪点，内核符号采样受限"
    else:
        hint = "非 Linux 环境，perf_event_paranoid 不可读"
    return {"value": val, "hint": hint}
