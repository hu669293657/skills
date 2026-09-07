# -*- coding: utf-8 -*-
"""L2 工具输出解析器：vmstat / mpstat / iostat -x / pidstat -t。

输入契约（collector/tools.py）：
- 四个文件均为工具原生 stdout（无快照头），失败落盘为 "unavailable\n# rc=.."
- vmstat/mpstat/iostat 首个数据块为 since-boot 基线，差分语义下必须剔除
语义约定（与 parser/proc.py 对齐）：
- busy 利用率不含 iowait；mpstat/vmstat 的 busy = 100 - idle - iowait（含 steal/irq/soft）
- 与 proc.py 冲突的特征（cpu.util_pct / cpu.iowait_pct / cpu.cv_util /
  sched.procs_blocked）一律 has() 守卫：proc.py 为权威源，工具输出仅作回退与交叉验证
- pidstat 只产事件与台账，不设特征（进程级聚合口径由 threads.py 负责，避免重复计数）
"""

from ..util import stats as ustats
from . import common


def _hms_to_sec(tok):
    """'12:00:02' → 43202.0；失败返回 None（仅用于 pidstat 事件排序）。"""
    if not tok or tok.count(":") != 2:
        return None
    try:
        h, m, s = tok.split(":")
        return int(h) * 3600.0 + int(m) * 60.0 + float(s)
    except ValueError:
        return None


def _is_noise_device(name):
    """过滤 loop/ram 等无意义块设备，避免其 %util 干扰热点判断。"""
    for pre in ("loop", "ram", "fd", "sr", "dm-", "zram"):
        if name.startswith(pre):
            return True
    return False


class VmstatToolsParser(common.BaseParser):
    """tools/vmstat.txt：CPU 占比 / iowait / 阻塞进程（首个样本为基线，剔除）。"""

    name = "vmstat_tools"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()

        # 列索引来自含 r/b/us/sy/id/wa/cs 的表头行，天然适配列序差异
        idx = None
        start = 0
        for i, ln in enumerate(lines):
            toks = ln.split()
            if toks and toks[0] == "r" and "us" in toks and "wa" in toks \
                    and "cs" in toks and "id" in toks:
                idx = {}
                for k, t in enumerate(toks):
                    if t not in idx:      # 重复列名取首次出现
                        idx[t] = k
                start = i + 1
                break
        if idx is None:
            result.record(rel, self.name, status="partial",
                          note="未找到 vmstat 表头（%d 行）" % len(lines))
            return

        rows = []
        for ln in lines[start:]:
            toks = ln.split()
            if len(toks) < len(idx):
                continue
            row = {}
            ok = True
            for col, k in idx.items():
                v = common.fnum(toks[k]) if k < len(toks) else None
                if v is None:
                    ok = False
                    break
                row[col] = v
            if ok:
                rows.append(row)
        if len(rows) > 1:
            rows = rows[1:]               # 首行 since-boot 基线剔除
        if not rows:
            result.record(rel, self.name, status="partial",
                          note="表头后无可解析数据行")
            return

        utils, iowaits, blocked_vals = [], [], []
        s_util, s_iowait, s_blocked = [], [], []
        for i, r in enumerate(rows):
            st = r.get("st", 0.0) or 0.0  # 无 st 列时按 0
            util = (r.get("us", 0.0) or 0.0) + (r.get("sy", 0.0) or 0.0) + st
            utils.append(util)
            iowaits.append(r.get("wa", 0.0) or 0.0)
            b = r.get("b", 0.0) or 0.0
            blocked_vals.append(b)
            s_util.append((float(i + 1), util))
            s_iowait.append((float(i + 1), iowaits[-1]))
            s_blocked.append((float(i + 1), b))

        feats = result.features
        util_avg = ustats.mean(utils)
        if util_avg is not None and not feats.has("cpu.util_pct"):
            result.set_feat("cpu.util_pct", round(util_avg, 2), "%", rel=rel,
                            lines="1-%d" % len(lines),
                            snippet="vmstat 回退源: us+sy+st 均值=%.2f" % util_avg,
                            note="vmstat 派生（/proc/stat 缺失时回退）")
        wa_avg = ustats.mean(iowaits)
        if wa_avg is not None and not feats.has("cpu.iowait_pct"):
            result.set_feat("cpu.iowait_pct", round(wa_avg, 2), "%", rel=rel,
                            lines="1-%d" % len(lines),
                            snippet="vmstat 回退源: wa 均值=%.2f" % wa_avg,
                            note="vmstat 派生（/proc/stat 缺失时回退）")
        b_max = max(blocked_vals) if blocked_vals else None
        if b_max is not None and not feats.has("sched.procs_blocked"):
            result.set_feat("sched.procs_blocked", int(round(b_max)), "",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet="vmstat 回退源: procs b 最大值=%d" % int(b_max),
                            note="vmstat 派生（loadavg/proc_stat 缺失时回退）")

        result.features.add_series("vmstat", "cpu_util", s_util)
        result.features.add_series("vmstat", "iowait", s_iowait)
        result.features.add_series("vmstat", "procs_blocked", s_blocked)
        result.record(rel, self.name, status="ok",
                      note="样本=%d（已剔除首行 since-boot）" % len(rows))


