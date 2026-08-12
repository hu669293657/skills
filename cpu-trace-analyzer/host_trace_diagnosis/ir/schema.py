"""
TraceEvent IR (Intermediate Representation) Schema
===================================================
All trace formats are converted into this unified representation.
This is the single source of truth for trace event structure.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Tuple

import json


class EventPhase(str, Enum):
    """Chrome Trace Event phases (also used for non-Chrome formats)."""
    BEGIN = "B"
    END = "E"
    COMPLETE = "X"
    INSTANT = "I"
    COUNTER = "C"
    METADATA = "M"
    ASYNC_BEGIN = "b"
    ASYNC_END = "e"
    ASYNC_INSTANT = "n"
    FLOW_START = "s"
    FLOW_STEP = "t"
    FLOW_END = "f"


class EventCategory(str, Enum):
    """Standardized event categories for classification."""
    # Host-side categories
    CPU_SCHED = "cpu_sched"
    CPU_FUNCTION = "cpu_function"
    MEMORY = "memory"
    IO = "io"
    RUNTIME = "runtime"
    DATA_LOADER = "data_loader"
    PYTHON = "python"
    CUDA_NPU_API = "cuda_npu_api"  # Host-to-device API calls

    # Device-side categories
    NPU_KERNEL = "npu_kernel"
    GPU_KERNEL = "gpu_kernel"
    NPU_MEMCPY = "npu_memcpy"
    STREAM_SYNC = "stream_sync"

    # Metadata
    METADATA = "metadata"
    UNKNOWN = "unknown"


class TraceSource(str, Enum):
    """Identifies the original trace format source."""
    CHROME_JSON = "chrome_json"
    FTRACE = "ftrace"
    MSPROF = "msprof"
    PERF = "perf"
    PERFETTO = "perfetto"
    UNKNOWN = "unknown"


@dataclass
class TraceEvent:
    """
    Unified trace event representation.

    All parsers convert their native format into this structure.
    Written to Parquet for efficient columnar storage and querying.

    Attributes:
        ts: Start timestamp in microseconds (relative to trace start or absolute).
        dur: Duration in microseconds (0 for instant events).
        pid: Process ID (-1 if not applicable).
        tid: Thread ID (-1 if not applicable).
        cpu: CPU core number (-1 if not applicable, e.g. device events).
        name: Event name (e.g. "sched_switch", "MatMul", "aclrtSynchronizeStream").
        cat: Event category (see EventCategory).
        ph: Event phase (see EventPhase).
        device_id: Device ID (-1 for host events, 0+ for NPU/GPU devices).
        stream_id: Stream ID (-1 if not applicable).
        args: Additional event-specific arguments as a JSON-serializable dict.
    """
    ts: int
    dur: int = 0
    pid: int = -1
    tid: int = -1
    cpu: int = -1
    name: str = ""
    cat: str = EventCategory.UNKNOWN.value
    ph: str = EventPhase.COMPLETE.value
    device_id: int = -1
    stream_id: int = -1
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        # args is already a dict, keep as-is for Parquet
        d["args"] = json.dumps(d.get("args", {}), ensure_ascii=False)
        return d

    def to_chrome_trace_dict(self) -> Dict[str, Any]:
        """Convert to Chrome Trace Event JSON format."""
        d: Dict[str, Any] = {
            "ph": self.ph,
            "ts": self.ts,
            "name": self.name,
            "cat": self.cat,
        }
        if self.dur > 0:
            d["dur"] = self.dur
        if self.pid >= 0:
            d["pid"] = self.pid
        if self.tid >= 0:
            d["tid"] = self.tid
        if self.args:
            d["args"] = self.args
        return d

    @classmethod
    def from_chrome_trace_dict(cls, d: Dict[str, Any]) -> "TraceEvent":
        """Create from a Chrome Trace Event JSON dict."""
        return cls(
            ts=int(d.get("ts", 0)),
            dur=int(d.get("dur", 0)),
            pid=int(d.get("pid", -1)),
            tid=int(d.get("tid", -1)),
            cpu=int(d.get("args", {}).get("cpu", -1)) if isinstance(d.get("args"), dict) else -1,
            name=str(d.get("name", "")),
            cat=str(d.get("cat", EventCategory.UNKNOWN.value)),
            ph=str(d.get("ph", EventPhase.COMPLETE.value)),
            args=d.get("args", {}) if isinstance(d.get("args"), dict) else {},
        )


@dataclass
class TraceMetadata:
    """Metadata about a parsed trace file."""
    source: TraceSource = TraceSource.UNKNOWN
    file_path: str = ""
    file_size_mb: float = 0.0
    total_events: int = 0
    ts_start: int = 0
    ts_end: int = 0
    duration_us: int = 0
    devices: List[int] = field(default_factory=list)
    processes: Dict[int, str] = field(default_factory=dict)
    threads: Dict[int, str] = field(default_factory=dict)
    cpu_cores: int = 0
    parallel_strategy: str = ""
    model_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source"] = self.source.value
        return d


@dataclass
class GapRecord:
    """
    Represents a device idle gap between two consecutive kernels.

    This is the core data structure for the gap-driven root cause analysis.
    """
    gap_start: int          # microseconds
    gap_end: int             # microseconds
    gap_dur: int             # microseconds
    device_id: int
    stream_id: int
    prev_kernel_name: str = ""
    next_kernel_name: str = ""
    # Host events overlapping with this gap (filled by correlation engine)
    host_events: List[Dict[str, Any]] = field(default_factory=list)
    # Primary attribution category (filled by rule engine)
    attribution: str = ""
    attribution_confidence: float = 0.0

    @property
    def gap_ms(self) -> float:
        return self.gap_dur / 1000.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FeatureVector:
    """
    Complete feature vector output by the feature extraction layer.

    Contains three categories of features:
    A) Scalar summary metrics
    B) Temporal sequence features
    C) Correlation features (Host <-> Device)
    """
    # --- A: Scalar summary metrics ---
    scalars: Dict[str, float] = field(default_factory=dict)

    # --- B: Temporal sequence features ---
    timelines: Dict[str, List[Tuple[int, float]]] = field(default_factory=dict)

    # --- C: Correlation features ---
    gap_host_op_pairs: List[Dict[str, Any]] = field(default_factory=list)
    bottleneck_attribution: Dict[str, float] = field(default_factory=dict)
    correlation_score: float = 0.0
    critical_path: List[Dict[str, Any]] = field(default_factory=list)

    # Top gaps (sorted by duration)
    top_gaps: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)


@dataclass
class DiagnosisResult:
    """
    Output of the diagnosis engine (rule engine + LLM agent).
    """
    # Matched rules with confidence scores
    matched_rules: List[Dict[str, Any]] = field(default_factory=list)

    # Overall assessment
    has_host_issue: bool = False
    severity: str = "NONE"  # NONE / LOW / MEDIUM / HIGH / CRITICAL
    primary_diagnosis: str = ""
    secondary_diagnoses: List[str] = field(default_factory=list)

    # Evidence chain
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    # Optimization suggestions
    suggestions: List[Dict[str, Any]] = field(default_factory=list)

    # Feature vector reference
    feature_vector: Optional[FeatureVector] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.feature_vector:
            d["feature_vector"] = self.feature_vector.to_dict()
        return d
