# -*- coding: utf-8 -*-
"""黄金合成测试案例：端到端 run_analysis + generate_html 验证。

每个案例构造一组最小产物集合，断言:
- 解析统计与数据质量分级
- 命中规则集合（fired rule_id 精确匹配）
- 评分 / 分档 / statement / classification
- 风险雷达轴值（公式推导，允许浮点误差）
- HTML 报告 18 章节完整且零外部依赖

评分推导（diagnosis/scoring.py 契约）:
  contribution = weight × SEVERITY_FACTOR × confidence
  score = 100 × Σ(top5 贡献) / max(Σ top5 权重, 3.0)，clamp [0, 100]
  SEVERITY_FACTOR = {CRITICAL:1.0, HIGH:0.85, MEDIUM:0.6, LOW:0.35, INFO:0.15}
  confidence = explicit 或 0.55 + 0.08×(证据源文件数)，×dq_factor(complete=1.0)
风险雷达（engine._risk_dimensions）:
  dims[dim] = max(现有, 100 × SEVERITY_FACTOR × confidence)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from host_bound.analyzer.host_bound import run_analysis  # noqa: E402
from host_bound.report.generator import generate_html  # noqa: E402

# 18 个固定章节标题（DESIGN 约定，顺序永不变化，英文标题）
_CHAPTERS = (
    "Executive Summary", "Overall Diagnosis", "Host Bound Assessment",
    "CPU Analysis", "Thread Analysis", "Scheduler Analysis",
    "NUMA Analysis", "Memory Analysis", "I/O Analysis",
    "Host-Device Timeline", "Framework Analysis", "Profiler Analysis",
    "Root Cause", "Evidence", "Optimization Suggestions",
    "Risk and Impact", "Collection Information", "Raw Data Appendix",
)

_SEV_FACTOR = {"CRITICAL": 1.0, "HIGH": 0.85, "MEDIUM": 0.6,
               "LOW": 0.35, "INFO": 0.15}


# ============================================================
# 夹具工具
# ============================================================

def _put(root, rel, content):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _common_files(root):
    """所有案例共享的基础产物（manifest/capabilities/环境/框架/未知文件）。"""
    _put(root, "manifest.json", '{"case_id": "golden", "hostname": "h1",'
          ' "created_at": "2026-09-06T10:00:00", "level": 1}')
    _put(root, "capabilities.json", '{"collected": ["proc_stat", "meminfo",'
          ' "cpuinfo", "threads", "npu_smi"]}')
    _put(root, "environment/cpuinfo.txt",
         "processor\t: 0\nprocessor\t: 1\nmodel name\t: TestCPU\n")
    _put(root, "environment/uname.txt",
         "Linux h1 5.10.0 #1 SMP x86_64 GNU/Linux\n")
    _put(root, "system/meminfo.txt",
         "MemTotal:       16000000 kB\nMemAvailable:  12000000 kB\n"
         "SwapTotal:             0 kB\nSwapFree:              0 kB\n")
    # scheduler 核心源（DQ complete 需 5 源全 present）
    _put(root, "system/pressure_cpu.txt",
         "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    _put(root, "framework/framework.json",
         '{"framework": "PyTorch", "version": "2.1"}')
    _put(root, "unknown/strange.log", "some unknown format log\n")


def _meminfo_with_swap(root, total_kb, free_kb):
    _put(root, "system/meminfo.txt",
         "MemTotal:       16000000 kB\nMemAvailable:  12000000 kB\n"
         "SwapTotal:      %d kB\nSwapFree:       %d kB\n" % (total_kb, free_kb))


def _proc_stat_ctxt(root, ctxt0, ctxt1, idle=900, iowait=50):
    """双快照 proc_stat，指定 ctxt 值（用于精确控制上下文切换率）。"""
    def snap(ts, ctxt):
        return ("=== snapshot proc_stat ts=%.3f epoch=%.3f ===\n"
                % (ts, ts)
                + "cpu  100 0 100 %d %d 0 0 0 0 0\n"
                  "cpu0 50 0 50 %d %d 0 0 0 0 0\n"
                  "cpu1 50 0 50 %d %d 0 0 0 0 0\n"
                  "intr 1000\nctxt %d\n"
                % (idle, iowait, idle // 2, iowait // 2,
                   idle // 2, iowait // 2, ctxt))
    _put(root, "system/proc_stat.txt", snap(1000.0, ctxt0) + snap(1001.0, ctxt1))


def _proc_stat(root, idle, iowait, d_idle=0, d_iowait=0):
    """双快照 proc_stat：差分区间 1.0s（1000 jiffies）。

    d_idle/d_iowait 为快照 1 相对快照 0 的 idle/iowait 增量，
    用于产生 cpu.iowait_pct（差分口径）。
    """
    def snap(ts, idle_v, iowait_v):
        return ("=== snapshot proc_stat ts=%.3f epoch=%.3f ===\n"
                % (ts, ts)
                + "cpu  100 0 100 %d %d 0 0 0 0 0\n"
                  "cpu0 50 0 50 %d %d 0 0 0 0 0\n"
                  "cpu1 50 0 50 %d %d 0 0 0 0 0\n"
                  "intr 1000\nctxt %d\n"
                % (idle_v, iowait_v, idle_v // 2, iowait_v // 2,
                   idle_v // 2, iowait_v // 2, ts))
    _put(root, "system/proc_stat.txt",
         snap(1000.0, idle, iowait) + snap(1001.0, idle + d_idle, iowait + d_iowait))


def _loadavg(root, load5):
    _put(root, "system/loadavg.txt",
         "%.2f %.2f %.2f 2/5 12345\n" % (load5, load5, load5))


def _threads_csv(root, rows_per_tick, ctx_nonvol_start=1):
    """两条 tick 快照的 threads.csv。

    rows_per_tick: 每 tick 活跃线程数（utime/stime 差分>0）
    """
    header = "ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed\n"
    lines = [header]
    for tick, ts in enumerate((1000.0, 1001.0)):
        for i in range(rows_per_tick):
            tid = 100 + i
            # tick0: utime=10+10i stime=0；tick1: utime=20+10i stime=5（差分>0）
            u0, s0 = 10 + 10 * i, 0
            u1, s1 = 20 + 10 * i, 5
            if tick == 0:
                lines.append("%s,%d,worker%d,R,%d,%d,10,%d,0-1\n"
                             % (ts, tid, i, u0, s0, ctx_nonvol_start))
            else:
                lines.append("%s,%d,worker%d,R,%d,%d,10,%d,0-1\n"
                             % (ts, tid, i, u1, s1,
                                ctx_nonvol_start + 100 * (i + 1)))
    _put(root, "threads/threads.csv", "".join(lines))


def _npu_idle(root):
    _put(root, "device/npu_smi_samples.txt",
         "=== snapshot npu ts=1000.000 epoch=1000.000 ===\n"
         "| AICore | 10 |\n"
         "=== snapshot npu ts=1001.000 epoch=1001.000 ===\n"
         "| AICore | 10 |\n")


def _environ(root, omp=None, workers=None):
    lines = []
    if omp is not None:
        lines.append("OMP_NUM_THREADS=%d" % omp)
    if workers is not None:
        lines.append("DATALOADER_NUM_WORKERS=%d" % workers)
    lines.append("MKL_NUM_THREADS=1")
    _put(root, "process/environ.txt", "\n".join(lines) + "\n")


def _numastat(root, local, other):
    _put(root, "numa/numastat_system.txt",
         "node0           node1\n"
         "numa_hit 1234567 7654321\n"
         "numa_miss 1000 2000\n"
         "numa_foreign 0 0\n"
         "interleave_hit 0 0\n"
         "local_node %d %d\n"
         "other_node %d %d\n" % (local // 2, local - local // 2,
                                 other // 2, other - other // 2))


def _iostat(root, util_pct, await_ms=5.0):
    """两块 Device 表（首块剔除），热点盘 sda；await 独立控制避免误触 HB_IO_AWAIT。"""
    block1 = ("Device  r/s   rkB/s   rrqm/s  %rrqm r_await rareq-sz  "
              "w/s    wkB/s   wrqm/s  %wrqm w_await wareq-sz  "
              "aqu-sz  %util\n"
              "sda     1.00  8.00    0.00    0.00   1.00   8.00     "
              "1.00   8.00    0.00    0.00   1.00   8.00     "
              "0.10    5.00\n")
    block2 = ("Device  r/s   rkB/s   rrqm/s  %%rrqm r_await rareq-sz  "
              "w/s    wkB/s   wrqm/s  %%wrqm w_await wareq-sz  "
              "aqu-sz  %%util\n"
              "sda     100.00 800.00 0.00 0.00 %.2f 8.00    "
              "50.00  400.00 0.00 0.00 %.2f 8.00     "
              "2.50    %.2f\n"
              % (await_ms, await_ms, util_pct))
    _put(root, "tools/iostat_x.txt", block1 + "\n" + block2)


def _perf_report_dataloader(root):
    _put(root, "perf/perf_report.txt",
         "# To display the perf.data header info, please try: perf report\n"
         "#\n"
         "# Samples: 1K of event 'cpu-clock'\n"
         "# Event count (approx.): 1000000000\n"
         "#\n"
         "# Overhead  Command  Shared Object  Symbol\n"
         "# ........  .......  .............  ......................\n"
         "#\n"
         "  50.00%  python   libpthread.so  pthread_cond_wait\n"
         "  20.00%  python   python         [.] PyEval_EvalFrameDefault\n"
         "  15.00%  python   libc.so        memcpy\n"
         "  10.00%  python   [kernel.kallsyms]  [k] _raw_spin_lock\n"
         "   5.00%  python   python         [.] PyObject_GenericGetAttr\n")


def _ps_threads(root, n):
    header = "   PID  SPID  PSR  %CPU  CMD\n"
    lines = [header]
    for i in range(n):
        lines.append("  1234  %d     %d    90.0  python\n"
                     % (100 + i, i % 2))
    _put(root, "process/ps_threads.txt", "".join(lines))


def _status(root, threads):
    _put(root, "process/status.txt",
         "Name:\tpython\nThreads:\t%d\nCpus_allowed_list:\t0-1\n" % threads)


# ============================================================
# 断言帮助
# ============================================================

def _fired_ids(diag):
    return [f.rule_id for f in diag.findings]


def _find(diag, rule_id):
    for f in diag.findings:
        if f.rule_id == rule_id:
            return f
    return None


def _assert_html_common(self, html):
    self.assertEqual(html.count("<h2>"), 18)
    for ch in _CHAPTERS:
        self.assertIn(ch, html)
    self.assertNotIn("<script", html.lower())
    # 零外部资源: xmlns 是 SVG 命名空间声明而非资源引用，允许出现
    cleaned = html.replace('xmlns="http://www.w3.org/2000/svg"', "")
    self.assertNotIn("http://", cleaned)
    self.assertNotIn("https://", cleaned)


# ============================================================
# 测试类
# ============================================================

class TestGoldenThreadOversub(unittest.TestCase):
    """线程超配案例: 2 核、5 活跃线程、OMP=8、ctx 差分 100000/s。

    特征: thread.oversub_ratio = 5/2 = 2.5 (>2.0)
          sched.ctx_switch_per_sec = 100000 (>=50000)
          thread.omp_threads_setting = 8 > cores 2
    """

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_thread_")
        _common_files(self.root)
        _proc_stat_ctxt(self.root, ctxt0=100000, ctxt1=200000,
                        idle=600, iowait=10)
        _loadavg(self.root, 1.0)
        # 5 条活跃线程，ctx_nonvol 差分 10000/线程（nonvol 均值 50000，
        # 高于 20000 预警线但不影响 ctx 规则判定——规则用 ctxt 口径）
        header = "ts,tid,name,state,utime,stime,ctx_vol,ctx_nonvol,cpu_allowed\n"
        lines = [header]
        for tick, ts in enumerate((1000.0, 1001.0)):
            for i in range(5):
                tid = 100 + i
                if tick == 0:
                    lines.append("%s,%d,worker%d,R,%d,%d,10,%d,0-1\n"
                                 % (ts, tid, i, 10 + 10 * i, 0, 1))
                else:
                    lines.append("%s,%d,worker%d,R,%d,%d,10,%d,0-1\n"
                                 % (ts, tid, i, 20 + 10 * i, 5, 10001))
        _put(self.root, "threads/threads.csv", "".join(lines))
        _npu_idle(self.root)
        _environ(self.root, omp=8, workers=0)
        _ps_threads(self.root, 5)
        _status(self.root, 5)
        _put(self.root, "process/target_summary.json",
             '{"found": true, "stat": {"threads": 5}, '
             ' "exe": "python", "cmdline": ["python", "train.py"], '
             ' "fd_count": 64}')
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_fired(self):
        ids = _fired_ids(self.diag)
        self.assertIn("HB_THREAD_OVERSUB", ids)
        self.assertIn("HB_OMP_OVERSUB", ids)

    def test_thread_risk(self):
        f = _find(self.diag, "HB_THREAD_OVERSUB")
        conf = f.confidence
        expect = 100.0 * _SEV_FACTOR["HIGH"] * conf
        self.assertAlmostEqual(self.diag.risk_dimensions["Thread"],
                               expect, delta=1.0)

    def test_score(self):
        f1 = _find(self.diag, "HB_THREAD_OVERSUB")
        f2 = _find(self.diag, "HB_OMP_OVERSUB")
        f3 = _find(self.diag, "HB_SCHED_NONVOL")
        c1 = f1.confidence
        c2 = 0.6  # explicit confidence
        c3 = f3.confidence
        w = [1.5, 1.0, 1.0]
        contrib = [1.5 * 0.85 * c1, 1.0 * 0.6 * c2, 1.0 * 0.6 * c3]
        expect = 100.0 * sum(contrib) / max(sum(w), 3.0)
        self.assertAlmostEqual(self.diag.host_bound_score, expect, delta=1.0)
        self.assertEqual(self.diag.score_label, "疑似")

    def test_classified_thread(self):
        self.assertEqual(self.diag.classification, "Thread Oversubscription")

    def test_html(self):
        _assert_html_common(self, self.html)


class TestGoldenNumaRemote(unittest.TestCase):
    """NUMA 远端访存: local=700 other=300 → remote=30%。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_numa_")
        _common_files(self.root)
        _proc_stat(self.root, idle=60, iowait=2)
        _loadavg(self.root, 1.0)
        _threads_csv(self.root, 2)
        _npu_idle(self.root)
        _numastat(self.root, 700, 300)
        _put(self.root, "numa/nodes.txt",
             "node0 cpulist=0-1 total_memory=8589934592\n"
             "node1 cpulist= total_memory=8589934592\n")
        _put(self.root, "process/target_summary.json",
             '{"found": true, "stat": {"threads": 2}, '
             ' "exe": "python", "cmdline": ["python", "train.py"]}')
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_fired(self):
        ids = _fired_ids(self.diag)
        self.assertIn("HB_NUMA_REMOTE", ids)
        f = _find(self.diag, "HB_NUMA_REMOTE")
        val = next(ev["value"] for ev in f.evidence
                   if ev["metric"] == "numa.remote_access_pct")
        self.assertAlmostEqual(val, 30.0, delta=0.5)

    def test_numa_risk(self):
        f = _find(self.diag, "HB_NUMA_REMOTE")
        conf = f.confidence
        expect = 100.0 * _SEV_FACTOR["HIGH"] * conf
        self.assertAlmostEqual(self.diag.risk_dimensions["NUMA"],
                               expect, delta=1.0)

    def test_score(self):
        f = _find(self.diag, "HB_NUMA_REMOTE")
        expect = 100.0 * (1.2 * 0.85 * f.confidence) / 3.0
        self.assertAlmostEqual(self.diag.host_bound_score, expect, delta=1.0)

    def test_html(self):
        _assert_html_common(self, self.html)


