# -*- coding: utf-8 -*-
"""脱敏：--sanitize 时在打包前对全部文本文件执行规则替换。

规则（对齐 DESIGN 5.5）：
  1. 敏感键行值 → ***   （token/secret/password/api_key/AK/SK/authorization）
  2. IPv4 → 10.0.0.x
  3. hostname（含 FQDN）→ host-1
  4. username → user
  5. 家目录绝对路径（/home/<x>、/root）→ /path/to
顺序：先掩码敏感行（避免值中的 IP/路径先被替换），再做其余替换。
注意：规则 2 会把形如 1.2.3.4 的版本号一并替换——脱敏是显式可选项，
正确性优先于便利，此取舍已写入 manifest.notes。
"""

import os
import re

from host_bound.collector import common

IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")

SENSITIVE_LINE_RE = re.compile(
    r"(?im)^(\s*[A-Za-z0-9_./-]*(?:token|secret|password|passwd|authorization"
    r"|credential|api[_-]?key|_ak|_sk)[A-Za-z0-9_./-]*\s*[:=]\s*)(\S+)\s*$")

HOME_RE = re.compile(r"/home/[A-Za-z0-9_.\-]+")
ROOT_RE = re.compile(r"/root(?=/|\s|$|['\"\]])")


class Sanitizer(object):
    def __init__(self, hostname="", username=""):
        self.hostname = hostname or ""
        self.fqdn = ""
        if hostname:
            try:
                import socket
                self.fqdn = socket.getfqdn() or ""
            except Exception:
                self.fqdn = ""
        self.username = username or ""
        self.stats = {"files": 0, "replacements": 0}

    # ---------- 单文本脱敏 ----------
    def sanitize_text(self, text):
        n = [0]

        def _mask(m):
            n[0] += 1
            return m.group(1) + "***"

        text = SENSITIVE_LINE_RE.sub(_mask, text)
        text, c1 = IP_RE.subn("10.0.0.x", text)
        n[0] += c1
        for secret, repl in ((self.fqdn, "host-1"), (self.hostname, "host-1")):
            if secret and secret not in ("localhost",):
                c = text.count(secret)
                if c:
                    text = text.replace(secret, repl)
                    n[0] += c
        if self.username:
            c = len(re.findall(r"\b%s\b" % re.escape(self.username), text))
            if c:
                text = re.sub(r"\b%s\b" % re.escape(self.username), "user", text)
                n[0] += c
        text, c2 = HOME_RE.subn("/path/to", text)
        text, c3 = ROOT_RE.subn("/path/to", text)
        n[0] += c2 + c3
        self.stats["replacements"] += n[0]
        return text

    # ---------- 目录树脱敏 ----------
    def sanitize_tree(self, root):
        """遍历目录树下所有文件做文本替换（二进制文件跳过）。"""
        root = str(root)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                path = os.path.join(dirpath, fn)
                # tar 包/二进制不处理
                if fn.endswith((".tar.gz", ".tgz", ".data", ".pyc")):
                    continue
                text, _enc = _read_as_text(path)
                if text is None:
                    continue
                new = self.sanitize_text(text)
                if new != text:
                    try:
                        with open(path, "w", encoding="utf-8", errors="replace") as f:
                            f.write(new)
                        self.stats["files"] += 1
                    except OSError:
                        pass
        return dict(self.stats)


def _read_as_text(path, max_bytes=64 * 1024 * 1024):
    try:
        if os.path.getsize(path) > max_bytes:
            return None, None
    except OSError:
        return None, None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    if b"\x00" in data[:4096]:  # 二进制启发：头部含 NUL
        return None, None
    return data.decode("utf-8", "replace"), "utf-8(replace)"


def current_identity():
    """返回 (hostname, username)，供 Sanitizer 与 manifest 使用。"""
    import getpass
    import socket
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    return host, user
