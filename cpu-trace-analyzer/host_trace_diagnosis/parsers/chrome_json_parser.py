"""
ChromeTraceJSONParser - Chrome Trace Format JSON Parser
=========================================================

Parses Chrome Trace Event format JSON files, which is the standard trace
format used by:
  - Chrome / Chromium tracing
  - PyTorch profiler (Chrome format export)
  - msprof trace_view.json
  - Many other profiling tools

Format overview:
  The file is a JSON object with a ``traceEvents`` array::

      {
        "traceEvents": [
          {"ph": "X", "ts": 1000, "dur": 500, "name": "kernel", "pid": 1, "tid": 2},
          {"ph": "B", "ts": 2000, "name": "begin", "pid": 1, "tid": 2},
          {"ph": "E", "ts": 2500, "name": "begin", "pid": 1, "tid": 2},
          {"ph": "M", "name": "process_name", "pid": 1, "args": {"name": "trainer"}}
        ]
      }

  Or a bare JSON array of events::

      [{"ph": "X", ...}, {"ph": "B", ...}, ...]

Streaming strategy:
  - Files < 100 MB: use standard ``json.load()`` for simplicity.
  - Files >= 100 MB: use ``ijson`` for streaming incremental parsing.
  - ``ijson`` parses JSON incrementally without loading the entire
    document into memory.

B/E event pair merging:
  Chrome trace uses Begin (``ph=B``) and End (``ph=E``) events to mark
  durations.  This parser merges matched B/E pairs into Complete (``ph=X``)
  events with a computed ``dur`` field.  Unmatched events are emitted as-is.
"""
from __future__ import annotations

import json
import logging
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

logger = logging.getLogger("host_trace_diagnosis.parsers.chrome_json")


# Optional dependency: ijson for streaming JSON parsing
try:
    import ijson  # type: ignore
    HAS_IJSON = True
except ImportError:
    HAS_IJSON = False
    logger.debug("ijson not installed; large JSON files will use standard json.load")