class TestGoldenIoBound(unittest.TestCase):
    """IO 案例: iowait 差分 20%+、热点盘 %util=90。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_io_")
        _common_files(self.root)
        # 快照差分: iowait 增量 200 / 总增量 1000 → cpu.iowait_pct = 20%
        _proc_stat(self.root, idle=200, iowait=200, d_idle=800, d_iowait=200)
        _loadavg(self.root, 1.0)
        _threads_csv(self.root, 2)
        _npu_idle(self.root)
        _iostat(self.root, 90.0)
        _put(self.root, "process/target_summary.json",
             '{"found": true, "stat": {"threads": 2}, '
             ' "exe": "python", "cmdline": ["python", "train.py"]}')
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_fired(self):
        ids = _fired_ids(self.diag)
        self.assertIn("HB_IO_IOWAIT", ids)
        self.assertIn("HB_IO_UTIL", ids)

    def test_score(self):
        f1 = _find(self.diag, "HB_IO_IOWAIT")
        f2 = _find(self.diag, "HB_IO_UTIL")
        c1 = f1.confidence
        c2 = f2.confidence
        w = [1.5, 1.2]
        contrib = [1.5 * 0.85 * c1, 1.2 * 0.85 * c2]
        expect = 100.0 * sum(contrib) / max(sum(w), 3.0)
        self.assertAlmostEqual(self.diag.host_bound_score, expect, delta=1.0)

    def test_io_risk(self):
        f1 = _find(self.diag, "HB_IO_IOWAIT")
        f2 = _find(self.diag, "HB_IO_UTIL")
        r = max(100.0 * 0.85 * f1.confidence,
                100.0 * 0.85 * f2.confidence)
        self.assertAlmostEqual(self.diag.risk_dimensions["IO"], r, delta=1.0)

    def test_html(self):
        _assert_html_common(self, self.html)


class TestGoldenMemorySwap(unittest.TestCase):
    """内存案例: SwapUsed=30%（CRITICAL）+ low available=5%。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_mem_")
        _common_files(self.root)
        _proc_stat(self.root, idle=80, iowait=5)
        _loadavg(self.root, 1.0)
        _threads_csv(self.root, 2)
        _npu_idle(self.root)
        # SwapTotal=4000000, SwapFree=2800000 → used=30%；available=5%
        _put(self.root, "system/meminfo.txt",
             "MemTotal:       16000000 kB\nMemAvailable:    800000 kB\n"
             "SwapTotal:      4000000 kB\nSwapFree:       2800000 kB\n")
        # pgmajfault 差分 400/s < 1000 → 不触发 HB_MEM_MAJFAULT
        _put(self.root, "system/proc_vmstat.txt",
             "pgmajfault 1000\npgfault 100000\npswpin 0\npswpout 0\n"
             "pgmajfault 1400\npgfault 100000\npswpin 0\npswpout 0\n")
        _put(self.root, "process/target_summary.json",
             '{"found": true, "stat": {"threads": 2}, '
             ' "exe": "python", "cmdline": ["python", "train.py"]}')
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_fired(self):
        ids = _fired_ids(self.diag)
        self.assertIn("HB_MEM_SWAP", ids)
        self.assertIn("HB_MEM_LOW", ids)

    def test_critical_severity(self):
        f = _find(self.diag, "HB_MEM_SWAP")
        self.assertEqual(f.severity, "CRITICAL")
        # 最高严重度 finding 决定 overall status
        self.assertEqual(self.diag.status, "CRITICAL")

    def test_score(self):
        f1 = _find(self.diag, "HB_MEM_SWAP")
        f2 = _find(self.diag, "HB_MEM_LOW")
        c1 = f1.confidence
        c2 = f2.confidence
        w = [2.0, 1.2]
        contrib = [2.0 * 1.0 * c1, 1.2 * 0.85 * c2]
        expect = 100.0 * sum(contrib) / max(sum(w), 3.0)
        self.assertAlmostEqual(self.diag.host_bound_score, expect, delta=1.0)

    def test_memory_risk(self):
        f = _find(self.diag, "HB_MEM_SWAP")
        expect = 100.0 * 1.0 * f.confidence
        self.assertAlmostEqual(self.diag.risk_dimensions["Memory"],
                               expect, delta=1.0)

    def test_html(self):
        _assert_html_common(self, self.html)


