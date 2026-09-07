# -*- coding: utf-8 -*-
"""perf 输出解析器：perf_stat / perf_report / perf_script / err 文件。

输入契约（collector/perf.py）：
- perf_stat.txt = stdout+stderr 合并文本（计数器块在 "Performance counter stats" 之后）
- perf_report.txt = `perf report --stdio --no-children --percent-limit 0.5`
- perf_script.txt = 原始调用栈（仅留证）；*_err.txt = 失败原因
语义约定（DESIGN 298-299）：
- perf.ipc = instructions / cycles；perf.cache_miss_pct = cache-misses / cache-references × 100
- perf.cpus_utilized = task-clock(msec)/1000 / elapsed(seconds)
- 热点归类（首个事件段，行匹配 symbol+lib 小写子串，按优先级首个命中）：
    comm(通信库) → memcpy → runtime(libgomp/mkl/openblas 等) → python 解释器
    → dataloader(等待/IO 类) → kernel → other
- perf.hotspot_category = top1 符号归类；perf.cpu_bound_pct = 计算类
  (python+runtime+memcpy+other) 样本占比合计——等待/通信/内核占比不计入
- 单文件解析失败 → 特征不设置（规则引擎对缺失特征自动 SKIP，绝不猜测）
"""

import re

from . import common

# 归类规则：(类别, 关键词元组)。顺序即优先级，全部小写匹配
_CAT_RULES = [
    ("comm", ("nccl", "hccl", "gloo", "mpi_", "allreduce", "all_reduce",
              "broadcast_", "allgather", "all_gather", "reduce_scatter")),
    ("memcpy", ("memcpy", "memmove", "memset", "__memmove", "__memcpy")),
    ("runtime", ("gomp", "omp_", "libiomp", "mkl", "cblas", "openblas",
                 "blis", "mkldnn", "onednn", "blas_", "acl", "atb")),
    ("python", ("pyeval", "_pyeval", "pyobject", "py_", "libpython",
                "ceval", "_pyfunction", "python-")),
    ("dataloader", ("pthread_cond", "pthread_mutex", "futex", "__poll",
                    "epoll_wait", "select", "nanosleep", "usleep",
                    "sched_yield", "wait4", "_worker_loop", "pin_memory",
                    "dataloader", "fetcher", "pread", "read", "write")),
]

_ROW_RE = re.compile(
    r"^\s*([\d.]+)%\s+(\S+)\s+(\S+)\s+(.+?)\s*$")

# perf stat 指标名（允许 :u/:uP 等后缀）
_STAT_METRICS = ("task-clock", "cycles", "instructions", "branches",
                 "branch-misses", "cache-references", "cache-misses",
                 "context-switches", "cpu-migrations")


def categorize(symbol, lib):
    """symbol + lib 联合小写匹配，返回类别串。"""
    text = ("%s %s" % (symbol or "", lib or "")).lower()
    if (lib or "").lower().startswith("[kernel") \
            or "[k]" in (symbol or "").lower():
        return "kernel"
    for cat, keys in _CAT_RULES:
        for k in keys:
            if k in text:
                return cat
    return "other"


def _fnum_csv(tok):
    """perf 数值含千分位逗号：'1,234,567.89' → float。"""
    return common.fnum(tok)


def _stat_line_metrics(line):
    """解析 perf stat 单行 → [(name, value)]。秒行与 <not supported> 自动跳过。"""
    if "#" in line:
        line = line.split("#", 1)[0]          # 去掉注释列
    if "seconds time elapsed" in line or "seconds user" in line \
            or "seconds sys" in line:
        v = _fnum_csv(line.split()[0]) if line.split() else None
        return [("elapsed", v)] if v is not None else []
    toks = line.split()
    out = []
    unit_mul = 1.0
    for i, tok in enumerate(toks):
        base = tok.split(":")[0]
        if base in _STAT_METRICS:
            # 数值 = 首个 token；单位前缀（msec/K/M/G）在指标名前
            if not toks:
                break
            v = _fnum_csv(toks[0])
            if v is None:
                break
            mul = 1.0
            for j in range(1, i):
                t = toks[j]
                if t == "msec":
                    mul = 1e-3
                elif t == "K":
                    mul = 1e3
                elif t == "M":
                    mul = 1e6
                elif t == "G":
                    mul = 1e9
            out.append((base, v * mul * unit_mul))
    return out


class PerfStatParser(common.BaseParser):
    """perf_stat.txt：IPC / cache_miss_pct / cpus_utilized。"""

    name = "perf_stat"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        vals = {}
        mode = ""
        for ln in (text or "").splitlines():
            if "Performance counter stats for" in ln:
                mode = ln.strip()
                continue
            for k, v in _stat_line_metrics(ln):
                if k not in vals or k == "elapsed":
                    vals[k] = v
        n_lines = len((text or "").splitlines())
        if not vals:
            result.record(rel, self.name, status="partial",
                          note="无 perf stat 计数行（%d 行）" % n_lines)
            ctx.warn("perf/perf_stat.txt 未解析出计数器")
            return

        lines_rng = "1-%d" % n_lines
        cyc = vals.get("cycles")
        ins = vals.get("instructions")
        if cyc and ins and cyc > 0:
            ipc = ins / cyc
            result.set_feat("perf.ipc", round(ipc, 4), "", rel=rel,
                            lines=lines_rng,
                            snippet="instructions=%.0f cycles=%.0f ipc=%.4f"
                                    % (ins, cyc, ipc),
                            note="perf stat 差分期间整体值")
        cmiss = vals.get("cache-misses")
        cref = vals.get("cache-references")
        if cmiss is not None and cref and cref > 0:
            pct = 100.0 * cmiss / cref
            result.set_feat("perf.cache_miss_pct", round(pct, 2), "%",
                            rel=rel, lines=lines_rng,
                            snippet="cache-misses=%.0f / cache-references=%.0f"
                                    " = %.2f%%" % (cmiss, cref, pct),
                            note="cache-misses 占 cache-references 比例")
        tms = vals.get("task-clock")
        elapsed = vals.get("elapsed")
        if tms and elapsed and elapsed > 0:
            cpus = (tms * 1e-3) / elapsed
            result.set_feat("perf.cpus_utilized", round(cpus, 3), "CPUs",
                            rel=rel, lines=lines_rng,
                            snippet="task-clock=%.0fms elapsed=%.3fs → %.3f CPUs"
                                    % (tms, elapsed, cpus),
                            note="perf stat 窗口平均并行度")
        result.record(rel, self.name, status="ok",
                      note="%s；计数=%s"
                           % (mode or "perf stat",
                              ", ".join("%s=%g" % (k, v)
                                        for k, v in sorted(vals.items()))))


