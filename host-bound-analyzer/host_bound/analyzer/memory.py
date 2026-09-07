# -*- coding: utf-8 -*-
"""内存维度分析视图：可用内存 / swap / 缺页 / 进程内存。"""

from .common import DimAnalyzer, fmt_num, levels_of, metric, new_section, register


@register
class MemoryAnalyzer(DimAnalyzer):
    dim = "memory"
    title = "内存分析"

    def analyze(self, feats, dq):
        s = new_section(self.dim, self.title)

        avail, avail_lv = metric(
            s, feats, "mem.available_pct", "可用内存占比（窗口最差值）", "%",
            warn_ge=20, bad_ge=10, low_bad=True,
            note="<=10%% 时内存回收压力显著")
        swap, swap_lv = metric(
            s, feats, "mem.swap_used_pct", "swap 占比（窗口最差值）", "%",
            warn_ge=5, bad_ge=20,
            note="swap 使用高说明物理内存不足")
        majflt, majflt_lv = metric(
            s, feats, "mem.pgmajfault_per_sec", "主缺页速率", "/s",
            warn_ge=200, bad_ge=1000,
            note="majfault 高说明频繁从盘加载页（映射文件/换页）")
        metric(s, feats, "mem.swap_io_per_sec", "swap 换页速率", "/s",
               warn_ge=50, bad_ge=500,
               note="pswpin+pswpout，>0 即有真实换入换出")
        metric(s, feats, "mem.available_mb_min", "可用内存最小值", "MB", info=True)
        metric(s, feats, "mem.swap_devices", "swap 设备数", "个", info=True)
        metric(s, feats, "process.rss_mb", "目标进程常驻内存", "MB", info=True)
        metric(s, feats, "kernel.swappiness", "vm.swappiness", "", info=True)
        metric(s, feats, "kernel.overcommit_memory", "vm.overcommit_memory", "", info=True)
        metric(s, feats, "device.mem_used_pct", "设备显存占用", "%", info=True)

        # ---- 判定 ----
        n_bad, n_warn, n_na = levels_of(s)
        parts = []
        if avail_lv == "bad":
            parts.append("可用内存仅 %s%%，内存压力显著" % fmt_num(avail))
        elif avail_lv == "warn":
            parts.append("可用内存偏低（%s%%）" % fmt_num(avail))
        if swap_lv == "bad":
            parts.append("swap 占用 %s%%，已在实质性换页" % fmt_num(swap))
        elif swap_lv == "warn":
            parts.append("swap 开始使用（%s%%）" % fmt_num(swap))
        if majflt_lv == "bad":
            parts.append("主缺页 %s/s，频繁盘上取页" % fmt_num(majflt))
        elif majflt_lv == "warn":
            parts.append("主缺页偏高（%s/s）" % fmt_num(majflt))
        swap_io = feats.get("mem.swap_io_per_sec")
        if isinstance(swap_io, (int, float)) and swap_io > 0:
            parts.append("存在换页 IO（%.1f/s）" % swap_io)
        if not parts:
            if n_na >= 4:
                parts.append("内存指标多数缺失，建议核对采集等级")
            else:
                parts.append("内存供给正常")
        s["verdict"] = "；".join(parts) + "。"
        return s
