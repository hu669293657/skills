# -*- coding: utf-8 -*-
"""NUMA 相关文件解析：nodes.txt / distance.txt / numactl_hardware.txt / numastat_*。

特征产出（规则驱动）:
  numa.node_count          节点数（nodes.txt 优先，numactl --hardware 兜底）
  numa.remote_access_pct   系统级远端访存占比 = other_node/(local_node+other_node)
                           → HB_NUMA_REMOTE（numa.remote_access_pct >= 30）
  numa.miss_pct            系统级缺页失配占比 = numa_miss/(numa_hit+numa_miss)
                           → HB_NUMA_MISS（numa.miss_pct >= 40）
  numa.crossnode_process   目标进程内存是否分布于多个节点（numastat -p）

numastat / numactl 输出为原始工具 stdout（无快照头），失败文件为
"unavailable\\n# rc=..." 前缀，由 common.is_unavailable_text 统一识别。
"""

import re

from host_bound.parser import common

# 头部行中的节点 token，如 "node0"
_NODE_TOKEN_RE = re.compile(r"^node(\d+)$")
# nodes.txt 中的节点行，如 "node0 cpulist=0-31,64-95"
_NODE_LINE_RE = re.compile(r"^(node\d+)\s+cpulist=(\S+)")
# nodes.txt 中缩进的 meminfo 行，如 "  MemTotal:       32768 kB"
_MEMTOTAL_RE = re.compile(r"^\s*MemTotal:\s+(\d+)\s*kB")
# numastat 系统级表中的经典行标签
_STAT_LABELS = ("numa_hit", "numa_miss", "numa_foreign",
                "interleave_hit", "local_node", "other_node")
_STAT_ROW_RE = re.compile(r"^(numa_\w+|interleave_hit|local_node|other_node)\b")
# numactl --hardware 的 "available: 2 nodes (0-1)"
_AVAILABLE_RE = re.compile(r"available:\s*(\d+)\s+nodes")
# numactl --hardware 距离矩阵行，如 "  0:  10  20"
_DIST_ROW_RE = re.compile(r"^\s*(\d+):\s+(.+)$")


class NumaNodesParser(common.BaseParser):
    """numa/nodes.txt：/sys/devices/system/node 静态拓扑（L1 兜底，必产出）。"""

    name = "numa_nodes"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        lines = text.splitlines()
        nodes = []
        mem_total_kb = 0
        first_line = ""
        for ln in lines:
            m = self._node_line(ln)
            if m:
                if not nodes:
                    first_line = ln.strip()
                nodes.append(m)
            else:
                mm = _MEMTOTAL_RE.match(ln)
                if mm:
                    v = common.inum(mm.group(1))
                    if v:
                        mem_total_kb += v
        if not nodes:
            result.record(rel, self.name, status="empty",
                          note="未发现 nodeX cpulist= 行")
            return
        feats = result.features
        if not feats.has("numa.node_count"):
            result.set_feat("numa.node_count", len(nodes), unit="个",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet=first_line,
                            note="来源 /sys/devices/system/node")
        note = "节点数=%d" % len(nodes)
        if mem_total_kb > 0:
            note += "，节点内存合计≈%.1f GB" % (mem_total_kb / 1048576.0)
        result.record(rel, self.name, events=len(nodes), status="ok", note=note)

    @staticmethod
    def _node_line(ln):
        m = _NODE_LINE_RE.match(ln)
        return m.group(1) if m else None


class NumaDistanceParser(common.BaseParser):
    """numa/distance.txt：node0 到各节点的距离（sysfs 原文，仅备注不设特征）。"""

    name = "numa_distance"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        vals = []
        for tok in text.replace(",", " ").split():
            v = common.inum(tok)
            if v is not None:
                vals.append(v)
        if not vals:
            result.record(rel, self.name, status="empty", note="无有效距离值")
            return
        # 本地距离通常为 10；>10 视为远端节点
        remote = sum(1 for v in vals if v > 10)
        result.record(rel, self.name, events=len(vals), status="ok",
                      note="距离值=%s，最大=%d，远端(>10)=%d 个"
                           % (",".join(str(v) for v in vals[:8]),
                              max(vals), remote))


class NumactlHardwareParser(common.BaseParser):
    """numa/numactl_hardware.txt：numactl --hardware 原始输出。"""

    name = "numactl_hardware"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        feats = result.features
        lines = text.splitlines()
        note_parts = []
        count = None
        start = 0
        for i, ln in enumerate(lines):
            m = _AVAILABLE_RE.search(ln)
            if m:
                count = common.inum(m.group(1))
                start = i
                break
        if count is not None and not feats.has("numa.node_count"):
            result.set_feat("numa.node_count", count, unit="个",
                            rel=rel, lines=str(start + 1),
                            snippet=lines[start].strip(),
                            note="来源 numactl --hardware（兜底）")
            note_parts.append("available=%d nodes" % count)
        # 距离矩阵（node distances: 之后的 \d+: 行）
        in_dist = False
        max_dist = 0
        for ln in lines:
            if "node distances" in ln.lower():
                in_dist = True
                continue
            if not in_dist:
                continue
            m = _DIST_ROW_RE.match(ln)
            if m:
                for tok in m.group(2).split():
                    v = common.inum(tok)
                    if v and v > max_dist:
                        max_dist = v
        if max_dist > 0:
            note_parts.append("最大节点距离=%d" % max_dist)
        result.record(rel, self.name, status="ok",
                      note="；".join(note_parts) if note_parts else "无可用行")


