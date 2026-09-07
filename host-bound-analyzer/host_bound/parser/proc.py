# -*- coding: utf-8 -*-
"""proc 快照族解析器：/proc/stat、vmstat、loadavg、meminfo、interrupts、softirqs、PSI。

输入契约（collector/system.py）：
- 9 个快照序列文件，头格式 "=== snapshot <label> ts=%.3f epoch=%.3f ==="
- proc_stat.txt 行: "cpu  u n s idle iowait irq softirq steal guest gnice" / "ctxt N" /
  "intr N" / "procs_running N" / "procs_blocked N"
- meminfo.txt / loadavg.txt 为内核原文
语义约定：
- cpu.util_pct 不含 iowait（busy = total - idle - iowait），避免把 IO 等待误判为 CPU 饱和
- 指标缺失/样本不足 → 特征不设置（诊断引擎对缺失特征自动 SKIP，绝不猜测）
"""

from ..util import stats as ustats
from . import common

# /proc/stat cpu 行字段序（除 idle/iowait/guest* 外都算 busy；guest 已计入 user，勿重复）
_CPU_USER, _CPU_NICE, _CPU_SYS, _CPU_IDLE, _CPU_IOWAIT, _CPU_IRQ, _CPU_SOFT, _CPU_STEAL = range(8)


def _pad8(vals):
    out = []
    for i in range(8):
        v = common.fnum(vals[i]) if i < len(vals) else None
        out.append(v if v is not None else 0.0)
    return out


