# -*- coding: utf-8 -*-
"""线程级采集：/proc/<pid>/task/<tid>/ 每 interval 全量线程表 → threads/threads.csv。

列：ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed
- utime/stime 为累计 jiffies（解析端两次快照差分可得线程级 CPU%）
- ctx_* 来自 status（voluntary / nonvoluntary_ctxt_switches）
- cpu_allowed 来自 status 的 Cpus_allowed_list（第九条：绑核与亲和必须可回溯）
"""

import glob
import os

from host_bound.collector import common

CSV_HEADER = "ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed\n"


def _parse_stat(path):
    text = common.read_text(path)
    if not text:
        return None
    try:
        head, rest = text.rsplit(")", 1)
    except ValueError:
        return None
    f = rest.split()
    if len(f) < 13:
        return None
    return head.split("(", 1)[-1], int(f[11]), int(f[12])  # name, utime, stime


def _parse_status(path):
    """status: Name / State / voluntary_ctxt_switches / nonvoluntary / Cpus_allowed_list"""
    text = common.read_text(path)
    out = {"name": "", "state": "", "ctx_vol": -1, "ctx_nonvol": -1, "cpu_allowed": ""}
    if not text:
        return out
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "Name":
            out["name"] = v
        elif k == "State":
            out["state"] = v.split()[0] if v else ""
        elif k == "voluntary_ctxt_switches":
            out["ctx_vol"] = int(v)
        elif k == "nonvoluntary_ctxt_switches":
            out["ctx_nonvol"] = int(v)
        elif k == "Cpus_allowed_list":
            out["cpu_allowed"] = v
    return out


class ThreadSampler(object):
    def __init__(self, outdir, pid):
        self.pid = pid
        self.path = os.path.join(outdir, "threads", "threads.csv")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.rows = 0
        self._header_written = False
        self.task_dir = "/proc/%s/task" % pid

    def available(self):
        return os.path.isdir(self.task_dir)

    def capture(self, rel_ts, epoch_ts, fh=None):
        """追加一次全量线程表快照。fh 为打开的文件句柄（由调用方复用）。"""
        if not self.available():
            return False
        if not self._header_written:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(CSV_HEADER)
            self._header_written = True
        fh = fh or open(self.path, "a", encoding="utf-8")
        try:
            for tid_dir in sorted(glob.glob(os.path.join(self.task_dir, "[0-9]*")),
                                  key=lambda p: int(os.path.basename(p))):
                tid = os.path.basename(tid_dir)
                st = _parse_stat(os.path.join(tid_dir, "stat"))
                su = _parse_status(os.path.join(tid_dir, "status"))
                if st is None and not su["name"]:
                    continue
                name = (st[0] if st else "") or su["name"]
                utime, stime = (st[1], st[2]) if st else (-1, -1)
                fh.write("%d,%s,%s,%s,%d,%d,%d,%d,%s\n" % (
                    int(rel_ts), tid, name, su["state"], utime, stime,
                    su["ctx_vol"], su["ctx_nonvol"], su["cpu_allowed"]))
                self.rows += 1
            return True
        finally:
            if fh is None:
                fh.close()

    def summary(self):
        return {"available": self.available(), "rows": self.rows,
                "path": "threads/threads.csv"}
