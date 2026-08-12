"""
Gap Scanner
============
Device idle gap detection -- the core of the gap-driven root cause analysis.

A "gap" is a period of device (NPU/GPU) idle time between two consecutive
kernels on the same device and stream. These gaps represent opportunities
where the device could have been doing useful work but was waiting for the
host to prepare data, launch kernels, or synchronize.

The scanner:
  1. Collects all device kernel events (NPU_KERNEL, GPU_KERNEL, NPU_MEMCPY).
  2. Groups them by (device_id, stream_id) and sorts by timestamp.
  3. Computes the idle gap between each pair of adjacent kernels.
  4. Filters out negligible gaps (< ``min_gap_us``) that represent normal
     stream switching overhead.
  5. Applies Pareto filtering: retains the gaps that account for
     ``pareto_threshold`` (default 80%) of total idle time.
  6. Returns the list of significant gaps (>= ``significant_gap_us``) for
     further analysis by the correlation engine.

Statistics (total gap time, kernel time, utilization, gap counts) are
stored as instance attributes after :meth:`scan` completes, making them
available to downstream consumers.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List

from ir.schema import EventCategory, GapRecord

if TYPE_CHECKING:
    from ir.reader import IRReader

logger = logging.getLogger("host_trace_diagnosis.features.gap_scanner")


# Device-side event categories that represent kernel execution or memcpy
DEVICE_KERNEL_CATS = frozenset({
    EventCategory.NPU_KERNEL.value,
    EventCategory.GPU_KERNEL.value,
    EventCategory.NPU_MEMCPY.value,
})


class GapScanner:
    """Scan device trace events for idle gaps between consecutive kernels.

    Parameters
    ----------
    config : dict
        Configuration dict containing:
        - ``min_gap_us`` (int): Minimum gap duration to consider (us).
          Gaps below this are normal stream switching overhead. Default 100.
        - ``significant_gap_us`` (int): Threshold for a gap to be considered
          significant. Default 1000.
        - ``top_n`` (int): Number of top gaps to retain for detailed
          analysis. Default 20.
        - ``pareto_threshold`` (float): Cumulative coverage ratio for
          Pareto filtering. Default 0.8.
    """

    def __init__(self, config: dict) -> None:
        self.min_gap_us: int = int(config.get("min_gap_us", 100))
        self.significant_gap_us: int = int(config.get("significant_gap_us", 1000))
        self.top_n: int = int(config.get("top_n", 20))
        self.pareto_threshold: float = float(config.get("pareto_threshold", 0.8))

        # Statistics (populated after scan())
        self.total_gap_us: int = 0
        self.total_kernel_us: int = 0
        self.device_utilization: float = 0.0
        self.gap_count: int = 0
        self.significant_gap_count: int = 0
        self.top_gaps: List[GapRecord] = []

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def scan(self, reader: "IRReader") -> List[GapRecord]:
        """Scan the trace for device idle gaps.

        Parameters
        ----------
        reader : IRReader
            The IR reader providing streaming access to trace events.

        Returns
        -------
        list of GapRecord
            Significant gaps (>= ``significant_gap_us``), sorted by
            ``gap_dur`` descending. Instance attributes ``total_gap_us``,
            ``total_kernel_us``, ``device_utilization``, ``gap_count``,
            ``significant_gap_count``, and ``top_gaps`` are also populated.
        """
        # Step 1: Collect all device kernel events
        logger.debug("Collecting device kernel events...")
        device_events = self._collect_device_events(reader)

        # Step 2: Group by (device_id, stream_id) and sort by ts
        logger.debug("Grouping and sorting device events...")
        grouped = self._group_and_sort(device_events)

        # Step 3: Compute gaps between adjacent kernels in each group
        logger.debug("Computing gaps...")
        all_gaps = self._compute_gaps(grouped)

        # Step 4: Filter gaps < min_gap_us
        filtered_gaps = [g for g in all_gaps if g.gap_dur >= self.min_gap_us]

        # Step 5: Sort by gap_dur descending
        filtered_gaps.sort(key=lambda g: g.gap_dur, reverse=True)

        # Step 6: Pareto filtering
        pareto_gaps = self._pareto_filter(filtered_gaps)

        # Step 7: Select top N
        self.top_gaps = pareto_gaps[: self.top_n]

        # Step 8: Significant gaps (>= significant_gap_us)
        significant_gaps = [g for g in filtered_gaps if g.gap_dur >= self.significant_gap_us]

        # Step 9: Compute statistics
        self._compute_statistics(all_gaps, device_events, significant_gaps)

        logger.info(
            "Gap scan complete: total_gaps=%d, significant_gaps=%d, "
            "total_gap_us=%d, device_util=%.1f%%",
            self.gap_count,
            self.significant_gap_count,
            self.total_gap_us,
            self.device_utilization * 100,
        )

        return significant_gaps

    # ------------------------------------------------------------------ #
    #  Step 1: Collect device kernel events                               #
    # ------------------------------------------------------------------ #

    def _collect_device_events(self, reader: "IRReader") -> List[dict]:
        """Collect all device-side kernel/memcpy events.

        Iterates over all events and filters for:
        - ``device_id >= 0`` (device events, not host)
        - ``cat`` in DEVICE_KERNEL_CATS

        Events are returned as lightweight dicts to reduce memory overhead
        compared to full TraceEvent objects.

        Parameters
        ----------
        reader : IRReader
            The IR reader.

        Returns
        -------
        list of dict
            List of event dicts with keys: ts, dur, device_id, stream_id,
            name, cat.
        """
        events: List[dict] = []

        for event in reader.iter_events():
            if event.device_id < 0:
                continue
            if event.cat not in DEVICE_KERNEL_CATS:
                continue
            events.append({
                "ts": event.ts,
                "dur": event.dur,
                "device_id": event.device_id,
                "stream_id": event.stream_id,
                "name": event.name,
                "cat": event.cat,
            })

        logger.debug("Collected %d device kernel events", len(events))
        return events

    # ------------------------------------------------------------------ #
    #  Step 2: Group by (device_id, stream_id) and sort                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _group_and_sort(
        events: List[dict],
    ) -> Dict[tuple, List[dict]]:
        """Group events by (device_id, stream_id) and sort by timestamp.

        Parameters
        ----------
        events : list of dict
            Device kernel events.

        Returns
        -------
        dict
            Mapping from ``(device_id, stream_id)`` to a list of event
            dicts sorted by ``ts``.
        """
        grouped: Dict[tuple, List[dict]] = defaultdict(list)

        for evt in events:
            key = (evt["device_id"], evt["stream_id"])
            grouped[key].append(evt)

        # Sort each group by timestamp
        for key in grouped:
            grouped[key].sort(key=lambda e: e["ts"])

        return grouped

    # ------------------------------------------------------------------ #
    #  Step 3: Compute gaps between adjacent kernels                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_gaps(
        grouped: Dict[tuple, List[dict]],
    ) -> List[GapRecord]:
        """Compute idle gaps between consecutive kernels in each stream group.

        For each pair of adjacent kernels (current, next) in a group:
        - ``gap_start`` = current.ts + current.dur
        - ``gap_end`` = next.ts
        - ``gap_dur`` = gap_end - gap_start

        Only gaps with ``gap_dur > 0`` are retained (negative or zero gaps
        indicate overlapping or back-to-back kernels).

        Parameters
        ----------
        grouped : dict
            Mapping from ``(device_id, stream_id)`` to sorted event lists.

        Returns
        -------
        list of GapRecord
            All computed gaps (unfiltered).
        """
        gaps: List[GapRecord] = []

        for (device_id, stream_id), events in grouped.items():
            if len(events) < 2:
                continue

            for i in range(len(events) - 1):
                current = events[i]
                next_evt = events[i + 1]

                gap_start = current["ts"] + current["dur"]
                gap_end = next_evt["ts"]
                gap_dur = gap_end - gap_start

                if gap_dur <= 0:
                    # Overlapping or adjacent kernels; no gap
                    continue

                gap = GapRecord(
                    gap_start=gap_start,
                    gap_end=gap_end,
                    gap_dur=gap_dur,
                    device_id=device_id,
                    stream_id=stream_id,
                    prev_kernel_name=current["name"],
                    next_kernel_name=next_evt["name"],
                )
                gaps.append(gap)

        return gaps

    # ------------------------------------------------------------------ #
    #  Step 4: Pareto filtering                                           #
    # ------------------------------------------------------------------ #

    def _pareto_filter(self, sorted_gaps: List[GapRecord]) -> List[GapRecord]:
        """Apply Pareto (80/20) filtering to retain the most impactful gaps.

        Gaps are already sorted by ``gap_dur`` descending. This method
        accumulates gap durations until the cumulative total reaches
        ``pareto_threshold`` of the total gap time, and returns those gaps.

        Parameters
        ----------
        sorted_gaps : list of GapRecord
            Gaps sorted by duration descending.

        Returns
        -------
        list of GapRecord
            Pareto-filtered gaps (the gaps accounting for
            ``pareto_threshold`` of total gap time).
        """
        if not sorted_gaps:
            return []

        total_gap = sum(g.gap_dur for g in sorted_gaps)
        if total_gap <= 0:
            return []

        cumulative = 0
        threshold = total_gap * self.pareto_threshold

        for i, gap in enumerate(sorted_gaps):
            cumulative += gap.gap_dur
            if cumulative >= threshold:
                # Include this gap (it crosses the threshold)
                return sorted_gaps[: i + 1]

        return sorted_gaps

    # ------------------------------------------------------------------ #
    #  Statistics                                                         #
    # ------------------------------------------------------------------ #

    def _compute_statistics(
        self,
        all_gaps: List[GapRecord],
        device_events: List[dict],
        significant_gaps: List[GapRecord],
    ) -> None:
        """Compute and store summary statistics.

        Populates the following instance attributes:
        - ``total_gap_us``: Sum of all gap durations (including < min_gap_us).
        - ``total_kernel_us``: Sum of all device kernel durations.
        - ``device_utilization``: kernel_time / (kernel_time + gap_time).
        - ``gap_count``: Total number of gaps (including < min_gap_us).
        - ``significant_gap_count``: Number of significant gaps.

        Parameters
        ----------
        all_gaps : list of GapRecord
            All computed gaps (before filtering).
        device_events : list of dict
            All device kernel events.
        significant_gaps : list of GapRecord
            Significant gaps (>= significant_gap_us).
        """
        self.total_gap_us = sum(g.gap_dur for g in all_gaps)
        self.total_kernel_us = sum(e["dur"] for e in device_events)
        total_time = self.total_gap_us + self.total_kernel_us
        self.device_utilization = (
            self.total_kernel_us / total_time if total_time > 0 else 0.0
        )
        self.gap_count = len(all_gaps)
        self.significant_gap_count = len(significant_gaps)

    # ------------------------------------------------------------------ #
    #  Utility                                                            #
    # ------------------------------------------------------------------ #

    def get_statistics(self) -> Dict[str, Any]:
        """Return a dict of scan statistics for downstream consumption.

        Returns
        -------
        dict
            Statistics including total_gap_us, total_kernel_us,
            device_utilization, gap_count, significant_gap_count.
        """
        return {
            "total_gap_us": self.total_gap_us,
            "total_kernel_us": self.total_kernel_us,
            "device_utilization": self.device_utilization,
            "gap_count": self.gap_count,
            "significant_gap_count": self.significant_gap_count,
            "top_gap_count": len(self.top_gaps),
        }