class ProcStatParser(common.BaseParser):
    """proc_stat.txt：CPU 利用率/iowait/上下文切换/运行队列（差分）。"""

    name = "proc_stat"

    def _parse_block(self, lines):
        s = {"cpus": 0, "fields": None, "ctxt": None, "intr": None,
             "procs_running": None, "procs_blocked": None, "cpu_line": ""}
        for ln in lines:
            toks = ln.split()
            if not toks:
                continue
            if toks[0] == "cpu" and s["fields"] is None:
                s["fields"] = _pad8(toks[1:])
                s["cpu_line"] = ln.strip()
            elif toks[0].startswith("cpu") and toks[0][3:].isdigit():
                s["cpus"] += 1
            elif toks[0] == "ctxt" and len(toks) > 1:
                s["ctxt"] = common.inum(toks[1])
            elif toks[0] == "intr" and len(toks) > 1:
                s["intr"] = common.inum(toks[1])
            elif toks[0] == "procs_running" and len(toks) > 1:
                s["procs_running"] = common.inum(toks[1])
            elif toks[0] == "procs_blocked" and len(toks) > 1:
                s["procs_blocked"] = common.inum(toks[1])
        return s if s["fields"] is not None else None

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        samples = []
        for ts, _epoch, lines in common.iter_snapshots(text or ""):
            s = self._parse_block(lines)
            if s is not None:
                s["ts"] = ts
                samples.append(s)
        n_lines = len((text or "").splitlines())
        if not samples:
            result.record(rel, self.name, status="partial",
                          note="无 cpu 数据行（%d 行）" % n_lines)
            ctx.warn("system/proc_stat.txt 未解析出任何 cpu 样本")
            return

        utils, iowaits, ctx_rates, intr_rates = [], [], [], []
        ts_util, ts_ctx = [], []
        blocked_max, running_max = None, None
        for i in range(1, len(samples)):
            a, b = samples[i - 1], samples[i]
            if a["ts"] is None or b["ts"] is None:
                continue
            dt = b["ts"] - a["ts"]
            if dt <= 0:
                continue
            fa, fb = a["fields"], b["fields"]
            d = [fb[k] - fa[k] for k in range(8)]
            if min(d) >= 0:
                total = sum(d)
                if total > 0:
                    idle = d[_CPU_IDLE] + d[_CPU_IOWAIT]
                    utils.append((total - idle) * 100.0 / total)
                    iowaits.append(d[_CPU_IOWAIT] * 100.0 / total)
                    ts_util.append(b["ts"])
            if a["ctxt"] is not None and b["ctxt"] is not None and b["ctxt"] >= a["ctxt"]:
                ctx_rates.append((b["ctxt"] - a["ctxt"]) / dt)
                ts_ctx.append(b["ts"])
            if a["intr"] is not None and b["intr"] is not None and b["intr"] >= a["intr"]:
                intr_rates.append((b["intr"] - a["intr"]) / dt)
            for k, cur in (("procs_blocked", b["procs_blocked"]),
                           ("procs_running", b["procs_running"])):
                if cur is not None:
                    if k == "procs_blocked":
                        blocked_max = cur if blocked_max is None else max(blocked_max, cur)
                    else:
                        running_max = cur if running_max is None else max(running_max, cur)

        feats = result.features
        ev_rel, ev_lines = rel, "1-%d" % n_lines
        snippet = (samples[0]["cpu_line"] or "")[:200]
        if utils:
            util = round(ustats.mean(utils), 2)
            result.set_feat("cpu.util_pct", util, unit="%", rel=ev_rel,
                            lines=ev_lines, snippet=snippet,
                            note="区间差分均值（不含 iowait），n=%d 区间" % len(utils))
            result.set_feat("cpu.util_pct_max", round(max(utils), 2), unit="%")
            feats.add_series("cpu", "util", list(zip(ts_util, [round(v, 2) for v in utils])))
        else:
            ctx.warn("proc_stat 快照不足 2 个或计数回退，cpu.util_pct 未计算")
        if iowaits:
            result.set_feat("cpu.iowait_pct", round(ustats.mean(iowaits), 2), unit="%",
                            rel=ev_rel, lines=ev_lines, snippet=snippet,
                            note="区间差分均值，n=%d" % len(iowaits))
            feats.add_series("cpu", "iowait",
                             list(zip(ts_util, [round(v, 2) for v in iowaits])))
        if len(utils) >= 3:
            result.set_feat("cpu.cv_util", round(ustats.cv(utils), 3), unit="",
                            rel=ev_rel, lines=ev_lines, snippet=snippet,
                            note="区间利用率的变异系数（时间维度波动性），n=%d" % len(utils))
        if ctx_rates:
            result.set_feat("sched.ctx_switch_per_sec", round(ustats.mean(ctx_rates), 1),
                            unit="/s", rel=ev_rel, lines=ev_lines, snippet=snippet,
                            note="ctxt 计数差分/秒，n=%d 区间" % len(ctx_rates))
            feats.add_series("sched", "ctx_switch_per_sec",
                             list(zip(ts_ctx, [round(v, 1) for v in ctx_rates])))
        if intr_rates:
            result.set_feat("sched.intr_per_sec", round(ustats.mean(intr_rates), 1), unit="/s")
        if blocked_max is not None:
            result.set_feat("sched.procs_blocked", blocked_max, unit="个",
                            rel=ev_rel, lines=ev_lines, snippet=snippet,
                            note="窗口内 procs_blocked 最大值（不可中断阻塞）")
        if running_max is not None:
            result.set_feat("sched.procs_running_max", running_max, unit="个")
        if samples[-1]["cpus"] > 0:
            result.set_feat("cpu.core_entries", samples[-1]["cpus"], unit="个",
                            note="/proc/stat 中 cpuN 行数")
        result.record(rel, self.name, events=0, status="ok",
                      note="样本=%d 区间=%d" % (len(samples), len(utils)))


class VmstatParser(common.BaseParser):
    """proc_vmstat.txt：pgmajfault / swap 换页速率。"""

    name = "proc_vmstat"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        blocks = []
        for ts, _epoch, lines in common.iter_snapshots(text or ""):
            kv = {}
            for ln in lines:
                toks = ln.split()
                if len(toks) == 2:
                    kv[toks[0]] = common.inum(toks[1])
            if kv:
                blocks.append((ts, kv))
        if not blocks:
            result.record(rel, self.name, status="partial", note="无键值行")
            return
        keys = ("pgmajfault", "pgfault", "pswpin", "pswpout")
        rates = {k: [] for k in keys}
        ts_pts = {k: [] for k in keys}
        for i in range(1, len(blocks)):
            (t0, a), (t1, b) = blocks[i - 1], blocks[i]
            dt = None if (t0 is None or t1 is None) else t1 - t0
            if not dt or dt <= 0:
                continue
            for k in keys:
                va, vb = a.get(k), b.get(k)
                if va is not None and vb is not None and vb >= va:
                    rates[k].append((vb - va) / dt)
                    ts_pts[k].append(t1)
        feats = result.features
        n_lines = len((text or "").splitlines())
        if rates["pgmajfault"]:
            result.set_feat("mem.pgmajfault_per_sec", round(ustats.mean(rates["pgmajfault"]), 2),
                            unit="/s", rel=rel, lines="1-%d" % n_lines,
                            snippet="pgmajfault 差分", note="区间差分均值，n=%d" % len(rates["pgmajfault"]))
            feats.add_series("mem", "pgmajfault_rate",
                             list(zip(ts_pts["pgmajfault"],
                                      [round(v, 2) for v in rates["pgmajfault"]])))
        if rates["pswpin"] or rates["pswpout"]:
            swap_io = [pin + pout for pin, pout in zip(rates["pswpin"] or [], rates["pswpout"] or [])] \
                if len(rates["pswpin"]) == len(rates["pswpout"]) and rates["pswpin"] \
                else (rates["pswpin"] + rates["pswpout"])
            if swap_io:
                result.set_feat("mem.swap_io_per_sec", round(ustats.mean(swap_io), 2), unit="/s",
                                note="pswpin+pswpout 差分速率")
        if rates["pgfault"]:
            result.set_feat("mem.pgfault_per_sec", round(ustats.mean(rates["pgfault"]), 1), unit="/s")
        result.record(rel, self.name, status="ok",
                      note="块=%d 区间=%d" % (len(blocks), len(rates["pgmajfault"])))


