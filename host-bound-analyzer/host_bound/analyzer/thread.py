# -*- coding: utf-8 -*-
"""线程维度分析视图：超订比 / 活跃线程 / 热点集中度 / 亲和偏斜。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class ThreadAnalyzer(DimAnalyzer):
    dim = "thread"
    title = "线程分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        ratio, ratio_lv = metric(
            s, feats, "thread.oversub_ratio", "线程超订比（活跃峰值/核数）", "",
            warn_ge=1.2, bad_ge=2.0,
            note=">2 且伴随高频上下文切换时为典型线程超订")
        total, _lv = metric(s, feats, "thread.total", "目标进程线程总数", "个", info=True)
        metric(s, feats, "thread.active_threads", "活跃线程峰值", "个", info=True)
        metric(s, feats, "thread.target_threads", "threads.csv 并发线程数", "个", info=True)
        metric(s, feats, "thread.hot_thread_pct", "热点线程 top1 集中度", "%",
               warn_ge=60, bad_ge=85,
               note="集中度高说明进程 CPU 时间集中在个别线程（可能单线程瓶颈）")
        metric(s, feats, "thread.affinity_skew", "线程亲和偏斜度", "",
               warn_ge=0.3, bad_ge=0.6,
               note="偏斜大说明线程亲和配置分散，可能加剧跨核/跨 NUMA 迁移")
        metric(s, feats, "thread.omp_threads_setting", "OMP 线程数设置", "个", info=True)
        metric(s, feats, "cpu.cores_logical", "逻辑核数", "个", info=True)

        # ---- OMP 设置 vs 核数的交叉说明 ----
        omp = feats.get("thread.omp_threads_setting")
        cores = feats.get("cpu.cores_logical")
        if isinstance(omp, (int, float)) and isinstance(cores, (int, float)) and omp > cores:
            s["missing"].append(
                "注意：OMP 线程数（%d）> 逻辑核数（%d），存在线程超订配置"
                % (int(omp), int(cores)))

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        parts = []
        if ratio_lv == "bad":
            parts.append("线程明显超订（ratio=%s）" % fmt_num(ratio))
        elif ratio_lv == "warn":
            parts.append("线程轻度超订（ratio=%s）" % fmt_num(ratio))
        elif ratio_lv == "ok":
            parts.append("线程数与核数匹配（ratio=%s）" % fmt_num(ratio))
        else:
            parts.append("超订比数据缺失（需 threads.csv 或线程总数+核数）")
        hot = feats.get("thread.hot_thread_pct")
        if isinstance(hot, (int, float)) and hot >= 60:
            parts.append("CPU 时间集中于单一线程（%.1f%%），存在单线程瓶颈风险" % hot)
        if isinstance(omp, (int, float)) and isinstance(cores, (int, float)) and omp > cores:
            parts.append("OMP 线程数超过核数")
        if n_bad == 0 and n_warn == 0:
            parts.append("未发现线程维度风险")
        s["verdict"] = "；".join(parts) + "。"
        return s
