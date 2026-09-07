# -*- coding: utf-8 -*-
"""report 层：纯离线 SVG 图表（charts）+ 固定 18 章节 HTML 模板（template）。

对外仅暴露 generate_html(analysis_dict) -> str：
输入为 AnalysisBundle.to_dict() 结构（diagnosis/sections/parse_stats/
derived_features/dq/case_info/series），输出单文件离线 HTML
（零 JS、零外部资源、固定 18 章节顺序）。
"""
from .generator import generate_html

__all__ = ["generate_html"]
