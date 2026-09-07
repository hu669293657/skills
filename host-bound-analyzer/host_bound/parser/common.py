# -*- coding: utf-8 -*-
"""parser 共享基础设施：快照切分、类型探测、上下文与结果对象。

约定：
- Parser 只做"文本 → 结构化"，不做诊断；所有数值解析失败一律返回 None，绝不猜测。
- 所有事件携带 source=相对路径，证据携带行号区间，保证可回溯到原始文件。
- 快照头兼容两种格式（collect 落盘时设备侧无 epoch 字段）：
    === snapshot <label> ts=%.3f epoch=%.3f ===
    === snapshot <label> ts=%.3f ===
"""

import os
import re

from ..models.events import Event, Timeline
from ..models.metrics import FeatureSet
from ..models.evidence import Evidence
from ..util import fsio

# Linux 用户态默认 CLK_TCK=100（采集侧未记录 getconf，解析侧按假设处理并留痕）
CLK_TCK = 100.0

SNAP_RE = re.compile(
    r"^===\s*snapshot\s+(\S+)\s+ts=(-?\d+(?:\.\d+)?)"
    r"(?:\s+epoch=(-?\d+(?:\.\d+)?))?\s*===\s*$")

UNAVAILABLE_RE = re.compile(r"^\s*unavailable\s*$", re.IGNORECASE)


def is_unavailable_text(text):
    """collector 失败落盘格式: 'unavailable' / 'unavailable\\n# rc=..'."""
    if not text:
        return True
    head = text.lstrip()[:64].lower()
    return head.startswith("unavailable")


def iter_snapshots(text):
    """把文本切成快照块列表 [(rel_ts, epoch_or_None, [lines]), ...]。

    - 无快照头的文件 → 单块 (None, None, 所有行)（prelude 也归入该块之前的独立块？否：
      prelude 与正文合并为第一块，保持"一个文件至少一个块"的简单约定）。
    - 空文本 → [(None, None, [])]。
    """
    blocks = []
    cur_ts = None
    cur_epoch = None
    cur_lines = []
    seen_header = False
    for line in text.splitlines():
        m = SNAP_RE.match(line)
        if m:
            if seen_header or cur_lines:
                blocks.append((cur_ts, cur_epoch, cur_lines))
            cur_ts = float(m.group(2))
            cur_epoch = float(m.group(3)) if m.group(3) is not None else None
            cur_lines = []
            seen_header = True
        else:
            cur_lines.append(line)
    if not seen_header and not cur_lines and not blocks:
        return [(None, None, [])]
    blocks.append((cur_ts, cur_epoch, cur_lines))
    return blocks


_FNUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def fnum(tok):
    """宽松数值解析：去逗号/百分号/常用单位后缀；失败返回 None。"""
    if tok is None:
        return None
    s = str(tok).strip().rstrip("%")
    s = s.replace(",", "")
    for suf in ("KB", "MB", "GB", "MiB", "GiB", "kB", "kb", "ms", "us", "s"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    if not s or not _FNUM_RE.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def inum(tok):
    v = fnum(tok)
    if v is None:
        return None
    try:
        return int(round(v))
    except (TypeError, ValueError):
        return None


_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_(\)\-]*?)\s*:\s*(.+?)\s*$")


def parse_kv_colon(text):
    """'Key: value unit' / 'Key:       value kB' → {key: value_str}。"""
    out = {}
    for line in text.splitlines():
        m = _KV_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def split_ws(line):
    return line.split()


class ParseContext(object):
    """解析上下文：数据质量登记、根目录、manifest。"""

    def __init__(self, root, dq, manifest=None, case=None):
        self.root = root
        self.dq = dq
        self.manifest = manifest if isinstance(manifest, dict) else {}
        self.case = case

    def warn(self, msg):
        self.dq.warn(msg)


class ParserResult(object):
    """discover() 的汇总产物：时间线 + 原始特征 + 解析台账。"""

    def __init__(self):
        self.timeline = Timeline()
        self.features = FeatureSet()
        self.notes = []       # 全局解析备注（str）
        self.parsed = []      # {"file","type","events","status","note"}
        self.skipped = []     # {"file","reason"}

    def note(self, msg):
        self.notes.append(str(msg))

    def add_event(self, ts, kind, source, pid=None, tid=None, cpu=None,
                  node=None, duration=0.0, **metrics):
        ev = Event(ts=ts, duration=duration, event=kind, pid=pid, tid=tid,
                   cpu=cpu, node=node, source=source, metrics=metrics)
        self.timeline.add(ev)
        return ev

    def set_feat(self, key, value, unit="", rel=None, lines="", snippet="",
                 note=""):
        """登记带证据指针的特征；evidence.source=相对路径可回溯。"""
        ev = None
        if rel is not None:
            ev = Evidence(metric=key, value=value, unit=unit, source=rel,
                          lines=lines or "1", snippet=(snippet or "")[:400],
                          note=note)
        self.features.set(key, value, unit=unit, evidence=[ev] if ev else None,
                          note=note)

    def record(self, rel, ptype, events=0, status="ok", note=""):
        self.parsed.append({"file": rel, "type": ptype, "events": int(events),
                            "status": status, "note": note})

    def skip(self, rel, reason):
        self.skipped.append({"file": rel, "reason": reason})

    def event_counts(self):
        out = {}
        for ev in self.timeline.events:
            out[ev.event] = out.get(ev.event, 0) + 1
        return out

    def to_dict(self):
        lo, hi = self.timeline.window()
        return {
            "event_counts": self.event_counts(),
            "timeline_window": {"start": lo, "end": hi},
            "parsed_files": self.parsed,
            "skipped_files": self.skipped,
            "notes": self.notes,
        }


class BaseParser(object):
    """所有解析器基类：单文件异常隔离由 registry 负责。"""

    name = "base"

    def parse(self, path, rel, ctx, result):
        raise NotImplementedError


def read_file(path):
    """编码自适应读取（utf-8→gbk→latin-1→replace）。返回 (text, encoding)。"""
    return fsio.read_text_safe(path)


def ev_from_lines(rel, start, lines):
    """证据片段：行区间 + 前 400 字符。"""
    return {"lines": "%d-%d" % (start, max(start, start + len(lines) - 1)),
            "snippet": "\n".join(lines)[:400]}
