"""Regression tests for native DB/text routing and detail-sheet selection."""
import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "prof-compare" / "scripts"))
from host_kernel import compare_gpu, compare_npus
from kernel_execution import COUNTERS
from npu_source import npu_source
from parsers.common import get_sheet_data
from run_compare import input_pair


class FormatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "ascend_pytorch_profiler_0.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE STRING_IDS (id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO STRING_IDS VALUES (1,'outer'), (2,'inner'), (3,'add'), (4,'Add'),
                (5,'AI_CORE'), (6,'aic_total_cycles'), (7,'queue');
            CREATE TABLE PYTORCH_API (name, startNs, endNs, globalTid, type, connectionId);
            INSERT INTO PYTORCH_API VALUES (1,1000,10000,11,50001,NULL), (2,2000,6000,11,50001,8),
                (7,3000,3100,11,50002,NULL);
            CREATE TABLE CONNECTION_IDS (id, connectionId);
            INSERT INTO CONNECTION_IDS VALUES (8,23), (8,24);
            CREATE TABLE TASK (globalTaskId, startNs, endNs, deviceId, streamId, taskId, connectionId);
            INSERT INTO TASK VALUES (0,14000,17000,0,1,1,24), (1,8000,10000,0,1,0,23);
            CREATE TABLE COMPUTE_TASK_INFO (globalTaskId, name, opType, taskType, waitNs, blockNum);
            INSERT INTO COMPUTE_TASK_INFO VALUES (0,3,4,5,0,2), (1,3,4,5,NULL,1);
            CREATE TABLE TASK_PMU_INFO (globalTaskId, name, value);
            INSERT INTO TASK_PMU_INFO VALUES (1,6,0);
        """)
        con.close()
        self.text = self.root / "text"
        self.text.mkdir()
        def write_csv(name, rows):
            with (self.text / name).open("w", newline="", encoding="utf8") as f:
                csv.writer(f).writerows(rows)
        write_csv("kernel_details.csv", [
            ["Name", "Type", "Accelerator Core", "Start Time(us)", "Duration(us)", "Block Dim"],
            ["add", "Add", "AI_CORE", 14, 3, 2], ["add", "Add", "AI_CORE", 8, 2, 1]])
        write_csv("operator_details.csv", [
            ["Name", "Host Self Duration(us)", "Host Total Duration(us)"], ["outer", 5, 9], ["inner", 4, 4]])
        self.gpu = self.root / "gpu.pt.trace.json"
        self.gpu.write_text(json.dumps({"traceEvents": [
            {"ph":"X", "cat":"cpu_op", "name":"inner", "ts":1, "dur":4, "pid":1, "tid":1},
            {"ph":"X", "cat":"kernel", "name":"add", "ts":9, "dur":3, "pid":2, "tid":2}]}))

    def test_npu_format_matrix(self):
        for left in (self.db, self.text):
            for right in (self.db, self.text):
                with self.subTest(left=left,right=right):
                    r = compare_npus(left, right)
                    self.assertEqual(r["device"][0]["diff"]["delta_ms"], 0)
                    self.assertEqual(r["device"][0]["npu"]["calls"], 2)
                    self.assertEqual(r["host"][0]["npu_name"], "outer")
                    self.assertEqual(r["host"][0]["npu_self_ms"], 0.005)
                    seq = r["execution"]["series"][0]
                    self.assertEqual([t[4:7:2] for t in seq["left"]], [[2, 1], [3, 2]])
                    self.assertEqual([t[0][4] for t in seq["right"]], [2, 3])

    def test_db_gpu_routing_and_fields(self):
        for npu in (self.db, self.text):
            self.assertEqual(input_pair(self.gpu, npu)[0], "npu_gpu")
            self.assertEqual(compare_gpu(npu, self.gpu)["device"][0]["gpu"]["calls"], 1)
        r = compare_gpu(self.db, self.gpu)
        self.assertEqual(r["device"][0]["npu_hosts"], {"outer > inner": 2})
        self.assertEqual(len(r["host"]), 2)
        a, b = r["execution"]["series"][0]["left"]
        counter = 13 + COUNTERS.index("aic_total_cycles")
        self.assertEqual((a[5],a[counter],b[5],b[counter]), (None,0,0,None))
        self.assertEqual(len(r["execution"]["fields"]), 47)

    def test_missing_framework_keeps_device(self):
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE PYTORCH_API")
        con.close()
        r = compare_gpu(self.db, self.gpu)
        self.assertEqual(r["host"], [])
        self.assertEqual(r["device"][0]["npu"]["calls"], 2)
        self.assertTrue(r["notes"])

    def test_schedule_tasks_in_derived_kernel_sheet(self):
        from kernel_sheets import reconcile_kernel_sheets
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE COMMUNICATION_SCHEDULE_TASK_INFO (globalTaskId, name, opType, taskType);
            INSERT INTO COMMUNICATION_SCHEDULE_TASK_INFO VALUES (2,3,4,5);
            INSERT INTO TASK VALUES (2,18000,20000,0,1,2,NULL);
        """)
        con.close()
        wb = Workbook()
        ws = wb.active
        ws.title = "KernelTypeCompare"
        ws.append(["Order Id", "Kernel Type", "Core Type"] + ["metric"]*12)
        ws.append([1, "Add", "AI_CORE", 7, 7/3, 3, 2, 3, 5, 2.5, 3, 2, 2, 5/7, 1])
        result = reconcile_kernel_sheets(wb, [self.text, self.db], [None, None])
        row = list(result[ws.title].values)[1]
        self.assertEqual((row[8], row[12], row[13]), (7, 3, 1))
        self.assertEqual(ws.cell(2, 13).value, 2)
        wb.close()
        result.close()

    def test_select_one_rank(self):
        (self.root / "analysis.db").touch()
        self.assertEqual(npu_source(self.root), self.db)
        (self.root / "ascend_pytorch_profiler_1.db").write_bytes(self.db.read_bytes())
        with self.assertRaises(ValueError):
            npu_source(self.root)
        self.assertEqual(npu_source(self.db), self.db)

    def test_empty_db_in_text_export(self):
        from contextlib import ExitStack
        from run_compare import msprof_input
        (self.text / "ascend_pytorch_profiler_0.db").touch()
        self.assertEqual(npu_source(self.text), self.text)
        with ExitStack() as stack:
            view = Path(msprof_input(self.text, stack)) / "ASCEND_PROFILER_OUTPUT"
            self.assertTrue((view / "kernel_details.csv").is_file())
            self.assertFalse(list(view.glob("*.db")))

    def test_kernel_type_without_statistics_csv(self):
        from contextlib import ExitStack
        from run_compare import msprof_input
        with ExitStack() as stack:
            view = Path(msprof_input(self.text, stack, kernel_type=True)) / "ASCEND_PROFILER_OUTPUT"
            with (view / "op_statistic.csv").open(encoding="utf8") as f:
                row, = csv.DictReader(f)
            self.assertEqual((row["OP Type"], row["Count"]), ("Add", "2"))
            self.assertEqual(float(row["Avg Time(us)"]), 2.5)
        self.assertFalse((self.text / "op_statistic.csv").exists())

    def test_detail_never_reads_statistic_sheet(self):
        wb = Workbook()
        for name in ("OperatorCompare", "MemoryCompare", "ModuleCompare"):
            ws = wb.create_sheet(name + "Statistic")
            ws.append(["Top", "Name"])
            ws.append([1, "stat"])
            self.assertEqual(get_sheet_data(wb, name), ([], []))
            ws = wb.create_sheet(name)
            ws.append(["metadata"])
            ws.append(["Order Id", "Name"])
            ws.append([1, "detail"])
            self.assertEqual(get_sheet_data(wb, name)[1], [["1", "detail"]])
        wb.close()


if __name__ == "__main__":
    unittest.main()
