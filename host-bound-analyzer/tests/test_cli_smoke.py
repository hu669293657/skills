# -*- coding: utf-8 -*-
"""CLI 冒烟测试：in-process 调 cli.main()，覆盖七个子命令的关键路径。

不覆盖 collect 的真实采样（Linux 专用，Windows 开发机上跳过）；
tar 解包用手工打包的夹具目录验证 _resolve_collection 路径。

运行：python tests/test_cli_smoke.py -v
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_analyzer_smoke import _build_high_collection  # noqa: E402
from host_bound import cli  # noqa: E402

_SEC_IDS = ["sec-%02d" % i for i in range(1, 19)]


class CliSmokeTest(unittest.TestCase):
    """CLI 七命令：退出码 0/2/1 与产物落地。"""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="hb_cli_")
        self.root = os.path.join(self.work, "collection")
        os.makedirs(self.root)
        _build_high_collection(self.root)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _out(self, name):
        return os.path.join(self.work, name)

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # -- version ------------------------------------------------------------

    def test_version(self):
        code, out, _err = self._run("version")
        self.assertEqual(code, 0)
        self.assertIn("host-bound", out)
        self.assertIn("0.2.1", out)

    def test_no_command_shows_help(self):
        code, _out, _err = self._run()
        self.assertEqual(code, 1)

    # -- analyze ------------------------------------------------------------

    def test_analyze_dir(self):
        out_json = self._out("analysis.json")
        code, out, _err = self._run("analyze", self.root, "-o", out_json)
        self.assertEqual(code, 0, out)
        with open(out_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
        for key in ("diagnosis", "sections", "dq", "case_info", "series",
                    "_versions"):
            self.assertIn(key, payload)
        self.assertEqual(payload["diagnosis"]["classification"],
                         "Device Idle Gap")

    def test_analyze_sanitize_replaces_identity(self):
        out_json = self._out("analysis_san.json")
        code, out, _err = self._run("analyze", self.root, "-o", out_json,
                                    "--sanitize")
        self.assertEqual(code, 0, out)
        with open(out_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
        hostname = (payload.get("case_info") or {}).get("hostname") or ""
        self.assertNotIn("test-host", hostname)
        self.assertNotIn("test-host", json.dumps(payload, ensure_ascii=False))

    def test_analyze_tarball(self):
        tar_path = self._out("case.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(self.root, arcname="hb-case-x")
        out_json = self._out("analysis_from_tar.json")
        code, out, _err = self._run("analyze", tar_path, "-o", out_json)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(out_json))

    def test_analyze_missing_input_fatal(self):
        code, _out, err = self._run("analyze",
                                    self._out("no_such_dir"), "-o",
                                    self._out("x.json"))
        self.assertEqual(code, 1)
        self.assertIn("错误", err)

    # -- report / run ---------------------------------------------------------

    def test_report_from_analysis_json(self):
        out_json = self._out("analysis.json")
        code, out, _err = self._run("analyze", self.root, "-o", out_json)
        self.assertEqual(code, 0, out)
        out_html = self._out("report.html")
        code, out, _err = self._run("report", out_json, "-o", out_html)
        self.assertEqual(code, 0, out)
        with open(out_html, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertEqual(re.findall(r'id="(sec-\d\d)"', html), _SEC_IDS)
        self.assertIn("Device Idle Gap", html)

    def test_run_one_shot(self):
        out_html = self._out("run_report.html")
        code, out, _err = self._run("run", self.root, "-o", out_html)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(out_html))
        with open(out_html, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertEqual(re.findall(r'id="(sec-\d\d)"', html), _SEC_IDS)

    def test_report_bad_json_fatal(self):
        bad = self._out("bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        code, _out, err = self._run("report", bad, "-o", self._out("x.html"))
        self.assertEqual(code, 1)
        self.assertIn("错误", err)

    # -- validate ---------------------------------------------------------------

    def test_validate_complete(self):
        code, out, _err = self._run("validate", self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("数据质量分级: complete", out)

    # -- run-collect / collect ----------------------------------------------------

    @unittest.skipIf(os.name != "posix", "采集器仅面向 Linux 客户机")
    def test_collect_smoke(self):
        code, out, _err = self._run("collect", "--duration", "1",
                                    "--interval", "0.5", "--level", "1",
                                    "-o", self.work, "--no-tar")
        self.assertIn(code, (0, 2), out)
        self.assertIn("采集目录", out)

    @unittest.skipIf(os.name != "posix", "采集器仅面向 Linux 客户机")
    def test_run_collect_smoke(self):
        out_html = self._out("rc_report.html")
        code, out, _err = self._run("run-collect", "--duration", "1",
                                    "--interval", "0.5", "--level", "1",
                                    "-o", out_html)
        self.assertIn(code, (0, 2), out)
        self.assertTrue(os.path.isfile(out_html))


if __name__ == "__main__":
    unittest.main(verbosity=2)
