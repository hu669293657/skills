"""
FormatDetector - Trace File Format Detection
=============================================

Automatically detects the format of a trace file by reading the first few
kilobytes (sniff) and checking for characteristic signatures.

Supported formats:
  - **Chrome JSON**: ``trace_view.json``, Chrome tracing output.
    Detected by leading ``[`` / ``{`` or presence of ``"traceEvents"``.
  - **ftrace**: Linux kernel function tracer output.
    Detected by ``tracer:`` or ``entries-in-buffer`` in the header.
  - **Perfetto**: Perfetto protobuf trace.
    Detected by protobuf magic bytes.
  - **perf.data**: Linux perf binary format.
    Detected by ``PERFILE2`` magic bytes.
  - **msprof**: Ascend msprof output directory.
    Detected by presence of ``trace_view.json`` or ``ASCEND`` in a directory.
  - **CSV**: Comma-separated values (msprof operator details etc.).
    Detected by comma-separated first line that is not JSON.

The detector also provides a factory method ``get_parser()`` that returns
the appropriate parser instance for a detected format.
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from .base import BaseParser

logger = logging.getLogger("host_trace_diagnosis.parsers.detector")


class TraceFormat(str, Enum):
    """Enumerates all supported trace file formats."""

    CHROME_JSON = "chrome_json"
    FTRACE = "ftrace"
    MSPROF = "msprof"
    PERF = "perf"
    PERFETTO = "perfetto"
    CSV = "csv"
    UNKNOWN = "unknown"


class FormatDetector:
    """
    Detects trace file format by sniffing the first few kilobytes.

    Usage::

        detector = FormatDetector(config)
        fmt = detector.detect("trace.json")
        parser = detector.get_parser(fmt, config)
        for event in parser.parse("trace.json"):
            ...
    """

    # Binary magic bytes for format identification
    PERF_MAGIC = b"PERFILE2"
    PERFETTO_MAGIC = b"\x0a\x0c"  # Protobuf field 1, length-delimited (heuristic)

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        Initialize the detector.

        Args:
            config: Configuration dict, typically from ``config.yaml``
                under the ``parser`` section.  Key ``sniff_bytes``
                controls how many bytes to read for detection.
        """
        self.config: Dict[str, Any] = config or {}
        self.sniff_bytes: int = int(self.config.get("sniff_bytes", 4096))

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def detect(self, file_path: str) -> TraceFormat:
        """
        Detect the format of a trace file or directory.

        For directories, checks for msprof-specific files.
        For files, reads the first ``sniff_bytes`` bytes and applies
        heuristics.

        Args:
            file_path: Path to the trace file or directory.

        Returns:
            TraceFormat: The detected format.  ``TraceFormat.UNKNOWN`` if
            the format cannot be determined.

        Raises:
            FileNotFoundError: If the path does not exist.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Trace path not found: {file_path}")

        # Directory → check for msprof structure
        if path.is_dir():
            return self._detect_directory(path)

        # File → sniff content
        return self._detect_file(path)

    def get_parser(
        self,
        fmt: TraceFormat,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[BaseParser]:
        """
        Factory method: return a parser instance for the given format.

        Args:
            fmt: The detected trace format.
            config: Parser configuration dict.

        Returns:
            BaseParser: An instance of the appropriate parser, or None
            if no parser is available for the format.
        """
        config = config or {}

        if fmt == TraceFormat.CHROME_JSON:
            from .chrome_json_parser import ChromeTraceJSONParser
            return ChromeTraceJSONParser(config)

        elif fmt == TraceFormat.FTRACE:
            from .ftrace_parser import FtraceParser
            return FtraceParser(config)

        elif fmt == TraceFormat.MSPROF:
            from .msprof_parser import MsprofParser
            return MsprofParser(config)

        elif fmt == TraceFormat.PERF:
            from .perf_parser import PerfParser
            return PerfParser(config)

        elif fmt == TraceFormat.PERFETTO:
            # Perfetto traces can be loaded via PerfettoBridge or treated
            # as Chrome JSON if exported.  For now, return None and let
            # the caller use PerfettoBridge directly.
            logger.warning(
                "Perfetto format detected. Use ir.perfetto_bridge.PerfettoBridge "
                "for direct SQL queries, or export to Chrome JSON first."
            )
            return None

        elif fmt == TraceFormat.CSV:
            # CSV is typically part of msprof output; standalone CSV
            # parsing is handled by MsprofParser.
            from .msprof_parser import MsprofParser
            return MsprofParser(config)

        else:
            logger.error(f"No parser available for format: {fmt}")
            return None

    # ------------------------------------------------------------------ #
    #  Detection heuristics                                               #
    # ------------------------------------------------------------------ #

    def _detect_directory(self, path: Path) -> TraceFormat:
        """
        Detect format for a directory input (typically msprof output).

        Checks for characteristic msprof files:
          - ``trace_view.json``
          - ``operator_detail_*.csv``
          - ``step_trace_*.json``
          - ``ascend_pytorch_profiler_*.db``
        """
        try:
            entries = list(path.iterdir())
        except OSError as e:
            logger.warning(f"Cannot list directory {path}: {e}")
            return TraceFormat.UNKNOWN

        entry_names = [e.name.lower() for e in entries]

        has_trace_view = any("trace_view" in n for n in entry_names)
        has_operator_csv = any("operator_detail" in n and n.endswith(".csv") for n in entry_names)
        has_step_trace = any("step_trace" in n for n in entry_names)
        has_db = any(n.endswith(".db") and "ascend" in n for n in entry_names)

        if has_trace_view or has_operator_csv or has_step_trace or has_db:
            return TraceFormat.MSPROF

        logger.info(f"Directory {path} does not match any known format.")
        return TraceFormat.UNKNOWN

    def _detect_file(self, path: Path) -> TraceFormat:
        """
        Detect format for a single file by sniffing its content.

        Detection order (first match wins):
          1. Binary magic bytes (perf.data, Perfetto protobuf)
          2. JSON (Chrome Trace)
          3. ftrace header
          4. CSV
          5. Unknown
        """
        sniff = self._read_sniff(path)
        if not sniff:
            return TraceFormat.UNKNOWN

        # 1. Check binary magic bytes first
        fmt = self._detect_binary(sniff)
        if fmt != TraceFormat.UNKNOWN:
            return fmt

        # Decode as text for further checks
        try:
            text = sniff.decode("utf-8", errors="replace")
        except Exception:
            return TraceFormat.UNKNOWN

        # 2. JSON detection
        stripped = text.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            if "traceEvents" in text or '"ph"' in text or '"pid"' in text:
                return TraceFormat.CHROME_JSON
            # Generic JSON could still be Chrome trace
            return TraceFormat.CHROME_JSON

        # 3. ftrace detection
        first_line = text.split("\n", 1)[0] if "\n" in text else text
        if "tracer:" in first_line or "entries-in-buffer" in first_line:
            return TraceFormat.FTRACE
        # ftrace events often start with a task-pid pattern
        if self._looks_like_ftrace(text):
            return TraceFormat.FTRACE

        # 4. CSV detection
        if self._looks_like_csv(text):
            return TraceFormat.CSV

        # 5. Check for perf script text output (not binary perf.data)
        if self._looks_like_perf_script(text):
            return TraceFormat.PERF

        return TraceFormat.UNKNOWN

    def _read_sniff(self, path: Path) -> bytes:
        """Read the first ``sniff_bytes`` bytes from a file."""
        try:
            with open(str(path), "rb") as f:
                return f.read(self.sniff_bytes)
        except OSError as e:
            logger.warning(f"Failed to read sniff bytes from {path}: {e}")
            return b""

    def _detect_binary(self, sniff: bytes) -> TraceFormat:
        """
        Check for binary format magic bytes.

        Args:
            sniff: Raw bytes from the start of the file.

        Returns:
            TraceFormat if a binary format is detected, else UNKNOWN.
        """
        # perf.data: starts with "PERFILE2"
        if sniff.startswith(self.PERF_MAGIC):
            return TraceFormat.PERF

        # Perfetto protobuf: heuristic check.
        # Perfetto traces are protobuf; the first field is typically
        # a uint32 for the number of packets.  We check for typical
        # protobuf field tag patterns.
        if len(sniff) >= 2:
            # Protobuf field 1, wire type 2 (length-delimited): tag = 0x0a
            # This is a weak heuristic; binary files that aren't text
            # and don't match perf magic might be Perfetto.
            if sniff[0] == 0x0a and not sniff[:1].isascii():
                # Additional check: ensure it's not valid UTF-8 text
                try:
                    sniff.decode("utf-8")
                    # If it decodes as valid UTF-8, it's probably text
                    return TraceFormat.UNKNOWN
                except UnicodeDecodeError:
                    return TraceFormat.PERFETTO

        return TraceFormat.UNKNOWN

    def _looks_like_ftrace(self, text: str) -> bool:
        """
        Heuristic: check if text looks like ftrace output.

        ftrace events typically have the format:
            TASK-PID  CPU#  FLAGS  TIMESTAMP:  FUNCTION

        We check for the presence of ``==>`` (sched_switch) or
        typical ftrace patterns like ``sched_switch`` or ``sched_wakeup``.
        """
        lines = text.split("\n")
        checked = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            checked += 1
            if checked > 5:
                break
            # sched_switch has ==>
            if "sched_switch" in line or "sched_wakeup" in line:
                return True
            if "==>" in line:
                return True
            # Generic function tracer line: TASK-PID CPU FLAGS TIME: func
            # Check for pattern: word-digits space digits
            parts = line.split()
            if len(parts) >= 4:
                # First part should be TASK-PID
                if "-" in parts[0] and parts[0].split("-")[-1].isdigit():
                    return True
        return False

    def _looks_like_csv(self, text: str) -> bool:
        """
        Heuristic: check if text looks like CSV (comma-separated values).

        Checks the first non-empty line for comma separation.
        """
        first_line = text.split("\n", 1)[0].strip()
        if not first_line:
            return False
        # Must have at least 2 commas to be CSV
        if first_line.count(",") >= 2:
            return True
        return False

    def _looks_like_perf_script(self, text: str) -> bool:
        """
        Heuristic: check if text looks like ``perf script`` output.

        perf script output format:
            comm  PID  TIME  CPU:
                func1 (addr)
                func2 (addr)

        The first line typically contains a process name, PID, timestamp,
        and ``CPU:`` keyword.
        """
        lines = text.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if "CPU:" in line or "cpu:" in line:
                return True
            break
        return False
