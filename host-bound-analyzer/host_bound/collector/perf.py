# -*- coding: utf-8 -*-
"""perf 采样：
- L2：perf stat（有目标 pid 用 -p，否则 -a 系统级），时长=duration
- L3：perf record -F 99 -g（默认 cap 60s）+ perf report --stdio + perf script（大文件截断）

L3 必须由用户显式 --level 3 触发（编排器保证），本模块不做二次确认。
"""

import os
import threading

from host_bound.collector import common

PERF_SCRIPT_MAX_BYTES = 256 * 1024 * 1024   # perf script 上限，超出截断并记录
PERF_RECORD_CAP_S = 60                       # record 默认 cap（第九条：默认 60s）


class PerfRunner(threading.Thread):
    """L2+L3 perf 任务线程：stat →（L3）record → report → script。"""

    def __init__(self, outdir, pid, duration, level, perf_path, results, logger):
        super(PerfRunner, self).__init__(name="hb-perf", daemon=True)
        self.outdir = outdir
        self.pid = pid
        self.duration = duration
        self.level = level
        self.perf = perf_path or "perf"
        self.results = results
        self.logger = logger

    def run(self):
        try:
            self._run_stat()
            if int(self.level) >= 3:
                self._run_record()
        except Exception as e:  # pragma: no cover - 防御性兜底
            self.logger.error("perf 线程异常: %r", e)
            self.results["perf_error"] = repr(e)

    # ---------- L2: perf stat ----------
    def _run_stat(self):
        target = ["-p", str(self.pid)] if self.pid else ["-a"]
        cmd = [self.perf, "stat"] + target + ["--", "sleep", "%d" % max(1, int(self.duration))]
        rc, out, err, elapsed = common.run_command(cmd, timeout=self.duration + 90)
        # perf stat 的计数器结果输出到 stderr（"Performance counter stats" 块），
        # 而 run_command 分离 stdout/stderr —— 必须合并两者再落盘与判定，
        # 否则成功场景会误落 "unavailable"。
        body = (out.strip() + "\n" + err.strip()).strip()
        ok = rc == 0 and ("Performance counter stats" in body or "cycles" in body)
        common.write_text(os.path.join(self.outdir, "perf", "perf_stat.txt"),
                          body if body else "unavailable\n")
        if not ok and err.strip():
            common.write_text(os.path.join(self.outdir, "perf", "perf_stat_err.txt"),
                              "# rc=%d\n%s" % (rc, err[:2000]))
        self.results["perf_stat"] = {"ok": bool(ok), "rc": rc,
                                     "elapsed_s": round(elapsed, 1)}
        self.logger.info("perf stat: rc=%d ok=%s", rc, ok)

    # ---------- L3: perf record + report + script ----------
    def _run_record(self):
        cap = min(int(self.duration), PERF_RECORD_CAP_S)
        data = os.path.join(self.outdir, "perf", "perf.data")
        cmd = [self.perf, "record", "-F", "99", "-g"]
        cmd += ["-p", str(self.pid)] if self.pid else ["-a"]
        cmd += ["-o", data, "--", "sleep", str(cap)]
        rc, _out, err, elapsed = common.run_command(cmd, timeout=cap + 90)
        ok = rc == 0 and os.path.exists(data)
        self.results["perf_record"] = {"ok": bool(ok), "rc": rc,
                                       "cap_s": cap, "elapsed_s": round(elapsed, 1)}
        self.logger.info("perf record: rc=%d ok=%s cap=%ds", rc, ok, cap)
        if not ok:
            common.write_text(os.path.join(self.outdir, "perf", "perf_record_err.txt"),
                              "# rc=%d\n%s" % (rc, (err or "")[:2000]))
            return
        # report --stdio（聚合热点表，解析端归因主输入）
        rc2, out2, err2, _ = common.run_command(
            [self.perf, "report", "--stdio", "--no-children",
             "--percent-limit", "0.5", "-i", data], timeout=120)
        ok2 = rc2 == 0 and out2.strip()
        common.write_text(os.path.join(self.outdir, "perf", "perf_report.txt"),
                          out2 if ok2 else "unavailable\n# %s\n" % (err2[:500] or "empty"))
        self.results["perf_report"] = {"ok": bool(ok2), "rc": rc2}
        # perf script（原始调用栈，截断保护）
        rc3, out3, err3, _ = common.run_command(
            [self.perf, "script", "-i", data], timeout=300)
        if rc3 == 0 and out3:
            note = ""
            encoded = out3.encode("utf-8", "replace")
            if len(encoded) > PERF_SCRIPT_MAX_BYTES:
                encoded = encoded[:PERF_SCRIPT_MAX_BYTES]
                note = "\n# TRUNCATED at %d bytes\n" % PERF_SCRIPT_MAX_BYTES
            with open(os.path.join(self.outdir, "perf", "perf_script.txt"), "wb") as f:
                f.write(encoded)
                if note:
                    f.write(note.encode("utf-8"))
            self.results["perf_script"] = {"ok": True, "rc": rc3,
                                           "truncated": bool(note)}
        else:
            common.write_text(os.path.join(self.outdir, "perf", "perf_script.txt"),
                              "unavailable\n# %s\n" % (err3[:500] or "empty"))
            self.results["perf_script"] = {"ok": False, "rc": rc3}
