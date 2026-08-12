"""
MsprofParser - Ascend msprof Profiling Output Parser
=====================================================

Parses the output of Ascend (Huawei NPU) ``msprof`` profiling tool.

msprof output is typically a **directory** containing multiple files:

  - ``trace_view.json``
      Chrome Trace format JSON containing the main timeline.
      Delegated to ``ChromeTraceJSONParser``.

  - ``operator_detail_*.csv``
      CSV files with per-operator details (name, type, duration, device, etc.)
      Parsed with ``csv.DictReader``.

  - ``step_trace_*.json``
      JSON files with iteration-level trace data (step boundaries, forward/
      backward/optimizer phases).
      Parsed with standard ``json.load``.

  - ``ascend_pytorch_profiler_*.db``
      SQLite database containing kernel-level execution data.
      Queried with ``sqlite3`` for kernel table entries.

This parser also handles the case where the input is a single CSV or
JSON file (not a directory), by detecting the file type and routing
to the appropriate sub-parser.

Event merging:
  Events from all sources are merged and yielded in timestamp order.
  Since we stream, we use a merge-sort approach: each source is an
  iterator, and we merge them using a priority queue (heapq).
"""
from __future__ import annotations

import csv
import heapq
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ir.schema import (
    TraceEvent,
    TraceMetadata,
    TraceSource,
    EventCategory,
    EventPhase,
)

from .base import BaseParser

logger = logging.getLogger("host_trace_diagnosis.parsers.msprof")


