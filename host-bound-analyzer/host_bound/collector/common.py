# -*- coding: utf-8 -*-
"""采集器公共工具：命令执行 / 快照序列 / 日志。仅标准库，全容错。"""

import logging
import subprocess
import time
from pathlib import Path

from host_bound.util import fsio


def run_command(cmd, timeout=120, cwd=None):
    """执行外部命令，永不抛异常。

    cmd 为列表形式（不经 shell，避免注入与引号问题）。
    返回 (rc, stdout, stderr, elapsed_s)；找不到命令/超时/其他异常一律 rc=-1，
    err 中带原因，调用方据此决定是否落盘与登记 dq note。
    """
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd, timeout=timeout, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        return p.returncode, out, err, time.monotonic() - t0
    except FileNotFoundError:
        return -1, "", "command not found: %s" % (cmd[0] if cmd else "?"), time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return -1, "", "timeout after %.0fs" % timeout, time.monotonic() - t0
    except Exception as e:  # pragma: no cover - 防御性兜底
        return -1, "", repr(e), time.monotonic() - t0


def read_text(path):
    """读取文本，失败返回 None（供快照序列/探测使用）。"""
    text, _enc = fsio.read_text_safe(path)
    return text


def write_text(path, text):
    fsio.write_text(str(path), text)


def write_json(path, obj):
    fsio.write_json(str(path), obj)


class SnapshotSeries(object):
    """把多个时间点的原始文本追加进同一文件。

    头部行 `=== snapshot <label> ts=<相对秒> epoch=<绝对秒> ===` 供解析器切分；
    reader() 返回 None（文件不存在/无权限）时只落一次 "unavailable" 标记，
    避免重复噪音，同时保证解析器能看到该源缺失。
    """

    def __init__(self, path, label):
        self.path = Path(path)
        self.label = label
        self.count = 0
        self.available = None  # None=未探测 / True / False
        self._failed_logged = False

    def capture(self, rel_ts, epoch_ts, reader):
        try:
            text = reader()
        except Exception as e:
            text = None
            if not self._failed_logged:
                self._failed_logged = True
                logging.getLogger("collector").debug("snapshot %s read err: %r", self.label, e)
        if text is None or text == "":
            if self.available is not False:
                self.available = False
                self._append(rel_ts, epoch_ts, "unavailable\n")
            return False
        self.available = True
        self._append(rel_ts, epoch_ts, text)
        return True

    def _append(self, rel_ts, epoch_ts, body):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", errors="replace") as f:
            f.write("=== snapshot %s ts=%.3f epoch=%.3f ===\n" % (self.label, rel_ts, epoch_ts))
            f.write(body if body.endswith("\n") else body + "\n")
        self.count += 1


def setup_logger(outdir=None):
    """采集器日志：stderr + 可选文件 collector.log。

    幂等语义：stderr 句柄只挂一次；outdir 句柄缺失时补挂
    （__init__ 先建 logger，run() 再带 outdir 调用也能落盘）。
    """
    lg = logging.getLogger("collector")
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if not any(isinstance(h, logging.StreamHandler) and
               not isinstance(h, logging.FileHandler) for h in lg.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        lg.addHandler(sh)
    if outdir and not any(isinstance(h, logging.FileHandler)
                          for h in lg.handlers):
        try:
            Path(outdir).mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(str(Path(outdir) / "collector.log"),
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        except Exception:
            pass
    return lg


def monotonic_window(duration, interval, tick_fn):
    """主采样循环：每 interval 秒调用一次 tick_fn(rel_ts, epoch_ts)，
    总时长 duration 秒；sleep 粒度不超过剩余时间。返回实际采样次数。"""
    start = time.monotonic()
    deadline = start + duration
    n = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        tick_fn(now - start, time.time())
        n += 1
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(interval, remain))
    return n
