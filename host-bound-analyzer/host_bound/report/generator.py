# -*- coding: utf-8 -*-
"""报告生成器：分析结果 dict -> 完整离线 HTML。

generate_html(analysis) 是 report 层唯一对外入口：
1. 浅拷贝输入 dict（不污染调用方数据），注入 "_versions" 版本块（页脚展示）；
2. 委托 template.render 渲染固定 18 章节单文件 HTML（零 JS、零外部资源）。

容错约定：非 dict 输入一律按空数据处理；dict 内任意键缺失均由模板做
缺省渲染（占位说明、"--"），绝不虚构数据。
"""
from .. import VERSION_BLOCK
from . import template


def generate_html(analysis):
    """AnalysisBundle.to_dict() 结构 -> 完整 HTML 文本。

    参数 analysis: dict（通常为 bundle.to_dict() 的产物）；
    返回: str，单文件 HTML（lang=zh-CN，内联 CSS/SVG）。
    """
    data = dict(analysis) if isinstance(analysis, dict) else {}
    data.setdefault("_versions", dict(VERSION_BLOCK))
    return template.render(data)
