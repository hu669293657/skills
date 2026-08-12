"""
IRReader - Intermediate Representation Reader
=============================================

Reads trace events from the IR storage format written by ``IRWriter``.

Primary format: **Apache Parquet** (via pyarrow)
  - Uses ``pyarrow.parquet.ParquetFile`` for row group-level streaming.
  - Supports predicate pushdown at the row group level for efficient
    time-range and category queries.
  - Column pruning: only reads requested columns.

Fallback format: **JSONL** (JSON Lines)
  - Used when pyarrow is not installed or the IR file is in JSONL format.
  - Reads line by line for streaming.

Query methods:
  - ``read_metadata()``: Load the sidecar metadata JSON.
  - ``iter_events(filter)``: Stream all events with optional filtering.
  - ``query_range(ts_start, ts_end)``: Events within a time range.
  - ``query_by_category(cat)``: Events of a specific category.
  - ``query_device_kernels(device_id)``: Device kernel events.

Predicate pushdown:
  For Parquet, row group statistics (min/max per column) are used to
  skip row groups that cannot contain matching events.  This provides
  significant speedups for selective queries on large IR files.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ir.schema import TraceEvent, TraceMetadata, TraceSource, EventCategory

logger = logging.getLogger("host_trace_diagnosis.ir.reader")


# Optional dependency: pyarrow for Parquet format
try:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore
    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False
    logger.debug("pyarrow not installed; IR reader will use JSONL format")


class IRReader:
    """
    Reads trace events from Parquet (or JSONL fallback) IR files.

    Usage::

        reader = IRReader("output.parquet", config)
        metadata = reader.read_metadata()
        for event in reader.iter_events():
            ...
        for event in reader.query_range(1000, 5000):
            ...
    """

    def __init__(
        self,
        ir_path: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Initialize the IR reader.

        Args:
            ir_path: Path to the IR file (Parquet or JSONL).
            config: IR configuration dict.

        Raises:
            FileNotFoundError: If the IR file does not exist.
        """
        self.ir_path: str = ir_path
        self.config: Dict[str, Any] = config or {}

        if not os.path.exists(ir_path):
            raise FileNotFoundError(f"IR file not found: {ir_path}")

        # Determine format
        ext = os.path.splitext(ir_path)[1].lower()
        self.is_parquet = ext in (".parquet", ".pq") and HAS_PYARROW
        self.is_jsonl = ext == ".jsonl" or not self.is_parquet

        if self.is_parquet:
            self._parquet_file = pq.ParquetFile(ir_path)
            self._row_groups: int = self._parquet_file.num_row_groups
            self._schema = self._parquet_file.schema_arrow
        else:
            self._parquet_file = None
            self._row_groups = 0

        # Lazy-loaded metadata
        self._metadata: Optional[TraceMetadata] = None

    # ------------------------------------------------------------------ #
    #  Metadata                                                           #
    # ------------------------------------------------------------------ #

    def read_metadata(self) -> TraceMetadata:
        """
        Read the sidecar metadata JSON file.

        The metadata file is named ``<ir_path_base>.meta.json`` and is
        written by ``IRWriter.finalize()``.

        Returns:
            TraceMetadata: Deserialized metadata.

        Raises:
            FileNotFoundError: If the metadata file does not exist.
        """
        if self._metadata is not None:
            return self._metadata

        meta_path = os.path.splitext(self.ir_path)[0] + ".meta.json"

        if not os.path.exists(meta_path):
            logger.warning(f"Metadata file not found: {meta_path}")
            # Return default metadata with inferred values
            self._metadata = self._infer_metadata()
            return self._metadata

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta_dict = json.load(f)

            # Reconstruct TraceMetadata
            source_str = meta_dict.get("source", "unknown")
            try:
                source = TraceSource(source_str)
            except ValueError:
                source = TraceSource.UNKNOWN

            self._metadata = TraceMetadata(
                source=source,
                file_path=meta_dict.get("file_path", self.ir_path),
                file_size_mb=meta_dict.get("file_size_mb", 0.0),
                total_events=meta_dict.get("total_events", 0),
                ts_start=meta_dict.get("ts_start", 0),
                ts_end=meta_dict.get("ts_end", 0),
                duration_us=meta_dict.get("duration_us", 0),
                devices=meta_dict.get("devices", []),
                processes={int(k): v for k, v in meta_dict.get("processes", {}).items()},
                threads={int(k): v for k, v in meta_dict.get("threads", {}).items()},
                cpu_cores=meta_dict.get("cpu_cores", 0),
                parallel_strategy=meta_dict.get("parallel_strategy", ""),
                model_name=meta_dict.get("model_name", ""),
            )
            return self._metadata

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read metadata: {e}")
            self._metadata = self._infer_metadata()
            return self._metadata

    def _infer_metadata(self) -> TraceMetadata:
        """Infer basic metadata from the IR file when sidecar is missing."""
        meta = TraceMetadata()
        meta.file_path = self.ir_path
        try:
            meta.file_size_mb = os.path.getsize(self.ir_path) / (1024 * 1024)
        except OSError:
            pass

        if self.is_parquet and self._parquet_file:
            meta.total_events = self._parquet_file.metadata.num_rows
            # Try to get ts range from row group statistics
            for i in range(self._parquet_file.num_row_groups):
                rg = self._parquet_file.metadata.row_group(i)
                ts_col = rg.column(0)  # ts is the first column
                if ts_col.statistics:
                    if ts_col.statistics.min is not None:
                        min_val = int(ts_col.statistics.min)
                        if meta.ts_start == 0 or min_val < meta.ts_start:
                            meta.ts_start = min_val
                    if ts_col.statistics.max is not None:
                        max_val = int(ts_col.statistics.max)
                        if max_val > meta.ts_end:
                            meta.ts_end = max_val
            meta.duration_us = meta.ts_end - meta.ts_start

        return meta

    # ------------------------------------------------------------------ #
    #  Event iteration (streaming)                                        #
    # ------------------------------------------------------------------ #

    def iter_events(
        self,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Iterator[TraceEvent]:
        """
        Stream all events from the IR file, with optional filtering.

        For Parquet, uses row group-level iteration.  For JSONL, reads
        line by line.

        Args:
            filter: Optional filter dict.  Supported keys:
                - ``ts_start`` (int): Only events with ts >= this value.
                - ``ts_end`` (int): Only events with ts <= this value.
                - ``cat`` (str): Only events with this category.
                - ``device_id`` (int): Only events for this device.
                - ``name`` (str): Only events with this name.

        Yields:
            TraceEvent: Matching events.
        """
        if self.is_parquet:
            yield from self._iter_parquet(filter)
        else:
            yield from self._iter_jsonl(filter)

    def _iter_parquet(self, filter: Optional[Dict[str, Any]]) -> Iterator[TraceEvent]:
        """
        Stream events from Parquet, using row group statistics for
        predicate pushdown.

        Skips row groups that cannot contain events matching the filter.
        """
        filter = filter or {}

        # Extract filter values
        ts_start = filter.get("ts_start")
        ts_end = filter.get("ts_end")
        cat = filter.get("cat")
        device_id = filter.get("device_id")
        name = filter.get("name")

        # Determine which columns to read (column pruning)
        all_columns = ["ts", "dur", "pid", "tid", "cpu", "name", "cat", "ph",
                       "device_id", "stream_id", "args"]

        for rg_idx in range(self._row_groups):
            row_group = self._parquet_file.metadata.row_group(rg_idx)

            # Predicate pushdown: check row group statistics
            if ts_start is not None or ts_end is not None:
                ts_col_stats = row_group.column(0).statistics  # ts is column 0
                if ts_col_stats:
                    rg_min = int(ts_col_stats.min) if ts_col_stats.min is not None else None
                    rg_max = int(ts_col_stats.max) if ts_col_stats.max is not None else None

                    if ts_start is not None and rg_max is not None and rg_max < ts_start:
                        continue  # Skip this row group
                    if ts_end is not None and rg_min is not None and rg_min > ts_end:
                        continue  # Skip this row group

            # Read the row group
            table = self._parquet_file.read_row_group(rg_idx, columns=all_columns)

            # Convert to list of dicts and filter
            rows = table.to_pylist()
            for row in rows:
                # Apply filters
                if ts_start is not None and row["ts"] < ts_start:
                    continue
                if ts_end is not None and row["ts"] > ts_end:
                    continue
                if cat is not None and row["cat"] != cat:
                    continue
                if device_id is not None and row["device_id"] != device_id:
                    continue
                if name is not None and row["name"] != name:
                    continue

                event = self._row_to_event(row)
                if event is not None:
                    yield event

    def _iter_jsonl(self, filter: Optional[Dict[str, Any]]) -> Iterator[TraceEvent]:
        """
        Stream events from JSONL, reading line by line.
        """
        filter = filter or {}
        ts_start = filter.get("ts_start")
        ts_end = filter.get("ts_end")
        cat = filter.get("cat")
        device_id = filter.get("device_id")
        name = filter.get("name")

        try:
            with open(self.ir_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Apply filters
                    if ts_start is not None and row.get("ts", 0) < ts_start:
                        continue
                    if ts_end is not None and row.get("ts", 0) > ts_end:
                        continue
                    if cat is not None and row.get("cat") != cat:
                        continue
                    if device_id is not None and row.get("device_id") != device_id:
                        continue
                    if name is not None and row.get("name") != name:
                        continue

                    event = self._row_to_event(row)
                    if event is not None:
                        yield event
        except OSError as e:
            raise IOError(f"Error reading IR file {self.ir_path}: {e}") from e

    # ------------------------------------------------------------------ #
    #  Convenience query methods                                          #
    # ------------------------------------------------------------------ #

    def query_range(
        self,
        ts_start: int,
        ts_end: int,
    ) -> Iterator[TraceEvent]:
        """
        Query events within a time range [ts_start, ts_end].

        Uses Parquet row group statistics for predicate pushdown when
        available, skipping row groups that fall outside the range.

        Args:
            ts_start: Start timestamp (inclusive), in microseconds.
            ts_end: End timestamp (inclusive), in microseconds.

        Yields:
            TraceEvent: Events with ``ts_start <= ts <= ts_end``.
        """
        yield from self.iter_events(filter={"ts_start": ts_start, "ts_end": ts_end})

    def query_by_category(self, cat: str) -> Iterator[TraceEvent]:
        """
        Query events of a specific category.

        Args:
            cat: Event category string (see ``EventCategory``).

        Yields:
            TraceEvent: Events with the matching category.
        """
        yield from self.iter_events(filter={"cat": cat})

    def query_device_kernels(self, device_id: int) -> Iterator[TraceEvent]:
        """
        Query kernel events for a specific device.

        Filters by ``device_id`` and typically returns NPU_KERNEL,
        NPU_MEMCPY, and STREAM_SYNC events.

        Args:
            device_id: The device ID to query.

        Yields:
            TraceEvent: Device events for the specified device.
        """
        yield from self.iter_events(filter={"device_id": device_id})

    def get_event_count(self) -> int:
        """
        Get the total number of events in the IR file.

        For Parquet, reads from metadata.  For JSONL, counts lines.

        Returns:
            int: Total event count.
        """
        if self.is_parquet and self._parquet_file:
            return self._parquet_file.metadata.num_rows

        # For JSONL, count lines
        count = 0
        try:
            with open(self.ir_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except OSError:
            pass
        return count

    def get_devices(self) -> List[int]:
        """
        Get the list of device IDs present in the IR.

        Returns:
            List[int]: Sorted list of device IDs.
        """
        meta = self.read_metadata()
        if meta.devices:
            return meta.devices

        # Infer from data if metadata is missing
        devices = set()
        for event in self.iter_events():
            if event.device_id >= 0:
                devices.add(event.device_id)
            # Early exit if we've found a reasonable number of devices
            if len(devices) >= 16:
                break
        return sorted(devices)

    # ------------------------------------------------------------------ #
    #  Conversion                                                         #
    # ------------------------------------------------------------------ #

    def _row_to_event(self, row: Dict[str, Any]) -> Optional[TraceEvent]:
        """
        Convert a storage row dict to a TraceEvent.

        Deserializes the ``args`` field from JSON string.

        Args:
            row: Row dict with keys matching COLUMNS.

        Returns:
            TraceEvent or None if conversion fails.
        """
        try:
            args_str = row.get("args", "{}")
            if isinstance(args_str, str):
                args = json.loads(args_str) if args_str else {}
            elif isinstance(args_str, dict):
                args = args_str
            else:
                args = {}

            return TraceEvent(
                ts=int(row.get("ts", 0)),
                dur=int(row.get("dur", 0)),
                pid=int(row.get("pid", -1)),
                tid=int(row.get("tid", -1)),
                cpu=int(row.get("cpu", -1)),
                name=str(row.get("name", "")),
                cat=str(row.get("cat", EventCategory.UNKNOWN.value)),
                ph=str(row.get("ph", "")),
                device_id=int(row.get("device_id", -1)),
                stream_id=int(row.get("stream_id", -1)),
                args=args,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            logger.debug(f"Failed to convert row to event: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Context manager support                                            #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "IRReader":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # ParquetFile doesn't need explicit closing, but be safe
        pass
