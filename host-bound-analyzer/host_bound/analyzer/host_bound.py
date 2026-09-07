# -*- coding: utf-8 -*-
"""Host Bound 总控编排：解析 -> 特征工程 -> 规则引擎 -> 维度视图 -> 汇总包。

流程（正确性优先，绝不虚报）：
  1. parser.registry.dispatch：逐文件解析（单文件异常隔离），产出 FeatureSet；
  2. enrich_features：跨维度派生与数据缺口告警；
  3. DiagnosisEngine.run：22 条 YAML 规则评估（缺特征即 SKIP）-> Score/Findings；
  4. 各维度分析器产出 Section（报告固定章节的结构化指标表）。
"""

import os

from ..models.case import DataQuality
from ..parser import dispatch
from ..parser.common import ParseContext, ParserResult
from ..diagnosis.engine import DiagnosisEngine
from .common import AnalysisBundle, all_analyzers, enrich_features

# 规则目录随包内置（host_bound/rules/*.yaml），离线可用
_RULES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rules"))


def default_rules_dir():
    return _RULES_DIR


def _case_info_from(feats):
    """从特征提取最小 case 信息（供引擎与报告头部）。"""
    info = {}
    for key, out in (("host.case_id", "case_id"),
                     ("host.hostname", "hostname"),
                     ("host.kernel", "kernel"),
                     ("host.arch", "arch"),
                     ("host.cmdline", "cmdline"),
                     ("framework.name", "framework"),
                     ("framework.version", "framework_version"),
                     ("manifest.collector_version", "collector_version")):
        v = feats.get(key)
        if v:
            info[out] = v
    return info or None


def run_analysis(root, case_info=None, rules_dir=None):
    """对一份采集目录完成完整分析，返回 AnalysisBundle。

    root        -- 采集产物目录（含 manifest.json 的目录结构）；
    case_info   -- 可选 dict；缺省时从 manifest 特征自动提取；
    rules_dir   -- 可选规则目录；缺省使用包内置 rules/。
    """
    dq = DataQuality()
    result = ParserResult()
    ctx = ParseContext(root, dq)

    # 1) 解析（单文件异常隔离在 registry 内完成）
    stats = dispatch(root, ctx, result)
    feats = result.features

    # 2) 特征工程（跨维度派生 + 数据缺口告警）
    derived = enrich_features(feats, dq)

    # 3) 规则引擎评估
    if case_info is None:
        case_info = _case_info_from(feats)
    engine = DiagnosisEngine(rules_dir=rules_dir or _RULES_DIR)
    diag = engine.run(feats, case_info, dq)

    # 4) 维度分析视图
    sections = [a.analyze(feats, dq) for a in all_analyzers()]

    bundle = AnalysisBundle()
    bundle.diag = diag
    bundle.sections = sections
    bundle.features = feats
    bundle.dq = dq
    bundle.parse_stats = stats
    bundle.derived = derived
    bundle.case_info = case_info
    # 解析阶段未成功产物占比高时给出整体提示
    total = stats.get("total", 0)
    ok = stats.get("ok", 0)
    if total and ok == 0:
        dq.warn("没有任何产物解析成功，请确认目录结构为 host-bound 采集格式")
    return bundle