class LoadAvgParser(common.BaseParser):
    """loadavg.txt：负载（按核归一）+ 阻塞进程数兜底。"""

    name = "loadavg"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        l1, l5, l15, blocked = [], [], [], []
        for _ts, _epoch, lines in common.iter_snapshots(text or ""):
            for ln in lines:
                toks = ln.split()
                if len(toks) < 4:
                    continue
                v1, v5, v15 = common.fnum(toks[0]), common.fnum(toks[1]), common.fnum(toks[2])
                if v1 is None:
                    continue
                l1.append(v1)
                l5.append(v5 if v5 is not None else 0.0)
                l15.append(v15 if v15 is not None else 0.0)
                if "/" in toks[3]:
                    run, tot = toks[3].split("/", 1)
                    run, tot = common.inum(run), common.inum(tot)
                    if run is not None and tot is not None and tot >= run:
                        blocked.append(tot - run)
        if not l5:
            result.record(rel, self.name, status="partial", note="无 loadavg 数据行")
            return
        feats = result.features
        result.set_feat("cpu.load1", round(ustats.mean(l1), 2), unit="")
        result.set_feat("cpu.load5", round(ustats.mean(l5), 2), unit="", rel=rel,
                        lines="1-%d" % len((text or "").splitlines()),
                        snippet=(text or "").splitlines()[0][:200],
                        note="窗口内均值，快照=%d" % len(l5))
        result.set_feat("cpu.load15", round(ustats.mean(l15), 2), unit="")
        feats.add_series("cpu", "load5", [(float(i), round(v, 2)) for i, v in enumerate(l5)])
        cores = feats.get("cpu.cores_logical")
        if cores:
            result.set_feat("cpu.load5_per_core", round(ustats.mean(l5) / float(cores), 3),
                            unit="核^-1", rel=rel, lines="1-%d" % len((text or "").splitlines()),
                            snippet=(text or "").splitlines()[0][:200],
                            note="load5 / %s 逻辑核" % cores)
        else:
            ctx.warn("cpu.cores_logical 缺失（capabilities.json 缺失？），cpu.load5_per_core 未计算")
        if blocked and not feats.has("sched.procs_blocked"):
            result.set_feat("sched.procs_blocked", max(blocked), unit="个", rel=rel,
                            lines="1-%d" % len((text or "").splitlines()),
                            snippet=(text or "").splitlines()[0][:200],
                            note="loadavg 兜底：total-running 最大值")
        result.record(rel, self.name, status="ok", note="快照=%d" % len(l5))


