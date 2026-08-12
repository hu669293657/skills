"""
PerfParser - Linux perf.data / perf script Parser
===================================================

Parses Linux ``perf`` profiling data, focusing on CPU-side function
call stacks that are relevant for Host-side performance diagnosis.

Problem:
  ``perf.data`` is a binary format that cannot be directly parsed
  without the ``perf`` tool.  This parser adopts a two-step strategy:

  1. If the input file is a binary ``perf.data`` file (detected by the
     ``PERFILE2`` magic bytes), the parser invokes ``perf script`` to
     convert it to text format, then parses the text output.

  2. If the input file is already a text file (output of ``perf script``),
     the parser parses it directly.

  3. If the ``perf`` command is not available on the system, the parser
     instructs the user to run ``perf script`` manually:

         perf script -i perf.data > perf.txt

perf script text format:
  Each sample consists of a header line followed by indented call stack
  entries::

      comm  PID  TIME  CPU:
          func1 (addr+0x0)
          func2 (addr+0x10)
          func3 (addr+0x20)

      comm  PID  TIME  CPU:
          ...

  The header line contains the process name, PID, timestamp, and CPU core.
  Each indented line is a stack frame (function name and address).

Streaming:
  Uses ``readline()`` for line-by-line processing of text input.
  For binary ``perf.data``, pipes ``perf script`` output through a
  subprocess and reads stdout line by line.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ir.schema import (
    TraceEvent,
    TraceMetadata,
    TraceSource,
    EventCategory,
    EventPhase,
)

from .base import BaseParser

logger = logging.getLogger("host_trace_diagnosis.parsers.perf")


# --------------------------------------------------------------------------- #
#  Regular expressions for perf script text format                             #
# --------------------------------------------------------------------------- #

# Sample header line:
#   comm  PID  TIME  CPU:
# Example: "python  1234  12345.678901  CPU:0"
_RE_PERF_HEADER = re.compile(
    r"""
    ^
    (?P<comm>\S+)              # Process name
    \s+
    (?P<pid>\d+)               # PID
    \s+
    (?P<time>[\d.]+)           # Timestamp (seconds.microseconds)
    \s+
    (?P<cpu_label>CPU:)        # "CPU:" literal
    \s*
    (?P<cpu>\d+)               # CPU number
    """,
    re.VERBOSE,
)

# Stack frame line (indented):
#   func_name (addr+offset)
# Example: "        __do_softirq (ffffffff810abc12+0x0)"
_RE_PERF_STACK = re.compile(
    r"""
    ^\s+                       # Leading whitespace (indentation)
    (?P<func>[^(]+)            # Function name (up to '(')
    \(
    (?P<addr>[^)]+)            # Address (possibly with offset)
    \)
    \s*$
    """,
    re.VERBOSE,
)


class PerfParser(BaseParser):
    """
    Parser for Linux ``perf.data`` (binary) or ``perf script`` text output.

    Streaming line-by-line parser.  For binary ``perf.data``, pipes
    ``perf script`` subprocess output.
    """

    # Magic bytes for perf.data binary format
    PERF_MAGIC = b"PERFILE2"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._is_binary: bool = False
        self._perf_command: str = config.get("perf_command", "perf") if config else "perf"

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def parse(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse a perf data file (binary or text).

        For binary ``perf.data``, invokes ``perf script`` to convert to
        text, then parses the text output.  For text files (already
        ``perf script`` output), parses directly.

        Args:
            file_path: Path to perf.data or perf script text file.

        Yields:
            TraceEvent: One event per sample (call stack).

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If ``perf`` command is not available for
                binary perf.data files.
            ValueError: If the text format is invalid.
        """
        path = self._validate_file(file_path)
        self.reset()

        self._is_binary = self._is_perf_binary(str(path))

        if self._is_binary:
            yield from self._parse_binary(str(path))
        else:
            yield from self._parse_text(str(path))

    def get_metadata(self) -> TraceMetadata:
        """Return metadata accumulated during parsing."""
        return self._finalize_metadata(TraceSource.PERF, self._metadata.file_path)

    # ------------------------------------------------------------------ #
    #  Binary perf.data parsing (via perf script subprocess)              #
    # ------------------------------------------------------------------ #

    def _is_perf_binary(self, file_path: str) -> bool:
        """
        Check if a file is a binary perf.data file.

        Args:
            file_path: Path to the file.

        Returns:
            bool: True if the file starts with ``PERFILE2`` magic bytes.
        """
        sniff = self._sniff(file_path, num_bytes=8)
        return sniff.startswith(self.PERF_MAGIC)

    def _parse_binary(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse binary perf.data by invoking ``perf script``.

        Runs ``perf script -i <file_path>`` as a subprocess and parses
        its stdout line by line.  This avoids creating a temporary text
        file while maintaining streaming behavior.

        Args:
            file_path: Path to the perf.data binary file.

        Yields:
            TraceEvent: Parsed events.

        Raises:
            RuntimeError: If the ``perf`` command is not available or fails.
        """
        # Check if perf is available
        if not self._is_perf_available():
            raise RuntimeError(
                f"Cannot parse binary perf.data file '{file_path}': "
                f"the 'perf' command is not available on this system.\n"
                f"Please run the following command manually to convert to text:\n"
                f"    perf script -i {file_path} > {file_path}.txt\n"
                f"Then pass the .txt file to this parser."
            )

        cmd = [self._perf_command, "script", "-i", file_path]
        logger.info(f"Running: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )
        except OSError as e:
            raise RuntimeError(f"Failed to execute 'perf script': {e}") from e

        try:
            yield from self._parse_text_stream(proc.stdout)
        finally:
            proc.stdout.close()
            stderr_output = proc.stderr.read() if proc.stderr else ""
            proc.stderr.close()
            return_code = proc.wait()

            if return_code != 0:
                logger.error(
                    f"perf script exited with code {return_code}. "
                    f"stderr: {stderr_output[:500]}"
                )

    def _is_perf_available(self) -> bool:
        """
        Check if the ``perf`` command is available on the system.

        Returns:
            bool: True if ``perf`` can be executed.
        """
        try:
            result = subprocess.run(
                [self._perf_command, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------ #
    #  Text format parsing                                                #
    # ------------------------------------------------------------------ #

    def _parse_text(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse a perf script text file line by line.

        Args:
            file_path: Path to the text file.

        Yields:
            TraceEvent: Parsed events.
        """
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                yield from self._parse_text_stream(f)
        except OSError as e:
            raise IOError(f"Error reading perf text file {file_path}: {e}") from e

    def _parse_text_stream(self, stream) -> Iterator[TraceEvent]:
        """
        Parse a text stream (file object or subprocess stdout) line by line.

        Groups lines into samples: each sample starts with a header line
        and is followed by zero or more indented stack frame lines.

        Args:
            stream: A file-like object with ``readline()`` method.

        Yields:
            TraceEvent: One event per sample.
        """
        current_sample: Optional[Dict[str, Any]] = None
        stack_frames: List[str] = []

        for line in stream:
            line = line.rstrip("\n\r")

            # Check for header line (non-indented, contains CPU:)
            header_match = _RE_PERF_HEADER.match(line)

            if header_match:
                # If we have a pending sample, yield it first
                if current_sample is not None:
                    yield from self._emit_sample(current_sample, stack_frames)

                # Start new sample
                current_sample = {
                    "comm": header_match.group("comm"),
                    "pid": int(header_match.group("pid")),
                    "time": float(header_match.group("time")),
                    "cpu": int(header_match.group("cpu")),
                }
                stack_frames = []

            elif line.strip() == "":
                # Empty line: sample boundary
                if current_sample is not None:
                    yield from self._emit_sample(current_sample, stack_frames)
                    current_sample = None
                    stack_frames = []

            else:
                # Check for stack frame line (indented)
                frame_match = _RE_PERF_STACK.match(line)
                if frame_match:
                    func_name = frame_match.group("func").strip()
                    addr = frame_match.group("addr").strip()
                    stack_frames.append(f"{func_name} ({addr})")
                else:
                    # Unrecognized line; could be a comment or metadata
                    logger.debug(f"Unrecognized perf line: {line[:100]}")

        # Yield any remaining sample
        if current_sample is not None:
            yield from self._emit_sample(current_sample, stack_frames)

    def _emit_sample(
        self,
        sample: Dict[str, Any],
        stack_frames: List[str],
    ) -> Iterator[TraceEvent]:
        """
        Emit a TraceEvent for a parsed perf sample.

        Each sample represents one profiling observation.  The call stack
        is stored in ``args.callstack`` as a list of function names.

        Args:
            sample: Dict with comm, pid, time, cpu.
            stack_frames: List of stack frame strings.

        Yields:
            One TraceEvent per sample.
        """
        ts_us = int(sample["time"] * 1_000_000)
        pid = sample["pid"]
        comm = sample["comm"]
        cpu = sample["cpu"]

        # Register process/thread name
        self._register_process(pid, comm)
        self._register_thread(pid, comm)

        # Determine the primary function name (top of stack, or comm)
        primary_name = stack_frames[0].split("(")[0].strip() if stack_frames else comm

        event = TraceEvent(
            ts=ts_us,
            dur=0,
            pid=pid,
            tid=pid,
            cpu=cpu,
            name=primary_name,
            cat=EventCategory.CPU_FUNCTION.value,
            ph=EventPhase.INSTANT.value,
            args={
                "comm": comm,
                "callstack": stack_frames,
                "stack_depth": len(stack_frames),
            },
        )
        self._update_metadata_from_event(event)
        yield event
