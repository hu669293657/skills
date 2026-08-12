"""
BaseParser - Abstract Base Class for All Trace Parsers
========================================================

This module defines the abstract base class that all format-specific
parsers (Chrome JSON, ftrace, msprof, perf, etc.) must inherit from.

Design principles:
  - **Streaming**: All parsers must yield events one-by-one via a generator,
    never loading the entire file into memory.
  - **Two-phase**: ``parse()`` produces events; ``get_metadata()`` returns
    summary metadata.  Metadata is only valid after ``parse()`` has been
    fully consumed (or at least partially consumed for streaming metadata).
  - **Configurable**: Each parser receives a config dict from the detector
    / CLI, covering batch sizes, streaming thresholds, etc.

Memory contract:
  For files larger than ``streaming_threshold_mb`` (default 100 MB),
  memory usage MUST stay O(1) (~50 MB) regardless of file size.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from ir.schema import TraceEvent, TraceMetadata, TraceSource

logger = logging.getLogger("host_trace_diagnosis.parsers.base")


class BaseParser(ABC):
    """
    Abstract base class for all trace file parsers.

    Subclasses must implement:
      - ``parse(file_path)``: yield ``TraceEvent`` objects one by one.
      - ``get_metadata()``: return a populated ``TraceMetadata``.

    Common utilities provided by this class:
      - File existence / size checks.
      - Line counting for text-based formats.
      - Config access helpers.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        Initialize the parser with a configuration dictionary.

        Args:
            config: Parser-specific configuration.  Expected keys (all optional):
                - ``batch_size`` (int): Number of events to buffer before
                  yielding to downstream.  Default 5000.
                - ``streaming_threshold_mb`` (float): File size above which
                  streaming mode is mandatory.  Default 100.
                - ``sniff_bytes`` (int): Number of bytes to read for format
                  detection.  Default 4096.
        """
        self.config: Dict[str, Any] = config or {}
        self.batch_size: int = int(self.config.get("batch_size", 5000))
        self.streaming_threshold_mb: float = float(
            self.config.get("streaming_threshold_mb", 100)
        )
        self.sniff_bytes: int = int(self.config.get("sniff_bytes", 4096))

        # Metadata accumulators – updated during parsing.
        self._metadata: TraceMetadata = TraceMetadata()
        self._event_count: int = 0
        self._ts_min: int = 0
        self._ts_max: int = 0
        self._ts_initialized: bool = False
        self._devices: set = set()
        self._processes: Dict[int, str] = {}
        self._threads: Dict[int, str] = {}
        self._cpu_cores: int = 0

    # ------------------------------------------------------------------ #
    #  Abstract methods                                                   #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def parse(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse a trace file and yield ``TraceEvent`` objects one by one.

        This is a **streaming** generator.  Implementations MUST NOT load
        the entire file into memory.  For very large files (>100 MB),
        memory usage should remain O(1).

        Args:
            file_path: Absolute or relative path to the trace file.

        Yields:
            TraceEvent: One event at a time.

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
            PermissionError: If the file cannot be read.
            ValueError: If the file format is invalid.
        """
        ...

    @abstractmethod
    def get_metadata(self) -> TraceMetadata:
        """
        Return metadata about the parsed trace.

        This should only be called **after** ``parse()`` has been fully
        consumed (i.e. the generator has been exhausted).  Calling it
        prematurely may return incomplete metadata.

        Returns:
            TraceMetadata: Populated metadata object.
        """
        ...

    # ------------------------------------------------------------------ #
    #  Common utility methods                                             #
    # ------------------------------------------------------------------ #

    def _validate_file(self, file_path: str) -> Path:
        """
        Validate that a file exists and is readable.

        Args:
            file_path: Path to the file.

        Returns:
            Path: A ``pathlib.Path`` object for the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            PermissionError: If the file is not readable.
            IsADirectoryError: If the path is a directory and a file was
                expected.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Trace file not found: {file_path}")
        if path.is_dir():
            raise IsADirectoryError(
                f"Expected a file but got a directory: {file_path}"
            )
        if not os.access(str(path), os.R_OK):
            raise PermissionError(f"Cannot read trace file (permission denied): {file_path}")
        return path

    def _get_file_size_mb(self, file_path: str) -> float:
        """
        Get file size in megabytes.

        Args:
            file_path: Path to the file.

        Returns:
            float: File size in MB.
        """
        try:
            size_bytes = os.path.getsize(file_path)
            return size_bytes / (1024 * 1024)
        except OSError:
            return 0.0

    def _requires_streaming(self, file_path: str) -> bool:
        """
        Determine whether streaming mode is required based on file size.

        Args:
            file_path: Path to the file.

        Returns:
            bool: True if the file exceeds the streaming threshold.
        """
        return self._get_file_size_mb(file_path) > self.streaming_threshold_mb

    def _count_lines(self, file_path: str, max_lines: int = 0) -> int:
        """
        Count the number of lines in a text file.

        This reads the file in binary mode and counts newline characters,
        which is fast and memory-efficient.

        Args:
            file_path: Path to the text file.
            max_lines: If > 0, stop counting after this many lines
                (for quick estimation).

        Returns:
            int: Number of lines (or max_lines if reached early).
        """
        count = 0
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    count += chunk.count(b"\n")
                    if max_lines and count >= max_lines:
                        return count
        except OSError as e:
            logger.warning(f"Failed to count lines in {file_path}: {e}")
        return count

    def _sniff(self, file_path: str, num_bytes: int = 0) -> bytes:
        """
        Read the first ``num_bytes`` bytes of a file for format detection.

        Args:
            file_path: Path to the file.
            num_bytes: Number of bytes to read.  Defaults to ``self.sniff_bytes``.

        Returns:
            bytes: The first ``num_bytes`` bytes of the file (or fewer if
            the file is smaller).
        """
        n = num_bytes or self.sniff_bytes
        try:
            with open(file_path, "rb") as f:
                return f.read(n)
        except OSError as e:
            logger.warning(f"Failed to sniff {file_path}: {e}")
            return b""

    def _update_metadata_from_event(self, event: TraceEvent) -> None:
        """
        Update internal metadata accumulators from a parsed event.

        Called by subclasses for each yielded event so that
        ``get_metadata()`` can return accurate summary data.

        Args:
            event: A parsed ``TraceEvent``.
        """
        self._event_count += 1

        # Track timestamp range
        if not self._ts_initialized:
            self._ts_min = event.ts
            self._ts_max = event.ts
            self._ts_initialized = True
        else:
            if event.ts < self._ts_min:
                self._ts_min = event.ts
            if event.ts > self._ts_max:
                self._ts_max = event.ts
            # Also consider end time for duration events
        if event.dur > 0:
            end_ts = event.ts + event.dur
            if end_ts > self._ts_max:
                self._ts_max = end_ts

        # Track devices
        if event.device_id >= 0:
            self._devices.add(event.device_id)

        # Track CPU cores
        if event.cpu >= 0 and event.cpu + 1 > self._cpu_cores:
            self._cpu_cores = event.cpu + 1

    def _finalize_metadata(
        self,
        source: TraceSource,
        file_path: str,
    ) -> TraceMetadata:
        """
        Build the final ``TraceMetadata`` from accumulated state.

        Args:
            source: The trace source format identifier.
            file_path: Path to the trace file.

        Returns:
            TraceMetadata: Fully populated metadata.
        """
        self._metadata.source = source
        self._metadata.file_path = file_path
        self._metadata.file_size_mb = self._get_file_size_mb(file_path)
        self._metadata.total_events = self._event_count
        self._metadata.ts_start = self._ts_min
        self._metadata.ts_end = self._ts_max
        self._metadata.duration_us = (
            self._ts_max - self._ts_min if self._ts_initialized else 0
        )
        self._metadata.devices = sorted(self._devices)
        self._metadata.processes = dict(self._processes)
        self._metadata.threads = dict(self._threads)
        self._metadata.cpu_cores = self._cpu_cores
        return self._metadata

    def _register_process(self, pid: int, name: str) -> None:
        """Register a process name in metadata."""
        if pid >= 0:
            self._processes[pid] = name

    def _register_thread(self, tid: int, name: str) -> None:
        """Register a thread name in metadata."""
        if tid >= 0:
            self._threads[tid] = name

    def reset(self) -> None:
        """Reset all internal accumulators (for re-parsing)."""
        self._metadata = TraceMetadata()
        self._event_count = 0
        self._ts_min = 0
        self._ts_max = 0
        self._ts_initialized = False
        self._devices = set()
        self._processes = {}
        self._threads = {}
        self._cpu_cores = 0