class MeminfoParser(common.BaseParser):
    """meminfo.txt：可用内存/swap 占比（窗口内最差值）。"""

    name = "meminfo"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        blocks = []
        for _ts, _epoch, lines in common.iter_snapshots(text or ""):
            kv = common.parse_kv_colon("\n".join(lines))
            total = common.inum(kv.get("MemTotal", ""))
            if total:
                avail = common.inum(kv.get("MemAvailable", ""))
                if avail is None:
                    parts = [common.inum(kv.get(k, "")) for k in ("MemFree", "Buffers", "Cached")]
                    avail = sum(p for p in parts if p is not None) if any(
                        p is not None for p in parts) else None
                st = common.inum(kv.get("SwapTotal", "")) or 0
                sf = common.inum(kv.get("SwapFree", ""))
                swap_used = (st - sf) * 100.0 / st if (st > 0 and sf is not None) else 0.0
                blocks.append({"total": total, "avail": avail, "swap_pct": swap_used,
                               "avail_line": "MemAvailable" in kv})
        if not blocks:
            result.record(rel, self.name, status="partial", note="无 MemTotal 行")
            return
        avails = [b["avail"] for b in blocks if b["avail"] is not None]
        swap_pcts = [b["swap_pct"] for b in blocks]
        feats = result.features
        n_lines = len((text or "").splitlines())
        snippet = "MemTotal: %d kB" % blocks[-1]["total"]
        if avails:
            pct = [a * 100.0 / b["total"] for a, b in zip(avails, [b for b in blocks
                                                                   if b["avail"] is not None])]
            result.set_feat("mem.available_pct", round(min(pct), 2), unit="%", rel=rel,
                            lines="1-%d" % n_lines, snippet=snippet,
                            note="窗口内最差（最小）可用内存占比，快照=%d" % len(pct))
            result.set_feat("mem.available_mb_min", round(min(avails) / 1024.0, 1), unit="MB")
            feats.add_series("mem", "available_pct",
                             [(float(i), round(v, 2)) for i, v in enumerate(pct)])
        result.set_feat("mem.total_mb", round(blocks[-1]["total"] / 1024.0, 1), unit="MB",
                        rel=rel, lines="1-%d" % n_lines, snippet=snippet)
        if swap_pcts:
            result.set_feat("mem.swap_used_pct", round(max(swap_pcts), 2), unit="%", rel=rel,
                            lines="1-%d" % n_lines, snippet=snippet,
                            note="窗口内最差（最大）swap 占比；无 swap 时为 0")
            feats.add_series("mem", "swap_used_pct",
                             [(float(i), round(v, 2)) for i, v in enumerate(swap_pcts)])
        result.record(rel, self.name, status="ok",
                      note="快照=%d MemAvailable=%s" % (
                          len(blocks), "原生" if blocks[-1]["avail_line"] else "估算"))


class InterruptsParser(common.BaseParser):
    """interrupts.txt：留证不做诊断（/proc/stat intr 已提供总量）。"""

    name = "interrupts"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()
        result.record(rel, self.name, status="ok", note="行数=%d（仅留证）" % len(lines))


class SoftirqsParser(common.BaseParser):
    """softirqs.txt：留证不做诊断。"""

    name = "softirqs"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()
        result.record(rel, self.name, status="ok", note="行数=%d（仅留证）" % len(lines))


class PsiParser(common.BaseParser):
    """pressure_{cpu,memory,io}.txt：PSI 压力指标（avg10 窗口内最大值）。"""

    name = "psi"

    def __init__(self, kind):
        self.kind = kind

    def _avg10(self, line):
        for tok in line.split():
            if tok.startswith("avg10="):
                return common.fnum(tok.split("=", 1)[1])
        return None

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        some, full = [], []
        for _ts, _epoch, lines in common.iter_snapshots(text or ""):
            for ln in lines:
                s = ln.strip()
                if s.startswith("some"):
                    v = self._avg10(s)
                    if v is not None:
                        some.append(v)
                elif s.startswith("full"):
                    v = self._avg10(s)
                    if v is not None:
                        full.append(v)
        if not some and not full:
            result.record(rel, self.name, status="partial", note="无 PSI 行（内核未启用 PSI？）")
            return
        if some:
            result.set_feat("psi.%s_some_avg10" % self.kind, round(max(some), 3), unit="%",
                            rel=rel, lines="1-%d" % len((text or "").splitlines()),
                            snippet=(text or "").splitlines()[0][:200],
                            note="窗口内 some avg10 最大值")
            result.features.add_series("psi", self.kind + "_some_avg10",
                                       [(float(i), v) for i, v in enumerate(some)])
        if full:
            result.set_feat("psi.%s_full_avg10" % self.kind, round(max(full), 3), unit="%",
                            note="窗口内 full avg10 最大值")
        result.record(rel, self.name, status="ok", note="some=%d full=%d" % (len(some), len(full)))
