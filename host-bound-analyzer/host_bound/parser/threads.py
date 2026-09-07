# -*- coding: utf-8 -*-
"""threads/threads.csv 解析器：线程数 / 超配比 / 非自愿切换 / 热线程集中度。

输入契约（collector/thread.py）：
- 表头 ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed
- ts 为采集相对秒（整数）；utime/stime 为累计 jiffies，缺失时 -1
- cpu_allowed 为 Cpus_allowed_list（可含逗号，如 "0-31,64-95"），因此按 maxsplit 切列
语义约定（DESIGN 315：线程总数 vs 核数 → oversub_ratio；热线程 CPU 集中度 top1_pct）：
- 活跃线程 = 窗口内 utime+stime 存在增量的 tid（真正在抢 CPU 的"工作线程"）
- oversub_ratio = 活跃线程数 / 可用核数（核数优先 cpu.cores_logical，
  缺失时回退 cpu_allowed 掩码大小，并在证据中注明来源）
- sched.nonvoluntary_ctx_per_sec = Σ_tids Δctx_nonvol / dt 的窗口均值（权威源，无其他解析器设置）
- affinity_skew = 1 - 多数派亲和掩码占比（0=全部同亲和；仅末快照集合）
"""

from ..util import stats as ustats
from . import common
from .common import CLK_TCK


def mask_size(mask):
    """'0-31,64-95' → 64；空/异常返回 None。"""
    if not mask or mask == "-1":
        return None
    total = 0
    for seg in mask.split(","):
        seg = seg.strip()
        if not seg:
            continue
        if "-" in seg:
            a, _sep, b = seg.partition("-")
            lo, hi = common.inum(a), common.inum(b)
            if lo is None or hi is None or hi < lo:
                return None
            total += hi - lo + 1
        else:
            v = common.inum(seg)
            if v is None:
                return None
            total += 1
    return total or None


