# -*- coding: utf-8 -*-
"""NUMA 维度分析视图：跨节点访问 / 远端命中率 / 节点拓扑。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class NumaAnalyzer(DimAnalyzer):
    dim = "numa"
    title = "NUMA 分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        remote, remote_lv = metric(
            s, feats, "numa.remote_access_pct", "跨节点访存占比", "%",
            warn_ge=15, bad_ge=30,
            note="占比高说明进程/线程与内存分布跨 NUMA 节点，访存延迟放大")
        miss, miss_lv = metric(
            s, feats, "numa.miss_pct", "NUMA 命中缺失率", "%",
            warn_ge=20, bad_ge=40,
            note="numastat -m 口径：本地命中率偏低")
        metric(s, feats, "numa.node_count", "NUMA 节点数", "个", info=True)
        metric(s, feats, "numa.available", "NUMA 可用", "", info=True)
        metric(s, feats, "numa.crossnode_process", "跨节点进程", "", info=True,
               note="进程内存与 CPU 位于不同节点时为真")
        metric(s, feats, "kernel.numa_balancing", "内核自动 NUMA 均衡", "", info=True)
        metric(s, feats, "cpu.sockets", "物理插槽", "个", info=True)

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        nodes = feats.get("numa.node_count")
        parts = []
        if isinstance(nodes, (int, float)) and nodes and nodes < 2:
            parts.append("单节点（node_count=%s），无 NUMA 跨节点风险" % fmt_num(nodes))
        else:
            if remote_lv == "bad":
                parts.append("跨节点访存严重（%s%%），建议绑核/绑内存"
                             % fmt_num(remote))
            elif remote_lv == "warn":
                parts.append("跨节点访存偏高（%s%%）" % fmt_num(remote))
            elif remote_lv == "ok":
                parts.append("跨节点访存可控（%s%%）" % fmt_num(remote))
            if miss_lv == "bad":
                parts.append("本地命中率缺失率 %s%%，访存局部性差" % fmt_num(miss))
            elif miss_lv == "warn":
                parts.append("本地命中率偏低（缺失 %s%%）" % fmt_num(miss))
        if isinstance(nodes, (int, float)) and nodes and nodes >= 2 \
                and remote_lv == "na" and miss_lv == "na":
            s["missing"].append(
                "多节点系统但缺少 numastat 量化数据，NUMA 风险无法评估（建议补采）")
            parts.append("NUMA 量化数据缺失")
        if not parts:
            parts.append("未发现 NUMA 维度风险")
        s["verdict"] = "；".join(parts) + "。"
        return s
