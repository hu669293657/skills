# -*- coding: utf-8 -*-
"""analyzer 公共层：维度分析视图（Section）+ 特征工程 + 汇总包。

职责边界（避免与规则引擎重复）：
- parser 层：采集产物 -> 特征（FeatureSet）/事件（Timeline）；
- diagnosis/ 规则引擎：特征 -> 规则判定 / 评分 / 建议 / 根因树；
- analyzer 层：
  1) 特征工程 enrich：跨维度派生与兜底（缺数据即标缺失原因，绝不虚报）；
  2) 维度分析视图 Section：供 HTML 报告固定章节使用的结构化指标表；
  3) 总控编排 host_bound.run_analysis。

Section 指标分级口径（仅用于报告可视化展示，与规则 severity 无关）：
  ok / warn / bad / info（纯信息）/ na（数据缺失，不参与判定）
"""

from ..models.case import DataQuality


# ---------------------------------------------------------------------------
# 指标取值与分级
# ---------------------------------------------------------------------------

def _collect_sources(section, feats, key):
    """把特征证据里的来源文件去重收进 section['files']。"""
    for ev in feats.evidence(key):
        src = ev.get("source")
        if src and src not in section["files"]:
            section["files"].append(src)


def metric(section, feats, key, label, unit="", warn_ge=None, bad_ge=None,
           low_bad=False, note="", info=False):
    """从 FeatureSet 取指标并按阈值分级，追加到 section['metrics']。

    - warn_ge/bad_ge：分级阈值。high_bad（默认）值越大越糟；low_bad 值越小越糟。
    - info=True：纯展示项，恒为 info 级。
    - 缺失：level=na，note 追加数据缺失说明（绝不编造数值）。
    返回 (value, level)；缺失时返回 (None, "na")。
    """
    item = {"key": key, "label": label, "unit": unit, "note": note}
    value = feats.get(key)
    if value is None or (isinstance(value, str) and not value):
        item["value"] = None
        item["level"] = "na"
        if note:
            item["note"] = note
        else:
            item["note"] = "数据缺失（对应采集产物未解析出该指标）"
        section["metrics"].append(item)
        if not info:
            section["missing"].append("%s（%s）" % (label, key))
        _collect_sources(section, feats, key)
        return None, "na"

    item["value"] = value
    if info:
        item["level"] = "info"
    elif low_bad:
        # bad_ge < warn_ge；值越小越糟
        if bad_ge is not None and value <= bad_ge:
            item["level"] = "bad"
        elif warn_ge is not None and value <= warn_ge:
            item["level"] = "warn"
        else:
            item["level"] = "ok"
    else:
        # warn_ge < bad_ge；值越大越糟
        if bad_ge is not None and value >= bad_ge:
            item["level"] = "bad"
        elif warn_ge is not None and value >= warn_ge:
            item["level"] = "warn"
        else:
            item["level"] = "ok"
    section["metrics"].append(item)
    _collect_sources(section, feats, key)
    return value, item["level"]


def fmt_num(v):
    """报告用数值格式化：浮点去尾零，整数直出。"""
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


def levels_of(section):
    """返回 section 内 (n_bad, n_warn, n_na)。"""
    n_bad = n_warn = n_na = 0
    for m in section["metrics"]:
        if m["level"] == "bad":
            n_bad += 1
        elif m["level"] == "warn":
            n_warn += 1
        elif m["level"] == "na":
            n_na += 1
    return n_bad, n_warn, n_na


def new_section(dim, title):
    return {"dim": dim, "title": title, "verdict": "",
            "metrics": [], "missing": [], "files": []}


# ---------------------------------------------------------------------------
# 各维度分析器基类与注册表
# ---------------------------------------------------------------------------

class DimAnalyzer(object):
    """维度分析器基类：FeatureSet -> Section（纯读取，不修改特征）。"""

    dim = "base"
    title = "维度分析"

    def analyze(self, feats, dq):
        raise NotImplementedError


_ANALYZERS = []


def register(cls):
    _ANALYZERS.append(cls())
    return cls


def all_analyzers():
    return list(_ANALYZERS)


# ---------------------------------------------------------------------------
# 特征工程：跨维度派生与兜底（引擎 run 之前调用）
# ---------------------------------------------------------------------------

