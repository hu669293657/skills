"""
End-to-End Test Script for Host Trace Diagnosis
=================================================
Creates synthetic trace data, runs the full pipeline:
  Parse -> IR Store -> Feature Extraction -> Rule Diagnosis -> Report Generation

Usage:
    python tests/e2e_test.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ir.schema import (
    TraceEvent, TraceMetadata, TraceSource,
    EventCategory, EventPhase,
)
from ir.writer import IRWriter
from ir.reader import IRReader
from features.host_metrics import HostMetricsExtractor
from features.gap_scanner import GapScanner
from features.correlation import CorrelationEngine
from features.timeline_features import TimelineFeatureExtractor
from features.vector import FeatureVectorBuilder
from rules.engine import RuleEngine
from agent.diagnosis_agent import DiagnosisAgent
from agent.knowledge_base import KnowledgeBase
from report.generator import ReportGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("e2e_test")


# ============================================================
# Synthetic Trace Data Generator
# ============================================================

def generate_synthetic_events() -> list[TraceEvent]:
    """
    Generate a synthetic trace simulating a Host-side bottleneck scenario:
    - DataLoader threads competing for CPU
    - NPU kernels with large idle gaps
    - H2D memcpy blocking
    - High scheduling latency
    """
    events: list[TraceEvent] = []
    base_ts = 1_000_000  # 1s base in microseconds
    step_duration = 50_000  # 50ms per training step
    num_steps = 10

    # --- Metadata events ---
    events.append(TraceEvent(
        ts=base_ts, ph=EventPhase.METADATA.value,
        name="process_name", cat=EventCategory.METADATA.value,
        pid=1000, args={"name": "training_process"}
    ))
    events.append(TraceEvent(
        ts=base_ts, ph=EventPhase.METADATA.value,
        name="thread_name", cat=EventCategory.METADATA.value,
        pid=1000, tid=100, args={"name": "RuntimeThread"}
    ))
    events.append(TraceEvent(
        ts=base_ts, ph=EventPhase.METADATA.value,
        name="thread_name", cat=EventCategory.METADATA.value,
        pid=1000, tid=101, args={"name": "DataLoaderWorker-0"}
    ))
    events.append(TraceEvent(
        ts=base_ts, ph=EventPhase.METADATA.value,
        name="thread_name", cat=EventCategory.METADATA.value,
        pid=1000, tid=102, args={"name": "DataLoaderWorker-1"}
    ))

    for step in range(num_steps):
        step_start = base_ts + step * step_duration

        # --- Host: DataLoader workers consuming CPU (causing contention) ---
        for worker_tid in [101, 102]:
            events.append(TraceEvent(
                ts=step_start, dur=15_000, pid=1000, tid=worker_tid,
                cpu=0 if worker_tid == 101 else 1,
                name="DataLoader._next_data", cat=EventCategory.DATA_LOADER.value,
                ph=EventPhase.COMPLETE.value,
                args={"batch_size": 32, "worker_id": worker_tid - 101}
            ))

        # --- Host: sched_switch events showing high runqueue ---
        events.append(TraceEvent(
            ts=step_start + 2_000, dur=0, pid=1000, tid=100,
            cpu=0,
            name="sched_switch", cat=EventCategory.CPU_SCHED.value,
            ph=EventPhase.INSTANT.value,
            args={"prev_pid": 101, "next_pid": 100, "prev_state": "R"}
        ))
        events.append(TraceEvent(
            ts=step_start + 5_000, dur=0, pid=1000, tid=100,
            cpu=0,
            name="sched_switch", cat=EventCategory.CPU_SCHED.value,
            ph=EventPhase.INSTANT.value,
            args={"prev_pid": 102, "next_pid": 101, "prev_state": "R"}
        ))
        # sched_wakeup with high latency
        events.append(TraceEvent(
            ts=step_start + 8_000, dur=0, pid=1000, tid=100,
            cpu=0,
            name="sched_wakeup", cat=EventCategory.CPU_SCHED.value,
            ph=EventPhase.INSTANT.value,
            args={"pid": 100, "target_cpu": 0}
        ))
        # Runtime thread gets scheduled late (10ms wakeup latency)
        events.append(TraceEvent(
            ts=step_start + 18_000, dur=0, pid=1000, tid=100,
            cpu=0,
            name="sched_switch", cat=EventCategory.CPU_SCHED.value,
            ph=EventPhase.INSTANT.value,
            args={"prev_pid": 0, "next_pid": 100, "prev_state": "S"}
        ))

        # --- Host: H2D memcpy (synchronous, blocking) ---
        events.append(TraceEvent(
            ts=step_start + 20_000, dur=8_000, pid=1000, tid=100,
            cpu=0,
            name="aclrtMemcpyH2D", cat=EventCategory.NPU_MEMCPY.value,
            ph=EventPhase.COMPLETE.value,
            device_id=0, stream_id=0,
            args={"size_bytes": 4_194_304, "kind": "H2D"}
        ))

        # --- Host: Kernel launch API calls ---
        events.append(TraceEvent(
            ts=step_start + 28_000, dur=500, pid=1000, tid=100,
            cpu=0,
            name="aclrtLaunchKernel", cat=EventCategory.CUDA_NPU_API.value,
            ph=EventPhase.COMPLETE.value,
            args={"kernel": "MatMul"}
        ))

        # --- Device: NPU kernel execution (with gap before it) ---
        # The gap between memcpy end and kernel start represents Host bottleneck
        events.append(TraceEvent(
            ts=step_start + 35_000, dur=5_000, pid=1000, tid=-1,
            cpu=-1,
            name="MatMul_0", cat=EventCategory.NPU_KERNEL.value,
            ph=EventPhase.COMPLETE.value,
            device_id=0, stream_id=0,
            args={"op_type": "MatMul", "input_shape": [32, 768, 768]}
        ))
        events.append(TraceEvent(
            ts=step_start + 40_000, dur=3_000, pid=1000, tid=-1,
            cpu=-1,
            name="Add_0", cat=EventCategory.NPU_KERNEL.value,
            ph=EventPhase.COMPLETE.value,
            device_id=0, stream_id=0,
        ))
        events.append(TraceEvent(
            ts=step_start + 43_000, dur=2_000, pid=1000, tid=-1,
            cpu=-1,
            name="LayerNorm_0", cat=EventCategory.NPU_KERNEL.value,
            ph=EventPhase.COMPLETE.value,
            device_id=0, stream_id=0,
        ))

        # --- Host: Runtime sync (blocking) ---
        events.append(TraceEvent(
            ts=step_start + 45_000, dur=3_000, pid=1000, tid=100,
            cpu=0,
            name="aclrtSynchronizeStream", cat=EventCategory.RUNTIME.value,
            ph=EventPhase.COMPLETE.value,
            device_id=0, stream_id=0,
        ))

        # --- Device: D2H memcpy ---
        events.append(TraceEvent(
            ts=step_start + 45_000, dur=2_000, pid=1000, tid=-1,
            cpu=-1,
            name="D2H_Copy", cat=EventCategory.NPU_MEMCPY.value,
            ph=EventPhase.COMPLETE.value,
            device_id=0, stream_id=0,
            args={"size_bytes": 1_048_576, "kind": "D2H"}
        ))

    # Sort by ts
    events.sort(key=lambda e: (e.ts, e.tid))
    return events


def generate_metadata(events: list[TraceEvent]) -> TraceMetadata:
    """Build metadata from the generated events."""
    ts_values = [e.ts for e in events]
    devices = set(e.device_id for e in events if e.device_id >= 0)
    cpus = set(e.cpu for e in events if e.cpu >= 0)
    return TraceMetadata(
        source=TraceSource.CHROME_JSON,
        file_path="<synthetic>",
        file_size_mb=0.01,
        total_events=len(events),
        ts_start=min(ts_values),
        ts_end=max(ts_values),
        duration_us=max(ts_values) - min(ts_values),
        devices=sorted(devices),
        processes={1000: "training_process"},
        threads={100: "RuntimeThread", 101: "DataLoaderWorker-0", 102: "DataLoaderWorker-1"},
        cpu_cores=max(len(cpus), 2),
    )


# ============================================================
# Test Pipeline
# ============================================================

def run_e2e_test():
    """Run the full pipeline end-to-end."""
    print("\n" + "=" * 60)
    print("  Host Trace Diagnosis - End-to-End Test")
    print("=" * 60 + "\n")

    tmpdir = tempfile.mkdtemp(prefix="htd_e2e_")
    logger.info(f"Temp directory: {tmpdir}")

    # Step 0: Generate synthetic data
    print("[0/6] Generating synthetic trace data...")
    events = generate_synthetic_events()
    metadata = generate_metadata(events)
    print(f"      Generated {len(events)} events, "
          f"duration={metadata.duration_us / 1e6:.1f}s, "
          f"devices={metadata.devices}, cpus={metadata.cpu_cores}")

    # Step 1: Write to IR
    print("\n[1/6] Writing IR (JSONL)...")
    ir_path = os.path.join(tmpdir, "trace_ir.jsonl")
    writer = IRWriter(ir_path, {"format": "jsonl"})
    for ev in events:
        writer.write_event(ev)
    writer.finalize(metadata)
    ir_size = os.path.getsize(ir_path)
    print(f"      IR written: {ir_path} ({ir_size / 1024:.1f} KB)")

    # Step 2: Read IR and extract features
    print("\n[2/6] Extracting features...")
    reader = IRReader(ir_path, {})

    # 2a: Host metrics
    host_extractor = HostMetricsExtractor({"window_us": 1_000_000, "step_us": 500_000})
    scalars, timelines = host_extractor.extract(reader)
    print(f"      Scalars: {len(scalars)} metrics")
    print(f"      Key scalars:")
    for k in ["cpu_util_avg", "runqueue_avg", "sched_latency_avg_us",
              "ctx_switch_rate", "h2d_bandwidth_mbs", "launch_count"]:
        print(f"        {k}: {scalars.get(k, 'N/A')}")

    # 2b: Gap scanning
    gap_scanner = GapScanner({
        "min_gap_us": 100, "significant_gap_us": 1000,
        "top_n": 20, "pareto_threshold": 0.8
    })
    gaps = gap_scanner.scan(reader)
    print(f"\n      Gaps: {len(gaps)} significant gaps found")
    print(f"        total_gap_us: {gap_scanner.total_gap_us}")
    print(f"        device_utilization: {gap_scanner.device_utilization:.1%}")
    if gaps:
        print(f"        Top gap: dur={gaps[0].gap_dur}us, "
              f"prev={gaps[0].prev_kernel_name}, next={gaps[0].next_kernel_name}")

    # 2c: Correlation
    corr_engine = CorrelationEngine({
        "max_offset_us": 5000, "min_coefficient": 0.6,
        "max_host_events_per_gap": 5
    })
    gap_host_pairs, attribution, corr_score = corr_engine.correlate(reader, gaps)
    print(f"\n      Correlation score: {corr_score:.3f}")
    print(f"      Bottleneck attribution:")
    for cat, ratio in sorted(attribution.items(), key=lambda x: -x[1]):
        print(f"        {cat}: {ratio:.1%}")

    # 2d: Timeline features
    timeline_ext = TimelineFeatureExtractor({})
    hot_funcs = timeline_ext.extract_hot_functions(reader, top_n=5)
    print(f"\n      Hot functions: {len(hot_funcs)}")
    for hf in hot_funcs[:3]:
        print(f"        {hf.get('name', '?')}: total_dur={hf.get('total_dur', 0)}, "
              f"count={hf.get('count', 0)}")
    gap_dist = timeline_ext.extract_gap_distribution(gaps)
    print(f"      Gap distribution: {gap_dist.get('total_count', 0)} total gaps")

    # 2e: Build feature vector
    builder = FeatureVectorBuilder()
    fv = builder.build(
        scalars=scalars, timelines=timelines, gaps=gaps,
        gap_host_pairs=gap_host_pairs, attribution=attribution,
        corr_score=corr_score, metadata=metadata,
    )
    fv_path = os.path.join(tmpdir, "features.json")
    with open(fv_path, "w", encoding="utf-8") as f:
        f.write(fv.to_json())
    print(f"\n      Feature vector written: {fv_path}")

    # Step 3: Rule engine
    print("\n[3/6] Running rule engine...")
    rules_dir = os.path.join(str(PROJECT_ROOT), "rules")
    rule_engine = RuleEngine({"rules_dir": rules_dir})
    matched_rules = rule_engine.evaluate(fv)
    print(f"      Matched {len(matched_rules)} rules:")
    for r in matched_rules:
        print(f"        {r.get('rule_id', '?')} [{r.get('severity', '?')}] "
              f"conf={r.get('confidence', 0):.2f}: {r.get('diagnosis', '?')[:60]}")

    # Step 4: Knowledge base
    print("\n[4/6] Searching knowledge base...")
    kb = KnowledgeBase()
    similar = kb.search_similar(fv, top_k=3)
    print(f"      Found {len(similar)} similar cases:")
    for c in similar:
        print(f"        {c.get('case_id', '?')}: sim={c.get('similarity', 0):.2f} "
              f"- {c.get('title', '?')[:50]}")

    # Step 5: Diagnosis agent
    print("\n[5/6] Running diagnosis agent...")
    agent = DiagnosisAgent({"include_evidence": True, "include_suggestions": True})
    result = agent.diagnose(fv, matched_rules)
    print(f"      Has host issue: {result.has_host_issue}")
    print(f"      Severity: {result.severity}")
    print(f"      Primary diagnosis: {result.primary_diagnosis}")
    print(f"      Evidence count: {len(result.evidence)}")
    print(f"      Suggestions count: {len(result.suggestions)}")

    # Step 6: Report generation
    print("\n[6/6] Generating report...")
    report_gen = ReportGenerator({
        "format": "both", "output_dir": tmpdir,
        "generate_timeline_viz": True, "max_gaps_in_viz": 10
    })
    report_path = report_gen.generate(result, tmpdir)
    print(f"      Report: {report_path}")
    print(f"      Report size: {os.path.getsize(report_path) / 1024:.1f} KB")

    # Check JSON report too
    json_report = os.path.join(tmpdir, "diagnosis_report.json")
    if os.path.exists(json_report):
        print(f"      JSON report: {json_report}")
        with open(json_report, "r", encoding="utf-8") as f:
            jr = json.load(f)
        print(f"      JSON keys: {list(jr.keys())}")

    # Summary
    print("\n" + "=" * 60)
    print("  E2E TEST SUMMARY")
    print("=" * 60)
    print(f"  Events: {len(events)}")
    print(f"  IR file: {ir_size / 1024:.1f} KB")
    print(f"  Scalar count: {len(scalars)}")
    print(f"  Gaps: {len(gaps)} (device_util={gap_scanner.device_utilization:.1%})")
    print(f"  Correlation: {corr_score:.3f}")
    print(f"  Rules matched: {len(matched_rules)}")
    print(f"  KB cases: {len(similar)}")
    print(f"  Diagnosis: {result.severity} - {result.primary_diagnosis[:50]}")
    print(f"  Evidence: {len(result.evidence)} items")
    print(f"  Suggestions: {len(result.suggestions)} items")
    print(f"  Report: {os.path.getsize(report_path) / 1024:.1f} KB")
    print("=" * 60)

    if result.has_host_issue and len(matched_rules) > 0:
        print("\n  RESULT: PASS - Host issue detected and diagnosed")
        return True
    else:
        print("\n  RESULT: WARN - No host issue detected (check synthetic data)")
        return False


if __name__ == "__main__":
    success = run_e2e_test()
    sys.exit(0 if success else 1)