class MpstatParser(common.BaseParser):
    """tools/mpstat_P_ALL.txt：all 行聚合回退 + 核间 CV 交叉验证（HB_CPU_IMBALANCE）。"""

    name = "mpstat"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()

        idx = None
        all_utils, all_iowaits = [], []
        per_tick_cores = []               # 每 tick 的 [core_util, ...]
        for ln in lines:
            toks = ln.split()
            if not toks or toks[0] == "Average:":
                continue
            if "%idle" in toks and "%usr" in toks:
                idx = {}
                for k, t in enumerate(toks):
                    if t not in idx:
                        idx[t] = k
                continue
            if idx is None or len(toks) <= idx.get("%idle", 99):
                continue
            p_us = idx.get("%usr")
            p_sys = idx.get("%sys")
            p_wait = idx.get("%iowait")
            p_idle = idx.get("%idle")
            p_cpu = idx.get("CPU", 1)
            if None in (p_us, p_sys, p_wait, p_idle) or p_cpu >= len(toks):
                continue
            us = common.fnum(toks[p_us])
            sy = common.fnum(toks[p_sys])
            wa = common.fnum(toks[p_wait])
            idle = common.fnum(toks[p_idle])
            if us is None or sy is None or wa is None or idle is None:
                continue
            util = max(0.0, 100.0 - idle - wa)
            cpu_id = toks[p_cpu]
            if cpu_id == "all":
                all_utils.append(util)
                all_iowaits.append(wa)
                per_tick_cores.append([])   # all 行开新 tick 桶（mpstat 每 tick 先 all 后核行）
            elif cpu_id.isdigit():
                if not per_tick_cores:      # 防御：核行先于 all 行出现
                    per_tick_cores.append([])
                per_tick_cores[-1].append(util)

        cores_cv = []                     # 每 tick 核间变异系数
        for cores in per_tick_cores:
            if len(cores) >= 2:
                cores_cv.append(ustats.cv(cores))
        n_ticks = len(all_utils)

        feats = result.features
        util_avg = ustats.mean(all_utils)
        if util_avg is not None and not feats.has("cpu.util_pct"):
            result.set_feat("cpu.util_pct", round(util_avg, 2), "%", rel=rel,
                            lines="1-%d" % len(lines),
                            snippet="mpstat 回退源: all 行 100-idle-iowait 均值=%.2f"
                                    % util_avg,
                            note="mpstat 派生（/proc/stat 缺失时回退）")
        util_max = max(all_utils) if all_utils else None
        if util_max is not None and not feats.has("cpu.util_pct_max"):
            result.set_feat("cpu.util_pct_max", round(util_max, 2), "%",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet="mpstat 回退源: all 行峰值=%.2f" % util_max,
                            note="mpstat 派生（/proc/stat 缺失时回退）")
        wa_avg = ustats.mean(all_iowaits)
        if wa_avg is not None and not feats.has("cpu.iowait_pct"):
            result.set_feat("cpu.iowait_pct", round(wa_avg, 2), "%", rel=rel,
                            lines="1-%d" % len(lines),
                            snippet="mpstat 回退源: all 行 iowait 均值=%.2f" % wa_avg,
                            note="mpstat 派生（/proc/stat 缺失时回退）")
        cv_avg = ustats.mean(cores_cv)
        if cv_avg is not None:
            if not feats.has("cpu.cv_util"):
                result.set_feat("cpu.cv_util", round(cv_avg, 4), "", rel=rel,
                                lines="1-%d" % len(lines),
                                snippet="mpstat 回退源: 核间 CV 均值=%.4f" % cv_avg,
                                note="mpstat 派生（/proc/stat 核数据缺失时回退）")
            else:
                prev = feats.get("cpu.cv_util")
                result.note("mpstat 核间 CV=%.4f 与 /proc/stat CV=%s 交叉验证"
                            % (cv_avg, ("%.4f" % prev) if prev is not None
                               else "缺失"))

        result.features.add_series(
            "mpstat", "cpu_util",
            [(float(i + 1), v) for i, v in enumerate(all_utils)])
        result.record(rel, self.name, status="ok",
                      note="tick=%d 核=%d 核间CV均值=%s（已剔除 Average 行）"
                           % (n_ticks,
                              len(per_tick_cores[0]) if per_tick_cores else 0,
                              ("%.4f" % cv_avg) if cv_avg is not None else "NA"))


