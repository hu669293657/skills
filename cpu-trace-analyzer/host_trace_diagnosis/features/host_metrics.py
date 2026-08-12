"""
Host Metrics Extractor
=======================
Extracts A-class scalar metrics and B-class timeline features from Host-side
trace events (CPU scheduling, runtime API calls, memory copy operations).

This module computes two categories of features:

A-class scalar metrics:
    - CPU utilization (avg / max / idle)
    - Runqueue length (avg / max)
    - Context switch rate
    - Scheduling latency (avg / p99 / max)
    - CPU load balance score
    - H2D / D2H memory copy bandwidth
    - Memory copy total duration
    - Kernel launch count and average gap

B-class timeline features:
    - cpu_util_per_step: CPU utilization per sliding window
    - runqueue_timeline: runqueue pressure over time
    - ctx_switch_rate_timeline: context switch rate over time
    - cpu_util_per_core: per-core utilization (stored in scalars as a dict)

All computations are based on streaming traversal (reader.iter_events /
reader.query_by_category) to avoid loading the entire trace into memory.
For algorithms requiring two passes (e.g. scheduling latency which needs a
wakeup map), key events are buffered in memory-efficient dictionaries.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from ir.schema import EventCategory, TraceEvent

if TYPE_CHECKING:
    from ir.reader import IRReader

logger = logging.getLogger("host_trace_diagnosis.features.host_metrics")


class HostMetricsExtractor:
    """Extract host-side scalar metrics and timeline features from trace events.

    Parameters
    ----------
    config : dict
        Configuration dict containing:
        - ``window_us`` (int): Sliding window size in microseconds. Default 1_000_000.
        - ``step_us`` (int): Sliding window step in microseconds. Default 500_000.
    """

    def __init__(self, config: dict) -> None:
        self.window_us: int = int(config.get("window_us", 1_000_000))
        self.step_us: int = int(config.get("step_us", 500_000))
        if self.step_us <= 0:
            self.step_us = self.window_us
        if self.window_us <= 0:
            self.window_us = 1_000_000

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def extract(
        self, reader: "IRReader"
    ) -> Tuple[Dict[str, Any], Dict[str, List[Tuple[int, float]]]]:
        """Extract host metrics from the trace IR.

        Performs multiple streaming passes over different event categories:
          1. CPU_SCHED events for scheduling metrics and scheduling latency.
          2. NPU_MEMCPY events for memory copy bandwidth.
          3. CUDA_NPU_API events for kernel launch statistics.

        Parameters
        ----------
        reader : IRReader
            The IR reader providing streaming access to trace events.

        Returns
        -------
        tuple
            ``(scalars, timelines)`` where:
            - ``scalars`` is a dict mapping metric names to scalar values.
            - ``timelines`` is a dict mapping metric names to lists of
              ``(ts, value)`` tuples.
        """
        metadata = reader.read_metadata()
        duration_us: int = max(metadata.duration_us, 1)
        duration_s: float = duration_us / 1e6
        num_cpus: int = max(metadata.cpu_cores, 1)

        # --- Pass 1: CPU scheduling metrics ---
        logger.debug("Extracting CPU scheduling metrics...")
        cpu_util_per_core, runqueue_timeline, ctx_switch_timeline, \
            sched_latencies, total_switches = self._extract_sched_metrics(
                reader, duration_us, num_cpus
            )

        # --- Pass 2: Memory copy metrics ---
        logger.debug("Extracting memory copy metrics...")
        h2d_bw, d2h_bw, memcpy_total_us = self._extract_memcpy_metrics(reader)

        # --- Pass 3: Kernel launch metrics ---
        logger.debug("Extracting launch metrics...")
        launch_count, launch_avg_gap_us = self._extract_launch_metrics(reader)

        # --- Assemble scalar metrics ---
        cpu_utils = list(cpu_util_per_core.values())
        cpu_util_avg = statistics.mean(cpu_utils) if cpu_utils else 0.0
        cpu_util_max = max(cpu_utils) if cpu_utils else 0.0
        cpu_idle_avg = max(1.0 - cpu_util_avg, 0.0)

        # Runqueue statistics
        rq_values = [v for _, v in runqueue_timeline]
        runqueue_avg = statistics.mean(rq_values) if rq_values else 0.0
        runqueue_max = max(rq_values) if rq_values else 0.0

        # Context switch rate
        ctx_switch_rate = total_switches / duration_s if duration_s > 0 else 0.0

        # Scheduling latency statistics
        sched_latency_avg_us = statistics.mean(sched_latencies) if sched_latencies else 0.0
        sched_latency_p99_us = self._percentile(sched_latencies, 99) if sched_latencies else 0.0
        sched_latency_max_us = max(sched_latencies) if sched_latencies else 0.0

        # CPU load balance score
        cpu_balance_score = self._compute_balance_score(cpu_utils)

        # Build cpu_util_per_step timeline from per-bucket running time
        cpu_util_timeline = self._build_cpu_util_timeline(num_cpus)

        scalars: Dict[str, Any] = {
            "cpu_util_avg": cpu_util_avg,
            "cpu_util_max": cpu_util_max,
            "cpu_idle_avg": cpu_idle_avg,
            "runqueue_avg": runqueue_avg,
            "runqueue_max": runqueue_max,
            "ctx_switch_rate": ctx_switch_rate,
            "sched_latency_avg_us": sched_latency_avg_us,
            "sched_latency_p99_us": sched_latency_p99_us,
            "sched_latency_max_us": sched_latency_max_us,
            "cpu_balance_score": cpu_balance_score,
            "h2d_bandwidth_mbs": h2d_bw,
            "d2h_bandwidth_mbs": d2h_bw,
            "memcpy_total_us": memcpy_total_us,
            "launch_count": launch_count,
            "launch_avg_gap_us": launch_avg_gap_us,
            # cpu_util_per_core is a dict[int, float] stored in scalars
            "cpu_util_per_core": cpu_util_per_core,
        }

        timelines: Dict[str, List[Tuple[int, float]]] = {
            "cpu_util_per_step": cpu_util_timeline,
            "runqueue_timeline": runqueue_timeline,
            "ctx_switch_rate_timeline": ctx_switch_timeline,
        }

        logger.info(
            "Host metrics extracted: cpu_util_avg=%.2f%%, ctx_switch_rate=%.1f/s, "
            "launch_count=%d, sched_latency_avg=%.1fus",
            cpu_util_avg * 100,
            ctx_switch_rate,
            launch_count,
            sched_latency_avg_us,
        )

        return scalars, timelines

    # ------------------------------------------------------------------ #
    #  CPU scheduling metrics                                             #
    # ------------------------------------------------------------------ #

    def _extract_sched_metrics(
        self, reader: "IRReader", duration_us: int, num_cpus: int
    ) -> Tuple[
        Dict[int, float],               # cpu_util_per_core
        List[Tuple[int, float]],         # runqueue_timeline
        List[Tuple[int, float]],         # ctx_switch_rate_timeline
        List[float],                     # sched_latencies
        int,                             # total_switches
    ]:
        """Extract all CPU scheduling related metrics in a single streaming pass.

        Processes CPU_SCHED category events to compute:
        - Per-core CPU utilization (running time / total time)
        - Per-bucket running time for the cpu_util_per_step timeline
        - Runqueue pressure (count of preempted tasks per window)
        - Context switch rate per window
        - Scheduling latency (wakeup to switch delay)

        The scheduling latency uses a wakeup_map that is built and consumed
        in the same pass, since events arrive in timestamp order and
        sched_wakeup always precedes the corresponding sched_switch.

        Parameters
        ----------
        reader : IRReader
            The IR reader.
        duration_us : int
            Total trace duration in microseconds.
        num_cpus : int
            Number of CPU cores.

        Returns
        -------
        tuple
            (cpu_util_per_core, runqueue_timeline, ctx_switch_timeline,
             sched_latencies, total_switches)
        """
        # Per-CPU running/idle time accumulators
        running_time: Dict[int, float] = defaultdict(float)
        idle_time: Dict[int, float] = defaultdict(float)
        last_switch_ts: Dict[int, int] = {}  # cpu -> last switch ts

        # Per-bucket running time for cpu_util_per_step timeline
        # bucket_idx -> running_time
        bucket_running: Dict[int, float] = defaultdict(float)

        # Per-bucket runqueue count and switch count
        bucket_rq_count: Dict[int, int] = defaultdict(int)
        bucket_switch_count: Dict[int, int] = defaultdict(int)

        # Scheduling latency: pid -> wakeup timestamp
        wakeup_map: Dict[int, int] = {}
        sched_latencies: List[float] = []

        total_switches: int = 0
        window_us = self.window_us

        sched_cat = EventCategory.CPU_SCHED.value
        for event in reader.query_by_category(sched_cat):
            name_lower = event.name.lower()

            if "switch" in name_lower:
                total_switches += 1
                cpu = event.cpu
                args = event.args or {}
                prev_pid = self._safe_int(args.get("prev_pid", -1))
                prev_state = str(args.get("prev_state", ""))
                next_pid = self._safe_int(args.get("next_pid", -1))

                # --- CPU running/idle time ---
                if cpu >= 0 and cpu in last_switch_ts:
                    delta = event.ts - last_switch_ts[cpu]
                    if delta > 0:
                        if prev_pid == 0:
                            # Swapper / idle task was running
                            idle_time[cpu] += delta
                        else:
                            running_time[cpu] += delta
                        # Attribute to the bucket where the slice started
                        bucket_idx = last_switch_ts[cpu] // window_us
                        if prev_pid != 0:
                            bucket_running[bucket_idx] += delta

                last_switch_ts[cpu] = event.ts

                # --- Bucket counters ---
                bucket_idx = event.ts // window_us
                bucket_switch_count[bucket_idx] += 1

                # --- Runqueue: prev_state contains 'R' means preempted (still runnable) ---
                if "R" in prev_state.upper():
                    bucket_rq_count[bucket_idx] += 1

                # --- Scheduling latency ---
                if next_pid >= 0 and next_pid in wakeup_map:
                    latency = event.ts - wakeup_map[next_pid]
                    if latency >= 0:
                        sched_latencies.append(float(latency))
                    del wakeup_map[next_pid]

            elif "wakeup" in name_lower:
                args = event.args or {}
                pid = self._safe_int(args.get("pid", -1))
                if pid >= 0:
                    wakeup_map[pid] = event.ts

        # --- Compute per-core CPU utilization ---
        cpu_util_per_core: Dict[int, float] = {}
        for cpu in set(list(running_time.keys()) + list(idle_time.keys())):
            total = running_time[cpu] + idle_time[cpu]
            if total > 0:
                cpu_util_per_core[cpu] = running_time[cpu] / total
            else:
                cpu_util_per_core[cpu] = 0.0

        # If no sched_switch events were found, try to infer CPUs from last_switch_ts
        if not cpu_util_per_core and last_switch_ts:
            for cpu in last_switch_ts:
                cpu_util_per_core[cpu] = 0.0

        # --- Build timelines ---
        runqueue_timeline = self._dict_to_timeline(bucket_rq_count, window_us, lambda v: float(v))
        ctx_switch_timeline = self._build_rate_timeline(bucket_switch_count, window_us)
        # Store bucket_running for _build_cpu_util_timeline
        self._bucket_running = bucket_running

        return (
            cpu_util_per_core,
            runqueue_timeline,
            ctx_switch_timeline,
            sched_latencies,
            total_switches,
        )

    # ------------------------------------------------------------------ #
    #  Memory copy metrics                                                #
    # ------------------------------------------------------------------ #

    def _extract_memcpy_metrics(
        self, reader: "IRReader"
    ) -> Tuple[float, float, int]:
        """Extract H2D/D2H bandwidth and total memcpy duration.

        Processes NPU_MEMCPY category events. Each event's ``args`` may
        contain:
        - ``bytes`` (int): Number of bytes transferred.
        - ``direction`` (str): "H2D", "D2H", or CUDA numeric kind
          (1=H2D, 2=D2H).

        Bandwidth is computed as total_bytes / total_dur_us, which equals
        MB/s (since 1 byte/us = 1e6 bytes/s = 1 MB/s).

        Parameters
        ----------
        reader : IRReader
            The IR reader.

        Returns
        -------
        tuple
            (h2d_bandwidth_mbs, d2h_bandwidth_mbs, memcpy_total_us)
        """
        h2d_bytes: int = 0
        h2d_dur_us: int = 0
        d2h_bytes: int = 0
        d2h_dur_us: int = 0
        total_dur_us: int = 0

        memcpy_cat = EventCategory.NPU_MEMCPY.value
        for event in reader.query_by_category(memcpy_cat):
            total_dur_us += event.dur
            args = event.args or {}
            bytes_transferred = self._safe_int(args.get("bytes", 0))
            direction = self._classify_memcpy_direction(args)

            if direction == "H2D":
                h2d_bytes += bytes_transferred
                h2d_dur_us += event.dur
            elif direction == "D2H":
                d2h_bytes += bytes_transferred
                d2h_dur_us += event.dur

        h2d_bw = h2d_bytes / h2d_dur_us if h2d_dur_us > 0 else 0.0
        d2h_bw = d2h_bytes / d2h_dur_us if d2h_dur_us > 0 else 0.0

        return h2d_bw, d2h_bw, total_dur_us

    def _classify_memcpy_direction(self, args: Dict[str, Any]) -> str:
        """Classify a memcpy event's direction from its args.

        Handles both string directions ("H2D", "D2H", "HostToDevice",
        "DeviceToHost") and CUDA numeric kinds (1=H2D, 2=D2H).

        Parameters
        ----------
        args : dict
            Event args dictionary.

        Returns
        -------
        str
            "H2D", "D2H", or "" (unknown).
        """
        # Check string direction fields
        direction = str(args.get("direction", args.get("kind", "")))
        dir_upper = direction.upper()

        if any(k in dir_upper for k in ("H2D", "HOST_TO_DEVICE", "HTOD")):
            return "H2D"
        if any(k in dir_upper for k in ("D2H", "DEVICE_TO_HOST", "DTOH")):
            return "D2H"

        # Check CUDA numeric cudaMemcpyKind enum
        kind = args.get("direction", args.get("kind", None))
        if isinstance(kind, (int, float)):
            kind_int = int(kind)
            if kind_int == 1:
                return "H2D"
            if kind_int == 2:
                return "D2H"

        # Check op field (some profilers use "op")
        op = str(args.get("op", "")).upper()
        if "H2D" in op or "HTOD" in op:
            return "H2D"
        if "D2H" in op or "DTOH" in op:
            return "D2H"

        return ""

    # ------------------------------------------------------------------ #
    #  Kernel launch metrics                                             #
    # ------------------------------------------------------------------ #

    def _extract_launch_metrics(
        self, reader: "IRReader"
    ) -> Tuple[int, float]:
        """Extract kernel launch count and average inter-launch gap.

        Processes CUDA_NPU_API category events (host-to-device API calls
        such as kernel launches).

        Parameters
        ----------
        reader : IRReader
            The IR reader.

        Returns
        -------
        tuple
            (launch_count, launch_avg_gap_us)
        """
        launch_ts: List[int] = []
        api_cat = EventCategory.CUDA_NPU_API.value

        for event in reader.query_by_category(api_cat):
            launch_ts.append(event.ts)

        launch_count = len(launch_ts)

        if launch_count < 2:
            return launch_count, 0.0

        # Compute gaps between consecutive launches (sorted by ts)
        launch_ts.sort()
        gaps = [
            launch_ts[i + 1] - launch_ts[i]
            for i in range(len(launch_ts) - 1)
        ]
        avg_gap = statistics.mean(gaps) if gaps else 0.0

        return launch_count, avg_gap

    # ------------------------------------------------------------------ #
    #  Timeline construction helpers                                      #
    # ------------------------------------------------------------------ #

    def _build_cpu_util_timeline(self, num_cpus: int) -> List[Tuple[int, float]]:
        """Build the cpu_util_per_step timeline from per-bucket running time.

        Uses the ``_bucket_running`` dict populated during
        :meth:`_extract_sched_metrics`. Each bucket's CPU utilization is
        computed as ``bucket_running_time / (window_us * num_cpus)``.

        Parameters
        ----------
        num_cpus : int
            Number of CPU cores (for normalization).

        Returns
        -------
        list of tuple
            Sorted list of ``(ts, cpu_utilization)`` tuples.
        """
        bucket_running: Dict[int, float] = getattr(self, "_bucket_running", {})
        if not bucket_running:
            return []

        denom = float(self.window_us * num_cpus)
        if denom <= 0:
            denom = float(self.window_us)

        timeline = []
        for bucket_idx in sorted(bucket_running.keys()):
            ts = bucket_idx * self.window_us
            util = min(bucket_running[bucket_idx] / denom, 1.0)
            timeline.append((ts, util))

        return timeline

    @staticmethod
    def _build_rate_timeline(
        bucket_counts: Dict[int, int], window_us: int
    ) -> List[Tuple[int, float]]:
        """Build a rate-per-second timeline from per-bucket event counts.

        Parameters
        ----------
        bucket_counts : dict
            Mapping from bucket index to event count.
        window_us : int
            Bucket size in microseconds.

        Returns
        -------
        list of tuple
            Sorted list of ``(ts, events_per_second)`` tuples.
        """
        if not bucket_counts:
            return []

        window_s = window_us / 1e6
        timeline = []
        for bucket_idx in sorted(bucket_counts.keys()):
            ts = bucket_idx * window_us
            rate = bucket_counts[bucket_idx] / window_s if window_s > 0 else 0.0
            timeline.append((ts, rate))

        return timeline

    @staticmethod
    def _dict_to_timeline(
        bucket_values: Dict[int, Any],
        window_us: int,
        transform: Any = None,
    ) -> List[Tuple[int, float]]:
        """Convert a bucket-indexed dict to a sorted timeline list.

        Parameters
        ----------
        bucket_values : dict
            Mapping from bucket index to a numeric value.
        window_us : int
            Bucket size in microseconds (for computing the timestamp).
        transform : callable, optional
            Optional transform function applied to each value.

        Returns
        -------
        list of tuple
            Sorted list of ``(ts, value)`` tuples.
        """
        if not bucket_values:
            return []

        timeline = []
        for bucket_idx in sorted(bucket_values.keys()):
            ts = bucket_idx * window_us
            val = bucket_values[bucket_idx]
            if transform is not None:
                val = transform(val)
            timeline.append((ts, float(val)))

        return timeline

    # ------------------------------------------------------------------ #
    #  Statistical helpers                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_balance_score(cpu_utils: List[float]) -> float:
        """Compute CPU load balance score.

        .. math::
            \\text{balance} = 1 - \\frac{\\sigma}{\\mu}

        Where :math:`\\sigma` is the standard deviation of per-core
        utilization and :math:`\\mu` is the mean. A score of 1.0 means
        perfectly balanced; 0.0 means highly imbalanced.

        Parameters
        ----------
        cpu_utils : list of float
            Per-core CPU utilization values.

        Returns
        -------
        float
            Balance score in [0, 1].
        """
        if len(cpu_utils) < 2:
            return 1.0

        mean_val = statistics.mean(cpu_utils)
        if mean_val <= 0:
            return 1.0

        stdev_val = statistics.stdev(cpu_utils)
        score = 1.0 - (stdev_val / mean_val)
        return max(0.0, min(1.0, score))

    @staticmethod
    def _percentile(data: List[float], p: float) -> float:
        """Compute the p-th percentile of a list using linear interpolation.

        Parameters
        ----------
        data : list of float
            Input data (will be sorted internally).
        p : float
            Percentile in range [0, 100].

        Returns
        -------
        float
            The p-th percentile value.
        """
        if not data:
            return 0.0

        sorted_data = sorted(data)
        n = len(sorted_data)
        if n == 1:
            return sorted_data[0]

        k = (n - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)

        if f == c:
            return sorted_data[int(k)]

        # Linear interpolation
        d0 = sorted_data[int(f)] * (c - k)
        d1 = sorted_data[int(c)] * (k - f)
        return d0 + d1

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """Safely convert a value to int, returning default on failure.

        Parameters
        ----------
        value : Any
            Value to convert.
        default : int
            Default value if conversion fails.

        Returns
        -------
        int
            Converted integer.
        """
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