class NumastatSystemParser(common.BaseParser):
    """numa/numastat_system.txt：系统级 numastat（每节点累计计数器）。

    输出形如:
        node0           node1
    numa_hit  1234567   7654321
    ...
    """

    name = "numastat_system"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = text.splitlines()
        vals = {}
        row_lines = {}
        for i, ln in enumerate(lines):
            m = _STAT_ROW_RE.match(ln)
            if not m:
                continue
            label = m.group(1)
            if label not in _STAT_LABELS:
                continue
            nums = []
            for tok in ln.split()[1:]:
                v = common.fnum(tok)
                if v is not None:
                    nums.append(v)
            if nums:
                vals[label] = sum(nums)
                row_lines[label] = i + 1
        if not vals:
            result.record(rel, self.name, status="empty",
                          note="未解析到 numastat 计数行")
            return
        feats = result.features
        note_parts = []
        # miss_pct = numa_miss / (numa_hit + numa_miss) → HB_NUMA_MISS
        hit = vals.get("numa_hit")
        miss = vals.get("numa_miss")
        if hit is not None and miss is not None and (hit + miss) > 0:
            miss_pct = miss * 100.0 / (hit + miss)
            result.set_feat("numa.miss_pct", round(miss_pct, 2), unit="%",
                            rel=rel,
                            lines=str(row_lines.get("numa_miss", 1)),
                            snippet="numa_miss=%d" % miss,
                            note="numa_miss/(numa_hit+numa_miss)")
            note_parts.append("miss_pct=%.1f" % miss_pct)
        # remote_access_pct = other_node / (local_node + other_node) → HB_NUMA_REMOTE
        local = vals.get("local_node")
        other = vals.get("other_node")
        if local is not None and other is not None and (local + other) > 0:
            remote_pct = other * 100.0 / (local + other)
            result.set_feat("numa.remote_access_pct", round(remote_pct, 2),
                            unit="%",
                            rel=rel,
                            lines=str(row_lines.get("other_node", 1)),
                            snippet="other_node=%d local_node=%d" % (other, local),
                            note="other_node/(local_node+other_node)")
            note_parts.append("remote_access_pct=%.1f" % remote_pct)
        if "numa_foreign" in vals:
            note_parts.append("numa_foreign=%d" % vals["numa_foreign"])
        result.record(rel, self.name, events=len(vals), status="ok",
                      note="；".join(note_parts) if note_parts else "仅采集到计数行")


class NumastatPidParser(common.BaseParser):
    """numa/numastat_pid.txt：numastat -p PID（目标进程每节点内存分布，单位 MB）。

    输出形如:
      Per-node process memory usage (in MBs) for PID 123 (python)
                     node0     node1    Total
                -------- -------- --------
      Heap          1.23    45.67    46.90
      ...
      Total         6.24   168.67   174.91
    """

    name = "numastat_pid"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = text.splitlines()
        nodes = []
        total_row = None
        total_idx = 0
        categories = 0
        pid = None
        for i, ln in enumerate(lines):
            toks = ln.split()
            if not toks:
                continue
            # 进程标题行：提取 PID
            if pid is None and "PID" in ln:
                m = re.search(r"PID\s+(\d+)", ln)
                if m:
                    pid = m.group(1)
                continue
            # 表头：识别 nodeN token，忽略 Total 列
            if not nodes and any(_NODE_TOKEN_RE.match(t) for t in toks):
                for t in toks:
                    if _NODE_TOKEN_RE.match(t):
                        nodes.append(t)
                continue
            # 分隔行跳过
            if all(set(t) <= set("-") for t in toks):
                continue
            # 数据行
            nums = []
            for t in toks[1:]:
                v = common.fnum(t)
                nums.append(v if v is not None else 0.0)
            if not nums or len(nums) < len(nodes):
                continue
            if toks[0] == "Total":
                total_row = nums
                total_idx = i + 1
            else:
                categories += 1
        if not nodes:
            result.record(rel, self.name, status="empty",
                          note="未解析到节点表头")
            return
        # 每节点内存：优先 Total 行；否则按类别累加
        per_node = None
        if total_row is not None:
            per_node = total_row[:len(nodes)]
        elif categories > 0:
            per_node = [0.0] * len(nodes)
        if per_node is None:
            result.record(rel, self.name, status="empty",
                          note="仅有表头，无内存数据行")
            return
        total_mem = sum(per_node)
        used_nodes = sum(1 for v in per_node if v >= 0.5)  # >=0.5MB 视为有效驻留
        cross = used_nodes > 1 and total_mem > 0
        feats = result.features
        if not feats.has("numa.crossnode_process"):
            result.set_feat("numa.crossnode_process", cross, unit="",
                            rel=rel, lines=str(total_idx),
                            snippet="Total=%s MB" %
                                    (",".join("%.1f" % v for v in per_node)),
                            note="多节点驻留(>=0.5MB)=%d，总驻留=%.1f MB"
                                 % (used_nodes, total_mem))
        note = "PID=%s" % (pid or "?")
        if total_mem > 0:
            note += "，驻留 %.1f MB 于 %d/%d 节点" % (total_mem, used_nodes,
                                                     len(nodes))
        result.record(rel, self.name, events=categories, status="ok", note=note)