class MsprofParser(BaseParser):
    """
    Parser for Ascend msprof profiling output.

    Accepts either a directory (msprof output folder) or a single file
    (CSV or JSON).  Automatically detects the input type and routes to
    the appropriate sub-parser.

    For directory inputs, merges events from all files in timestamp order
    using a streaming merge-sort.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._input_path: str = ""
        self._is_directory: bool = False
        # Track all file sources for metadata
        self._source_files: List[str] = []

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def parse(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse msprof output (directory or single file).

        Args:
            file_path: Path to msprof directory or individual file.

        Yields:
            TraceEvent: Merged events from all sources, sorted by timestamp.

        Raises:
            FileNotFoundError: If the path does not exist.
            ValueError: If no recognizable msprof files are found.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"msprof path not found: {file_path}")

        self.reset()
        self._input_path = str(path)
        self._is_directory = path.is_dir()

        if self._is_directory:
            yield from self._parse_directory(path)
        else:
            yield from self._parse_single_file(path)

    def get_metadata(self) -> TraceMetadata:
        """Return metadata accumulated during parsing."""
        return self._finalize_metadata(TraceSource.MSPROF, self._input_path)

    # ------------------------------------------------------------------ #
    #  Directory parsing (multi-source merge)                             #
    # ------------------------------------------------------------------ #

    def _parse_directory(self, dir_path: Path) -> Iterator[TraceEvent]:
        """
        Parse all msprof files in a directory and merge events by timestamp.

        Uses a streaming merge-sort with ``heapq`` to combine events from
        multiple sources while maintaining O(1) memory per source.

        Args:
            dir_path: Path to the msprof output directory.

        Yields:
            TraceEvent: Merged events sorted by timestamp.
        """
        # Discover source files
        sources: List[Tuple[int, Iterator[TraceEvent]]] = []
        file_index = 0

        try:
            entries = sorted(dir_path.iterdir())
        except OSError as e:
            raise IOError(f"Cannot list msprof directory {dir_path}: {e}") from e

        for entry in entries:
            if not entry.is_file():
                continue

            name = entry.name.lower()
            self._source_files.append(str(entry))

            if name.endswith(".json") and "trace_view" in name:
                # Chrome trace JSON – delegate to ChromeTraceJSONParser
                iterator = self._iter_trace_view_json(str(entry))
                sources.append((file_index, iterator))
                file_index += 1

            elif name.endswith(".json") and "step_trace" in name:
                # Step trace JSON
                iterator = self._iter_step_trace_json(str(entry))
                sources.append((file_index, iterator))
                file_index += 1

            elif name.endswith(".csv") and "operator_detail" in name:
                # Operator detail CSV
                iterator = self._iter_operator_csv(str(entry))
                sources.append((file_index, iterator))
                file_index += 1

            elif name.endswith(".db"):
                # SQLite database
                iterator = self._iter_sqlite_db(str(entry))
                sources.append((file_index, iterator))
                file_index += 1

            elif name.endswith(".csv"):
                # Generic CSV (might also be operator details)
                iterator = self._iter_operator_csv(str(entry))
                sources.append((file_index, iterator))
                file_index += 1

        if not sources:
            logger.warning(f"No recognizable msprof files found in {dir_path}")

        # Streaming merge-sort using heapq
        yield from self._merge_sorted(sources)

    def _merge_sorted(
        self,
        sources: List[Tuple[int, Iterator[TraceEvent]]],
    ) -> Iterator[TraceEvent]:
        """
        Merge multiple sorted event iterators into a single sorted stream.

        Uses a min-heap to always yield the event with the smallest timestamp
        across all sources.  Each source must yield events in non-decreasing
        timestamp order.

        Args:
            sources: List of (source_id, iterator) pairs.

        Yields:
            TraceEvent: Events in timestamp order.
        """
        heap: List[Tuple[int, int, TraceEvent]] = []

        # Initialize heap with first event from each source
        for source_id, iterator in sources:
            try:
                first_event = next(iterator)
                heapq.heappush(heap, (first_event.ts, source_id, first_event))
            except StopIteration:
                continue

        # Merge
        while heap:
            ts, source_id, event = heapq.heappop(heap)
            yield event

            # Get next event from the same source
            # We need to find the iterator for this source_id
            # Since we can't store iterators in the heap, we re-fetch
            try:
                # Find the iterator for this source_id
                # We stored sources as a list; find by index
                iterator = sources[source_id][1]
                next_event = next(iterator)
                heapq.heappush(heap, (next_event.ts, source_id, next_event))
            except StopIteration:
                continue

    # ------------------------------------------------------------------ #
    #  Single-file parsing                                                #
    # ------------------------------------------------------------------ #

    def _parse_single_file(self, file_path: Path) -> Iterator[TraceEvent]:
        """
        Parse a single msprof file (CSV or JSON).

        Args:
            file_path: Path to the file.

        Yields:
            TraceEvent: Parsed events.
        """
        name = file_path.name.lower()
        self._source_files.append(str(file_path))

        if name.endswith(".json"):
            if "trace_view" in name:
                yield from self._iter_trace_view_json(str(file_path))
            elif "step_trace" in name:
                yield from self._iter_step_trace_json(str(file_path))
            else:
                # Treat as Chrome trace JSON
                yield from self._iter_trace_view_json(str(file_path))

        elif name.endswith(".csv"):
            yield from self._iter_operator_csv(str(file_path))

        elif name.endswith(".db"):
            yield from self._iter_sqlite_db(str(file_path))

        else:
            logger.warning(f"Unrecognized file type: {file_path}")

    # ------------------------------------------------------------------ #
    #  Sub-parsers for individual file types                              #
    # ------------------------------------------------------------------ #

    def _iter_trace_view_json(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse trace_view.json using ChromeTraceJSONParser.

        Delegates to the Chrome JSON parser for the main timeline data.
        """
        from .chrome_json_parser import ChromeTraceJSONParser

        chrome_parser = ChromeTraceJSONParser(self.config)
        for event in chrome_parser.parse(file_path):
            self._update_metadata_from_event(event)
            yield event

    def _iter_step_trace_json(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse step_trace_*.json for iteration-level trace data.

        Step trace JSON contains information about training iterations,
        including forward, backward, and optimizer phase boundaries.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in {file_path}: {e}")
            return
        except OSError as e:
            logger.warning(f"Cannot read {file_path}: {e}")
            return

        # Step trace can be a list of steps or a dict with a "steps" key
        if isinstance(data, dict):
            steps = data.get("steps", data.get("traceEvents", []))
        elif isinstance(data, list):
            steps = data
        else:
            return

        for step in steps:
            if not isinstance(step, dict):
                continue
            yield from self._convert_step_to_events(step)

    def _convert_step_to_events(self, step: Dict[str, Any]) -> Iterator[TraceEvent]:
        """
        Convert a step trace entry to TraceEvent(s).

        Step trace entries typically contain:
          - step_id / iteration
          - start_time / end_time (or ts + duration)
          - forward_start, backward_start, optimizer_start, etc.
        """
        step_id = step.get("step_id", step.get("iteration", -1))
        ts_us = int(step.get("start_time", step.get("ts", 0)))
        end_us = int(step.get("end_time", ts_us))
        dur = max(0, end_us - ts_us)

        # Emit a complete event for the entire step
        event = TraceEvent(
            ts=ts_us,
            dur=dur,
            pid=0,
            tid=0,
            cpu=-1,
            name=f"Step#{step_id}",
            cat=EventCategory.RUNTIME.value,
            ph=EventPhase.COMPLETE.value,
            args={
                "step_id": step_id,
                "forward_start": step.get("forward_start"),
                "backward_start": step.get("backward_start"),
                "optimizer_start": step.get("optimizer_start"),
                "fp_start": step.get("fp_start"),
                "bp_end": step.get("bp_end"),
            },
        )
        self._update_metadata_from_event(event)
        yield event

        # Also emit phase boundary events if available
        for phase_key, phase_name in [
            ("forward_start", "Forward"),
            ("backward_start", "Backward"),
            ("optimizer_start", "Optimizer"),
        ]:
            phase_ts = step.get(phase_key)
            if phase_ts is not None:
                phase_event = TraceEvent(
                    ts=int(phase_ts),
                    dur=0,
                    pid=0,
                    tid=0,
                    name=phase_name,
                    cat=EventCategory.RUNTIME.value,
                    ph=EventPhase.INSTANT.value,
                    args={"step_id": step_id},
                )
                self._update_metadata_from_event(phase_event)
                yield phase_event

    def _iter_operator_csv(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse operator_detail_*.csv files.

        CSV columns typically include:
          - Op Name / Name
          - Op Type / Type
          - Start Time (us) / ts
          - Duration (us) / dur
          - Device ID / device
          - Stream ID / stream
          - Input Shapes / shapes
          - etc.
        """
        try:
            with open(file_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    event = self._convert_csv_row_to_event(row)
                    if event is not None:
                        self._update_metadata_from_event(event)
                        yield event
        except OSError as e:
            logger.warning(f"Cannot read CSV {file_path}: {e}")
        except csv.Error as e:
            logger.warning(f"CSV parsing error in {file_path}: {e}")

    def _convert_csv_row_to_event(self, row: Dict[str, str]) -> Optional[TraceEvent]:
        """
        Convert a CSV row to a TraceEvent.

        Handles various column naming conventions found in msprof CSVs.
        """
        # Flexible column name matching
        def get_val(*keys: str) -> Optional[str]:
            for k in keys:
                for col_name, col_val in row.items():
                    if col_name and k.lower() in col_name.lower():
                        return col_val
            return None

        try:
            name = get_val("Op Name", "Name", "op_name") or ""
            op_type = get_val("Op Type", "Type", "op_type") or ""

            ts_str = get_val("Start Time", "ts", "start") or "0"
            ts = int(float(ts_str))

            dur_str = get_val("Duration", "dur", "duration") or "0"
            dur = int(float(dur_str))

            device_str = get_val("Device ID", "device", "Device") or "-1"
            device_id = int(float(device_str))

            stream_str = get_val("Stream ID", "stream", "Stream") or "-1"
            stream_id = int(float(stream_str))

            pid_str = get_val("PID", "pid", "Process") or "-1"
            pid = int(float(pid_str))

            tid_str = get_val("TID", "tid", "Thread") or "-1"
            tid = int(float(tid_str))

        except (ValueError, TypeError) as e:
            logger.debug(f"Failed to convert CSV row: {e}")
            return None

        # Determine category
        cat = EventCategory.NPU_KERNEL.value
        if "memcpy" in op_type.lower() or "copy" in name.lower():
            cat = EventCategory.NPU_MEMCPY.value
        elif "sync" in name.lower() or "synchronize" in name.lower():
            cat = EventCategory.STREAM_SYNC.value

        event = TraceEvent(
            ts=ts,
            dur=dur,
            pid=pid,
            tid=tid,
            cpu=-1,
            name=name,
            cat=cat,
            ph=EventPhase.COMPLETE.value,
            device_id=device_id,
            stream_id=stream_id,
            args={
                "op_type": op_type,
                "input_shapes": get_val("Input", "Shape", "input_shapes") or "",
                "output_shapes": get_val("Output", "output_shapes") or "",
                "source_file": "operator_detail_csv",
            },
        )
        return event

    def _iter_sqlite_db(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Query SQLite database for kernel execution data.

        The msprof SQLite DB typically has tables like:
          - ``kernel``: NPU kernel execution records
          - ``operator``: Operator-level records
        """
        try:
            conn = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Try to find kernel table
            tables = self._get_table_names(cursor)
            if not tables:
                logger.warning(f"No tables found in SQLite DB: {file_path}")
                conn.close()
                return

            # Query kernel table if it exists
            kernel_table = self._find_table(tables, ["kernel", "Kernel", "op", "Operator"])
            if kernel_table:
                yield from self._iter_sqlite_table(cursor, kernel_table, file_path)

            conn.close()

        except sqlite3.Error as e:
            logger.warning(f"SQLite error in {file_path}: {e}")

    def _get_table_names(self, cursor: sqlite3.Cursor) -> List[str]:
        """Get all table names from a SQLite database."""
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def _find_table(self, tables: List[str], candidates: List[str]) -> Optional[str]:
        """Find a table name matching any of the candidate names."""
        tables_lower = {t.lower(): t for t in tables}
        for candidate in candidates:
            if candidate.lower() in tables_lower:
                return tables_lower[candidate.lower()]
        # Also check partial matches
        for t in tables:
            for candidate in candidates:
                if candidate.lower() in t.lower():
                    return t
        return None

    def _iter_sqlite_table(
        self,
        cursor: sqlite3.Cursor,
        table_name: str,
        db_path: str,
    ) -> Iterator[TraceEvent]:
        """
        Iterate over rows in a SQLite table and yield TraceEvents.

        Attempts to map common column names to TraceEvent fields.
        """
        try:
            # Get column names
            cursor.execute(f"SELECT * FROM {table_name} LIMIT 1")
            col_names = [desc[0] for desc in cursor.description]

            # Build SELECT query
            cursor.execute(f"SELECT * FROM {table_name}")

            for row in cursor:
                row_dict = {col_names[i]: row[i] for i in range(len(col_names))}
                event = self._convert_db_row_to_event(row_dict, table_name, db_path)
                if event is not None:
                    self._update_metadata_from_event(event)
                    yield event

        except sqlite3.Error as e:
            logger.warning(f"Error querying table {table_name} in {db_path}: {e}")

    def _convert_db_row_to_event(
        self,
        row: Dict[str, Any],
        table_name: str,
        db_path: str,
    ) -> Optional[TraceEvent]:
        """
        Convert a SQLite row to a TraceEvent.

        Uses flexible column name matching similar to CSV parsing.
        """
        def get_val(*keys: str) -> Any:
            for k in keys:
                for col_name, col_val in row.items():
                    if col_name and k.lower() in col_name.lower():
                        return col_val
            return None

        try:
            name = str(get_val("Op Name", "Name", "op_name", "kernel_name") or "")
            op_type = str(get_val("Op Type", "Type", "op_type", "kernel_type") or "")

            ts_val = get_val("Start Time", "ts", "start", "start_time")
            ts = int(float(ts_val)) if ts_val is not None else 0

            dur_val = get_val("Duration", "dur", "duration", "elapsed")
            dur = int(float(dur_val)) if dur_val is not None else 0

            device_val = get_val("Device ID", "device", "Device")
            device_id = int(float(device_val)) if device_val is not None else -1

            stream_val = get_val("Stream ID", "stream", "Stream")
            stream_id = int(float(stream_val)) if stream_val is not None else -1

        except (ValueError, TypeError) as e:
            logger.debug(f"Failed to convert DB row: {e}")
            return None

        cat = EventCategory.NPU_KERNEL.value
        if "memcpy" in op_type.lower() or "copy" in name.lower():
            cat = EventCategory.NPU_MEMCPY.value
        elif "sync" in name.lower():
            cat = EventCategory.STREAM_SYNC.value

        event = TraceEvent(
            ts=ts,
            dur=dur,
            pid=-1,
            tid=-1,
            cpu=-1,
            name=name,
            cat=cat,
            ph=EventPhase.COMPLETE.value,
            device_id=device_id,
            stream_id=stream_id,
            args={
                "op_type": op_type,
                "source_table": table_name,
                "source_db": os.path.basename(db_path),
            },
        )
        return event
