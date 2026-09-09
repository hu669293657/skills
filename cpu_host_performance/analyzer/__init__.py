# -*- coding: utf-8 -*-
"""cpu_host_performance.analyzer — 解析 / 指标 / 诊断 / 报告 子模块包

模块职责：
- parser      多格式输入解析（tar.gz / zip / 目录 / ftrace 文本 / trace-cmd / Chrome JSON）
- metrics     指标计算 + 中文指标释义表
- diagnosis   13 类诊断规则引擎（组合判断 + 置信度）
- report      Markdown 报告 + inline SVG 图表
- report_html 单文件离线 HTML 报告渲染
"""
__all__ = ["parser", "metrics", "diagnosis", "report", "report_html"]