class PerfReportParser(common.BaseParser):
    """perf_report.txt：热点 top 表 → 归类、cpu_bound_pct、top_symbols。"""

    name = "perf_report"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        rows = []
        event_name = ""
        seen_section = False
        for ln in (text or "").splitlines():
            if ln.startswith("#"):
                if "Overhead" in ln:
                    if seen_section:
                        break          # 第二个事件段：只取首个事件，防重复计数
                    seen_section = True
                elif "Samples:" in ln and "of event" in ln:
                    try:
                        event_name = ln.split("of event", 1)[1].strip().strip("'")
                    except IndexError:
                        pass
                continue
            m = _ROW_RE.match(ln)
            if not m:
                continue
            try:
                overhead = float(m.group(1))
            except ValueError:
                continue
            cmd, lib, symbol = m.group(2), m.group(3), m.group(4)
            rows.append({"overhead_pct": overhead, "cmd": cmd, "lib": lib,
                         "symbol": symbol[:160],
                         "category": categorize(symbol, lib)})
        n_lines = len((text or "").splitlines())
        if not rows:
            result.record(rel, self.name, status="partial",
                          note="无热点数据行（%d 行）" % n_lines)
            ctx.warn("perf/perf_report.txt 未解析出热点符号")
            return

        cat_sum = {}
        for r in rows:
            cat_sum[r["category"]] = cat_sum.get(r["category"], 0.0) \
                + r["overhead_pct"]
        top = rows[0]
        lines_rng = "1-%d" % n_lines

        result.set_feat("perf.hotspot_category", top["category"], "", rel=rel,
                        lines=lines_rng,
                        snippet="top1 %.2f%% %s [%s] %s → %s"
                                % (top["overhead_pct"], top["cmd"],
                                   top["lib"], top["symbol"][:80],
                                   top["category"]),
                        note="perf report 首个事件段 top1 符号归类")
        compute = 0.0
        for cat in ("python", "runtime", "memcpy", "other"):
            compute += cat_sum.get(cat, 0.0)
        result.set_feat("perf.cpu_bound_pct", round(compute, 2), "%",
                        rel=rel, lines=lines_rng,
                        snippet="计算类占比合计=%.2f%%（python=%.1f runtime=%.1f"
                                " memcpy=%.1f other=%.1f；dataloader=%.1f"
                                " comm=%.1f kernel=%.1f）"
                                % (compute, cat_sum.get("python", 0.0),
                                   cat_sum.get("runtime", 0.0),
                                   cat_sum.get("memcpy", 0.0),
                                   cat_sum.get("other", 0.0),
                                   cat_sum.get("dataloader", 0.0),
                                   cat_sum.get("comm", 0.0),
                                   cat_sum.get("kernel", 0.0)),
                        note="计算类=python+runtime+memcpy+other；等待/通信/内核不计入")
        top_syms = [{"symbol": r["symbol"], "lib": r["lib"], "cmd": r["cmd"],
                     "overhead_pct": round(r["overhead_pct"], 2),
                     "category": r["category"]} for r in rows[:10]]
        result.set_feat("perf.top_symbols", top_syms, "", rel=rel,
                        lines=lines_rng,
                        snippet="; ".join("%.1f%% %s" % (r["overhead_pct"],
                                                         r["symbol"][:40])
                                          for r in rows[:5]),
                        note="top10 热点符号（含类别）")
        result.record(rel, self.name, status="ok",
                      note="事件=%s 符号=%d 归类=%s"
                           % (event_name or "?", len(rows),
                              ", ".join("%s=%.1f%%" % (k, v)
                                        for k, v in sorted(cat_sum.items(),
                                                           key=lambda kv: -kv[1]))))


class PerfScriptParser(common.BaseParser):
    """perf_script.txt：仅留证与台账（调用栈原文不结构化）。"""

    name = "perf_script"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable", )
            return
        n_lines = len((text or "").splitlines())
        truncated = "TRUNCATED" in (text or "")
        result.record(rel, self.name, status="ok",
                      note="行数=%d%s（原始调用栈仅留证）"
                           % (n_lines, "，已截断" if truncated else ""))


class PerfErrParser(common.BaseParser):
    """perf_stat_err.txt / perf_record_err.txt：失败原因留证。"""

    name = "perf_err"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        head = (text or "").strip()[:200].replace("\n", " | ")
        result.record(rel, self.name, status="failed",
                      note="perf 失败留证: %s" % (head or "empty"))
