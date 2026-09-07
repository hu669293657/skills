# -*- coding: utf-8 -*-
"""目标进程相关文件解析：target_summary/cmdline/environ/status/limits/
smaps_rollup/io/fd_count/process_tree/ps 与 top 快照。

规则驱动特征:
  thread.total             目标进程线程总数（target_summary.stat.threads，
                           /proc/status 兜底）→ 配合 oversub 判定
  thread.omp_threads_setting  OMP_NUM_THREADS（environ.txt）→ HB_THREAD_002
  dl.workers_setting       *WORKERS* 环境变量（environ.txt）→ HB_DL_002
其余（rss/fd_limit/framework 观测等）为参考指标，仅供报告展示，不参与规则。
"""

import json

from host_bound.parser import common

# environ 中 DataLoader worker 数的候选键（命中即取，优先级从左到右）
_WORKER_KEYS = ("DATALOADER_NUM_WORKERS", "DATALOADER_WORKERS",
                "NUM_WORKERS", "WORKERS")


def _env_lines(text):
    """提取 environ.txt 中的 KEY=VALUE 行 → dict（保持出现顺序）。"""
    env = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if "=" in ln:
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _first_int(value):
    """取值中第一个可解析整数（兼容 "8,8,8" 分层写法）；失败返回 None。"""
    for tok in (value or "").replace(",", " ").split():
        v = common.inum(tok)
        if v is not None:
            return v
    return None


class TargetSummaryParser(common.BaseParser):
    """process/target_summary.json：目标进程一次性详情汇总。"""

    name = "target_summary"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        try:
            data = json.loads(text)
        except ValueError:
            result.skip(rel, "bad-json")
            return
        if not isinstance(data, dict) or not data.get("found"):
            result.record(rel, self.name, status="empty",
                          note="target not found")
            return
        feats = result.features
        note_parts = []
        stat = data.get("stat") or {}
        threads = stat.get("threads")
        if threads and not feats.has("thread.total"):
            result.set_feat("thread.total", int(threads), unit="个",
                            rel=rel, lines="1",
                            snippet=json.dumps({"stat": stat},
                                               ensure_ascii=False)[:300],
                            note="target_summary.stat.threads")
            note_parts.append("threads=%s" % threads)
        exe = data.get("exe") or ""
        if exe and not feats.has("process.exe"):
            result.set_feat("process.exe", exe, unit="", rel=rel, lines="1",
                            note="目标进程可执行文件")
        cmdline = data.get("cmdline") or []
        if cmdline and not feats.has("process.cmdline"):
            joined = " ".join(str(c) for c in cmdline)
            result.set_feat("process.cmdline", joined[:200], unit="",
                            rel=rel, lines="1", snippet=joined[:120],
                            note="目标进程命令行")
        fd = data.get("fd_count")
        if isinstance(fd, int) and fd >= 0 and not feats.has("process.fd_count"):
            result.set_feat("process.fd_count", fd, unit="个", rel=rel,
                            lines="1", note="target_summary.fd_count")
        note_parts.append("cmdline=%d 段" % len(cmdline))
        result.record(rel, self.name, status="ok",
                      note="；".join(note_parts) if note_parts else "ok")


class CmdlineParser(common.BaseParser):
    """process/cmdline.txt：每行一个 argv（兜底，target_summary 优先）。"""

    name = "cmdline"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = [ln for ln in text.splitlines() if ln.strip()]
        feats = result.features
        if lines and not feats.has("process.cmdline"):
            joined = " ".join(lines)
            result.set_feat("process.cmdline", joined[:200], unit="",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet=joined[:120], note="cmdline.txt")
        result.record(rel, self.name, status="ok",
                      note="argv=%d 段" % len(lines))


