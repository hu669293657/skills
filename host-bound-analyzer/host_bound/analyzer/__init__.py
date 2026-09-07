# -*- coding: utf-8 -*-
"""analyzer 包出口：总控编排 + 各维度分析器（import 即注册）。"""

from .common import (AnalysisBundle, DimAnalyzer, all_analyzers,
                     enrich_features, new_section)
from . import cpu, thread, scheduler, numa, io, memory, perf_analyzer  # noqa: F401
from .host_bound import default_rules_dir, run_analysis

__all__ = [
    "AnalysisBundle", "DimAnalyzer", "all_analyzers", "new_section",
    "enrich_features", "run_analysis", "default_rules_dir",
]
