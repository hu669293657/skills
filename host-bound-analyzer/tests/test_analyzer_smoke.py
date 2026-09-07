# -*- coding: utf-8 -*-
"""analyzer 层冒烟测试：run_analysis 全链路（解析 -> 特征工程 -> 规则引擎 -> 维度视图）。

两个方向的合成夹具：
1. high：CPU 饱和（util≈95%）+ 设备空闲（NPU util=10%）-> 精确命中
   HB_GAP_CORE / HB_CPU_SAT_HIGH / HB_CPU_SAT_NO_DEVICE 三条规则；
   score≈60（较明显）、status=CRITICAL、分类=Device Idle Gap、
   风险雷达 Host-Device=90、CPU≈60.35、总体置信度≈0.91。
2. low：主机空闲（util≈2%）+ 设备高载（NPU util=95%）-> 零命中，
   score=0 / INFO / Unknown，且 9 轴风险全部为 0。

运行：python tests/test_analyzer_smoke.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from host_bound.analyzer import run_analysis  # noqa: E402


def _put(root, rel, content):
    path = os.path.join(root, rel.replace("/", os.sep))
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w") as fh:
        fh.write(content)


def _common_files(put):
    """两套夹具共用的产物：manifest/capabilities/环境/内存/PSI/进程/框架/未知兜底。"""
    put("manifest.json", json.dumps({
        "case_id": "analyzer-smoke", "created_at": "2026-01-01T00:00:00",
        "host": {"hostname": "test-host", "kernel": "5.15.0"},
        "target": {"pid": 1234, "cmdline": "python3 train.py"},
    }, ensure_ascii=False))
    put("capabilities.json", json.dumps({
        "host_tools": {"mpstat": False, "pidstat": False, "perf": False},
        "collected": ["proc_stat", "threads", "loadavg", "npu_smi"],
    }, ensure_ascii=False))
    # 2 逻辑核：cpuinfo 先于 loadavg 解析（按 rel 排序），保证 load5_per_core 可算
    put("environment/cpuinfo.txt",
        "processor\t: 0\n"
        "model name\t: Test CPU @ 2.50GHz\n"
        "processor\t: 1\n"
        "model name\t: Test CPU @ 2.50GHz\n")
    put("environment/uname.txt",
        "sysname=Linux\n"
        "nodename=test-host\n"
        "release=5.15.0-91-generic\n"
        "version=#1-Ubuntu SMP\n"
        "machine=x86_64\n")
    put("system/meminfo.txt",
        "MemTotal: 16000000 kB\n"
        "MemFree: 8000000 kB\n"
        "MemAvailable: 12000000 kB\n"
        "Buffers: 100000 kB\n"
        "Cached: 2000000 kB\n"
        "SwapTotal: 0 kB\n"
        "SwapFree: 0 kB\n")
    put("system/pressure_cpu.txt",
        "some avg10=12.34 avg60=5.00 avg300=1.00 total=100000\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    put("process/target_summary.json", json.dumps({
        "found": True, "pid": 1234, "exe": "/usr/bin/python3",
        "cmdline": ["python3", "train.py"], "fd_count": 128,
        "stat": {"threads": 12},
    }, ensure_ascii=False))
    put("framework/framework.json", json.dumps({
        "frameworks": ["pytorch"],
        "thread_class": {"counts": {"dataloader": 8, "omp": 4}},
        "python_path": "/usr/bin/python3",
    }, ensure_ascii=False))
    # 未知产物：触发 UnknownFileParser 兜底
    put("unknown/strange.log", "this is not a known artifact\n")


def _build_high_collection(root):
    """高负载夹具：CPU 饱和 + 设备空闲，精确命中 3 条规则。"""
    _common_files(lambda rel, content: _put(root, rel, content))

    # CPU 饱和：Δ(user+system+softirq)=950 / Δtotal=1000 -> util=95.0%，iowait=1.0%
    # ctxt 1000->3000 -> 2000/s（< 100000，不触发 HB_SCHED_CTX_HIGH）
    put = lambda rel, content: _put(root, rel, content)  # noqa: E731
    put("system/proc_stat.txt",
        "=== snapshot proc_stat ts=0.000 epoch=1700000000.000 ===\n"
        "cpu  100 0 50 800 10 0 10 0 0 0\n"
        "cpu0 100 0 50 800 10 0 10 0 0 0\n"
        "ctxt 1000\n"
        "intr 2000\n"
        "procs_running 2\n"
        "procs_blocked 0\n"
        "=== snapshot proc_stat ts=1.000 epoch=1700000001.000 ===\n"
        "cpu  990 0 100 840 20 0 20 0 0 0\n"
        "cpu0 990 0 100 840 20 0 20 0 0 0\n"
        "ctxt 3000\n"
        "intr 2500\n"
        "procs_running 2\n"
        "procs_blocked 0\n")
    # load5=4.20 / 2 核 = 2.1（>= 1.0，配合 util>=90 命中 HB_CPU_SAT_NO_DEVICE）
    # 兜底 blocked = 5-2 = 3 < 5，不会误触 HB_SCHED_BLOCKED_IO
    put("system/loadavg.txt", "4.50 4.20 4.00 2/5 12345\n")
    # 线程差分：非自愿切换 ~400/s（< 20000，不触发 HB_SCHED_NONVOL）
    put("threads/threads.csv",
        "ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed\n"
        "0.000,100,python3,R,10,5,3,1,0-3\n"
        "0.000,101,python3,S,0,1,2,0,0-3\n"
        "1.000,100,python3,R,60,10,30,401,0-3\n"
        "1.000,101,python3,S,0,3,4,2,0-3\n")
    put("process/status.txt",
        "Name:\tpython3\n"
        "State:\tR (running)\n"
        "Threads:\t12\n"
        "voluntary_ctxt_switches:\t100\n"
        "nonvoluntary_ctxt_switches:\t5\n")
    # 设备空闲：两次快照 AICore 均 10%（<= 40，配合 util>=70 命中 HB_GAP_CORE）
    put("device/npu_smi_samples.txt",
        "=== snapshot npu ts=0.000 epoch=1700000000.000 ===\n"
        "+-------------------------------------------------------------------------+\n"
        "| NPU     Chip           BusId              AICore(%)  Temp(C)  Power(W)\n"
        "| 0 | 910B4 | 0000:C1:00.0 | 10 | 40 | 65.5 |\n"
        "+-------------------------------------------------------------------------+\n"
        "=== snapshot npu ts=1.000 epoch=1700000001.000 ===\n"
        "| 0 | 910B4 | 0000:C1:00.0 | 10 | 41 | 65.5 |\n")


def _build_low_collection(root):
    """空闲夹具：主机空闲 + 设备高载，零规则命中。"""
    _common_files(lambda rel, content: _put(root, rel, content))

    # 主机空闲：Δ(user+system)=20 / Δtotal=1000 -> util=2.0%，iowait=0%
    # ctxt 1000->1200 -> 200/s
    put = lambda rel, content: _put(root, rel, content)  # noqa: E731
    put("system/proc_stat.txt",
        "=== snapshot proc_stat ts=0.000 epoch=1700000000.000 ===\n"
        "cpu  50 0 30 900 10 0 10 0 0 0\n"
        "ctxt 1000\n"
        "intr 2000\n"
        "procs_running 1\n"
        "procs_blocked 0\n"
        "=== snapshot proc_stat ts=1.000 epoch=1700000001.000 ===\n"
        "cpu  60 0 40 1880 10 0 10 0 0 0\n"
        "ctxt 1200\n"
        "intr 2010\n"
        "procs_running 1\n"
        "procs_blocked 0\n")
    put("system/loadavg.txt", "0.30 0.20 0.10 1/3 12345\n")
    put("threads/threads.csv",
        "ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed\n"
        "0.000,100,python3,S,5,2,2,1,0-1\n"
        "1.000,100,python3,S,6,3,7,11,0-1\n")
    put("process/status.txt",
        "Name:\tpython3\n"
        "State:\tS (sleeping)\n"
        "Threads:\t2\n"
        "voluntary_ctxt_switches:\t100\n"
        "nonvoluntary_ctxt_switches:\t5\n")
    # 设备高载：AICore 95%（>= 70，无供给缺口信号，风险雷达 Host-Device 归零）
    put("device/npu_smi_samples.txt",
        "=== snapshot npu ts=0.000 epoch=1700000000.000 ===\n"
        "| 0 | 910B4 | 0000:C1:00.0 | 95 | 40 | 200.0 |\n"
        "=== snapshot npu ts=1.000 epoch=1700000001.000 ===\n"
        "| 0 | 910B4 | 0000:C1:00.0 | 95 | 41 | 200.0 |\n")


_RADAR_DIMS = {"CPU", "Memory", "NUMA", "Thread", "Scheduler",
               "IO", "Runtime", "Data Pipeline", "Host-Device"}


class AnalyzerSmokeTest(unittest.TestCase):
    """run_analysis 全链路：高位精确命中 + 低位零命中。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hb_analyzer_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_high_bound_full_chain(self):
        _build_high_collection(self.root)
        bundle = run_analysis(self.root)

        # -- 解析统计：14 个产物，1 个未知兜底，其余全部成功；DQ=complete
        self.assertEqual(bundle.parse_stats["total"], 14)
        self.assertEqual(bundle.parse_stats["unknown"], 1)
        self.assertEqual(bundle.parse_stats["ok"], 13)
        self.assertEqual(bundle.dq.grade(), "complete")

        # -- 关键特征（差分口径）
        feats = bundle.features
        self.assertAlmostEqual(feats.get("cpu.util_pct"), 95.0, delta=0.5)
        self.assertAlmostEqual(feats.get("cpu.iowait_pct"), 1.0, delta=0.5)
        self.assertAlmostEqual(feats.get("cpu.load5_per_core"), 2.1, delta=0.01)
        self.assertAlmostEqual(feats.get("device.util_pct"), 10.0, delta=0.5)
        self.assertAlmostEqual(feats.get("sched.ctx_switch_per_sec"),
                               2000.0, delta=1.0)
        nonvol = feats.get("sched.nonvoluntary_ctx_per_sec")
        self.assertGreater(nonvol, 0.0)
        self.assertLess(nonvol, 20000.0)
        self.assertAlmostEqual(feats.get("mem.available_pct"), 75.0, delta=0.5)
        self.assertAlmostEqual(feats.get("host.data_completeness"),
                               100.0, delta=0.05)

        # -- 特征工程派生
        self.assertIn("cpu.bottleneck_hint", bundle.derived)
        self.assertIn("host.data_completeness", bundle.derived)
        self.assertTrue(str(feats.get("cpu.bottleneck_hint")).startswith("CPU 饱和"))

        # -- 维度视图：7 个固定章节，结构完整；CPU 章节 util 指标为 bad 级
        self.assertEqual(len(bundle.sections), 7)
        self.assertEqual(set(s["dim"] for s in bundle.sections),
                         {"cpu", "thread", "scheduler", "numa",
                          "io", "memory", "perf"})
        for sec in bundle.sections:
            for key in ("dim", "title", "verdict", "metrics", "missing", "files"):
                self.assertIn(key, sec)
        cpu_sec = [s for s in bundle.sections if s["dim"] == "cpu"][0]
        by_key = dict((m["key"], m) for m in cpu_sec["metrics"])
        self.assertIn("cpu.util_pct", by_key)
        self.assertEqual(by_key["cpu.util_pct"]["level"], "bad")
        self.assertTrue(cpu_sec["verdict"])

        # -- 规则命中：精确三条，按 sort_key 降序，首位 HB_GAP_CORE
        diag = bundle.diag
        fired = [f.rule_id for f in diag.findings]
        self.assertEqual(set(fired), {"HB_GAP_CORE", "HB_CPU_SAT_HIGH",
                                      "HB_CPU_SAT_NO_DEVICE"})
        keys = [f.sort_key() for f in diag.findings]
        self.assertEqual(keys, sorted(keys, reverse=True))
        self.assertEqual(diag.findings[0].rule_id, "HB_GAP_CORE")

        # -- 汇总判定：score≈60.2 -> 较明显 / CRITICAL / Device Idle Gap
        self.assertAlmostEqual(diag.host_bound_score, 60.2, delta=5.0)
        self.assertEqual(diag.score_label, "较明显")
        self.assertEqual(diag.status, "CRITICAL")
        self.assertEqual(diag.classification, "Device Idle Gap")
        self.assertGreaterEqual(diag.confidence, 0.80)
        self.assertLessEqual(diag.confidence, 0.97)
        self.assertIn("存在较明显 Host Bound", diag.statement)

        # -- 风险雷达：9 轴；Host-Device=90（设备低载直接信号），CPU≈60.35
        self.assertEqual(set(diag.risk_dimensions.keys()), _RADAR_DIMS)
        self.assertAlmostEqual(diag.risk_dimensions["Host-Device"], 90.0, delta=0.5)
        self.assertAlmostEqual(diag.risk_dimensions["CPU"], 60.35, delta=0.5)
        for dim in ("Memory", "NUMA", "Thread", "Scheduler", "IO",
                    "Runtime", "Data Pipeline"):
            self.assertAlmostEqual(diag.risk_dimensions[dim], 0.0, delta=1e-6)

        # -- 根因树非空（按 diagnosis_type 分组）
        self.assertTrue(diag.root_cause_tree)

    def test_low_bound_no_finding(self):
        _build_low_collection(self.root)
        bundle = run_analysis(self.root)

        self.assertEqual(bundle.parse_stats["ok"], 13)
        self.assertEqual(bundle.dq.grade(), "complete")

        feats = bundle.features
        self.assertAlmostEqual(feats.get("cpu.util_pct"), 2.0, delta=0.5)
        self.assertAlmostEqual(feats.get("device.util_pct"), 95.0, delta=0.5)

        diag = bundle.diag
        self.assertEqual(len(diag.findings), 0)
        self.assertEqual(diag.host_bound_score, 0.0)
        self.assertEqual(diag.score_label, "基本不存在")
        self.assertEqual(diag.status, "INFO")
        self.assertEqual(diag.classification, "Unknown")
        self.assertGreaterEqual(diag.confidence, 0.5)
        self.assertLessEqual(diag.confidence, 0.8)
        self.assertIn("基本不存在 Host Bound", diag.statement)
        self.assertIn("部分规则因数据缺失未启用", diag.statement)

        self.assertEqual(set(diag.risk_dimensions.keys()), _RADAR_DIMS)
        for _dim, v in diag.risk_dimensions.items():
            self.assertAlmostEqual(v, 0.0, delta=1e-6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
