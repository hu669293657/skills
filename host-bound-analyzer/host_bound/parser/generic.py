# -*- coding: utf-8 -*-
"""未知产物兜底解析：不提取任何特征，仅登记台账（第九条：未知文件必须
被识别并说明，而不是被静默丢弃）。
"""

import os

from host_bound.parser import common

# 二进制/压缩产物：只登记，不读取
_SKIP_EXT = (".gz", ".tgz", ".zip", ".xz", ".bz2", ".data", ".bin", ".so",
             ".pyc", ".png", ".jpg")

# 超大文本阈值（字节）：超过则只登记大小，不读内容
_MAX_READ = 8 * 1024 * 1024


class UnknownFileParser(common.BaseParser):
    """所有未匹配专用解析器的文件：登记存在性、大小与首行预览。"""

    name = "generic"

    def parse(self, path, rel, ctx, result):
        try:
            size = os.path.getsize(path)
        except OSError:
            result.skip(rel, "stat-failed")
            return
        ext = os.path.splitext(rel)[1].lower()
        if ext in _SKIP_EXT:
            result.record(rel, self.name, status="skipped-binary",
                          note="二进制/压缩产物（%d 字节），不作文本解析" % size)
            return
        if size > _MAX_READ:
            result.record(rel, self.name, status="ok",
                          note="文件过大（%d 字节），仅登记不读取" % size)
            return
        text, enc = common.read_file(path)
        if not text or not text.strip():
            result.record(rel, self.name, status="empty", note="空文件")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = text.splitlines()
        preview = " | ".join(ln.strip() for ln in lines[:3] if ln.strip())[:200]
        result.record(rel, self.name, status="ok",
                      note="未知产物已登记（%d 行, %d 字节, enc=%s）: %s"
                           % (len(lines), size, enc, preview))
        ctx.warn("未知产物 %s（未匹配专用解析器，已登记未解析）" % rel)
