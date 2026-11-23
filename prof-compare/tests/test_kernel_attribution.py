from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from host_kernel import compare_gpu
from kernel_attribution import DeviceEvent, Span, add_groups, ce_groups, gemm_groups, gpu_attribution


class KernelAttributionTests(unittest.TestCase):
    def test_ce_chunks_use_framework_calls_and_graph_is_counted_separately(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "ascend_pytorch_profiler_0.db"
            gpu = root / "cuda.pt.trace.json"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("""
                    CREATE TABLE STRING_IDS(id, value);
                    INSERT INTO STRING_IDS VALUES
                      (1, 'cross_entropy_backward_kernel'), (2, 'AI_VECTOR_CORE'),
                      (3, '_MMQKernelsLinearCrossEntropyBackward'),
                      (4, 'mmq_ascendc::ce_backward_out'), (5, '4096,155648'),
                      (6, 'launch'), (7, 'c10::BFloat16');
                    CREATE TABLE PYTORCH_API (startNs, endNs, globalTid, connectionId, name, inputShapes, inputDtypes);
                    INSERT INTO PYTORCH_API VALUES (1000, 50000, 1, NULL, 3, NULL, NULL),
                      (2000, 40000, 1, NULL, 4, 5, 7);
                    CREATE TABLE TASK (startNs, endNs, deviceId, streamId, taskId, connectionId, globalTaskId);
                    INSERT INTO TASK VALUES (60000, 160000, 0, 3, 8, 10, 17);
                    CREATE TABLE COMPUTE_TASK_INFO (globalTaskId, name, opType, taskType);
                    INSERT INTO COMPUTE_TASK_INFO VALUES (17, 1, 1, 2);
                    CREATE TABLE CANN_API (startNs, endNs, globalTid, connectionId, name);
                    INSERT INTO CANN_API VALUES (5000, 10000, 1, 10, 6);
                """)
            module = "_MMQKernelsLinearCrossEntropyBackward"
            symbol = "kernel_cutlass_kernel_quackcross_entropyCrossEntropyBackward_object_at__tensorptrbf16gmemalign16o155648i64"
            def event(cat, name, ts, dur, corr=None, on_device=False, **args):
                if corr is not None:
                    args["correlation"] = corr
                return dict(cat=cat, name=name, ph="X", ts=ts, dur=dur, args=args,
                            pid=0 if on_device else 20, tid=3 if on_device else 20)

            events = [event("cpu_op", module, 10, 70, **{"Input Dims": [[4096]], "External id": 6}),
                      event("cuda_runtime", "cudaLaunchKernel", 20, 2, 11, **{"External id": 6}),
                      event("cuda_driver", "cuLaunchKernelEx", 30, 2, 12, **{"External id": 6}),
                      event("kernel", symbol, 40, 10, 11, True, grid=[2048, 10, 1]),
                      event("kernel", symbol, 52, 15, 12, True, grid=[2048, 10, 1]),
                      event("cuda_runtime", "cudaGraphLaunch", 100, 2, 13),
                      event("kernel", symbol, 120, 20, 13, True, **{"External id": 6})]
            gpu.write_text(json.dumps({"traceEvents": events}), encoding="utf-8")
            result = compare_gpu(db, gpu)
            audit = result["attribution"]
            self.assertEqual(audit["gpu"]["graph_replay"]["calls"], 1)
            self.assertEqual(result["gpu_graph_calls"], 1)
            self.assertEqual(audit["gpu"]["framework"]["calls"], 2)
            self.assertEqual(audit["npu"]["cann_context"]["calls"], 1)
            row, = audit["groups"]
            self.assertEqual(row["npu"]["event_ids"], [17])
            self.assertEqual(row["gpu"]["event_ids"], [3, 4])
            self.assertEqual(row["gpu"]["logical_calls"], 1)
            self.assertAlmostEqual(row["gpu"]["mean_ms"], 0.025)
            self.assertEqual(row["mean_ratio"], 4)
            self.assertIsNone(result["device"][0]["diff"])


    def test_uniform_but_incomplete_ce_tiles_have_no_ratio(self):
        module = "_MMQKernelsLinearCrossEntropyBackward"
        owner = Span(11, 0, 10, "mmq_ascendc::ce_backward_out", {"shape": "4096,155648", "dtype": "DT_BF16"})
        npu = {1: DeviceEvent(1, 100, "ce", (Span(10, 0, 10, module), owner), 8, 2, {}, "cann_context")}
        cuda_owner = Span(30, 0, 10, module, {"Input Dims": [[4096]]})
        symbol = "quackcross_entropyCrossEntropyBackward_object_at__tensorptrbf16gmemalign16o155648i64"
        gpu = {2: DeviceEvent(2, 25, symbol, (cuda_owner,), 7, 2, {"grid": [2048, 10, 1]}, "framework")}
        row, = ce_groups(npu, gpu)
        self.assertFalse(row["comparable"])
        self.assertIsNone(row["mean_ratio"])

    def test_duplicate_correlation_does_not_pick_an_owner(self):
        def event(cat, name, pid, ts, **args):
            return dict(cat=cat, name=name, pid=pid, tid=pid, ts=ts, dur=2, ph="X", args=args)

        events = [event("cuda_runtime", "cudaLaunchKernel", 1, 1, correlation=4),
                  event("cuda_driver", "cuLaunchKernelEx", 2, 2, correlation=4),
                  event("kernel", "gemm", 0, 6, correlation=4)]
        records, coverage = gpu_attribution(events)
        self.assertFalse(records)
        self.assertEqual(coverage["ambiguous_launch"]["calls"], 1)

    def test_addmm_and_mismatched_device_shape_cannot_create_add_speed_ratio(self):
        owner = Span(10, 0, 10, "aten::add_", {"shape": "8;8", "dtype": "float;float;Scalar"})
        excluded = Span(11, 0, 10, "aten::addmm", owner.args)
        left_attrs = {"Type": "Add", "Input Shapes": "64;8", "Output Shapes": "8"}
        npu = {i: DeviceEvent(i, 30, name, (host,), i, 2, left_attrs, "native_framework")
               for i, name, host in ((1, "aclnnAdd", owner), (2, "aclnnAddmm", excluded))}
        cuda_owner = Span(30, 0, 10, "aten::add_", {
            "Input Dims": [[8], [8], []], "Input type": ["float", "float", "Scalar"]})
        gpu = {3: DeviceEvent(3, 3, "CUDAFunctor_add<float>", (cuda_owner,), 4, 5, {}, "framework")}
        row, = add_groups(npu, gpu)
        self.assertEqual(row["npu"]["event_ids"], [1])
        self.assertIsNone(row["mean_ratio"])

    def test_grouped_wgrad_counts_same_expert_calls_but_checks_operand_rows(self):
        host = Span(10, 0, 10, "npu::npu_grouped_matmul_add_")
        n_attrs = {"Type": "GroupedMatmulAdd", "Input Shapes": "40,8;40,16;4;512",
                   "Input Data Types": "DT_BF16;DT_BF16;INT64;FLOAT", "Output Data Types": "FLOAT"}
        npu = {1: DeviceEvent(1, 100, "aclnnGroupedMatmulAdd", (host,), None, 1, n_attrs, "native_framework")}
        tag = Span(30, 0, 10, "grouped_gemm_bf16_tn_o_fp32.cuda: a[48x8] b[48x16] out[4x8x16]")
        gpu = {3: DeviceEvent(3, 200, "gemm", (tag,), 4, 5, {}, "framework")}
        row, = gemm_groups(npu, gpu)
        self.assertEqual(row["npu"]["operand_rows"], 40)
        self.assertEqual(row["gpu"]["operand_rows"], 48)
        self.assertIsNone(row["mean_ratio"])


if __name__ == "__main__":
    unittest.main()


