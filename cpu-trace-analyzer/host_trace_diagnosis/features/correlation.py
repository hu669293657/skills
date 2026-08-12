"""
Correlation Engine
===================
Host<->Device causal correlation engine.

This module correlates device idle gaps (detected by :class:`GapScanner`)
with host-side trace events to determine the root cause of each gap.

For each gap, the engine:
  1. Queries host events overlapping the gap's time window.
  2. Selects the top-N host events by duration.
  3. Classifies each host event into an attribution category.
  4. Determines the gap's primary attribution (longest host event).
  5. Computes the bottleneck attribution distribution across all gaps.

Additionally, the engine computes:
  - ``correlation_score``: Pearson correlation between host busy ratio and
    device idle ratio across 1-second windows. A high positive correlation
    indicates that host activity (or lack thereof) explains device idle
    periods.
  - ``critical_path``: The top-5 gaps with their host events, forming a
    causal chain for the most impactful idle periods.

Attribution categories:
    - ``CPU_SCHED``: CPU scheduling issues (preemption, context switches)
    - ``DATA_LOADER``: Data loading bottlenecks
    - ``MEMCPY``: Memory copy operations blocking the stream
    - ``LAUNCH_GAP``: Kernel launch gaps (host not launching fast enough)
    - ``IO_WAIT``: I/O wait (disk, network)
    - ``RUNTIME_BLOCK``: Runtime synchronization (sync, lock, mutex)
    - ``OTHER``: Unclassified
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from ir.schema import EventCategory, GapRecord, TraceEvent

if TYPE_CHECKING:
    from ir.reader import IRReader

logger = logging.getLogger("host_trace_diagnosis.features.correlation")


# Attribution category constants
ATTR_CPU_SCHED = "CPU_SCHED"
ATTR_DATA_LOADER = "DATA_LOADER"
ATTR_MEMCPY = "MEMCPY"
ATTR_LAUNCH_GAP = "LAUNCH_GAP"
ATTR_IO_WAIT = "IO_WAIT"
ATTR_RUNTIME_BLOCK = "RUNTIME_BLOCK"
ATTR_OTHER = "OTHER"

# Window size for correlation score computation (1 second in microseconds)
CORR_WINDOW_US = 1_000_000


class CorrelationEngine:
    """Correlate device idle gaps with host-side trace events.

    Parameters
    ----------
    config : dict
        Configuration dict containing:
        - ``max_offset_us`` (int): Maximum time offset for Host-Device
          correlation (us). Default 5000.
        - ``min_coefficient`` (float): Minimum correlation coefficient to
          consider significant. Default 0.6.
        - ``max_host_events_per_gap`` (int): Maximum number of host events
          to retrieve per gap window. Default 5.
    """

    def __init__(self, config: dict) -> None:
        self.max_offset_us: int = int(config.get("max_offset_us", 5000))
        self.min_coefficient: float = float(config.get("min_coefficient", 0.6))
        self.max_host_events_per_gap: int = int(config.get("max_host_events_per_gap", 5))

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def correlate(
        self, reader: "IRReader", gaps: List[GapRecord]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, float], float]:
        """Correlate device gaps with host events.

        Parameters
        ----------
        reader : IRReader
            The IR reader for querying host events.
        gaps : list of GapRecord
            Device idle gaps from :class:`GapScanner`.

        Returns
        -------
        tuple
            ``(gap_host_op_pairs, bottleneck_attribution, correlation_score)``
            where:
            - ``gap_host_op_pairs``: List of dicts, each describing the
              dominant host operation for a gap.
            - ``bottleneck_attribution``: Dict mapping attribution
              categories to their fraction of total gap time.
            - ``correlation_score``: Pearson correlation between host busy
              ratio and device idle ratio.
        """
        if not gaps:
            return [], {}, 0.0

        gap_host_op_pairs: List[Dict[str, Any]] = []
        attribution_durations: Dict[str, float] = defaultdict(float)
        total_gap_us: float = 0.0

        # --- Step 1-5: Process each gap ---
        for gap in gaps:
            # Query host events in the gap's time window
            host_events = self._query_host_events(reader, gap)

            # Select top-N by duration
            top_events = self._select_top_events(host_events)

            # Fill gap.host_events
            gap.host_events = [self._event_to_dict(e) for e in top_events]

            # Determine primary attribution
            primary_cat, primary_event = self._determine_attribution(top_events)

            # Set gap attribution
            gap.attribution = primary_cat
            if primary_event is not None:
                ratio = primary_event.dur / gap.gap_dur if gap.gap_dur > 0 else 0.0
                gap.attribution_confidence = min(ratio, 1.0)
            else:
                gap.attribution_confidence = 0.0

            # Build gap_host_op_pair
            pair = self._build_gap_host_pair(gap, primary_event, primary_cat)
            gap_host_op_pairs.append(pair)

            # Accumulate attribution durations
            attribution_durations[primary_cat] += gap.gap_dur
            total_gap_us += gap.gap_dur

        # --- Step 6: Compute bottleneck attribution ---
        bottleneck_attribution = self._compute_bottleneck_attribution(
            attribution_durations, total_gap_us
        )

        # --- Step 7: Compute correlation score ---
        correlation_score = self._compute_correlation_score(reader, gaps)

        # --- Step 8: Build critical path (done by caller or vector builder) ---
        # Critical path is stored for potential later use
        self.critical_path = self._build_critical_path(gaps)

        logger.info(
            "Correlation complete: %d gaps analyzed, corr_score=%.3f, "
            "top_attribution=%s",
            len(gaps),
            correlation_score,
            max(bottleneck_attribution, key=bottleneck_attribution.get)
            if bottleneck_attribution
            else "N/A",
        )

        return gap_host_op_pairs, bottleneck_attribution, correlation_score

    # ------------------------------------------------------------------ #
    #  Step 1: Query host events for a gap                                #
    # ------------------------------------------------------------------ #

    def _query_host_events(
        self, reader: "IRReader", gap: GapRecord
    ) -> List[TraceEvent]:
        """Query host-side events overlapping with the gap's time window.

        Uses ``reader.query_range`` to get events in
        ``[gap_start, gap_end]`` and filters for host events
        (``device_id == -1``).

        Parameters
        ----------
        reader : IRReader
            The IR reader.
        gap : GapRecord
            The device idle gap.

        Returns
        -------
        list of TraceEvent
            Host events overlapping with the gap.
        """
        host_events: List[TraceEvent] = []

        # Expand the query window slightly to catch events that start
        # just before the gap but extend into it
        query_start = max(0, gap.gap_start - self.max_offset_us)
        query_end = gap.gap_end + self.max_offset_us

        for event in reader.query_range(query_start, query_end):
            # Only host events (device_id == -1)
            if event.device_id >= 0:
                continue
            # Skip metadata events
            if event.cat == EventCategory.METADATA.value:
                continue
            # Check actual overlap with the gap
            event_end = event.ts + event.dur
            if event_end < gap.gap_start or event.ts > gap.gap_end:
                continue
            host_events.append(event)

        return host_events

    # ------------------------------------------------------------------ #
    #  Step 2: Select top-N host events by duration                       #
    # ------------------------------------------------------------------ #

    def _select_top_events(self, events: List[TraceEvent]) -> List[TraceEvent]:
        """Select the top-N host events by duration.

        Parameters
        ----------
        events : list of TraceEvent
            Host events overlapping with a gap.

        Returns
        -------
        list of TraceEvent
            Top-N events sorted by duration descending.
        """
        if not events:
            return []

        sorted_events = sorted(events, key=lambda e: e.dur, reverse=True)
        return sorted_events[: self.max_host_events_per_gap]

    # ------------------------------------------------------------------ #
    #  Step 3-5: Attribution classification                               #
    # ------------------------------------------------------------------ #

    def _determine_attribution(
        self, top_events: List[TraceEvent]
    ) -> Tuple[str, TraceEvent]:
        """Determine the primary attribution category for a gap.

        The primary attribution is based on the longest host event
        (top_events[0] if non-empty).

        Parameters
        ----------
        top_events : list of TraceEvent
            Top-N host events sorted by duration descending.

        Returns
        -------
        tuple
            (attribution_category, primary_event) where primary_event is
            the longest host event (or None if no events).
        """
        if not top_events:
            return ATTR_OTHER, None

        primary_event = top_events[0]
        attribution = self._classify_event(primary_event)
        return attribution, primary_event

    @staticmethod
    def _classify_event(event: TraceEvent) -> str:
        """Classify a host event into an attribution category.

        Classification rules (checked in priority order):
          1. cat=CPU_SCHED or name contains "sched" -> CPU_SCHED
          2. cat=DATA_LOADER or name contains "DataLoader" -> DATA_LOADER
          3. cat=NPU_MEMCPY or name contains "memcpy"/"copy" -> MEMCPY
          4. cat=CUDA_NPU_API or name contains "launch" -> LAUNCH_GAP
          5. cat=IO or name contains "io"/"read"/"write" -> IO_WAIT
          6. cat=RUNTIME or name contains "sync"/"lock"/"mutex" -> RUNTIME_BLOCK
          7. Otherwise -> OTHER

        Parameters
        ----------
        event : TraceEvent
            The host event to classify.

        Returns
        -------
        str
            Attribution category string.
        """
        cat = event.cat
        name_lower = event.name.lower()

        # 1. CPU scheduling
        if cat == EventCategory.CPU_SCHED.value or "sched" in name_lower:
            return ATTR_CPU_SCHED

        # 2. Data loader
        if cat == EventCategory.DATA_LOADER.value or "dataloader" in name_lower:
            return ATTR_DATA_LOADER

        # 3. Memory copy
        if cat == EventCategory.NPU_MEMCPY.value or "memcpy" in name_lower or "copy" in name_lower:
            return ATTR_MEMCPY

        # 4. Kernel launch gap
        if cat == EventCategory.CUDA_NPU_API.value or "launch" in name_lower:
            return ATTR_LAUNCH_GAP

        # 5. I/O wait
        if cat == EventCategory.IO.value or "io" in name_lower or "read" in name_lower or "write" in name_lower:
            return ATTR_IO_WAIT

        # 6. Runtime blocking
        if cat == EventCategory.RUNTIME.value or "sync" in name_lower or "lock" in name_lower or "mutex" in name_lower:
            return ATTR_RUNTIME_BLOCK

        # 7. Other
        return ATTR_OTHER

    # ------------------------------------------------------------------ #
    #  Step 6: Bottleneck attribution                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_bottleneck_attribution(
        attribution_durations: Dict[str, float],
        total_gap_us: float,
    ) -> Dict[str, float]:
        """Compute the bottleneck attribution distribution.

        Normalizes attribution durations by total gap time to produce
        a fractional contribution per category.

        Parameters
        ----------
        attribution_durations : dict
            Mapping from attribution category to total gap duration (us).
        total_gap_us : float
            Total gap duration across all gaps (us).

        Returns
        -------
        dict
            Mapping from attribution category to fraction of total gap
            time. Sorted by value descending.
        """
        if total_gap_us <= 0:
            return {}

        attribution = {
            cat: dur / total_gap_us
            for cat, dur in attribution_durations.items()
        }

        # Sort by value descending
        sorted_attr = dict(
            sorted(attribution.items(), key=lambda x: x[1], reverse=True)
        )
        return sorted_attr

    # ------------------------------------------------------------------ #
    #  Step 7: Correlation score (Pearson)                                #
    # ------------------------------------------------------------------ #

    def _compute_correlation_score(
        self, reader: "IRReader", gaps: List[GapRecord]
    ) -> float:
        """Compute Pearson correlation between host busy ratio and device idle ratio.

        The trace timeline is divided into 1-second windows. For each
        window:
        - ``host_busy_ratio`` = sum(host event durations) / window_duration
        - ``device_idle_ratio`` = sum(gap durations) / window_duration

        The Pearson correlation coefficient between these two series
        indicates how strongly host activity correlates with device idle
        periods.

        Parameters
        ----------
        reader : IRReader
            The IR reader.
        gaps : list of GapRecord
            Device idle gaps.

        Returns
        -------
        float
            Pearson correlation coefficient in [-1, 1]. Returns 0.0 if
            insufficient data.
        """
        metadata = reader.read_metadata()
        duration_us = max(metadata.duration_us, 1)
        num_windows = max(int(duration_us / CORR_WINDOW_US), 1)

        # --- Compute device idle ratio per window ---
        device_idle_per_window: Dict[int, float] = defaultdict(float)
        for gap in gaps:
            # Attribute gap duration to the window where it starts
            window_idx = gap.gap_start // CORR_WINDOW_US
            device_idle_per_window[window_idx] += gap.gap_dur

        # --- Compute host busy ratio per window ---
        host_busy_per_window: Dict[int, float] = defaultdict(float)
        for event in reader.iter_events():
            if event.device_id >= 0:
                continue  # Skip device events
            if event.cat == EventCategory.METADATA.value:
                continue
            if event.dur <= 0:
                continue
            window_idx = event.ts // CORR_WINDOW_US
            host_busy_per_window[window_idx] += event.dur

        # --- Build aligned series ---
        window_s = CORR_WINDOW_US / 1e6
        host_series: List[float] = []
        device_series: List[float] = []

        for w in range(num_windows):
            host_busy = host_busy_per_window.get(w, 0.0) / (CORR_WINDOW_US)
            device_idle = device_idle_per_window.get(w, 0.0) / (CORR_WINDOW_US)
            host_series.append(min(host_busy, 1.0))
            device_series.append(min(device_idle, 1.0))

        # --- Compute Pearson correlation ---
        score = self._pearson(host_series, device_series)

        # If correlation is negative (host busy -> device busy), clip to 0
        # since we're interested in host-caused device idle
        return max(score, 0.0) if not math.isnan(score) else 0.0

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> float:
        """Compute the Pearson correlation coefficient between two series.

        Parameters
        ----------
        x : list of float
            First data series.
        y : list of float
            Second data series.

        Returns
        -------
        float
            Pearson correlation coefficient in [-1, 1]. Returns 0.0 if
            insufficient data or zero variance.
        """
        n = min(len(x), len(y))
        if n < 2:
            return 0.0

        x = x[:n]
        y = y[:n]

        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)

        numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))

        sum_sq_x = sum((xi - mean_x) ** 2 for xi in x)
        sum_sq_y = sum((yi - mean_y) ** 2 for yi in y)

        denom = math.sqrt(sum_sq_x * sum_sq_y)

        if denom == 0:
            return 0.0

        return numerator / denom

    # ------------------------------------------------------------------ #
    #  Step 8: Critical path                                              #
    # ------------------------------------------------------------------ #

    def _build_critical_path(
        self, gaps: List[GapRecord], top_n: int = 5
    ) -> List[Dict[str, Any]]:
        """Build the critical path from the top gaps.

        The critical path is a causal chain of the top-N gaps (by
        duration) with their associated host events.

        Parameters
        ----------
        gaps : list of GapRecord
            Device idle gaps (sorted by duration descending).
        top_n : int
            Number of gaps to include in the critical path.

        Returns
        -------
        list of dict
            Critical path entries, each containing gap info and host events.
        """
        # Sort gaps by duration descending (in case they aren't already)
        sorted_gaps = sorted(gaps, key=lambda g: g.gap_dur, reverse=True)
        top_gaps = sorted_gaps[:top_n]

        critical_path: List[Dict[str, Any]] = []
        for gap in top_gaps:
            entry: Dict[str, Any] = {
                "gap_start": gap.gap_start,
                "gap_end": gap.gap_end,
                "gap_dur": gap.gap_dur,
                "device_id": gap.device_id,
                "stream_id": gap.stream_id,
                "prev_kernel": gap.prev_kernel_name,
                "next_kernel": gap.next_kernel_name,
                "attribution": gap.attribution,
                "attribution_confidence": gap.attribution_confidence,
                "host_events": gap.host_events,
            }
            critical_path.append(entry)

        return critical_path

    # ------------------------------------------------------------------ #
    #  Utility helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _event_to_dict(event: TraceEvent) -> Dict[str, Any]:
        """Convert a TraceEvent to a lightweight dict for storage.

        Parameters
        ----------
        event : TraceEvent
            The event to convert.

        Returns
        -------
        dict
            Dict with ts, dur, name, cat, pid, tid, cpu.
        """
        return {
            "ts": event.ts,
            "dur": event.dur,
            "name": event.name,
            "cat": event.cat,
            "pid": event.pid,
            "tid": event.tid,
            "cpu": event.cpu,
        }

    @staticmethod
    def _build_gap_host_pair(
        gap: GapRecord,
        primary_event: TraceEvent,
        attribution: str,
    ) -> Dict[str, Any]:
        """Build a gap-host-op pair dict.

        Parameters
        ----------
        gap : GapRecord
            The device idle gap.
        primary_event : TraceEvent or None
            The dominant host event for this gap.
        attribution : str
            The attribution category.

        Returns
        -------
        dict
            Gap-host-op pair with keys: gap_dur, gap_start, host_op,
            host_cat, ratio, attribution.
        """
        if primary_event is not None:
            ratio = primary_event.dur / gap.gap_dur if gap.gap_dur > 0 else 0.0
            return {
                "gap_dur": gap.gap_dur,
                "gap_start": gap.gap_start,
                "host_op": primary_event.name,
                "host_cat": primary_event.cat,
                "ratio": round(min(ratio, 1.0), 4),
                "attribution": attribution,
            }
        else:
            return {
                "gap_dur": gap.gap_dur,
                "gap_start": gap.gap_start,
                "host_op": "",
                "host_cat": "",
                "ratio": 0.0,
                "attribution": attribution,
            }