class IostatXParser(common.BaseParser):
    """tools/iostat_x.txt：%util / await（首块基线剔除；热点设备口径）。"""

    name = "iostat_x"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()

        blocks = []                       # [(idx_map, [(dev, toks), ...]), ...]
        idx = None
        cur = None
        for ln in lines:
            toks = ln.split()
            if not toks:
                continue
            if toks[0].startswith("Device"):
                idx = {}
                for k, t in enumerate(toks):
                    if t not in idx:
                        idx[t] = k
                cur = []
                blocks.append((idx, cur))
                continue
            if idx is None or cur is None:
                continue
            if len(toks) != len(idx):
                continue                  # avg-cpu 行/杂行按列数过滤
            if common.fnum(toks[-1]) is None:
                continue                  # 末列 %util 非数值 → 非设备行
            cur.append((toks[0], toks))

        n_blocks = len(blocks)
        if n_blocks <= 1:
            result.record(rel, self.name, status="partial",
                          note="设备数据块=%d（不足 2，无差分意义）" % n_blocks)
            return
        blocks = blocks[1:]               # 首块 since-boot 基线剔除

        hot_util = None                   # 全局最忙 (util, await, dev, tick)
        hot_await = None
        dev_count = set()
        s_util, s_await = [], []
        for i, (bidx, rows) in enumerate(blocks):
            p_util = bidx.get("%util")
            p_await = bidx.get("await")
            p_rw = bidx.get("r_await")
            p_ww = bidx.get("w_await")
            if p_util is None:
                continue
            tick_hot = None               # (util, await, dev)
            for dev, toks in rows:
                if _is_noise_device(dev):
                    continue
                dev_count.add(dev)
                util = common.fnum(toks[p_util])
                if util is None:
                    continue
                if p_await is not None and p_await < len(toks):
                    await_ms = common.fnum(toks[p_await])
                else:
                    r_a = common.fnum(toks[p_rw]) if p_rw is not None \
                        and p_rw < len(toks) else None
                    w_a = common.fnum(toks[p_ww]) if p_ww is not None \
                        and p_ww < len(toks) else None
                    vals = [v for v in (r_a, w_a) if v is not None]
                    await_ms = max(vals) if vals else None
                if tick_hot is None or util > tick_hot[0]:
                    tick_hot = (util, await_ms, dev)
            if tick_hot is not None:
                s_util.append((float(i + 1), tick_hot[0]))
                if tick_hot[1] is not None:
                    s_await.append((float(i + 1), tick_hot[1]))
                if hot_util is None or tick_hot[0] > hot_util[0]:
                    hot_util = (tick_hot[0], tick_hot[2], i + 1)
                if tick_hot[1] is not None \
                        and (hot_await is None or tick_hot[1] > hot_await[0]):
                    hot_await = (tick_hot[1], tick_hot[2], i + 1)

        if hot_util is not None:
            result.set_feat("io.util_pct", round(hot_util[0], 2), "%", rel=rel,
                            lines="1-%d" % len(lines),
                            snippet="iostat 热点设备 %s 峰值 %%util=%.2f"
                                    % (hot_util[1], hot_util[0]),
                            note="最忙设备口径（loop/ram 已过滤）")
        if hot_await is not None:
            result.set_feat("io.await_ms", round(hot_await[0], 2), "ms",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet="iostat 热点设备 %s await 峰值=%.2f"
                                    % (hot_await[1], hot_await[0]),
                            note="最忙设备 await 峰值（r_await/w_await 取大）")

        result.features.add_series("iostat", "util_pct", s_util)
        result.features.add_series("iostat", "await_ms", s_await)
        result.record(rel, self.name, status="ok",
                      note="块=%d 设备=%d（已剔除首块 since-boot）"
                           % (len(blocks), len(dev_count)))


