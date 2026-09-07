# -*- coding: utf-8 -*-
"""File IO helpers with encoding fallback and safe read."""

import io
import os
import re
import time


def read_text_safe(path, max_bytes=8 * 1024 * 1024):
    """Read a text file trying utf-8 then gbk then latin-1. Returns (text, encoding) or (None, None)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, None
    if size > max_bytes:
        # Cap huge files: read prefix only (analysis is sample-based anyway).
        size = max_bytes
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc, errors="strict") as f:
                return f.read(), enc
        except (UnicodeDecodeError, OSError):
            continue
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), "utf-8(replace)"
    except OSError:
        return None, None


def write_text(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_json(path, obj, indent=2):
    import json
    write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False, default=str))


def read_json(path):
    import json
    text, _ = read_text_safe(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    r"|(\d{2}:\d{2}:\d{2})"
    r"|(\[\s*\d+\.\d+\s*\])"
)


def sniff_has_timestamp(line):
    return bool(_TS_RE.search(line))


def relative_lines(path, start, end):
    """Return 'start-end' style line marker for evidence; 1-based, inclusive."""
    return "%d-%d" % (start, end)
