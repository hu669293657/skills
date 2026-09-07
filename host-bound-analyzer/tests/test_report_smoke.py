# -*- coding: utf-8 -*-
"""report 层冒烟测试：bundle.to_dict() -> generate_html -> 固定 18 章节 HTML。

复用 analyzer 夹具（high：精确命中三条规则；low：零命中），校验四件事：
1. 结构：18 个章节锚点（id="sec-01" .. "sec-18"）按序完整出现；
2. 高位夹具内容：评分/分档/规则命中/雷达/时间线双序列/页脚版本注入；
3. 离线约束：零 <script>、零外部 http(s) 资源引用（仅允许 SVG xmlns）；
4. 容错：低位夹具与空数据/非 dict 输入渲染不崩溃，占位原则生效。

运行：python tests/test_report_smoke.py -v
"""
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_analyzer_smoke import (  # noqa: E402
    _build_high_collection, _build_low_collection)
from host_bound.analyzer import run_analysis  # noqa: E402
from host_bound.report import generate_html  # noqa: E402

_EXPECTED_IDS = ["sec-%02d" % i for i in range(1, 19)]

_XMLNS = 'xmlns="http://www.w3.org/2000/svg"'


def _fmt_num(v):
    """与模板 _fmtval 同口径的数值展示（float 去尾零），用于一致性断言。"""
    if isinstance(v, float):
        s = ("%.1f" % v).rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


class ReportSmokeTest(unittest.TestCase):
    """generate_html：结构 + 内容 + 离线约束 + 容错。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hb_report_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _render_high(self):
        _build_high_collection(self.root)
        bundle = run_analysis(self.root)
        return bundle.diag.to_dict(), generate_html(bundle.to_dict())

    # -- 1. 结构 ------------------------------------------------------------

    def test_structure_18_sections_in_order(self):
        _, html = self._render_high()
        ids = re.findall(r'id="(sec-\d\d)"', html)
        self.assertEqual(ids, _EXPECTED_IDS)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("<style>", html)
        self.assertIn('<meta charset="utf-8"/>', html)

    # -- 2. 高位夹具内容 -----------------------------------------------------

    def test_high_content(self):
        diag, html = self._render_high()

        # 评分与分档落地（与 diagnosis 输出一致）
        self.assertIn(_fmt_num(diag["host_bound_score"]), html)
        self.assertIn("较明显", html)
        self.assertIn("Device Idle Gap", html)

        # 发现表：三条精确命中的规则
        for rid in ("HB_GAP_CORE", "HB_CPU_SAT_HIGH", "HB_CPU_SAT_NO_DEVICE"):
            self.assertIn(rid, html)

        # 图表：雷达多边形 + 时间线双序列 + 全部 SVG（9 gauge + 雷达/时间线/根因树）
        self.assertIn("<polygon", html)
        self.assertGreaterEqual(html.count("<polyline"), 2)
        self.assertGreaterEqual(html.count("<svg"), 11)

        # 页脚版本块由 generate_html 注入
        self.assertIn("0.2.0", html)

    # -- 3. 离线约束 ---------------------------------------------------------

    def test_offline_no_script_no_external(self):
        _, html = self._render_high()
        self.assertNotIn("<script", html.lower())
        self.assertNotIn(" onclick=", html.lower())
        cleaned = html.replace(_XMLNS, "")
        self.assertNotIn("http://", cleaned)
        self.assertNotIn("https://", cleaned)

    # -- 4. 容错与占位 -------------------------------------------------------

    def test_low_renders_without_findings(self):
        _build_low_collection(self.root)
        bundle = run_analysis(self.root)
        html = generate_html(bundle.to_dict())
        ids = re.findall(r'id="(sec-\d\d)"', html)
        self.assertEqual(ids, _EXPECTED_IDS)
        self.assertIn("基本不存在", html)
        # 零命中：任何规则 ID 都不应出现（发现表为空占位）
        self.assertNotIn("HB_GAP_CORE", html)

    def test_empty_and_non_dict_input(self):
        for bad in ({}, None):
            html = generate_html(bad)
            ids = re.findall(r'id="(sec-\d\d)"', html)
            self.assertEqual(ids, _EXPECTED_IDS)
            self.assertIn("--", html)  # 缺失值占位而非虚构


if __name__ == "__main__":
    unittest.main(verbosity=2)
