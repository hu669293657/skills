# -*- coding: utf-8 -*-
"""IO 维度分析视图：iowait / 盘利用率 / 等待时延 / 阻塞队列。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class IoAnalyzer(DimAnalyzer):
    dim = "io"
    title = "IO 分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        iowait, iowait_lv = metric(
            s, feats, "cpu.iowait_pct", "iowait 占比", "%",
            warn_ge=10, bad_ge=20,
            note="CPU 花在等待 IO 的时间比例，>=20%% 为强 IO Bound 信号")
        util, util_lv = metric(
            s, feats, "io.util_pct", "热点盘利用率", "%",
            warn_ge=70, bad_ge=85,
            note="iostat 口径：最繁忙设备的 %util 均值")
        await_ms, await_lv = metric(
            s, feats, "io.await_ms", "热点盘平均等待时延", "ms",
            warn_ge=20, bad_ge=50,
            note="await 升高说明设备层排队")
        blocked, blocked_lv = metric(
            s, feats, "sched.procs_blocked", "不可中断阻塞进程数（峰值）", "个",
            warn_ge=2, bad_ge=5)

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        parts = []
        signals = 0
        if iowait_lv == "bad":
            parts.append("iowait %s%%，CPU 大量时间在等 IO" % fmt_num(iowait))
            signals += 1
        elif iowait_lv == "warn":
            parts.append("iowait %s%%，存在 IO 等待" % fmt_num(iowait))
            signals += 1
        if util_lv == "bad":
            parts.append("热点盘利用率 %s%%，设备饱和" % fmt_num(util))
            signals += 1
        elif util_lv == "warn":
            parts.append("热点盘利用率 %s%%" % fmt_num(util))
            signals += 1
        if await_lv == "bad":
            parts.append("盘等待时延 %sms，设备层排队明显" % fmt_num(await_ms))
            signals += 1
        elif await_lv == "warn":
            parts.append("盘等待时延 %sms" % fmt_num(await_ms))
            signals += 1
        if blocked_lv == "bad":
            parts.append("阻塞进程峰值 %s 个与 IO 等待相互印证" % fmt_num(blocked))
            signals += 1
        if signals >= 2:
            s["verdict"] = "IO Bound 证据充分（%d 项指标越限）：%s。" % (signals, "；".join(parts))
        elif signals == 1:
            s["verdict"] = "存在 IO 压力迹象：%s。" % "；".join(parts)
        else:
            if n_na >= 3:
                parts.append("IO 指标多数缺失（需 iostat -x 周期数据），建议补采")
            else:
                parts.append("未发现 IO 瓶颈")
            s["verdict"] = "；".join(parts) + "。"
        return s
