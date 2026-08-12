"""
Timeline Feature Extractor
===========================
Auxiliary B-class timeline feature extractor that complements
:class:`HostMetricsExtractor`.

This module provides three extraction functions:

1. :meth:`extract_hot_functions` - Aggregates CPU_FUNCTION events by name
   to identify the most time-consuming host functions (hot spots).

2. :meth:`extract_step_timeline` - Extracts per-step (per-iteration)
   timing from step trace or iteration marker events, providing a
   timeline of training step durations for trend analysis.

3. :meth:`extract_gap_distribution` - Buckets device idle gaps by duration
   into predefined ranges (<1ms, 1-5ms, 5-10ms, 10-50ms, 50-100ms, >100ms)
   to characterize the distribution of idle periods.

All methods use streaming traversal and Python standard library only
(collections, statistics).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List

from ir.schema import EventCategory, GapRecord

if TYPE_CHECKING:
    from ir.reader import IRReader

logger = logging.getLogger("host_trace_diagnosis.features.timeline_features")


# Gap distribution bucket definitions: (label, min_us, max_us)
# max_us=None means no upper bound
GAP_BUCKETS = [
    ("<1ms", 0, 1000),
    ("1-5ms", 1000, 5000),
    ("5-10ms", 5000, 10000),
    ("10-50ms", 10000, 50000),
    ("50-100ms", 50000, 100000),
    (">100ms", 100000, None),
]


class TimelineFeatureExtractor:
    """Extract auxiliary timeline features from trace events.

    This extractor complements :class:`HostMetricsExtractor` by providing
    function-level profiling, step timeline, and gap distribution analysis.

    Parameters
    ----------
    config : dict
        Configuration dict (currently unused, reserved for future
        parameters such as custom bucket definitions).
    """

    def __init__(self, config: dict) -> None:
        self.config: dict = config or {}

    # ------------------------------------------------------------------ #
    #  Hot functions                                                      #
    # ------------------------------------------------------------------ #

    def extract_hot_functions(
        self, reader: "IRReader", top_n: int = 20
    ) -> List[Dict[str, Any]]:
        """Extract the top-N hottest host functions by total duration.

        Iterates over CPU_FUNCTION category events, aggregates by event
        name, and computes:
        - ``total_dur``: Total duration across all calls (us).
        - ``count``: Number of calls.
        - ``avg_dur``: Average duration per call (us).
        - ``max_dur``: Maximum single-call duration (us).

        Parameters
        ----------
        reader : IRReader
            The IR reader.
        top_n : int
            Number of top functions to return. Default 20.

        Returns
        -------
        list of dict
            Top-N functions sorted by ``total_dur`` descending. Each dict
            has keys: name, total_dur, count, avg_dur, max_dur.
        """
        # Aggregate: name -> [total_dur, count, max_dur]
        func_stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])

        cpu_func_cat = EventCategory.CPU_FUNCTION.value
        for event in reader.query_by_category(cpu_func_cat):
            stats = func_stats[event.name]
            stats[0] += event.dur  # total_dur
            stats[1] += 1           # count
            if event.dur > stats[2]:
                stats[2] = event.dur  # max_dur

        # Build result list
        result: List[Dict[str, Any]] = []
        for name, (total_dur, count, max_dur) in func_stats.items():
            avg_dur = total_dur / count if count > 0 else 0.0
            result.append({
                "name": name,
                "total_dur": total_dur,
                "count": count,
                "avg_dur": round(avg_dur, 2),
                "max_dur": max_dur,
            })

        # Sort by total_dur descending and take top N
        result.sort(key=lambda x: x["total_dur"], reverse=True)
        top_functions = result[:top_n]

        logger.debug(
            "Extracted %d hot functions (out of %d unique)",
            len(top_functions),
            len(func_stats),
        )

        return top_functions

    # ------------------------------------------------------------------ #
    #  Step timeline                                                      #
    # ------------------------------------------------------------------ #

    def extract_step_timeline(self, reader: "IRReader") -> List[Dict[str, Any]]:
        """Extract per-step (per-iteration) timing from step marker events.

        Searches for events that mark training step boundaries. These
        events are identified by:
        - Event name containing "step", "iteration", "iter", "epoch"
        - Or events in a category commonly used for step markers

        Each step entry contains:
        - ``step_ts``: Step start timestamp (us).
        - ``step_dur``: Step duration (us).
        - ``name``: Event name.

        Parameters
        ----------
        reader : IRReader
            The IR reader.

        Returns
        -------
        list of dict
            Step timeline sorted by ``step_ts`` ascending.
        """
        step_events: List[Dict[str, Any]] = []

        # Keywords that identify step/iteration marker events
        step_keywords = ("step", "iteration", "iter", "epoch")

        for event in reader.iter_events():
            name_lower = event.name.lower()

            # Check if this is a step marker event
            if not any(kw in name_lower for kw in step_keywords):
                continue

            # Skip metadata phase events (they are process/thread naming)
            if event.cat == EventCategory.METADATA.value:
                continue

            step_events.append({
                "step_ts": event.ts,
                "step_dur": event.dur,
                "name": event.name,
                "cat": event.cat,
            })

        # Sort by timestamp
        step_events.sort(key=lambda x: x["step_ts"])

        logger.debug("Extracted %d step timeline entries", len(step_events))

        return step_events

    # ------------------------------------------------------------------ #
    #  Gap distribution                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_gap_distribution(gaps: List[GapRecord]) -> Dict[str, Any]:
        """Compute the distribution of gap durations across predefined buckets.

        Buckets (by duration):
        - ``<1ms``: [0, 1000) us
        - ``1-5ms``: [1000, 5000) us
        - ``5-10ms``: [5000, 10000) us
        - ``10-50ms``: [10000, 50000) us
        - ``50-100ms``: [50000, 100000) us
        - ``>100ms``: [100000, +inf) us

        Parameters
        ----------
        gaps : list of GapRecord
            Device idle gaps (typically from :class:`GapScanner`).

        Returns
        -------
        dict
            Distribution with keys:
            - ``buckets``: List of bucket dicts, each with label, count,
              min_us, max_us, total_gap_us.
            - ``total_count``: Total number of gaps.
            - ``total_gap_us``: Sum of all gap durations.
        """
        # Initialize buckets
        bucket_data: List[Dict[str, Any]] = []
        for label, min_us, max_us in GAP_BUCKETS:
            bucket_data.append({
                "label": label,
                "min_us": min_us,
                "max_us": max_us if max_us is not None else -1,
                "count": 0,
                "total_gap_us": 0,
            })

        total_count = 0
        total_gap_us = 0

        for gap in gaps:
            total_count += 1
            total_gap_us += gap.gap_dur

            # Find the appropriate bucket
            for bucket in bucket_data:
                min_us = bucket["min_us"]
                max_us = bucket["max_us"]

                if max_us == -1:  # No upper bound (last bucket)
                    if gap.gap_dur >= min_us:
                        bucket["count"] += 1
                        bucket["total_gap_us"] += gap.gap_dur
                        break
                elif min_us <= gap.gap_dur < max_us:
                    bucket["count"] += 1
                    bucket["total_gap_us"] += gap.gap_dur
                    break

        return {
            "buckets": bucket_data,
            "total_count": total_count,
            "total_gap_us": total_gap_us,
        }