class EnvironParser(common.BaseParser):
    """process/environ.txt：白名单环境变量 → OMP 线程数 / DataLoader worker 数。"""

    name = "environ"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        env = _env_lines(text)
        if not env:
            result.record(rel, self.name, status="empty",
                          note="无可解析的 KEY=VALUE 行")
            return
        feats = result.features
        note_parts = ["保留变量=%d" % len(env)]
        # OMP_NUM_THREADS → thread.omp_threads_setting（HB_THREAD_002）
        if "OMP_NUM_THREADS" in env:
            n = _first_int(env["OMP_NUM_THREADS"])
            if n is not None and n >= 0 and not feats.has("thread.omp_threads_setting"):
                result.set_feat("thread.omp_threads_setting", n, unit="个",
                                rel=rel, lines="1",
                                snippet="OMP_NUM_THREADS=%s" % env["OMP_NUM_THREADS"],
                                note="环境变量 OMP_NUM_THREADS")
                note_parts.append("OMP_NUM_THREADS=%d" % n)
        # *WORKERS* → dl.workers_setting（HB_DL_002）
        for key in _WORKER_KEYS:
            if key in env:
                n = _first_int(env[key])
                if n is not None and n >= 0 and not feats.has("dl.workers_setting"):
                    result.set_feat("dl.workers_setting", n, unit="个",
                                    rel=rel, lines="1",
                                    snippet="%s=%s" % (key, env[key]),
                                    note="环境变量 %s" % key)
                    note_parts.append("%s=%d" % (key, n))
                    break
        # MKL_NUM_THREADS 仅记录
        if "MKL_NUM_THREADS" in env:
            note_parts.append("MKL_NUM_THREADS=%s" % env["MKL_NUM_THREADS"])
        result.record(rel, self.name, status="ok", note="；".join(note_parts))


class StatusParser(common.BaseParser):
    """process/status.txt：/proc/<pid>/status 原文（线程数兜底 + 亲和性备注）。"""

    name = "proc_status"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        threads = None
        allowed = ""
        for ln in text.splitlines():
            if ln.startswith("Threads:"):
                threads = common.inum(ln.split(":", 1)[1])
            elif ln.startswith("Cpus_allowed_list:"):
                allowed = ln.split(":", 1)[1].strip()
        feats = result.features
        note_parts = []
        if threads and not feats.has("thread.total"):
            result.set_feat("thread.total", threads, unit="个", rel=rel,
                            lines="1", snippet="Threads: %d" % threads,
                            note="/proc/status 兜底")
            note_parts.append("Threads=%d" % threads)
        if allowed:
            note_parts.append("Cpus_allowed_list=%s" % allowed)
        result.record(rel, self.name, status="ok",
                      note="；".join(note_parts) if note_parts else "ok")


class LimitsParser(common.BaseParser):
    """process/limits.txt：Max open files → process.fd_limit（参考指标）。"""

    name = "limits"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        for ln in text.splitlines():
            if "Max open files" in ln:
                toks = ln.split()
                # 形如 "Max open files  1024  4096  62914567  ..."
                nums = [common.inum(t) for t in toks[3:5] if common.fnum(t)]
                soft = nums[0] if nums else None
                if soft is not None:
                    feats = result.features
                    if not feats.has("process.fd_limit"):
                        result.set_feat("process.fd_limit", soft, unit="个",
                                        rel=rel, lines="1", snippet=ln.strip()[:80],
                                        note="limits Max open files（soft）")
                    result.record(rel, self.name, status="ok",
                                  note="Max open files soft=%s" % soft)
                    return
        result.record(rel, self.name, status="empty", note="未找到 Max open files 行")


class SmapsRollupParser(common.BaseParser):
    """process/smaps_rollup.txt：Rss/Pss/Swap → 进程内存参考指标（MB）。"""

    name = "smaps_rollup"

    _KB = 1024.0 * 1024.0

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        vals = {}
        for ln in text.splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            toks = v.split()
            if toks:
                n = common.fnum(toks[0])
                if n is not None:
                    vals[k.strip()] = n
        feats = result.features
        note_parts = []
        if "Rss" in vals and not feats.has("process.rss_mb"):
            result.set_feat("process.rss_mb", round(vals["Rss"] / self._KB, 1),
                            unit="MB", rel=rel, lines="1",
                            snippet="Rss: %d kB" % int(vals["Rss"]),
                            note="目标进程常驻内存")
            note_parts.append("Rss=%.1fMB" % (vals["Rss"] / self._KB))
        if "Pss" in vals:
            note_parts.append("Pss=%.1fMB" % (vals["Pss"] / self._KB))
        if "Swap" in vals and vals["Swap"] > 0:
            note_parts.append("Swap=%.1fMB" % (vals["Swap"] / self._KB))
        result.record(rel, self.name, status="ok",
                      note="；".join(note_parts) if note_parts else "ok")


