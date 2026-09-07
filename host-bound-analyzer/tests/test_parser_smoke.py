# -*- coding: utf-8 -*-
"""parser 层冒烟测试：合成采集目录 → dispatch 全链路验证。

覆盖：
1. 55 条派发表在真实文件上的路由（专用解析器命中 / 未知文件兜底）；
2. 单文件异常隔离（一个解析器崩溃不拖垮整体，异常入台账与 DQ）；
3. 关键差分特征正确性（cpu.util_pct / sched.ctx_switch_per_sec）；
4. DQ 打标与分级（5 核心源 present → complete；单源 error → 降级）。
运行：python tests/test_parser_smoke.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from host_bound.models.case import DataQuality            # noqa: E402
from host_bound.parser import ParseContext, ParserResult, dispatch  # noqa: E402
from host_bound.parser import registry as reg             # noqa: E402


def _build_collection(root):
    """构建最小合成采集目录（12 个文件，覆盖核心源 + 兜底路径）。"""
    def put(rel, content):
        path = os.path.join(root, rel.replace("/", os.sep))
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "w") as fh:
            fh.write(content)

    put("manifest.json", json.dumps({
        "case_id": "smoke", "created_at": "2026-01-01T00:00:00",
        "host": {"hostname": "test-host", "kernel": "5.15.0"},
        "target": {"pid": 1234, "cmdline": "python3 train.py"},
    }, ensure_ascii=False))
    put("capabilities.json", json.dumps({
        "host_tools": {"mpstat": False, "pidstat": False, "perf": False},
        "collected": ["proc_stat", "threads"],
    }, ensure_ascii=False))

    put("system/proc_stat.txt",
        "=== snapshot proc_stat ts=0.000 epoch=1700000000.000 ===\n"
        "cpu  100 0 100 900 0 0 0 0 0 0\n"
        "cpu0 100 0 100 900 0 0 0 0 0 0\n"
        "ctxt 1000\n"
        "intr 2000\n"
        "procs_running 1\n"
        "procs_blocked 0\n"
        "=== snapshot proc_stat ts=1.000 epoch=1700000001.000 ===\n"
        "cpu  500 0 200 1300 100 0 0 0 0 0\n"
        "cpu0 500 0 200 1300 100 0 0 0 0 0\n"
        "ctxt 3000\n"
        "intr 2500\n"
        "procs_running 2\n"
        "procs_blocked 0\n")

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

    put("threads/threads.csv",
        "ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed\n"
        "0.000,100,python3,R,10,5,3,1,0-3\n"
        "0.000,101,python3,S,0,1,2,0,0-3\n"
        "1.000,100,python3,R,60,10,30,401,0-3\n"
        "1.000,101,python3,S,0,3,4,2,0-3\n")

    put("process/target_summary.json", json.dumps({
        "found": True, "pid": 1234, "exe": "/usr/bin/python3",
        "cmdline": ["python3", "train.py"], "fd_count": 128,
        "stat": {"threads": 12},
    }, ensure_ascii=False))

    put("process/status.txt",
        "Name:\tpython3\n"
        "State:\tR (running)\n"
        "Threads:\t12\n"
        "voluntary_ctxt_switches:\t100\n"
        "nonvoluntary_ctxt_switches:\t5\n")

    put("framework/framework.json", json.dumps({
        "frameworks": ["pytorch"],
        "thread_class": {"counts": {"dataloader": 8, "omp": 4}},
        "python_path": "/usr/bin/python3",
    }, ensure_ascii=False))

    # 未知产物：触发 UnknownFileParser 兜底（第九条）
    put("unknown/strange.log", "this is not a known artifact\n")


class _BadParser(object):
    """故意抛异常的假解析器：验证单文件异常隔离。"""
    name = "bad"

    def parse(self, path, rel, ctx, result):
        raise RuntimeError("boom-for-test")


class ParserSmokeTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hb_smoke_")
        _build_collection(self.root)
        self.dq = DataQuality()
        self.ctx = ParseContext(self.root, self.dq)
        self.result = ParserResult()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dispatch_routes_and_unknown_fallback(self):
        stats = dispatch(self.root, self.ctx, self.result)
        self.assertEqual(stats["total"], 12)
        self.assertEqual(stats["unknown"], 1)
        # 11 个已知文件全部成功解析（无 empty / error）
        self.assertEqual(stats["ok"], 11)
        self.assertEqual(stats["empty"], 0)
        self.assertEqual(stats["error"], 0)
        # 未知产物被登记 + 告警（第九条）
        warns = " ".join(self.dq.warnings)
        self.assertIn("未知产物 unknown/strange.log", warns)
        types = [e["type"] for e in self.result.parsed]
        self.assertIn("threads_csv", types)
        self.assertIn("target_summary", types)
        self.assertIn("generic", [e["type"] for e in self.result.parsed] +
                      [e.get("type", "generic") for e in []])

    def test_core_sources_marked_and_grade_complete(self):
        dispatch(self.root, self.ctx, self.result)
        for src in ("cpu", "mem", "process", "thread", "scheduler"):
            self.assertEqual(self.dq.sources[src]["status"], "present", src)
        self.assertEqual(self.dq.grade(), DataQuality.COMPLETE)

    def test_differential_features(self):
        dispatch(self.root, self.ctx, self.result)
        f = self.result.features
        # Δ(user+system+...) = 1000 jiffies，Δidle+iowait = 500 → util 50%
        self.assertAlmostEqual(f.get("cpu.util_pct"), 50.0, places=1)
        # Δctxt = 2000，dt = 1s → 2000/s
        self.assertAlmostEqual(f.get("sched.ctx_switch_per_sec"), 2000.0, delta=1.0)
        # mem.available_pct = 12e6/16e6 = 75%
        self.assertAlmostEqual(f.get("mem.available_pct"), 75.0, places=1)
        # target_summary.stat.threads
        self.assertEqual(f.get("thread.total"), 12)

    def test_exception_isolation(self):
        table = reg._build_table()
        orig = table["system/meminfo.txt"]
        table["system/meminfo.txt"] = _BadParser()
        try:
            stats = dispatch(self.root, self.ctx, self.result)
            self.assertEqual(stats["error"], 1)
            # 异常入台账
            reasons = [e.get("reason", "") for e in self.result.skipped]
            self.assertTrue(any("boom-for-test" in r for r in reasons), reasons)
            # 其余文件不受影响（11 个已知文件中 meminfo 异常 → 其余 10 个 ok；unknown 另计）
            self.assertEqual(stats["ok"], 10)
            # mem 源标 error 且不虚报完整性（grade 降级为 partial）
            self.assertEqual(self.dq.sources["mem"]["status"], "error")
            self.assertEqual(self.dq.grade(), DataQuality.PARTIAL)
        finally:
            table["system/meminfo.txt"] = orig

    def test_missing_core_source_conservative(self):
        # 删除 meminfo → mem 源 missing，绝不虚报 complete
        os.remove(os.path.join(self.root, "system", "meminfo.txt"))
        dispatch(self.root, self.ctx, self.result)
        self.assertEqual(self.dq.sources["mem"]["status"], "missing")
        self.assertEqual(self.dq.grade(), DataQuality.PARTIAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