class TestGoldenDataLoader(unittest.TestCase):
    """DataLoader 案例: perf_report top1 = pthread_cond → dataloader。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_dl_")
        _common_files(self.root)
        _proc_stat(self.root, idle=60, iowait=3)
        _loadavg(self.root, 1.0)
        _threads_csv(self.root, 2)
        _npu_idle(self.root)
        _perf_report_dataloader(self.root)
        _put(self.root, "process/target_summary.json",
             '{"found": true, "stat": {"threads": 2}, '
             ' "exe": "python", "cmdline": ["python", "train.py"]}')
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_hotspot_category(self):
        feats = self.bundle.features
        self.assertEqual(feats.get("perf.hotspot_category"), "dataloader")

    def test_fired(self):
        ids = _fired_ids(self.diag)
        self.assertIn("HB_DL_HOTSPOT", ids)

    def test_classification_no_subtype_suffix(self):
        # DATA_PIPELINE_BOUND 不在 SUBTYPE_HINTS 集合 → 无子类型后缀
        self.assertEqual(self.diag.classification, "Data Pipeline Bound")

    def test_score(self):
        f = _find(self.diag, "HB_DL_HOTSPOT")
        expect = 100.0 * (1.5 * 0.85 * f.confidence) / 3.0
        self.assertAlmostEqual(self.diag.host_bound_score, expect, delta=1.0)

    def test_html(self):
        _assert_html_common(self, self.html)


class TestGoldenNormal(unittest.TestCase):
    """正常案例: 全绿零命中。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_normal_")
        _common_files(self.root)
        _proc_stat(self.root, idle=600, iowait=10)
        _loadavg(self.root, 0.5)
        _threads_csv(self.root, 2)
        _npu_idle(self.root)
        _put(self.root, "process/target_summary.json",
             '{"found": true, "stat": {"threads": 2}, '
             ' "exe": "python", "cmdline": ["python", "train.py"]}')
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_no_fired(self):
        self.assertEqual(_fired_ids(self.diag), [])

    def test_score_zero(self):
        self.assertEqual(self.diag.host_bound_score, 0.0)
        self.assertEqual(self.diag.score_label, "基本不存在")
        self.assertEqual(self.diag.status, "INFO")

    def test_classified_unknown(self):
        self.assertEqual(self.diag.classification, "Unknown")

    def test_html(self):
        _assert_html_common(self, self.html)


class TestGoldenIncomplete(unittest.TestCase):
    """不完整数据案例: 仅 manifest + 少量文件。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="hb_incomplete_")
        _put(self.root, "manifest.json", '{"case_id": "inc"}')
        _put(self.root, "environment/cpuinfo.txt", "processor\t: 0\n")
        self.bundle = run_analysis(self.root)
        self.diag = self.bundle.diag
        self.html = generate_html(self.bundle.to_dict())

    def test_dq_not_complete(self):
        self.assertNotEqual(self.bundle.dq.grade(), "complete")

    def test_no_fired(self):
        self.assertEqual(_fired_ids(self.diag), [])

    def test_statement_mentions_skip(self):
        self.assertIn("部分规则因数据缺失未启用", self.diag.statement)

    def test_low_confidence(self):
        # 无 findings → overall confidence = dq_factor
        self.assertLess(self.diag.confidence, 0.7)

    def test_html(self):
        _assert_html_common(self, self.html)


if __name__ == "__main__":
    unittest.main()
