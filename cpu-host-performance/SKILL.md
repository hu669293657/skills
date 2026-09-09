---
name: cpu-host-performance
description: Analyze offline Linux CPU Host ftrace bundles for training or inference slowdowns, CPU saturation, scheduler latency, IRQ/SoftIRQ hotspots, affinity, NUMA, or frequency problems; generate evidence-led Markdown and offline HTML reports. Do not use for GPU/NPU device-side profiling alone.
---

# CPU Host Performance

Use this skill when a Linux training/inference workload (including Ascend/NPU workloads) may be Host CPU-bound: high or unbalanced CPU use, scheduler delay, data-loader stalls, HCCL/communication pressure, IRQ/SoftIRQ load, CPU-frequency anomalies, or suspect CPU/NUMA affinity.

The normal input is a `cpu_trace_*.tar.gz` bundle made by `scripts/cpu_trace_collect.sh`; the analyzer also accepts its unpacked directory. The output is `report.md` and a self-contained offline `report.html`.

## Workflow

1. Give the customer `scripts/cpu_trace_collect.sh`. It requires root and only uses Bash plus kernel tracefs/debugfs, `/proc`, and `/sys`; do not ask them to install packages. Suggested command: `bash cpu_trace_collect.sh --duration 30`.
2. Copy the returned bundle locally, then run the analyzer using Python 3 standard library only:
   ```bash
   cd analyzer
   python3 analyze_cpu_trace.py /path/to/cpu_trace_YYYYMMDD_HHMMSS.tar.gz --output /path/to/report-dir
   ```
   Or from this skill root: `PYTHONPATH=. python3 -m cpu_host_performance analyze <bundle> --output <dir>`.
3. Lead the response with Host status, primary issue, impact, evidence, confidence, and prioritized actions. Link both generated reports.

## Interpretation constraints

- Treat static `/proc` snapshots as point-in-time evidence, not a rate over the trace interval.
- Scheduling latency is measured by matching `sched_wakeup*` to the next `sched_switch` running the same PID. State the matching limitations explicitly.
- Do not claim NPU wait, thermal throttling, NUMA misplacement, or a hardware defect without direct evidence. Use `INSUFFICIENT_EVIDENCE` and name the extra capture needed.
- `trace.txt` can contain sensitive process names; reports must redact obvious command-line arguments and host names by default. Preserve metric evidence.
- The collector saves and restores the active ftrace configuration; never alter the customer's governor, affinity, IRQ affinity, workload, or network settings.

## Resources

- `scripts/cpu_trace_collect.sh`: portable customer-side collector.
- `references/diagnosis.md`: thresholds, diagnosis model, and report evidence rules.
- `analyzer/`: standard-library offline parser, metric engine, diagnosis logic, and renderers.
