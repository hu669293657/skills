# -*- coding: utf-8 -*-
"""CPU 维度分析视图：利用率 / 均衡性 / 负载 / 频率。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class CpuAnalyzer(DimAnalyzer):
    dim = "cpu"
    title = "CPU 分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        util, util_lv = metric(
            s, feats, "cpu.util_pct", "CPU 利用率（窗口均值）", "%",
            warn_ge=70, bad_ge=85,
            note="值越高说明 Host 计算侧越繁忙")
        metric(s, feats, "cpu.util_pct_max", "CPU 利用率（窗口峰值）", "%",
               warn_ge=85, bad_ge=95)
        metric(s, feats, "cpu.iowait_pct", "iowait 占比", "%",
               warn_ge=10, bad_ge=20,
               note=">=20%% 通常意味着等待存储/网络 IO")
        metric(s, feats, "cpu.cv_util", "利用率离散度 CV", "",
               warn_ge=0.5, bad_ge=0.8,
               note="CV 大说明核间负载不均（部分核打满、部分空闲）")
        metric(s, feats, "cpu.load5_per_core", "每核负载（load5/核数）", "",
               warn_ge=0.7, bad_ge=1.0,
               note=">=1 表示排队负载超过核数")
        metric(s, feats, "cpu.load1", "负载 load1", "")
        metric(s, feats, "cpu.load15", "负载 load15", "")
        metric(s, feats, "cpu.cores_logical", "逻辑核数", "个", info=True)
        metric(s, feats, "cpu.model", "CPU 型号", "", info=True)
        metric(s, feats, "cpu.freq_mhz_mean", "平均频率", "MHz", info=True)
        metric(s, feats, "cpu.freq_mhz_cv", "频率波动 CV", "", info=True,
               note="波动大可能提示降频/限频")
        metric(s, feats, "cpu.governors", "调频策略", "", info=True)

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        parts = []
        hint = feats.get("cpu.bottleneck_hint")
        if util_lv == "bad":
            parts.append("CPU 高负载（util=%s%%）" % fmt_num(util))
        elif util_lv == "warn":
            parts.append("CPU 负载偏高（util=%s%%）" % fmt_num(util))
        elif util_lv == "ok":
            parts.append("CPU 利用率正常（util=%s%%）" % fmt_num(util))
        else:
            parts.append("CPU 利用率数据缺失")
        iowait = feats.get("cpu.iowait_pct")
        if isinstance(iowait, (int, float)) and iowait >= 10:
            parts.append("存在明显 IO 等待（%.1f%%）" % iowait)
        cv = feats.get("cpu.cv_util")
        if isinstance(cv, (int, float)) and cv > 0.5:
            parts.append("核间负载不均（CV=%.2f）" % cv)
        if hint:
            parts.append("粗判：%s" % hint)
        if n_bad == 0 and n_warn == 0 and n_na >= 4:
            parts.append("（多数指标缺失，建议核对采集等级）")
        s["verdict"] = "；".join(parts) + "。"
        return s
