# -*- coding: utf-8 -*-
"""采集编排器：环境探测 → 能力文件 → 目标解析 → 三级采样窗口 → 后处理
→ sanitize → manifest → tar.gz。

启动流程（对齐 DESIGN 5.1）：
  1. mkdir + logger
  2. probe_environment() → capabilities.json
  3. 目标进程解析（--pid / --auto-detect / 无目标则只采系统级）
  4. 静态采集（environment/system/numa/process-tree/ps/framework/device 静态）
  5. 采样窗口：主线程 L1 循环（system+threads）；工作线程 L2 工具组、
     L2/L3 perf、设备循环采样
  6. 窗口后：第二次进程树对比、线程归类、framework 汇总
  7. dq 汇总 → sanitize（可选）→ manifest.json → tar.gz（可选）
"""

import os
import time

from host_bound.collector import common
from host_bound.collector import environment as env_mod
from host_bound.collector import framework as fw_mod
from host_bound.collector import npu as npu_mod
from host_bound.collector import packer
from host_bound.collector import perf as perf_mod
from host_bound.collector import process as proc_mod
from host_bound.collector import system as sys_mod
from host_bound.collector import thread as thr_mod
from host_bound.collector import tools as tools_mod
from host_bound.collector.sanitize import Sanitizer, current_identity


class CollectParams(object):
    def __init__(self, duration=60, interval=1.0, pid=None, auto_detect=False,
                 level=1, output=None, sanitize=False, no_tar=False):
        self.duration = float(duration)
        self.interval = float(interval)
        self.pid = int(pid) if pid else None
        self.auto_detect = bool(auto_detect)
        self.level = int(level)
        self.output = output
        self.sanitize = bool(sanitize)
        self.no_tar = bool(no_tar)


