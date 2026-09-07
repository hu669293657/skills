# -*- coding: utf-8 -*-
"""调度维度分析视图：上下文切换 / 非自愿切换 / 阻塞队列 / 中断。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class SchedulerAnalyzer(DimAnalyzer):
    dim = "scheduler"
    title = "调度器分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        ctx, ctx_lv = metric(
            s, feats, "sched.ctx_switch_per_sec", "上下文切换速率", "/s",
            warn_ge=100000, bad_ge=300000,
            note="高切换率伴随高 CPU 时提示调度竞争/线程超订")
        nonvol, nonvol_lv = metric(
            s, feats, "sched.nonvoluntary_ctx_per_sec", "非自愿上下文切换速率", "/s",
            warn_ge=5000, bad_ge=20000,
            note="非自愿切换高说明可运行线程在争抢 CPU 时间片")
        blocked, blocked_lv = metric(
            s, feats, "sched.procs_blocked", "不可中断阻塞进程数（峰值）", "个",
            warn_ge=2, bad_ge=5,
            note="持续 >0 通常指向存储 IO 等待")
        metric(s, feats, "sched.procs_running_max", "可运行队列长度（峰值）", "个",
               warn_ge=4, bad_ge=8,
               note="明显超过核数说明 CPU 排队")
        metric(s, feats, "sched.intr_per_sec", "中断速率", "/s",
               warn_ge=50000, bad_ge=200000, info=False)
        metric(s, feats, "kernel.isolcpus", "isolcpus 隔离核", "", info=True)

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        parts = []
        if nonvol_lv == "bad":
            parts.append("非自愿切换显著（%s/s），CPU 时间片争抢严重" % fmt_num(nonvol))
        elif nonvol_lv == "warn":
            parts.append("非自愿切换偏高（%s/s）" % fmt_num(nonvol))
        if ctx_lv == "bad":
            parts.append("上下文切换极高（%s/s）" % fmt_num(ctx))
        elif ctx_lv == "warn":
            parts.append("上下文切换偏高（%s/s）" % fmt_num(ctx))
        if blocked_lv == "bad":
            parts.append("存在不可中断阻塞进程（峰值 %s 个），优先排查存储 IO"
                         % fmt_num(blocked))
        elif blocked_lv == "warn":
            parts.append("偶发不可中断阻塞（峰值 %s 个）" % fmt_num(blocked))
        if not parts:
            if n_na >= 3:
                parts.append("调度指标多数缺失，建议核对采集等级")
            else:
                parts.append("调度负载处于正常范围")
        s["verdict"] = "；".join(parts) + "。"
        return s
