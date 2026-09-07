# -*- coding: utf-8 -*-
"""Diagnosis 包：规则引擎(YAML) + 证据链 + 评分 + 优化建议。

对外只暴露稳定接口；规则文件位于包内 rules/ 目录（随包离线分发）。
"""

from .engine import DiagnosisEngine
from .rules import (ConditionError, MissingFeatures, Rule,
                    evaluate_condition, load_rules_from_dir, parse_condition)
from .scoring import aggregate_score, evidence_confidence
from .recommendation import (build_recommendation, dedupe_priorities,
                             inject_placeholders)

__all__ = [
    "DiagnosisEngine",
    "Rule",
    "ConditionError",
    "MissingFeatures",
    "evaluate_condition",
    "parse_condition",
    "load_rules_from_dir",
    "aggregate_score",
    "evidence_confidence",
    "build_recommendation",
    "dedupe_priorities",
    "inject_placeholders",
]
