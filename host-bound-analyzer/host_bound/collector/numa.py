# -*- coding: utf-8 -*-
"""NUMA 采集：/sys/devices/system/node 静态拓扑 + numastat/numactl（工具存在才采）。

输出：numa/nodes.txt（每节点 cpulist+meminfo）、numactl_hardware.txt、
numa/numastat_system.txt、numa/numastat_pid.txt（目标进程跨节点命中/缺失）。
"""

import glob
import os

from host_bound.collector import common


def collect_static(outdir, tools, pid=None):
    result = {}
    # sysfs 拓扑（L1 兜底，无需工具）
    node_dirs = sorted(glob.glob("/sys/devices/system/node/node[0-9]*"))
    if node_dirs:
        lines = []
        for nd in node_dirs:
            node = os.path.basename(nd)
            cpulist = common.read_text(os.path.join(nd, "cpulist")) or "?"
            lines.append("%s cpulist=%s" % (node, cpulist.strip()))
            mi = common.read_text(os.path.join(nd, "meminfo"))
            if mi:
                for ln in mi.splitlines():
                    if any(k in ln for k in ("MemTotal", "MemFree", "MemUsed")):
                        lines.append("  " + ln.strip())
        common.write_text(os.path.join(outdir, "numa", "nodes.txt"),
                          "\n".join(lines) + "\n")
        result["nodes.txt"] = True
        # 节点距离
        dist = common.read_text("/sys/devices/system/node/node0/distance")
        if dist:
            common.write_text(os.path.join(outdir, "numa", "distance.txt"), dist)
            result["distance.txt"] = True
    else:
        result["nodes.txt"] = False
    # numactl --hardware
    if tools.get("numactl"):
        rc, out, err, _ = common.run_command(["numactl", "--hardware"], timeout=15)
        common.write_text(os.path.join(outdir, "numa", "numactl_hardware.txt"),
                          out if rc == 0 and out.strip() else "unavailable\n# %s\n" % (err or ""))
        result["numactl_hardware.txt"] = rc == 0 and bool(out.strip())
    else:
        result["numactl_hardware.txt"] = False
    # numastat
    if tools.get("numastat"):
        rc, out, err, _ = common.run_command(["numastat"], timeout=15)
        ok = rc == 0 and out.strip()
        common.write_text(os.path.join(outdir, "numa", "numastat_system.txt"),
                          out if ok else "unavailable\n# %s\n" % (err or ""))
        result["numastat_system.txt"] = ok
        if pid:
            rc, out, err, _ = common.run_command(["numastat", "-p", str(pid)], timeout=15)
            ok = rc == 0 and out.strip()
            common.write_text(os.path.join(outdir, "numa", "numastat_pid.txt"),
                              out if ok else "unavailable\n# %s\n" % (err or ""))
            result["numastat_pid.txt"] = ok
        else:
            result["numastat_pid.txt"] = False
    else:
        result["numastat_system.txt"] = False
        result["numastat_pid.txt"] = False
    return result
