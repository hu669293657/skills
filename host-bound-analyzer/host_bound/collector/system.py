# -*- coding: utf-8 -*-
"""L1 系统级采集：/proc、/sys 快照序列与静态信息。

快照序列：按 interval 循环追加到 system/ 下同名文件（供差分计算速率）；
静态信息：cpuinfo、/proc/cmdline、内核参数等只采一次进 environment/。
全部容错：文件缺失 → SnapshotSeries 落一次 unavailable 标记。
"""

import glob
import os
import platform
import time

from host_bound.collector import common

# (文件名, 读取器) —— 读取器是函数，便于测试时替换
def _make_reader(path):
    return lambda: common.read_text(path)


PROC_SNAPSHOT_SOURCES = [
    ("proc_stat.txt", "/proc/stat"),              # CPU jiffies / 中断 / 上下文切换
    ("proc_vmstat.txt", "/proc/vmstat"),          # pgscan/pgsteal/allocstall 等
    ("loadavg.txt", "/proc/loadavg"),             # load1/5/15 + 可运行/存在任务数
    ("meminfo.txt", "/proc/meminfo"),             # 内存总量/可用/swap
    ("interrupts.txt", "/proc/interrupts"),       # 每核中断分布
    ("softirqs.txt", "/proc/softirqs"),
    ("pressure_cpu.txt", "/proc/pressure/cpu"),   # PSI：部分停滞占比（4.20+）
    ("pressure_memory.txt", "/proc/pressure/memory"),
    ("pressure_io.txt", "/proc/pressure/io"),
]

STATIC_SOURCES = [
    ("cpuinfo.txt", "/proc/cpuinfo"),
    ("cmdline_kernel.txt", "/proc/cmdline"),
    ("sched_features.txt", "/sys/kernel/debug/sched_features"),  # 无权限则为空
    ("swaps.txt", "/proc/swaps"),
]


class SystemSampler(object):
    """L1 主采样器：窗口期内按 interval 快照所有 PROC_SNAPSHOT_SOURCES。"""

    def __init__(self, outdir):
        self.outdir = outdir
        self.series = {}
        for fname, src in PROC_SNAPSHOT_SOURCES:
            self.series[fname] = common.SnapshotSeries(
                os.path.join(outdir, "system", fname), fname)
            self.series[fname]._src = src

    def tick(self, rel_ts, epoch_ts):
        for fname, series in self.series.items():
            series.capture(rel_ts, epoch_ts, _make_reader(series._src))

    def availability(self):
        """返回 {文件名: available}，供 manifest 数据质量汇总。"""
        return {k: (v.available is True) for k, v in self.series.items()}


def collect_static(outdir):
    """静态系统信息：只采一次。返回 {文件名: 是否成功}。"""
    result = {}
    for fname, src in STATIC_SOURCES:
        text = common.read_text(src)
        common.write_text(os.path.join(outdir, "environment", fname), text or "unavailable\n")
        result[fname] = text is not None
    # uname（平台无关）
    u = platform.uname()
    common.write_text(os.path.join(outdir, "environment", "uname.txt"),
                      "system=%s\nnode=%s\nrelease=%s\nversion=%s\nmachine=%s\nprocessor=%s\n"
                      % (u.system, u.node, u.release, u.version, u.machine, u.processor))
    result["uname.txt"] = True
    # 每核当前频率（存在才写）
    freq_lines = []
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")):
        v = common.read_text(p)
        if v:
            freq_lines.append("%s: %s" % (os.path.basename(p.split("/cpufreq")[0]), v.strip()))
    if freq_lines:
        common.write_text(os.path.join(outdir, "environment", "cpu_freqs.txt"),
                          "\n".join(freq_lines) + "\n")
        result["cpu_freqs.txt"] = True
    # 内核关键参数
    kparams = {}
    for kp in ("/proc/sys/kernel/sched_autogroup_enabled",
               "/proc/sys/vm/swappiness",
               "/proc/sys/vm/overcommit_memory",
               "/proc/sys/kernel/numa_balancing"):
        v = common.read_text(kp)
        if v:
            kparams[kp] = v.strip()
    if kparams:
        common.write_text(os.path.join(outdir, "environment", "kernel_params.json"),
                          __import__("json").dumps(kparams, indent=2, ensure_ascii=False))
        result["kernel_params.json"] = True
    return result


def epoch():
    return time.time()
