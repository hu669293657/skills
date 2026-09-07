# -*- coding: utf-8 -*-
"""perf 维度分析视图：IPC / cache 命中 / 热点函数归类。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class PerfAnalyzer(DimAnalyzer):
    dim = "perf"
    title = "Perf 微架构分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        ipc, ipc_lv = metric(
            s, feats, "perf.ipc", "IPC（每周期指令数）", "",
            warn_ge=0.8, bad_ge=0.5, low_bad=True,
            note="IPC 低提示访存/前端瓶颈或序列化点；仅 L3 采集可用")
        miss, miss_lv = metric(
            s, feats, "perf.cache_miss_pct", "cache-miss 占比", "%",
            warn_ge=5, bad_ge=10,
            note="cache miss 高提示访存密集/局部性差")
        metric(s, feats, "perf.cpus_utilized", "等效利用核数", "CPUs", info=True,
               note="perf stat task-clock 换算")
        bound, bound_lv = metric(
            s, feats, "perf.cpu_bound_pct", "CPU 计算型热点占比", "%",
            warn_ge=40, bad_ge=60,
            note="热点样本中计算类符号占比，高即计算瓶颈证据")
        metric(s, feats, "perf.hotspot_category", "热点归类", "", info=True)
        metric(s, feats, "perf.top_symbols", "热点符号 TOP", "", info=True)
        metric(s, feats, "kernel.perf_paranoid", "perf_paranoid 等级", "", info=True,
               note=">1 时 perf 采集中受限，可能拿不到全量事件")

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        parts = []
        if ipc_lv == "bad":
            parts.append("IPC %s 偏低，存在访存/前端瓶颈" % fmt_num(ipc))
        elif ipc_lv == "ok" and feats.has("perf.ipc"):
            parts.append("IPC %s，流水线利用良好" % fmt_num(ipc))
        if miss_lv == "bad":
            parts.append("cache miss %s%%，访存局部性差" % fmt_num(miss))
        elif miss_lv == "warn":
            parts.append("cache miss 偏高（%s%%）" % fmt_num(miss))
        if bound_lv == "bad":
            parts.append("计算型热点占 %s%%，CPU 计算瓶颈证据明确" % fmt_num(bound))
        elif bound_lv == "warn":
            parts.append("计算型热点占 %s%%" % fmt_num(bound))
        cat = feats.get("perf.hotspot_category")
        if cat:
            parts.append("热点归类：%s" % cat)
        if not parts:
            if n_na >= 4:
                parts.append("perf 数据缺失（L3 采集或 perf 权限受限时无此维度）")
            else:
                parts.append("perf 维度未见异常")
        s["verdict"] = "；".join(parts) + "。"
        return s
