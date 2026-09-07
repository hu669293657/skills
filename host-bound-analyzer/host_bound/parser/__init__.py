# -*- coding: utf-8 -*-
"""host_bound.parser：采集产物解析层。

对外主要出口：
- ParseContext / ParserResult / BaseParser / read_file（common）
- dispatch（registry）：扫描采集目录、派发解析、异常隔离、DQ 打标
- CORE_SOURCE_FILES / parser_names（registry，调试与验证辅助）
"""
from .common import BaseParser, ParseContext, ParserResult, read_file
from .registry import CORE_SOURCE_FILES, dispatch, parser_names

__all__ = ["BaseParser", "ParseContext", "ParserResult", "read_file",
           "dispatch", "parser_names", "CORE_SOURCE_FILES"]
