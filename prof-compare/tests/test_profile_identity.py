import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook, load_workbook

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from main_analyzer import analyze


def write_npu(root, host, device=0, duration=2000):
    export = root / "ASCEND_PROFILER_OUTPUT"
    export.mkdir(parents=True)
    with (export / "kernel_details.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Type", "Accelerator Core", "Duration(us)", "Device_id",
                         "Start Time(us)", "Stream ID", "Task ID"])
        writer.writerow(["test_kernel", "TestOp", "AI_CORE", duration, device, 1, 2, 3])
    (root / "profiler_info_0.json").write_text(json.dumps({"rank_id": 0}), encoding="utf-8")
    host_dir = root / "PROF_test" / "host"
    host_dir.mkdir(parents=True)
    (host_dir / "info.json").write_text(json.dumps({"hostname": host}), encoding="utf-8")
    return root


class ProfileIdentityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def cli(self, script, *args):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def report(self, output):
        return (output / "compare_analysis_report.html").read_text(encoding="utf-8")

    def result(self, output):
        return json.loads((output / "compare_analysis_result.json").read_text(encoding="utf-8"))

    def test_same_device_number_on_different_hosts(self):
        base = write_npu(self.root / "old" / "run_042" / "rank_0", "node-a")
        comp = write_npu(self.root / "new" / "run_057" / "rank_0", "node-b", duration=3000)
        out = self.root / "out"
        self.cli("run_compare.py", "-bp", base, "-d", comp, "-o", out)
        data = self.result(out)
        identities = data["metadata"]["comparison"]
        self.assertEqual(identities["base"]["short_label"], "基线 · NPU 0 · node-a")
        self.assertEqual(identities["compare"]["short_label"], "比对 · NPU 0 · node-b")
        self.assertEqual(data["host_kernel"]["device"][0]["diff"]["delta_ms"], 1)
        self.assertIn("比对 · NPU 0 · node-b − 基线 · NPU 0 · node-a", self.report(out))
        wb = load_workbook(out / "chinese_xlsx" / "compare_chinese_result.xlsx", read_only=True)
        self.addCleanup(wb.close)
        self.assertNotIn("比对来源", wb.sheetnames)
        self.assertNotIn('id="comparison-sources"', self.report(out))
        self.assertNotIn('class="source-paths"', self.report(out))

    def test_same_host_and_device_number_use_run_path_to_distinguish(self):
        from profile_identity import comparison_identity, read_npu_identity
        base = write_npu(self.root / "old" / "run_042" / "rank_0", "node-a")
        comp = write_npu(self.root / "new" / "run_057" / "rank_0", "node-a")
        sides = comparison_identity(read_npu_identity(base, base / "ASCEND_PROFILER_OUTPUT"),
                                    read_npu_identity(comp, comp / "ASCEND_PROFILER_OUTPUT"))
        self.assertEqual(sides["base"]["short_label"], "基线 · NPU 0 · run_042")
        self.assertEqual(sides["compare"]["short_label"], "比对 · NPU 0 · run_057")

    def test_rank_without_device_number_is_reported_as_unknown(self):
        from profile_identity import comparison_identity, read_npu_identity
        base = write_npu(self.root / "run_042" / "rank_0", "node-a", device="")
        sides = comparison_identity(read_npu_identity(base, base / "ASCEND_PROFILER_OUTPUT"))
        self.assertEqual(sides["base"]["rank"], 0)
        self.assertIsNone(sides["base"]["device_id"])

    def test_cross_platform_hotspot_keeps_npu_left_when_roles_swap(self):
        npu = write_npu(self.root / "npu", "node-a")
        op_csv = npu / "ASCEND_PROFILER_OUTPUT" / "operator_details.csv"
        op_csv.write_text("Name,Host Self Duration(us),Host Total Duration(us)\naten::test,2500,3000\n",
                          encoding="utf-8")
        gpu = self.root / "gpu.pt.trace.json"
        gpu.write_text(json.dumps({
            "deviceProperties": [{"id": 0, "name": "NVIDIA H20"}],
            "traceEvents": [
                {"cat": "cpu_op", "ph": "X", "name": "aten::test", "pid": 99, "tid": 99, "ts": 0, "dur": 1000},
                {"cat": "kernel", "ph": "X", "name": "test_kernel", "pid": 0, "tid": 1, "ts": 1, "dur": 700,
                 "args": {"device": 0}},
            ],
        }), encoding="utf-8")
        for npu_is_base, paths in ((True, (npu, gpu)), (False, (gpu, npu))):
            with self.subTest(npu_is_base=npu_is_base):
                out = self.root / ("npu_base" if npu_is_base else "gpu_base")
                self.cli("run_compare.py", "-bp", paths[0], "-d", paths[1], "-o", out)
                raw = self.result(out)["host_kernel"]
                expected_roles = ["base", "compare"] if npu_is_base else ["compare", "base"]
                self.assertEqual(raw["side_roles"], expected_roles)
                self.assertEqual(raw["host"][0]["diff"]["delta_ms"], 2)
                left, right = ("基线", "比对") if npu_is_base else ("比对", "基线")
                html = self.report(out)
                self.assertIn(f"{left} · NPU 0 − {right} · GPU 0", html)
                self.assertIn(f'"labels":["{left} · NPU 0","{right} · GPU 0"],"gpu_peer":true', html)
                self.assertNotIn("NVIDIA H20", html)

    def test_db_uses_rank_map_and_model_for_selected_device(self):
        from profile_identity import read_npu_identity
        path = self.root / "ascend_pytorch_profiler_0.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE HOST_INFO (hostUid, hostName);
                INSERT INTO HOST_INFO VALUES ('1', 'node-a');
                CREATE TABLE NPU_INFO (id, name);
                INSERT INTO NPU_INFO VALUES (0, 'Different model'), (3, 'Ascend950');
                CREATE TABLE RANK_DEVICE_MAP (rankId, deviceId);
                INSERT INTO RANK_DEVICE_MAP VALUES (0, 3);
            """)
        identity = read_npu_identity(path, path)
        self.assertEqual((identity["hostname"], identity["device_id"], identity["device_model"], identity["rank"]),
                         ("node-a", 3, "Ascend950", 0))

    def test_gpu_model_follows_kernel_device_and_pid_is_not_a_device(self):
        from profile_identity import gpu_identity
        header = {"deviceProperties": [{"id": 0, "name": "Other GPU"}, {"id": 3, "name": "NVIDIA H20"}],
                  "distributedInfo": {"rank": 0}, "hostname": "node-gpu"}
        events = [{"cat": "kernel", "ph": "X", "pid": 17, "args": {"device": 3}}]
        identity = gpu_identity(header, events)
        self.assertEqual((identity["device_id"], identity["device_model"], identity["rank"], identity["hostname"]),
                         (3, "NVIDIA H20", 0, "node-gpu"))
        events[0]["args"] = {}
        self.assertIsNone(gpu_identity(header, events)["device_id"])
        self.assertIsNone(gpu_identity(header, events)["device_model"])

    def test_legacy_xlsx_reads_workbook_paths_and_accepts_labels(self):
        path = self.root / "original.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "OverallMetrics"
        ws.append([None, r"Base Profiling: D:\old\rank_0", None, None,
                   r"Comparison Profiling: D:\new\rank_0"])
        ws.append([])
        ws.append(["Index", "Duration(ms)", "Duration Ratio", "Number", "Duration(ms)",
                   "Duration Ratio", "Number", "Diff Duration(ms)", "Diff Ratio"])
        ws.append(["E2E Time", 100, 1, 1, 150, 1, 1, 50, 1.5])
        wb.save(path)
        out = self.root / "out"
        self.cli("main_analyzer.py", path, "-o", out,
                 "--base-label", "=旧版<script>", "--compare-label", "新版")
        data = self.result(out)
        base = data["metadata"]["comparison"]["base"]
        self.assertEqual(base["input_path"], r"D:\old\rank_0")
        self.assertEqual(base["rank"], 0)
        self.assertIsNone(base["device_id"])
        self.assertIsNone(base["device_type"])
        self.assertEqual(data["overall_metrics"]["e2e"]["diff"], 50)
        html = self.report(out)
        self.assertIn("比对 · 新版 − 基线 · =旧版&lt;script&gt;", html)
        self.assertNotIn("=旧版<script>", html)
        wb = load_workbook(out / "chinese_xlsx" / "compare_chinese_result.xlsx", read_only=True)
        self.addCleanup(wb.close)
        self.assertNotIn("比对来源", wb.sheetnames)
        cells = [cell for row in wb["总体性能比对"] for cell in row if cell.value == "基线 · =旧版<script>"]
        self.assertEqual([cell.data_type for cell in cells], ["s"])

    def test_e2e_selection_requires_two_positive_finite_measurements(self):
        from run_compare import read_e2e
        path = self.root / "probe.xlsx"
        for base, comp, expected in ((200, 100, (200, 100)), (0, 100, None), (100, None, None),
                                     (100, "INF", None), (100, 100, (100, 100))):
            with self.subTest(base=base, comp=comp):
                wb = Workbook()
                wb.active.title = "OverallMetrics"
                wb.active.append(["Index"])
                wb.active.append(["E2E Time", base, 1, 1, comp, 1, 1, 0, 1])
                wb.save(path)
                self.assertEqual(read_e2e(path), expected)

    def test_auto_base_keeps_candidate_order_without_e2e(self):
        base = write_npu(self.root / "first", "node-a")
        comp = write_npu(self.root / "second", "node-b")
        out = self.root / "out"
        self.cli("run_compare.py", "-bp", base, "-d", comp, "--auto-base", "-o", out)
        self.assertEqual(self.result(out)["metadata"]["comparison"]["base"]["hostname"], "node-a")

    def test_xlsx_without_source_headers_keeps_identity_unknown(self):
        path = self.root / "original.xlsx"
        Workbook().save(path)
        data = analyze(str(path), str(self.root / "out"))
        for role in ("base", "compare"):
            self.assertIsNone(data["metadata"]["comparison"][role]["input_path"])


if __name__ == "__main__":
    unittest.main()