class CollectorOrchestrator(object):
    def __init__(self, params, logger=None):
        self.params = params
        self.logger = logger or common.setup_logger()
        self.results = {}      # 各采样器结果（线程安全：仅赋值）
        self.notes = []        # dq notes

    # ---------------- 入口 ----------------
    def run(self):
        p = self.params
        outdir = self._make_outdir()
        common.setup_logger(outdir)
        self.logger.info("采集开始: outdir=%s level=%d duration=%.0fs interval=%.1fs",
                         outdir, p.level, p.duration, p.interval)
        try:
            return self._run_inner(outdir)
        except Exception as e:  # 致命错误兜底：已采文件仍保留
            self.logger.exception("采集致命错误")
            self.notes.append("collector fatal: %r" % e)
            return {"status": "error", "outdir": outdir, "tar": None,
                    "dq_grade": "insufficient", "notes": self.notes,
                    "error": repr(e)}

    def _run_inner(self, outdir):
        p = self.params
        # 1. 能力探测
        cap = env_mod.probe_environment()
        common.write_json(os.path.join(outdir, "capabilities.json"), cap)
        self.logger.info("能力探测完成: os=%s %s, 容器=%s, 工具=%d/%d",
                         cap["os"]["name"], cap["os"]["kernel"],
                         cap["container"]["kind"],
                         sum(1 for v in cap["tools"].values() if v),
                         len(cap["tools"]))
        if cap["perf_paranoid"].get("hint"):
            self.notes.append(cap["perf_paranoid"]["hint"])
        # 2. 目标进程
        target = self._resolve_target()
        pid = target.get("pid")
        # 3. 静态采集
        sys_mod.collect_static(outdir)
        numa_res = self._numa_static(outdir, cap, pid)
        proc_tree = proc_mod.capture_process_tree(outdir)
        ps_res = proc_mod.capture_ps(outdir, cap["tools"], pid)
        target_details = {}
        if pid:
            target_details = proc_mod.capture_target_details(outdir, pid)
        device_static = npu_mod.collect_static(outdir, self.logger)
        # 4. 采样窗口
        window = self._window(outdir, cap, pid)
        # 5. 窗口后处理
        proc_mod.capture_process_tree(outdir)  # 第二次（覆盖写，作为窗口后快照）
        thread_names = self._read_thread_names(outdir, pid)
        fw = fw_mod.collect(outdir, target_details or {}, thread_names)
        # 6. dq 汇总
        sys_avail = window["system_avail"]
        dq = packer.compute_dq(
            sys_avail, bool(pid and target_details.get("found")),
            window["thread_rows"], self.results.get("tools", {}),
            device_static, self.results, self.notes)
        hostname, username = current_identity()
        # 7. sanitize
        sanitized = False
        if p.sanitize:
            san = Sanitizer(hostname, username)
            stats = san.sanitize_tree(outdir)
            sanitized = True
            self.notes.append("sanitized: %d 个文件被替换, 共 %d 处"
                              % (stats["files"], stats["replacements"]))
            hostname = "host-1" if hostname else hostname
            username = "user" if username else username
        # 8. manifest + tar
        params_view = CollectParams(p.duration, p.interval, pid, p.auto_detect,
                                    p.level, p.output, p.sanitize, p.no_tar)
        manifest = packer.build_manifest(outdir, cap, params_view,
                                         {"pid": pid,
                                          "cmdline": target_details.get("cmdline", []),
                                          "exe": target_details.get("exe", ""),
                                          "summary": {k: v for k, v in
                                                      target_details.items()
                                                      if k != "cmdline"} or target},
                                         dq, sanitized, hostname, username)
        packer.write_manifest(outdir, manifest)
        tar_path = None
        if not p.no_tar:
            tar_path = packer.make_tar(outdir, manifest["case_id"])
            self.logger.info("打包完成: %s", tar_path)
        status = {"complete": "ok", "partial": "partial",
                  "insufficient": "partial"}.get(dq["grade"], "partial")
        self.logger.info("采集结束: status=%s dq=%s", status, dq["grade"])
        return {"status": status, "outdir": outdir, "tar": tar_path,
                "case_id": manifest["case_id"], "dq_grade": dq["grade"],
                "dq": dq, "manifest": manifest, "framework": fw,
                "notes": self.notes}

    # ---------------- 步骤 ----------------
    def _make_outdir(self):
        p = self.params
        if p.output:
            base = p.output
        else:
            base = os.getcwd()
        outdir = os.path.join(base, "host_bound_collection_%s"
                              % time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(outdir, exist_ok=True)
        return outdir

    def _resolve_target(self):
        p = self.params
        if p.pid:
            return {"pid": p.pid, "how": "explicit"}
        if p.auto_detect:
            pid = proc_mod.auto_detect_pid(self.logger)
            if pid:
                return {"pid": pid, "how": "auto_detect"}
            self.notes.append("auto-detect 未发现训练/推理进程，仅采集系统级数据")
        return {"pid": None, "how": "none"}

    def _numa_static(self, outdir, cap, pid):
        return self._call_numa(outdir, cap["tools"], pid)

    def _call_numa(self, outdir, tools, pid):
        from host_bound.collector import numa as numa_mod
        return numa_mod.collect_static(outdir, tools, pid)

    def _window(self, outdir, cap, pid):
        p = self.params
        start_epoch = time.time()
        workers = []
        # L2 工具组
        if p.level >= 2:
            jobs = tools_mod.build_jobs(outdir, cap["tools"], pid,
                                        p.duration, p.interval)
            tool_results = {}
            self.results["tools"] = tool_results
            if jobs:
                for name, cmd, outfile in jobs:
                    t = tools_mod.ToolRunner(name, cmd, outfile, p.duration,
                                             tool_results, self.logger)
                    workers.append(t)
            else:
                self.notes.append("L2 请求但无可用采样工具（vmstat/pidstat/iostat/mpstat 均缺失）")
        # perf（L2 stat 恒尝试；L3 record 由 level>=3 触发）
        if p.level >= 2 and cap["tools"].get("perf"):
            pr = perf_mod.PerfRunner(outdir, pid, p.duration, p.level,
                                     cap["tools"]["perf"], self.results, self.logger)
            workers.append(pr)
        elif p.level >= 2:
            self.notes.append("perf 不可用：IPC/cache-miss 特征将缺失")
        # 设备循环采样
        if p.level >= 2:
            ds = npu_mod.DeviceSampler(outdir, p.interval, p.duration,
                                       self.results, self.logger)
            workers.append(ds)
            self._device_sampler = ds
        # 线程采样器（L1，主循环内）
        ts = thr_mod.ThreadSampler(outdir, pid) if pid else None
        if ts and not ts.available():
            self.notes.append("目标进程线程目录不可读：线程级特征缺失")
            ts = None
        # L1 系统采样器
        ss = sys_mod.SystemSampler(outdir)
        started = []
        for t in workers:
            t.start()
            started.append(t)
        # 主线程 L1 循环
        def tick(rel, epoch):
            ss.tick(rel, epoch)
            if ts:
                ts.capture(rel, epoch)
        n = common.monotonic_window(p.duration, p.interval, tick)
        self.logger.info("L1 主循环采样 %d 轮", n)
        # 等待工作线程（perf L3 最长 2×duration+120）
        join_timeout = p.duration * 2 + 120 if p.level >= 3 else p.duration + 90
        for t in started:
            t.join(timeout=join_timeout)
        return {"system_avail": any(v for v in ss.availability().values()),
                "thread_rows": ts.rows if ts else 0,
                "snapshots": n}

    def _read_thread_names(self, outdir, pid):
        names = []
        if not pid:
            return names
        task_dir = "/proc/%s/task" % pid
        if not os.path.isdir(task_dir):
            # 退化：从 threads.csv 读取
            return self._thread_names_from_csv(outdir)
        for tid_dir in os.listdir(task_dir)[:5000]:
            st = common.read_text(os.path.join(task_dir, tid_dir, "comm"))
            if st:
                names.append(st.strip())
        return names

    def _thread_names_from_csv(self, outdir):
        path = os.path.join(outdir, "threads", "threads.csv")
        if not os.path.exists(path):
            return []
        names = set()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            next(f, None)
            for line in f:
                parts = line.split(",")
                if len(parts) >= 3:
                    names.add(parts[2])
        return sorted(names)