class PidstatParser(common.BaseParser):
    """tools/pidstat_t.txt：仅产事件与台账（线程 CPU% / 上下文切换），不设特征。

    pidstat -t -u -r -d -w 每 tick 依次输出 CPU/内存/IO/上下文 四个分段，
    以含 TGID+TID 的表头行分段；本解析器只取 CPU 段与上下文切换段。
    """

    name = "pidstat_t"

    def _section(self, toks):
        has_tgid = "TGID" in toks and "TID" in toks
        if not has_tgid:
            return None
        if "%CPU" in toks:
            return "cpu"
        if "cswch/s" in toks:
            return "ctx"
        if "kB_rd/s" in toks or "minflt-s" in toks or "%MEM" in toks:
            return "other"
        return "other"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = (text or "").splitlines()

        idx = None
        sec = None
        n_cpu_ev = 0
        n_ctx_ev = 0
        tids = set()
        for ln in lines:
            toks = ln.split()
            if not toks:
                continue
            stype = self._section(toks)
            if stype is not None:
                idx = {}
                for k, t in enumerate(toks):
                    if t not in idx:
                        idx[t] = k
                sec = stype
                continue
            if toks[0] == "Average:" or idx is None:
                continue
            p_tgid = idx.get("TGID", 2)
            p_tid = idx.get("TID", 3)
            if p_tid >= len(toks):
                continue
            tid_tok = toks[p_tid]
            tgid = common.inum(toks[p_tgid]) if p_tgid < len(toks) else None
            ts = _hms_to_sec(toks[0])
            if sec == "cpu":
                p_cpu = idx.get("%CPU")
                if p_cpu is None or p_cpu >= len(toks):
                    continue
                cpu_val = common.fnum(toks[p_cpu])
                if cpu_val is None:
                    continue
                if tid_tok != "-":
                    try:
                        tid = int(tid_tok)
                    except ValueError:
                        continue
                    tids.add(tid)
                    result.add_event(ts if ts is not None else 0.0,
                                     "thread_cpu", source=rel, pid=tgid,
                                     tid=tid, cpu_pct=cpu_val)
                    n_cpu_ev += 1
            elif sec == "ctx":
                p_nvc = idx.get("nvcswch/s")
                if p_nvc is None or p_nvc >= len(toks):
                    continue
                nvc = common.fnum(toks[p_nvc])
                if nvc is None or tid_tok == "-":
                    continue
                try:
                    tid = int(tid_tok)
                except ValueError:
                    continue
                result.add_event(ts if ts is not None else 0.0, "thread_ctx",
                                 source=rel, pid=tgid, tid=tid,
                                 nonvol_per_sec=nvc)
                n_ctx_ev += 1

        if n_cpu_ev == 0 and n_ctx_ev == 0:
            result.record(rel, self.name, status="partial",
                          note="未解析出线程级行（%d 行）" % len(lines))
            return
        result.record(rel, self.name, status="ok",
                      note="线程=%d cpu事件=%d ctx事件=%d（特征由 threads.py 权威派生）"
                           % (len(tids), n_cpu_ev, n_ctx_ev))