def enrich_features(feats, dq):
    """跨维度派生特征与数据缺口告警。

    原则：只做"有依据"的派生，来源口径写入 note；无依据即告警提示补采。
    返回派生特征 key 列表（供测试与报告"特征工程"说明使用）。
    """
    derived = []

    # 1) thread.oversub_ratio 兜底：无 threads.csv 差分时，用线程总数/核数粗估。
    #    口径差异（总数 vs 活跃峰值）必须写入 note，规则侧按保守值使用。
    if not feats.has("thread.oversub_ratio"):
        total = feats.get("thread.total")
        cores = feats.get("cpu.cores_logical")
        if total and cores:
            ratio = float(total) / float(cores)
            feats.set("thread.oversub_ratio", round(ratio, 3), "",
                      note="兜底口径：线程总数/逻辑核数（无 threads.csv 活跃峰值差分）")
            derived.append("thread.oversub_ratio")

    # 2) device.util_pct 缺失告警：供给缺口类规则（HB_GAP_*）会整体 SKIP。
    if not feats.has("device.util_pct"):
        has_dev = feats.has("device.npu_available") or feats.has("device.gpu_available")
        if has_dev:
            dq.warn("设备已识别但利用率数据缺失（device.util_pct），"
                    "Host-Device 供给缺口判定受限，建议补采 npu-smi/nvidia-smi 周期数据")
        else:
            dq.warn("未检测到 NPU/GPU 设备信息（device.* 缺失），"
                    "请确认采集包含 npu-smi/nvidia-smi 或设备清单")

    # 3) swap 有 IO 但占比缺失：提示 meminfo 解析受限。
    swap_io = feats.get("mem.swap_io_per_sec")
    if swap_io and not feats.has("mem.swap_used_pct"):
        dq.warn("检测到 swap 换页 IO（%.2f/s）但 swap 占比缺失，内存压力判定不完整"
                % float(swap_io))

    # 4) cpu.bottleneck_hint：主机侧粗判提示（仅报告展示，不参与评分）。
    hint = _bottleneck_hint(feats)
    if hint:
        feats.set("cpu.bottleneck_hint", hint, "",
                  note="主机侧粗判提示：依据 util/iowait/ctx/cv 的朴素优先级")
        derived.append("cpu.bottleneck_hint")

    # 5) host.data_completeness：核心特征可用度（供报告头部展示）。
    core_keys = ("cpu.util_pct", "cpu.load5_per_core", "device.util_pct",
                 "mem.available_pct", "sched.ctx_switch_per_sec")
    have = sum(1 for k in core_keys if feats.has(k))
    feats.set("host.data_completeness", round(100.0 * have / len(core_keys), 1), "%",
              note="5 项核心特征可用比例（%d/%d）" % (have, len(core_keys)))
    derived.append("host.data_completeness")

    return derived


def _bottleneck_hint(feats):
    """朴素优先级粗判：iowait > 调度竞争 > 负载不均 > 饱和 > 空闲。"""
    util = feats.get("cpu.util_pct")
    iowait = feats.get("cpu.iowait_pct")
    ctx = feats.get("sched.ctx_switch_per_sec")
    nonvol = feats.get("sched.nonvoluntary_ctx_per_sec")
    cv = feats.get("cpu.cv_util")
    if isinstance(iowait, (int, float)) and iowait >= 20:
        return "IO 等待主导（iowait %.1f%%）" % iowait
    if isinstance(nonvol, (int, float)) and nonvol >= 20000:
        return "调度竞争主导（非自愿切换 %.0f/s）" % nonvol
    if isinstance(ctx, (int, float)) and ctx >= 100000 and \
       isinstance(util, (int, float)) and util >= 70:
        return "调度竞争主导（上下文切换 %.0f/s）" % ctx
    if isinstance(cv, (int, float)) and cv > 0.5 and \
       isinstance(util, (int, float)) and util >= 60:
        return "负载不均（利用率 CV %.2f）" % cv
    if isinstance(util, (int, float)):
        if util >= 90:
            return "CPU 饱和（util %.1f%%）" % util
        if util < 30:
            return "主机空闲（util %.1f%%）" % util
    return ""


# ---------------------------------------------------------------------------
# 分析汇总包
# ---------------------------------------------------------------------------

class AnalysisBundle(object):
    """run_analysis 的完整产物：诊断 + 各维度视图 + 特征 + 数据质量。"""

    def __init__(self):
        self.diag = None            # models.diagnosis.Diagnosis
        self.sections = []          # list[dict]（报告固定章节用）
        self.features = None        # FeatureSet
        self.dq = DataQuality()
        self.parse_stats = {}       # dispatch 统计
        self.derived = []           # 特征工程派生 key
        self.case_info = None       # dict 或 None

    def _case_info_dict(self):
        ci = self.case_info
        if ci is None:
            return None
        if hasattr(ci, "to_dict"):
            return ci.to_dict()
        return ci if isinstance(ci, dict) else None

    def to_dict(self):
        return {
            "diagnosis": self.diag.to_dict() if self.diag else None,
            "sections": self.sections,
            "parse_stats": self.parse_stats,
            "derived_features": self.derived,
            "dq": self.dq.to_dict() if hasattr(self.dq, "to_dict") else {
                "grade": self.dq.grade(),
                "sources": self.dq.sources,
                "warnings": self.dq.warnings,
                "limitations": self.dq.limitations(),
            },
            "case_info": self._case_info_dict(),
            "series": self.features.series_dump() if self.features else {},
        }