class ProcIoParser(common.BaseParser):
    """process/io.txt：/proc/<pid>/io 单次快照（无窗口，仅记录不设速率）。"""

    name = "proc_io"

    _WANT = ("rchar", "wchar", "read_bytes", "write_bytes")

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        vals = {}
        for ln in text.splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            k = k.strip()
            if k in self._WANT:
                n = common.inum(v.strip())
                if n is not None:
                    vals[k] = n
        if not vals:
            result.record(rel, self.name, status="empty", note="无 io 计数")
            return
        note = "；".join("%s=%s" % (k, _fmt_bytes(vals[k])) for k in self._WANT
                        if k in vals)
        result.record(rel, self.name, events=len(vals), status="ok",
                      note=note + "（单次快照，无速率）")


class FdCountParser(common.BaseParser):
    """process/fd_count.txt：`open_fds=N`（fd 数兜底）。"""

    name = "fd_count"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        n = None
        for ln in text.splitlines():
            if ln.startswith("open_fds="):
                n = common.inum(ln.split("=", 1)[1])
        if n is None or n < 0:
            result.record(rel, self.name, status="empty", note="fd 不可用")
            return
        feats = result.features
        if not feats.has("process.fd_count"):
            result.set_feat("process.fd_count", n, unit="个", rel=rel,
                            lines="1", snippet="open_fds=%d" % n)
        result.record(rel, self.name, status="ok", note="open_fds=%d" % n)


class ProcessTreeParser(common.BaseParser):
    """process/process_tree.json：全量进程树（节点数与最大线程进程备注）。"""

    name = "process_tree"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        try:
            tree = json.loads(text)
        except ValueError:
            result.skip(rel, "bad-json")
            return
        count = [0]
        max_thr = (0, None)      # (threads, pid)

        def walk(node):
            if not isinstance(node, dict):
                return
            count[0] += 1
            thr = node.get("threads") or 0
            if isinstance(thr, int) and thr > max_thr[0]:
                max_thr[0] = thr
                max_thr[1] = node.get("pid")
            for c in node.get("children") or []:
                walk(c)

        for child in tree.get("children") or []:
            walk(child)
        note = "节点数=%d" % count[0]
        if max_thr[0] > 0:
            note += "，最大线程进程 pid=%s threads=%d" % (max_thr[1], max_thr[0])
        result.record(rel, self.name, events=count[0], status="ok", note=note)


class PsThreadsParser(common.BaseParser):
    """process/ps_threads.txt：ps -T 输出（线程数 / psr 去重核数交叉验证）。"""

    name = "ps_threads"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            result.record(rel, self.name, status="empty", note="无数据行")
            return
        header = lines[0].split()
        nthread = len(lines) - 1
        psrs = set()
        try:
            idx = header.index("PSR")
            for ln in lines[1:]:
                toks = ln.split()
                if len(toks) > idx and toks[idx].isdigit():
                    psrs.add(toks[idx])
        except ValueError:
            pass
        note = "线程行=%d" % nthread
        if psrs:
            note += "，占用核=%d（%s）" % (len(psrs), ",".join(sorted(psrs))[:40])
        result.record(rel, self.name, events=nthread, status="ok", note=note)


class RawSnapshotParser(common.BaseParser):
    """通用原始文本记录器：ps_aux/pstree/top_batch 等仅需入台账的文件。"""

    def __init__(self, kind):
        self.name = kind

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = [ln for ln in text.splitlines() if ln.strip()]
        result.record(rel, self.name, events=len(lines), status="ok",
                      note="原始快照 %d 行" % len(lines))


def _fmt_bytes(n):
    """字节数人性化（用于 note）。"""
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return "%.1f%s" % (n / float(div), unit)
    return "%dB" % n