class ThreadsCsvParser(common.BaseParser):
    """threads_csv：目标进程线程表差分（线程级 CPU 抢占与切换风暴的权威来源）。"""

    name = "threads_csv"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()
        if not lines or not lines[0].strip().startswith("ts,"):
            result.record(rel, self.name, status="partial",
                          note="缺少 CSV 表头（%d 行）" % len(lines))
            return

        ticks = []                        # [(ts, {tid: row_dict}), ...]
        for ln in lines[1:]:
            parts = ln.split(",", 8)      # cpu_allowed 可含逗号
            if len(parts) < 8:
                continue
            ts = common.fnum(parts[0])
            tid = common.inum(parts[1])
            if ts is None or tid is None:
                continue
            row = {"tid": tid, "name": parts[2], "state": parts[3],
                   "utime": common.inum(parts[4]),
                   "stime": common.inum(parts[5]),
                   "ctx_vol": common.inum(parts[6]),
                   "ctx_nonvol": common.inum(parts[7]),
                   "mask": parts[8].strip() if len(parts) > 8 else ""}
            if not ticks or ticks[-1][0] != ts:
                ticks.append((ts, {}))
            ticks[-1][1][tid] = row

        if len(ticks) < 2:
            result.record(rel, self.name, status="partial",
                          note="快照 tick=%d（不足 2，无法差分）" % len(ticks))
            return

        # ---- 差分：非自愿切换速率 / 线程级 CPU 增量 ----
        nonvol_rates = []                 # [(t1, rate)]
        active_per_tick = []              # [(t1, n_active)]
        cpu_delta = {}                    # tid -> Σ Δ(utime+stime) jiffies
        names, masks, states = {}, {}, {}
        for i in range(1, len(ticks)):
            t0, rows0 = ticks[i - 1]
            t1, rows1 = ticks[i]
            dt = (t1 or 0.0) - (t0 or 0.0)
            if dt <= 0:
                continue
            nvol_sum, n_active = 0, 0
            for tid, r1 in rows1.items():
                r0 = rows0.get(tid)
                if r0 is None:
                    continue
                if r1["ctx_nonvol"] is not None and r0["ctx_nonvol"] is not None \
                        and r1["ctx_nonvol"] >= 0 and r0["ctx_nonvol"] >= 0:
                    d = r1["ctx_nonvol"] - r0["ctx_nonvol"]
                    if d > 0:
                        nvol_sum += d
                du = ds = 0
                if r1["utime"] is not None and r0["utime"] is not None \
                        and r1["utime"] >= 0 and r0["utime"] >= 0:
                    du = r1["utime"] - r0["utime"]
                if r1["stime"] is not None and r0["stime"] is not None \
                        and r1["stime"] >= 0 and r0["stime"] >= 0:
                    ds = r1["stime"] - r0["stime"]
                if du + ds > 0:
                    n_active += 1
                    cpu_delta[tid] = cpu_delta.get(tid, 0) + du + ds
                if r1["name"]:
                    names[tid] = r1["name"]
                if r1["state"]:
                    states[tid] = r1["state"]
                if r1["mask"]:
                    masks[tid] = r1["mask"]
            nonvol_rates.append((t1, nvol_sum / dt))
            active_per_tick.append((t1, n_active))

        n_threads = max(len(rows) for _ts, rows in ticks)
        n_active_peak = max((n for _t, n in active_per_tick), default=0)

        feats = result.features
        # ---- 可用核数：cpu.cores_logical 优先，回退亲和掩码 ----
        cores = feats.get("cpu.cores_logical")
        cores_src = "cpu.cores_logical"
        if not cores:
            sizes = [mask_size(m) for m in masks.values()]
            sizes = [s for s in sizes if s]
            if sizes:
                cores = max(sizes)
                cores_src = "cpu_allowed 掩码"

        # ---- oversub_ratio = 峰值活跃线程 / 可用核数 ----
        if cores and n_active_peak > 0:
            ratio = n_active_peak / float(cores)
            result.set_feat(
                "thread.oversub_ratio", round(ratio, 3), "", rel=rel,
                lines="1-%d" % len(lines),
                snippet="活跃线程峰值=%d / 可用核=%d(%s)"
                        % (n_active_peak, cores, cores_src),
                note="活跃=窗口内 utime+stime 有增量的线程；线程总数=%d"
                     % n_threads)
        if n_threads:
            result.set_feat("thread.target_threads", n_threads, "", rel=rel,
                            lines="1-%d" % len(lines),
                            snippet="threads.csv 最大并发线程数=%d" % n_threads,
                            note="目标进程线程总数（含空闲池线程）")
        if n_active_peak:
            result.set_feat("thread.active_threads", n_active_peak, "",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet="窗口内活跃线程峰值=%d" % n_active_peak,
                            note="有 CPU 时间增量的线程数")

        # ---- 热线程集中度 top1_pct ----
        if cpu_delta:
            total = sum(cpu_delta.values())
            top_tid, top_v = None, 0
            for tid, v in cpu_delta.items():
                if v > top_v:
                    top_tid, top_v = tid, v
            if total > 0 and top_tid is not None:
                pct = 100.0 * top_v / float(total)
                result.set_feat(
                    "thread.hot_thread_pct", round(pct, 2), "%", rel=rel,
                    lines="1-%d" % len(lines),
                    snippet="top1 tid=%s(%s) 占进程 CPU 时间 %.1f%%"
                            % (top_tid, names.get(top_tid, "?"), pct),
                    note="top1 集中度（jiffies 差分占比）")

        # ---- 亲和偏斜：末快照多数派掩码 ----
        if masks:
            cnt = {}
            for m in masks.values():
                cnt[m] = cnt.get(m, 0) + 1
            maj_mask = max(cnt, key=lambda k: (cnt[k], k))
            skew = 1.0 - cnt[maj_mask] / float(len(masks))
            result.set_feat(
                "thread.affinity_skew", round(skew, 3), "", rel=rel,
                lines="1-%d" % len(lines),
                snippet="线程亲和掩码 %d 种，多数派 %s 占 %d/%d"
                        % (len(cnt), maj_mask, cnt[maj_mask], len(masks)),
                note="0=全部同亲和，1=完全分散")

        # ---- 非自愿上下文切换速率（均值口径，峰值入 series/note）----
        if nonvol_rates:
            rates = [v for _t, v in nonvol_rates]
            avg = ustats.mean(rates)
            peak = max(rates)
            result.set_feat(
                "sched.nonvoluntary_ctx_per_sec", round(avg, 1), "/s", rel=rel,
                lines="1-%d" % len(lines),
                snippet="Σ_tids Δctx_nonvol/dt 窗口均值=%.1f/s 峰值=%.1f/s"
                        % (avg, peak),
                note="threads.csv 差分（CLK_TCK 假设 %g）" % CLK_TCK)
            result.features.add_series("threads", "nonvol_rate", nonvol_rates)
        if active_per_tick:
            result.features.add_series("threads", "active_threads",
                                       active_per_tick)

        top5 = sorted(cpu_delta.items(), key=lambda kv: -kv[1])[:5]
        top5_txt = "; ".join("tid=%s(%s) %.1fs"
                             % (tid, names.get(tid, "?"),
                                v / float(CLK_TCK))
                             for tid, v in top5) or "无"
        result.record(rel, self.name, status="ok",
                      note="tick=%d 线程=%d 活跃峰值=%d；top5 CPU: %s"
                           % (len(ticks), n_threads, n_active_peak, top5_txt))
