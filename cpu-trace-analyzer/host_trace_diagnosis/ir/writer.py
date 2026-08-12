"""
IRWriter - Intermediate Representation Writer
=============================================

Writes ``TraceEvent`` objects to a persistent columnar storage format
for efficient downstream querying by the feature extraction and
diagnosis layers.

Primary format: **Apache Parquet** (via pyarrow)
  - Columnar storage with optional compression (zstd, snappy, gzip).
  - Efficient for analytical queries (column pruning, predicate pushdown).
  - Row group-based for streaming reads.

Fallback format: **JSONL** (JSON Lines)
  - Used when pyarrow is not installed.
  - Each line is a JSON object representing one event.
  - Simpler but less efficient; suitable for development / testing.

Columns:
  ts (int64), dur (int64), pid (int64), tid (int64), cpu (int64),
  name (string), cat (string), ph (string),
  device_id (int64), stream_id (int64), args (string, JSON-encoded).

Batching:
  Events are buffered in memory and flushed in batches of
  ``batch_size`` (default 5000) to amortize I/O overhead.  The final
  flush occurs in ``finalize()``.

Metadata:
  After all events are written, ``finalize()`` appends a metadata
  record (stored as a sidecar JSON file or Parquet metadata).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ir.schema import TraceEvent, TraceMetadata

logger = logging.getLogger("host_trace_diagnosis.ir.writer")


# Optional dependency: pyarrow for Parquet format
try:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore
    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False
    logger.debug("pyarrow not installed; IR will use JSONL format")


class IRWriter:
    """
    Writes trace events to Parquet (or JSONL fallback) format.

    Usage::

        writer = IRWriter("output.parquet", config)
        for event in parser.parse("trace.json"):
            writer.write_event(event)
        writer.finalize(parser.get_metadata())
    """

    # Column schema definition
    COLUMNS: List[tuple] = [
        ("ts", "int64"),
        ("dur", "int64"),
        ("pid", "int64"),
        ("tid", "int64"),
        ("cpu", "int64"),
        ("name", "string"),
        ("cat", "string"),
        ("ph", "string"),
        ("device_id", "int64"),
        ("stream_id", "int64"),
        ("args", "string"),  # JSON-encoded
    ]

    def __init__(
        self,
        output_path: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Initialize the IR writer.

        Args:
            output_path: Path to the output file.  Extension determines
                format: ``.parquet`` → Parquet, ``.jsonl`` → JSONL.
                If pyarrow is not available, forces JSONL regardless.
            config: IR configuration dict.  Expected keys:
                - ``compression`` (str): Parquet compression codec
                  (``zstd``, ``snappy``, ``gzip``, ``none``).  Default ``zstd``.
                - ``row_group_size`` (int): Parquet row group size.
                  Default 100000.
                - ``batch_size`` (int): Number of events to buffer before
                  flush.  Default 5000.
                - ``format`` (str): Force format (``parquet`` or ``jsonl``).
        """
        self.output_path: str = output_path
        self.config: Dict[str, Any] = config or {}
        self.batch_size: int = int(self.config.get("batch_size", 5000))
        self.compression: str = self.config.get("compression", "zstd")
        self.row_group_size: int = int(self.config.get("row_group_size", 100000))

        # Determine format
        forced_format = self.config.get("format", "").lower()
        if forced_format == "jsonl":
            self.use_parquet = False
        elif forced_format == "parquet":
            self.use_parquet = True
        else:
            # Auto-detect from extension
            ext = Path(output_path).suffix.lower()
            self.use_parquet = ext in (".parquet", ".pq") and HAS_PYARROW

        if self.use_parquet and not HAS_PYARROW:
            logger.warning(
                "pyarrow not available; falling back to JSONL format."
            )
            self.use_parquet = False

        # Adjust output path if falling back to JSONL
        if not self.use_parquet:
            if not output_path.endswith(".jsonl"):
                base = os.path.splitext(output_path)[0]
                self.output_path = base + ".jsonl"

        # Internal state
        self._buffer: List[Dict[str, Any]] = []
        self._total_events: int = 0
        self._finalized: bool = False

        # Parquet writer state
        self._parquet_writer = None
        self._schema = None

        # JSONL writer state
        self._jsonl_file = None

        # Initialize writer
        self._init_writer()

    # ------------------------------------------------------------------ #
    #  Initialization                                                     #
    # ------------------------------------------------------------------ #

    def _init_writer(self) -> None:
        """Initialize the underlying writer (Parquet or JSONL)."""
        # Ensure output directory exists
        output_dir = os.path.dirname(self.output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        if self.use_parquet:
            self._init_parquet_writer()
        else:
            self._init_jsonl_writer()

    def _init_parquet_writer(self) -> None:
        """Initialize the Parquet writer with pyarrow."""
        # Build Arrow schema
        fields = []
        for col_name, col_type in self.COLUMNS:
            arrow_type = pa.type_for_alias(col_type) if col_type != "string" else pa.string()
            fields.append(pa.field(col_name, arrow_type))
        self._schema = pa.schema(fields)

        # Map compression name
        compression_map = {
            "zstd": "zstd",
            "snappy": "snappy",
            "gzip": "gzip",
            "none": None,
            "lz4": "lz4",
            "": None,
        }
        compression = compression_map.get(self.compression.lower(), "zstd")

        try:
            self._parquet_writer = pq.ParquetWriter(
                self.output_path,
                self._schema,
                compression=compression,
            )
            logger.info(
                f"Parquet writer initialized: {self.output_path} "
                f"(compression={compression})"
            )
        except Exception as e:
            logger.error(f"Failed to initialize Parquet writer: {e}")
            raise

    def _init_jsonl_writer(self) -> None:
        """Initialize the JSONL writer."""
        try:
            self._jsonl_file = open(self.output_path, "w", encoding="utf-8")
            logger.info(f"JSONL writer initialized: {self.output_path}")
        except OSError as e:
            raise IOError(f"Cannot create output file {self.output_path}: {e}") from e

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def write_event(self, event: TraceEvent) -> None:
        """
        Write a single trace event to the buffer.

        Events are buffered and flushed in batches of ``batch_size``.
        Call ``finalize()`` to flush remaining events and write metadata.

        Args:
            event: The ``TraceEvent`` to write.

        Raises:
            RuntimeError: If called after ``finalize()``.
        """
        if self._finalized:
            raise RuntimeError("Cannot write events after finalize() has been called.")

        row = self._event_to_row(event)
        self._buffer.append(row)
        self._total_events += 1

        if len(self._buffer) >= self.batch_size:
            self._flush()

    def write_events(self, events) -> None:
        """
        Write multiple trace events.

        Convenience method for batch writing.

        Args:
            events: An iterable of ``TraceEvent`` objects.
        """
        for event in events:
            self.write_event(event)

    def finalize(self, metadata: TraceMetadata) -> None:
        """
        Finalize the IR file: flush remaining events and write metadata.

        After calling this method, no more events can be written.

        Args:
            metadata: ``TraceMetadata`` to store alongside the events.
                Stored as a sidecar JSON file (``<output>.meta.json``).
        """
        if self._finalized:
            logger.warning("finalize() called more than once; ignoring.")
            return

        # Flush remaining buffered events
        if self._buffer:
            self._flush()

        # Close the writer
        if self.use_parquet:
            if self._parquet_writer:
                self._parquet_writer.close()
                self._parquet_writer = None
        else:
            if self._jsonl_file:
                self._jsonl_file.close()
                self._jsonl_file = None

        # Write metadata sidecar file
        self._write_metadata(metadata)

        self._finalized = True
        logger.info(
            f"IR finalized: {self._total_events} events written to {self.output_path}"
        )

    # ------------------------------------------------------------------ #
    #  Internal methods                                                   #
    # ------------------------------------------------------------------ #

    def _event_to_row(self, event: TraceEvent) -> Dict[str, Any]:
        """
        Convert a TraceEvent to a row dict for storage.

        The ``args`` field is serialized to a JSON string.
        """
        return {
            "ts": event.ts,
            "dur": event.dur,
            "pid": event.pid,
            "tid": event.tid,
            "cpu": event.cpu,
            "name": event.name,
            "cat": event.cat,
            "ph": event.ph,
            "device_id": event.device_id,
            "stream_id": event.stream_id,
            "args": json.dumps(event.args, ensure_ascii=False, default=str),
        }

    def _flush(self) -> None:
        """Flush the current buffer to the underlying writer."""
        if not self._buffer:
            return

        if self.use_parquet:
            self._flush_parquet()
        else:
            self._flush_jsonl()

        self._buffer.clear()

    def _flush_parquet(self) -> None:
        """Flush buffered events to Parquet as a new row group."""
        if not self._parquet_writer:
            raise RuntimeError("Parquet writer not initialized.")

        # Convert buffer to Arrow Table
        columns = {col_name: [] for col_name, _ in self.COLUMNS}
        for row in self._buffer:
            for col_name, _ in self.COLUMNS:
                columns[col_name].append(row.get(col_name))

        arrays = []
        for col_name, col_type in self.COLUMNS:
            arrow_type = pa.type_for_alias(col_type) if col_type != "string" else pa.string()
            arrays.append(pa.array(columns[col_name], type=arrow_type))

        table = pa.Table.from_arrays(arrays, schema=self._schema)

        self._parquet_writer.write_table(table)

    def _flush_jsonl(self) -> None:
        """Flush buffered events to JSONL file."""
        if not self._jsonl_file:
            raise RuntimeError("JSONL writer not initialized.")

        for row in self._buffer:
            self._jsonl_file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _write_metadata(self, metadata: TraceMetadata) -> None:
        """
        Write metadata as a sidecar JSON file.

        The metadata file is named ``<output_path>.meta.json`` and
        contains the serialized ``TraceMetadata``.
        """
        meta_path = os.path.splitext(self.output_path)[0] + ".meta.json"
        meta_dict = metadata.to_dict()

        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_dict, f, indent=2, ensure_ascii=False, default=str)
            logger.debug(f"Metadata written to: {meta_path}")
        except OSError as e:
            logger.warning(f"Failed to write metadata file {meta_path}: {e}")

    # ------------------------------------------------------------------ #
    #  Context manager support                                            #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "IRWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._finalized:
            # Write minimal metadata if finalize wasn't called
            meta = TraceMetadata()
            meta.total_events = self._total_events
            self.finalize(meta)

    def __del__(self):
        """Ensure resources are cleaned up."""
        if not self._finalized:
            try:
                if self._jsonl_file:
                    self._jsonl_file.close()
                if self._parquet_writer:
                    self._parquet_writer.close()
            except Exception:
                pass
