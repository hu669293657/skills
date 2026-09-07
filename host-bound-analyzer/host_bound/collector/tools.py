# -*- coding: utf-8 -*-
"""L2 工具采样：vmstat / pidstat / iostat / mpstat 按 interval 循环 duration 秒。

设计：每个工具一次性下达 `工具 <interval> <count>` 的命令（自带循环），
各自独立线程执行，超时 duration+60s；命令失败只落 err 文件并登记 note。
count 至少 2（首个样本常为 since-boot 基线，解析端差分需要 ≥2 行）。
"""

import os
import threading

from host_bound.collector import common


def _count(duration, interval):
    return max(2, int(duration / max(interval, 0.1)) + 1)


def build_jobs(outdir, tools, pid, duration, interval):
    """返回 [(job_name, cmd, outfile)]。工具缺失或参数不适配 → 不加入。"""
    n = _count(duration, interval)
    jobs = []
    if tools.get("vmstat"):
        jobs.append(("vmstat", ["vmstat", "%d" % max(1, int(interval)), str(n)],
                     os.path.join(outdir, "tools", "vmstat.txt")))
    if tools.get("mpstat"):
        jobs.append(("mpstat", ["mpstat", "-P", "ALL",
                                "%d" % max(1, int(interval)), str(n)],
                     os.path.join(outdir, "tools", "mpstat_P_ALL.txt")))
    if tools.get("iostat"):
        jobs.append(("iostat", ["iostat", "-x",
                                "%d" % max(1, int(interval)), str(n)],
                     os.path.join(outdir, "tools", "iostat_x.txt")))
    if tools.get("pidstat"):
        base = ["pidstat", "-t", "-u", "-r", "-d", "-w"]
        if pid:
            base += ["-p", str(pid)]
        base += ["%d" % max(1, int(interval)), str(n)]
        jobs.append(("pidstat", base, os.path.join(outdir, "tools", "pidstat_t.txt")))
    return jobs


class ToolRunner(threading.Thread):
    """在独立线程中执行一个 L2 工具采样任务，结果写文件 + 记录状态。"""

    def __init__(self, name, cmd, outfile, duration, results, logger):
        super(ToolRunner, self).__init__(name="hb-tool-%s" % name, daemon=True)
        self.job_name = name
        self.cmd = cmd
        self.outfile = outfile
        self.timeout = duration + 60
        self.results = results
        self.logger = logger

    def run(self):
        rc, out, err, elapsed = common.run_command(self.cmd, timeout=self.timeout)
        ok = rc == 0 and out.strip()
        try:
            if ok:
                common.write_text(self.outfile, out)
            else:
                common.write_text(self.outfile, "unavailable\n# rc=%d\n# %s\n"
                                  % (rc, (err or "empty output")[:400]))
        except Exception as e:  # 写盘失败不致命
            self.logger.error("L2 工具 %s 写文件失败: %r", self.job_name, e)
            ok = False
        self.results[self.job_name] = {
            "ok": bool(ok), "rc": rc, "elapsed_s": round(elapsed, 1),
            "file": os.path.relpath(self.outfile, os.path.dirname(
                os.path.dirname(self.outfile))) if ok else "",
        }
        self.logger.info("L2 工具 %s: rc=%d elapsed=%.1fs ok=%s",
                         self.job_name, rc, elapsed, ok)
