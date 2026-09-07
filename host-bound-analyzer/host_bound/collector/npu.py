# -*- coding: utf-8 -*-
"""设备采集：NPU（npu-smi）/ GPU（nvidia-smi）快照与窗口内循环采样。

- 静态快照：device/npu_smi.txt、device/nvidia_smi.txt（启动时一次）
- 循环采样：device/npu_smi_samples.txt、device/gpu_samples.csv
  （每 interval 一次，头部行与 SnapshotSeries 一致，解析端差分利用率）
- 工具缺失：落 unavailable 标记（不要求所有文件存在）
"""

import os
import shutil
import threading

from host_bound.collector import common

NVIDIA_QUERY_FIELDS = ("timestamp,index,utilization.gpu,memory.used,memory.total")


def collect_static(outdir, logger):
    """启动时一次性快照。返回 {npu: bool, gpu: bool}。"""
    result = {"npu": False, "gpu": False}
    if shutil.which("npu-smi"):
        rc, out, err, _ = common.run_command(["npu-smi", "info"], timeout=20)
        ok = rc == 0 and out.strip()
        common.write_text(os.path.join(outdir, "device", "npu_smi.txt"),
                          out if ok else "unavailable\n# %s\n" % (err[:200] or "empty"))
        result["npu"] = ok
    else:
        common.write_text(os.path.join(outdir, "device", "npu_smi.txt"), "unavailable\n")
    if shutil.which("nvidia-smi"):
        rc, out, err, _ = common.run_command(["nvidia-smi"], timeout=20)
        ok = rc == 0 and out.strip()
        common.write_text(os.path.join(outdir, "device", "nvidia_smi.txt"),
                          out if ok else "unavailable\n# %s\n" % (err[:200] or "empty"))
        # 结构化查询（解析端优先读 csv）
        rc2, out2, _e, _ = common.run_command(
            ["nvidia-smi", "--query-gpu=" + NVIDIA_QUERY_FIELDS,
             "--format=csv"], timeout=20)
        if rc2 == 0 and out2.strip():
            common.write_text(os.path.join(outdir, "device", "nvidia_smi.csv"), out2)
            ok = True
        result["gpu"] = ok
    else:
        common.write_text(os.path.join(outdir, "device", "nvidia_smi.txt"), "unavailable\n")
    logger.info("设备静态快照: npu=%s gpu=%s", result["npu"], result["gpu"])
    return result


class DeviceSampler(threading.Thread):
    """窗口内循环采样设备利用率（每 interval 一次）。"""

    def __init__(self, outdir, interval, duration, results, logger):
        super(DeviceSampler, self).__init__(name="hb-device", daemon=True)
        self.outdir = outdir
        self.interval = max(1.0, interval)
        self.duration = duration
        self.results = results
        self.logger = logger
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        import time
        npu = shutil.which("npu-smi")
        gpu = shutil.which("nvidia-smi")
        n_npu = n_gpu = 0
        start = time.monotonic()
        deadline = start + self.duration
        while time.monotonic() < deadline and not self._stop.is_set():
            rel = time.monotonic() - start
            if npu:
                rc, out, err, _ = common.run_command(["npu-smi", "info"], timeout=20)
                if rc == 0 and out.strip():
                    self._append("device/npu_smi_samples.txt",
                                 "=== snapshot npu ts=%.3f ===\n%s" % (rel, out))
                    n_npu += 1
            if gpu:
                rc, out, err, _ = common.run_command(
                    ["nvidia-smi", "--query-gpu=" + NVIDIA_QUERY_FIELDS,
                     "--format=csv,noheader"], timeout=20)
                if rc == 0 and out.strip():
                    self._append("device/gpu_samples.csv",
                                 "=== snapshot gpu ts=%.3f ===\n%s" % (rel, out))
                    n_gpu += 1
            remain = deadline - time.monotonic()
            if remain > 0:
                self._stop.wait(min(self.interval, remain))
        self.results["device_samples"] = {"npu": n_npu, "gpu": n_gpu}
        self.logger.info("设备循环采样完成: npu=%d gpu=%d", n_npu, n_gpu)

    def _append(self, rel_path, text):
        path = os.path.join(self.outdir, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
