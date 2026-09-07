# -*- coding: utf-8 -*-
"""Recommendation post-processing: placeholder injection + fallback templates."""

from ..models.diagnosis import Recommendation

LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Fallback recommendations when a rule has none (keyed by diagnosis type).
FALLBACK = {
    "CPU_COMPUTE_BOUND": (
        "降低 Host 侧 CPU 计算开销：结合热点分析优化最重函数路径；"
        "检查 OMP/worker 线程配置与 CPU 亲和性。",
        "CPU 利用率与 run queue 下降，设备利用率上升。"),
    "THREAD_OVERSUBSCRIPTION": (
        "将线程池大小调整到与物理核数匹配（先减半做 A/B 验证），"
        "避免多个运行时层（OMP/MKL/DataLoader）同时超配线程。",
        "上下文切换与调度等待下降，吞吐上升。"),
    "NUMA_REMOTE_ACCESS": (
        "使用 numactl 将进程与内存绑定到设备所在 NUMA 节点；"
        "检查网卡/设备与内存节点拓扑。",
        "远端内存访问占比下降。"),
    "IO_BOUND": (
        "检查数据集本地化与读取方式（顺序读/缓存/并发度），"
        "必要时迁移数据到 NVMe 或内存盘。",
        "iowait 下降，数据供给延迟下降。"),
    "MEMORY_PRESSURE": (
        "减少常驻内存占用（batch size / 缓存策略），关闭无用的页缓存消耗方。",
        "内存回收与 swap 活动消失。"),
    "SCHEDULER_CONTENTION": (
        "减少活动线程数或合并线程池；为关键线程设置亲和性/优先级。",
        "上下文切换速率显著下降。"),
    "DATA_PIPELINE_BOUND": (
        "增加/均衡 DataLoader worker、启用 prefetch 与 pin_memory，"
        "将 CPU 密集预处理下放或向量化。",
        "设备利用率上升，step time 下降。"),
    "DEVICE_IDLE_GAP": (
        "排查 Host→Device 提交路径（同步点、小算子、频繁 H2D），"
        "合并小任务并异步化。",
        "device idle gap 占比下降。"),
}


def inject_placeholders(text, features):
    """Replace {feature.key} placeholders with formatted feature values."""
    if not text or "{" not in text:
        return text
    out = text
    i = 0
    while True:
        i = out.find("{", i)
        if i < 0:
            break
        j = out.find("}", i)
        if j < 0:
            break
        key = out[i + 1:j].strip()
        if features.has(key):
            v = features.get(key)
            if isinstance(v, float):
                v = ("%.1f" % v)
            elif isinstance(v, int):
                v = str(v)
            else:
                v = str(v)
            out = out[:i] + str(v) + out[j + 1:]
            i += len(str(v))
        else:
            i = j + 1
    return out


def build_recommendation(rule, features):
    rec = rule.recommendation or {}
    if not rec.get("action"):
        fb_action, fb_expected = FALLBACK.get(rule.diagnosis_type,
                                              ("结合热点与系统指标人工复核。", "复测指标改善。"))
        rec = dict(rec)
        rec.setdefault("action", fb_action)
        rec.setdefault("expected", fb_expected)
        rec.setdefault("verify", "优化后重采并对比 Host Bound Score 与关键指标。")
        rec.setdefault("level", "P1")
        rec.setdefault("certainty", "建议实验验证")
    return Recommendation(
        level=rec.get("level", "P2"),
        action=inject_placeholders(rec.get("action", ""), features),
        expected=inject_placeholders(rec.get("expected", ""), features),
        verify=inject_placeholders(rec.get("verify", ""), features),
        certainty=rec.get("certainty", "建议实验验证"),
        risk=rec.get("risk", ""),
    )


def dedupe_priorities(recommendations):
    """Sort by level, merge identical actions."""
    seen = set()
    out = []
    for r in sorted(recommendations, key=lambda x: LEVEL_ORDER.get(x.level, 9)):
        key = (r.level, r.action)
        if key in seen or not r.action:
            continue
        seen.add(key)
        out.append(r.to_dict())
    return out
