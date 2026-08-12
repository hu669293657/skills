"""
FtraceParser - Linux Kernel ftrace Text Format Parser
=======================================================

Parses ftrace (function tracer) text output from the Linux kernel,
focusing on scheduler events that are critical for Host-side performance
diagnosis in NPU training scenarios.

Supported event types:
  - **sched_switch**: Context switch events.  Format::

        TASK-PID  CPU#  FLAGS  TIMESTAMP: sched_switch:
            prev_comm=... prev_pid=... prev_state=... ==>
            next_comm=... next_pid=... next_prio=...

  - **sched_wakeup** / **sched_wakeup_new**: Thread wakeup events.
        Format::

        TASK-PID  CPU#  FLAGS  TIMESTAMP: sched_wakeup:
            comm=... pid=... prio=... target_cpu=...

  - **Generic function tracer**: Plain function tracing lines::

        TASK-PID  CPU#  FLAGS  TIMESTAMP: FUNCTION

Header parsing:
  The ftrace header contains metadata like::

      # tracer: nop
      #
      # entries-in-buffer/entries-written: 12345/12345
      # P:8

  The ``#P:N`` line indicates the number of CPU cores.

Timestamps:
  ftrace timestamps are in seconds with microsecond precision (e.g.
  ``1234.567890``).  We convert them to integer microseconds.

Streaming:
  Uses ``readline()`` for line-by-line processing, maintaining O(1)
  memory usage regardless of file size.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from ir.schema import (
    TraceEvent,
    TraceMetadata,
    TraceSource,
    EventCategory,
    EventPhase,
)

from .base import BaseParser

logger = logging.getLogger("host_trace_diagnosis.parsers.ftrace")


# --------------------------------------------------------------------------- #
#  Regular expressions for parsing ftrace lines                                #
# --------------------------------------------------------------------------- #

# Header patterns
_RE_TRACER = re.compile(r"^#\s*tracer:\s*(\S+)")
_RE_ENTRIES = re.compile(r"^#\s*entries-in-buffer/entries-written:\s*(\d+)/(\d+)")
_RE_CPU_COUNT = re.compile(r"^#\s*P:(\d+)")

# Generic ftrace event line: TASK-PID  CPU#  FLAGS  TIMESTAMP:  CONTENT
# Example: "bash-1234  [001] .... 123456.789012: sched_switch: ..."
_RE_FTRACE_LINE = re.compile(
    r"""
    ^
    (?P<task>[^\s]+)          # TASK (may include -PID)
    \s+
    \[(?P<cpu>\d+)\]          # [CPU#]
    \s+
    (?P<flags>\S+)            # FLAGS (e.g. "...." or "d...")
    \s+
    (?P<timestamp>[\d.]+):    # TIMESTAMP:
    \s*
    (?P<content>.*)           # rest of the line
    """,
    re.VERBOSE,
)

# sched_switch field extraction
_RE_SCHED_SWITCH = re.compile(
    r"""
    prev_comm=(?P<prev_comm>[^\s]+)\s+
    prev_pid=(?P<prev_pid>\d+)\s+
    prev_prio=(?P<prev_prio>\d+)\s+
    prev_state=(?P<prev_state>[^\s]+)\s+
    ==>\s+
    next_comm=(?P<next_comm>[^\s]+)\s+
    next_pid=(?P<next_pid>\d+)\s+
    next_prio=(?P<next_prio>\d+)
    """,
    re.VERBOSE,
)

# sched_wakeup field extraction
_RE_SCHED_WAKEUP = re.compile(
    r"""
    comm=(?P<comm>[^\s]+)\s+
    pid=(?P<pid>\d+)\s+
    prio=(?P<prio>\d+)\s+
    target_cpu=(?P<target_cpu>\d+)
    """,
    re.VERBOSE,
)


class FtraceParser(BaseParser):
    """
    Streaming parser for Linux kernel ftrace text output.

    Processes one line at a time, yielding ``TraceEvent`` objects for
    each parsed event.  Memory usage is O(1) regardless of file size.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._tracer_type: str = ""
        self._entries_in_buffer: int = 0

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def parse(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse an ftrace text file line by line.

        Args:
            file_path: Path to the ftrace text file.

        Yields:
            TraceEvent: One event per parsed line (some lines may yield
            zero events if they are comments or unparseable).

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file is not valid ftrace format.
        """
        path = self._validate_file(file_path)
        self.reset()

        try:
            with open(str(path), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    yield from self._parse_line(line)
        except OSError as e:
            raise IOError(f"Error reading ftrace file {file_path}: {e}") from e

    def get_metadata(self) -> TraceMetadata:
        """Return metadata accumulated during parsing."""
        return self._finalize_metadata(TraceSource.FTRACE, self._metadata.file_path)

    # ------------------------------------------------------------------ #
    #  Line-level parsing                                                 #
    # ------------------------------------------------------------------ #

    def _parse_line(self, line: str) -> Iterator[TraceEvent]:
        """
        Parse a single ftrace line and yield zero or more events.

        Args:
            line: A single line from the ftrace file (including newline).

        Yields:
            TraceEvent: Parsed events (may be zero for comments/empty lines).
        """
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            return

        # Handle header / comment lines
        if stripped.startswith("#"):
            self._parse_header_line(stripped)
            return

        # Parse event line
        match = _RE_FTRACE_LINE.match(line)
        if not match:
            # Could be a continuation line or malformed; skip
            logger.debug(f"Unparseable ftrace line: {stripped[:100]}")
            return

        cpu = int(match.group("cpu"))
        timestamp_str = match.group("timestamp")
        ts_us = self._parse_timestamp(timestamp_str)
        content = match.group("content")
        task_pid = match.group("task")

        # Extract task name and PID from TASK-PID
        task_name, task_pid_val = self._split_task_pid(task_pid)

        # Route to specific event parser
        if content.startswith("sched_switch:"):
            yield from self._parse_sched_switch(
                content, ts_us, cpu, task_pid_val, task_name
            )
        elif content.startswith("sched_wakeup"):
            yield from self._parse_sched_wakeup(
                content, ts_us, cpu, task_pid_val, task_name
            )
        else:
            yield from self._parse_function_trace(
                content, ts_us, cpu, task_pid_val, task_name
            )

    def _parse_header_line(self, line: str) -> None:
        """
        Parse ftrace header comment lines to extract metadata.

        Handles:
          - ``# tracer: nop`` → tracer type
          - ``# entries-in-buffer/entries-written: N/M``
          - ``# P:N`` → number of CPU cores
        """
        m = _RE_TRACER.match(line)
        if m:
            self._tracer_type = m.group(1)
            logger.debug(f"ftrace tracer type: {self._tracer_type}")
            return

        m = _RE_ENTRIES.match(line)
        if m:
            self._entries_in_buffer = int(m.group(1))
            return

        m = _RE_CPU_COUNT.match(line)
        if m:
            self._cpu_cores = int(m.group(1))
            logger.debug(f"ftrace CPU cores: {self._cpu_cores}")
            return

    def _parse_sched_switch(
        self,
        content: str,
        ts_us: int,
        cpu: int,
        task_pid: int,
        task_name: str,
    ) -> Iterator[TraceEvent]:
        """
        Parse a sched_switch event line.

        A context switch event records which task was running before
        the switch and which task will run next.  We produce a single
        TraceEvent with all fields in ``args``.

        Yields:
            One TraceEvent representing the context switch.
        """
        # Remove the "sched_switch:" prefix
        fields_str = content[len("sched_switch:"):].strip()
        m = _RE_SCHED_SWITCH.search(fields_str)
        if not m:
            logger.debug(f"Failed to parse sched_switch fields: {fields_str}")
            return

        prev_pid = int(m.group("prev_pid"))
        next_pid = int(m.group("next_pid"))
        prev_state = m.group("prev_state")

        # Register process names
        prev_comm = m.group("prev_comm")
        next_comm = m.group("next_comm")
        self._register_process(prev_pid, prev_comm)
        self._register_process(next_pid, next_comm)
        self._register_thread(prev_pid, prev_comm)
        self._register_thread(next_pid, next_comm)

        event = TraceEvent(
            ts=ts_us,
            dur=0,
            pid=prev_pid,
            tid=prev_pid,  # In ftrace, tid == pid for the running task
            cpu=cpu,
            name="sched_switch",
            cat=EventCategory.CPU_SCHED.value,
            ph=EventPhase.INSTANT.value,
            args={
                "prev_comm": prev_comm,
                "prev_pid": prev_pid,
                "prev_prio": int(m.group("prev_prio")),
                "prev_state": prev_state,
                "next_comm": next_comm,
                "next_pid": next_pid,
                "next_prio": int(m.group("next_prio")),
            },
        )
        self._update_metadata_from_event(event)
        yield event

    def _parse_sched_wakeup(
        self,
        content: str,
        ts_us: int,
        cpu: int,
        task_pid: int,
        task_name: str,
    ) -> Iterator[TraceEvent]:
        """
        Parse a sched_wakeup (or sched_wakeup_new) event line.

        A wakeup event records that a task was woken up on a target CPU.

        Yields:
            One TraceEvent representing the wakeup.
        """
        # Extract event name (sched_wakeup or sched_wakeup_new)
        colon_idx = content.find(":")
        event_name = content[:colon_idx] if colon_idx > 0 else "sched_wakeup"
        fields_str = content[colon_idx + 1:].strip() if colon_idx > 0 else ""

        m = _RE_SCHED_WAKEUP.search(fields_str)
        if not m:
            logger.debug(f"Failed to parse sched_wakeup fields: {fields_str}")
            return

        woken_pid = int(m.group("pid"))
        woken_comm = m.group("comm")
        target_cpu = int(m.group("target_cpu"))

        self._register_process(woken_pid, woken_comm)
        self._register_thread(woken_pid, woken_comm)

        event = TraceEvent(
            ts=ts_us,
            dur=0,
            pid=woken_pid,
            tid=woken_pid,
            cpu=cpu,
            name=event_name,
            cat=EventCategory.CPU_SCHED.value,
            ph=EventPhase.INSTANT.value,
            args={
                "comm": woken_comm,
                "pid": woken_pid,
                "prio": int(m.group("prio")),
                "target_cpu": target_cpu,
                "woken_on_cpu": cpu,
            },
        )
        self._update_metadata_from_event(event)
        yield event

    def _parse_function_trace(
        self,
        content: str,
        ts_us: int,
        cpu: int,
        task_pid: int,
        task_name: str,
    ) -> Iterator[TraceEvent]:
        """
        Parse a generic function tracer line.

        For the ``nop`` tracer (most common for sched events), there are
        no function trace lines.  For ``function`` or ``function_graph``
        tracers, each line records a kernel function call.

        Yields:
            One TraceEvent for the function call.
        """
        func_name = content.strip()
        if not func_name:
            return

        event = TraceEvent(
            ts=ts_us,
            dur=0,
            pid=task_pid,
            tid=task_pid,
            cpu=cpu,
            name=func_name,
            cat=EventCategory.CPU_FUNCTION.value,
            ph=EventPhase.INSTANT.value,
            args={
                "task": task_name,
            },
        )
        self._update_metadata_from_event(event)
        yield event

    # ------------------------------------------------------------------ #
    #  Utility methods                                                    #
    # ------------------------------------------------------------------ #

    def _parse_timestamp(self, ts_str: str) -> int:
        """
        Convert ftrace timestamp (seconds.microseconds) to integer microseconds.

        ftrace timestamps look like ``123456.789012`` (seconds with 6
        decimal places).  We multiply by 1,000,000 to get microseconds.

        Args:
            ts_str: Timestamp string like ``123456.789012``.

        Returns:
            int: Timestamp in microseconds.
        """
        try:
            return int(float(ts_str) * 1_000_000)
        except (ValueError, TypeError):
            logger.debug(f"Failed to parse timestamp: {ts_str}")
            return 0

    def _split_task_pid(self, task_pid: str) -> tuple:
        """
        Split a ``TASK-PID`` string into (task_name, pid).

        Example: ``bash-1234`` → ``("bash", 1234)``

        Args:
            task_pid: Combined task and PID string.

        Returns:
            tuple: (task_name: str, pid: int).  pid is -1 if not parseable.
        """
        # Find the last hyphen that precedes digits
        idx = task_pid.rfind("-")
        if idx > 0:
            name = task_pid[:idx]
            pid_str = task_pid[idx + 1:]
            try:
                return name, int(pid_str)
            except ValueError:
                pass
        return task_pid, -1