class ChromeTraceJSONParser(BaseParser):
    """
    Parser for Chrome Trace Event JSON format.

    Handles both ``{"traceEvents": [...]}`` and bare ``[...]`` formats.
    Merges B/E event pairs into X (complete) events.
    Detects device events from args containing ``device`` or ``stream``.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        # Stack for B/E pairing: key = (pid, tid, name), value = list of begin events
        self._begin_stack: Dict[Tuple[int, int, str], List[int]] = {}

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def parse(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse a Chrome Trace JSON file, yielding TraceEvent objects.

        For files >= ``streaming_threshold_mb``, uses ``ijson`` for
        streaming.  Otherwise uses standard ``json.load``.

        Args:
            file_path: Path to the JSON trace file.

        Yields:
            TraceEvent: Unified trace events.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the JSON is malformed.
        """
        path = self._validate_file(file_path)
        self.reset()

        use_streaming = self._requires_streaming(str(path))
        if use_streaming and not HAS_IJSON:
            logger.warning(
                "File %s is large (%.1f MB) but ijson is not installed. "
                "Falling back to json.load (may use significant memory).",
                path,
                self._get_file_size_mb(str(path)),
            )
            use_streaming = False

        if use_streaming:
            yield from self._parse_streaming(str(path))
        else:
            yield from self._parse_standard(str(path))

    def get_metadata(self) -> TraceMetadata:
        """Return metadata accumulated during parsing."""
        return self._finalize_metadata(TraceSource.CHROME_JSON, self._metadata.file_path)

    # ------------------------------------------------------------------ #
    #  Streaming parsing with ijson                                       #
    # ------------------------------------------------------------------ #

    def _parse_streaming(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Stream-parse a large JSON file using ijson.

        Tries both ``traceEvents.item`` (object wrapper) and ``item``
        (bare array) prefixes.
        """
        prefix_tried = []

        for prefix in ("traceEvents.item", "item"):
            prefix_tried.append(prefix)
            try:
                with open(file_path, "rb") as f:
                    for raw_event in ijson.items(f, prefix):
                        event = self._convert_event(raw_event)
                        if event is not None:
                            yield from self._process_event(event)
                # Successfully parsed; return
                return
            except ijson.IncompleteJSONError:
                # Wrong prefix; try the next one
                logger.debug(f"Prefix '{prefix}' failed; trying next.")
                continue
            except ijson.JSONError as e:
                logger.debug(f"ijson error with prefix '{prefix}': {e}")
                continue

        # If we get here, all prefixes failed
        raise ValueError(
            f"Failed to parse JSON with ijson. Tried prefixes: {prefix_tried}. "
            f"The file may be malformed or not a valid Chrome Trace JSON."
        )

    # ------------------------------------------------------------------ #
    #  Standard parsing with json.load                                    #
    # ------------------------------------------------------------------ #

    def _parse_standard(self, file_path: str) -> Iterator[TraceEvent]:
        """
        Parse a JSON file using standard ``json.load``.

        Suitable for files smaller than the streaming threshold.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {file_path}: {e}") from e
        except OSError as e:
            raise FileNotFoundError(f"Cannot read file {file_path}: {e}") from e

        # Handle both {"traceEvents": [...]} and [...] formats
        if isinstance(data, dict):
            events_list = data.get("traceEvents", [])
        elif isinstance(data, list):
            events_list = data
        else:
            raise ValueError(
                f"Unexpected JSON structure: expected dict with 'traceEvents' "
                f"or list, got {type(data).__name__}"
            )

        for raw_event in events_list:
            if not isinstance(raw_event, dict):
                continue
            event = self._convert_event(raw_event)
            if event is not None:
                yield from self._process_event(event)

    # ------------------------------------------------------------------ #
    #  Event conversion and processing                                    #
    # ------------------------------------------------------------------ #

    def _convert_event(self, raw: Dict[str, Any]) -> Optional[TraceEvent]:
        """
        Convert a raw Chrome trace event dict to TraceEvent.

        Handles device_id and stream_id extraction from args.

        Args:
            raw: Raw event dict from JSON.

        Returns:
            TraceEvent or None if the event is invalid.
        """
        if not isinstance(raw, dict):
            return None

        try:
            event = TraceEvent.from_chrome_trace_dict(raw)
        except (ValueError, TypeError) as e:
            logger.debug(f"Skipping invalid event: {e}")
            return None

        args = raw.get("args", {})
        if not isinstance(args, dict):
            args = {}

        # Extract device_id from args
        device_id = -1
        if "device" in args:
            try:
                device_id = int(args["device"])
            except (ValueError, TypeError):
                pass
        elif "device_id" in args:
            try:
                device_id = int(args["device_id"])
            except (ValueError, TypeError):
                pass
        event.device_id = device_id

        # Extract stream_id from args
        stream_id = -1
        if "stream" in args:
            try:
                stream_id = int(args["stream"])
            except (ValueError, TypeError):
                pass
        elif "stream_id" in args:
            try:
                stream_id = int(args["stream_id"])
            except (ValueError, TypeError):
                pass
        event.stream_id = stream_id

        # Extract cpu from args if present
        if "cpu" in args:
            try:
                event.cpu = int(args["cpu"])
            except (ValueError, TypeError):
                pass

        # Enrich args with device/stream info for downstream consumers
        event.args = args

        return event

    def _process_event(self, event: TraceEvent) -> Iterator[TraceEvent]:
        """
        Process a single event: handle B/E pairing, metadata extraction,
        and yield the (possibly transformed) event.

        For B/E pairs:
          - B (begin) events are pushed onto a stack keyed by (pid, tid, name).
          - E (end) events pop the matching B and emit an X (complete) event
            with dur = E.ts - B.ts.
          - If no matching B is found, E is emitted as-is.
          - At the end of parsing, any unmatched B events are emitted as
            zero-duration events (handled by ``flush_remaining_begins``).

        For M (metadata) events:
          - ``process_name``: register process name in metadata.
          - ``thread_name``: register thread name in metadata.
        """
        ph = event.ph

        if ph == EventPhase.BEGIN.value:
            # Push onto stack
            key = (event.pid, event.tid, event.name)
            if key not in self._begin_stack:
                self._begin_stack[key] = []
            self._begin_stack[key].append(event.ts)
            # Don't yield yet; will be yielded as X when E arrives

        elif ph == EventPhase.END.value:
            # Pop matching B and create X (complete) event with dur = end - begin
            key = (event.pid, event.tid, event.name)
            stack = self._begin_stack.get(key)
            if stack:
                begin_ts = stack.pop()
                end_ts = event.ts  # Save original E timestamp before modifying
                event.ph = EventPhase.COMPLETE.value
                event.ts = begin_ts
                event.dur = max(0, end_ts - begin_ts)
                self._update_metadata_from_event(event)
                yield event
            else:
                # No matching B; emit E as-is
                self._update_metadata_from_event(event)
                yield event

        elif ph == EventPhase.METADATA.value:
            # Handle metadata events
            self._handle_metadata_event(event)
            # Don't yield metadata events as regular trace events
            # But still count them
            self._event_count += 1

        else:
            # X, I, C, and other phases: yield directly
            self._update_metadata_from_event(event)
            yield event

    def _handle_metadata_event(self, event: TraceEvent) -> None:
        """
        Process metadata (M phase) events to extract process/thread names.

        Chrome trace metadata events:
          - ``process_name``: sets the name for a process (pid).
          - ``thread_name``: sets the name for a thread (tid) within a process.
        """
        name = event.name
        args = event.args or {}

        if name == "process_name":
            proc_name = str(args.get("name", ""))
            if proc_name:
                self._register_process(event.pid, proc_name)
        elif name == "thread_name":
            thread_name = str(args.get("name", ""))
            if thread_name:
                self._register_thread(event.tid, thread_name)

    def flush_remaining_begins(self) -> Iterator[TraceEvent]:
        """
        Emit any unmatched B (begin) events as zero-duration X events.

        Should be called after ``parse()`` is exhausted to handle events
        that never received a matching E.

        Yields:
            TraceEvent: Remaining begin events converted to X with dur=0.
        """
        for key, stack in self._begin_stack.items():
            pid, tid, name = key
            while stack:
                begin_ts = stack.pop()
                event = TraceEvent(
                    ts=begin_ts,
                    dur=0,
                    pid=pid,
                    tid=tid,
                    name=name,
                    cat=EventCategory.UNKNOWN.value,
                    ph=EventPhase.COMPLETE.value,
                )
                self._update_metadata_from_event(event)
                yield event
        self._begin_stack.clear()
