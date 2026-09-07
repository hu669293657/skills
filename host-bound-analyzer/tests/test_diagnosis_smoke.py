# -*- coding: utf-8 -*-
"""Diagnosis 全链路冒烟/回归测试（pytest 可收集，亦可直接运行）。

覆盖场景：
  1. 规则加载与条件编译（含坏条件负例）
  2. 强 Host Bound 场景（多规则触发、证据链、评分、根因树、雷达、建议）
  3. 正常场景（零误报）
  4. 数据不完整场景（缺失特征 SKIP，置信度降级，提示补采）
  5. 空数据场景（不崩溃）

运行方式：
  pytest tests/test_diagnosis_smoke.py -v
  python tests/test_diagnosis_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from host_bound.diagnosis import (ConditionError, DiagnosisEngine,
                                  load_rules_from_dir, parse_condition)
from host_bound.models.case import DataQuality
from host_bound.models.evidence import Evidence
from host_bound.models.metrics import FeatureSet

RULES_DIR = os.path.join(ROOT, "host_bound", "rules")


def ev(key, value, unit, source, line="1", raw=""):
    """模拟 parser 行为：每个特征都携带指向原始采集文件的证据指针。"""
    return [Evidence(key, value, unit, source, line, raw)]


# ----------------------------------------------------------------------
# 1. 规则加载与条件编译
# ----------------------------------------------------------------------
def test_rules_load_and_compile():
    rules = load_rules_from_dir(RULES_DIR)
    assert len(rules) >= 15
    ids = [r.rule_id for r in rules]
    assert len(ids) == len(set(ids)), "规则 ID 重复"
    assert all(r._ast for r in rules), "存在编译失败的条件"
    assert len(set(r.diagnosis_type for r in rules)) >= 8, "诊断类型覆盖不足"


def test_bad_condition_raises():
    try:
        parse_condition("cpu.util_pct > and")
    except ConditionError:
        return
    raise AssertionError("坏条件应报 ConditionError")


# ----------------------------------------------------------------------
# 2. 强 Host Bound 场景
# ----------------------------------------------------------------------
def _strong_features():
    fs = FeatureSet()
    fs.set("cpu.util_pct", 95.0, "%", ev("cpu.util_pct", 95.0, "%", "host/cpu_stat.txt", "3-10"))
    fs.set("cpu.load5_per_core", 2.4, "", ev("cpu.load5_per_core", 2.4, "", "host/loadavg.txt"))
    fs.set("cpu.cv_util", 0.15, "", ev("cpu.cv_util", 0.15, "", "host/cpu_stat.txt", "3-10"))
    fs.set("cpu.iowait_pct", 3.0, "%", ev("cpu.iowait_pct", 3.0, "%", "host/vmstat.txt", "2-12"))
    fs.set("cpu.cores_logical", 64, "", ev("cpu.cores_logical", 64, "", "host/cpuinfo.txt"))
    fs.set("device.util_pct", 18.0, "%", ev("device.util_pct", 18.0, "%", "device/npu_util.csv", "1-60"))
    fs.set("sched.ctx_switch_per_sec", 180000.0, "/s",
           ev("sched.ctx_switch_per_sec", 180000.0, "/s", "host/vmstat.txt", "2-12"))
    fs.set("sched.nonvoluntary_ctx_per_sec", 30000.0, "/s",
           ev("sched.nonvoluntary_ctx_per_sec", 30000.0, "/s", "host/vmstat.txt", "2-12"))
    fs.set("sched.procs_blocked", 0, "", ev("sched.procs_blocked", 0, "", "host/vmstat.txt", "2-12"))
    fs.set("thread.oversub_ratio", 4.2, "x", ev("thread.oversub_ratio", 4.2, "x", "host/process.txt", "5"))
    fs.set("thread.omp_threads_setting", 256, "", ev("thread.omp_threads_setting", 256, "", "env/omp.txt"))
    fs.set("mem.available_pct", 6.0, "%", ev("mem.available_pct", 6.0, "%", "host/meminfo.txt", "1-3"))
    fs.set("mem.swap_used_pct", 35.0, "%", ev("mem.swap_used_pct", 35.0, "%", "host/meminfo.txt", "16-17"))
    fs.set("mem.pgmajfault_per_sec", 1500.0, "/s",
           ev("mem.pgmajfault_per_sec", 1500.0, "/s", "host/vmstat.txt", "2-12"))
    fs.set("perf.hotspot_category", "dataloader", "",
           ev("perf.hotspot_category", "dataloader", "", "perf/report.txt"))
    fs.set("perf.top_symbols", "dataloader_worker;tokenize", "",
           ev("perf.top_symbols", "dataloader_worker;tokenize", "", "perf/report.txt"))
    fs.set("dl.workers_setting", 4, "", ev("dl.workers_setting", 4, "", "log/train.log", "88"))
    return fs


def _complete_dq():
    dq = DataQuality()
    for src in ("cpu", "mem", "process", "thread", "scheduler", "device", "vmstat"):
        dq.mark(src, "present")
    return dq


def test_strong_host_bound_scenario():
    eng = DiagnosisEngine(rules_dir=RULES_DIR)
    diag = eng.run(_strong_features(), None, dq=_complete_dq())

    assert diag.host_bound_score >= 60, "强场景得分应 >= 60，实际 %.1f" % diag.host_bound_score
    assert diag.status in ("HIGH", "CRITICAL")
    fired = {f.rule_id for f in diag.findings}
    for rid in ("HB_GAP_CORE", "HB_THREAD_OVERSUB", "HB_DL_HOTSPOT", "HB_MEM_SWAP"):
        assert rid in fired, "%s 应触发" % rid
    # 证据链可追溯：每条 finding 至少 1 条证据，且证据含源文件路径
    assert all(len(f.evidence) > 0 for f in diag.findings)
    assert all(e.get("source") for f in diag.findings for e in f.evidence)
    # 结构完整性
    assert diag.root_cause_tree is not None and len(diag.root_cause_tree.children) >= 3
    assert len(diag.risk_dimensions) == 9, "固定 9 维雷达"
    assert "DataLoader" in diag.classification
    assert any(p["level"] == "P0" for p in diag.priorities)


# ----------------------------------------------------------------------
# 3. 正常场景
# ----------------------------------------------------------------------
def test_normal_scenario_no_false_positive():
    fs = FeatureSet()
    fs.set("cpu.util_pct", 38.0, "%", ev("cpu.util_pct", 38.0, "%", "host/cpu_stat.txt", "3-10"))
    fs.set("cpu.load5_per_core", 0.5, "", ev("cpu.load5_per_core", 0.5, "", "host/loadavg.txt"))
    fs.set("cpu.cv_util", 0.2, "", ev("cpu.cv_util", 0.2, "", "host/cpu_stat.txt", "3-10"))
    fs.set("cpu.iowait_pct", 1.0, "%", ev("cpu.iowait_pct", 1.0, "%", "host/vmstat.txt", "2-12"))
    fs.set("device.util_pct", 96.0, "%", ev("device.util_pct", 96.0, "%", "device/npu_util.csv", "1-60"))
    fs.set("sched.ctx_switch_per_sec", 9000.0, "/s",
           ev("sched.ctx_switch_per_sec", 9000.0, "/s", "host/vmstat.txt", "2-12"))
    fs.set("sched.nonvoluntary_ctx_per_sec", 500.0, "/s",
           ev("sched.nonvoluntary_ctx_per_sec", 500.0, "/s", "host/vmstat.txt", "2-12"))
    fs.set("sched.procs_blocked", 0, "", ev("sched.procs_blocked", 0, "", "host/vmstat.txt", "2-12"))
    fs.set("thread.oversub_ratio", 0.8, "x", ev("thread.oversub_ratio", 0.8, "x", "host/process.txt", "5"))
    fs.set("mem.available_pct", 45.0, "%", ev("mem.available_pct", 45.0, "%", "host/meminfo.txt", "1-3"))
    fs.set("mem.swap_used_pct", 0.0, "%", ev("mem.swap_used_pct", 0.0, "%", "host/meminfo.txt", "16-17"))
    fs.set("mem.pgmajfault_per_sec", 2.0, "/s",
           ev("mem.pgmajfault_per_sec", 2.0, "/s", "host/vmstat.txt", "2-12"))
    dq = DataQuality()
    for src in ("cpu", "mem", "process", "device"):
        dq.mark(src, "present")
    diag = DiagnosisEngine(rules_dir=RULES_DIR).run(fs, None, dq=dq)
    assert diag.host_bound_score < 20, "正常场景得分应 < 20，实际 %.1f" % diag.host_bound_score
    assert all(f.severity not in ("CRITICAL", "HIGH") for f in diag.findings), "正常场景不应有 HIGH+ finding"


# ----------------------------------------------------------------------
# 4. 数据不完整场景：缺失特征必须 SKIP 而非误判
# ----------------------------------------------------------------------
def test_partial_data_skip_not_guess():
    fs = FeatureSet()
    # 只有 CPU 利用率：HB_CPU_SAT_NO_DEVICE 需要的 cpu.load5_per_core 缺失，
    # HB_GAP_CORE 需要的 device.util_pct 缺失 => 全部规则 SKIP，不得凭单一指标下结论
    fs.set("cpu.util_pct", 95.0, "%", ev("cpu.util_pct", 95.0, "%", "host/cpu_stat.txt", "3-10"))
    dq = DataQuality()
    dq.mark("cpu", "present")
    dq.warn("vmstat 缺失")
    dq.warn("device 缺失")
    diag = DiagnosisEngine(rules_dir=RULES_DIR).run(fs, None, dq=dq)
    assert diag.host_bound_score < 40, "不完整场景不得虚高"
    assert diag.confidence < 0.5, "置信度应降级，实际 %.2f" % diag.confidence
    assert ("补" in diag.statement or "未启用" in diag.statement), "结论应提示补采"
    assert dq.grade() in ("partial", "insufficient")


# ----------------------------------------------------------------------
# 5. 空数据场景
# ----------------------------------------------------------------------
def test_empty_data_no_crash():
    diag = DiagnosisEngine(rules_dir=RULES_DIR).run(FeatureSet(), None, dq=DataQuality())
    assert diag.host_bound_score == 0.0
    assert diag.status == "INFO"


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    _fail = 0
    for _t in _tests:
        try:
            _t()
            print("PASS %s" % _t.__name__)
        except AssertionError as _e:
            _fail += 1
            print("FAIL %s: %s" % (_t.__name__, _e))
    print("\n==== %s ====" % ("ALL PASS" if not _fail else "FAILED: %d" % _fail))
    sys.exit(1 if _fail else 0)
