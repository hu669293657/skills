"""
PerfettoBridge - Perfetto TraceProcessor SQL Bridge
=====================================================

Provides a Python interface to Perfetto's TraceProcessor for executing
SQL queries directly on Perfetto trace files (``.perfetto-trace`` or
``.pb`` format).

Perfetto TraceProcessor is a SQL engine that can parse Perfetto trace
protobuf files and expose their contents as virtual SQL tables.  This
module wraps the Python API to provide:

  - ``load_trace(trace_path)``: Load a trace file into TraceProcessor.
  - ``query(sql)``: Execute an arbitrary SQL query.
  - Predefined query methods for common analysis tasks:
    - ``query_sched_events()``: Scheduler events (sched_switch, etc.)
    - ``query_thread_states()``: Thread running/sleeping states.
    - ``query_cpu_frequency()``: CPU frequency changes.

Requirements:
  The Perfetto Python API must be installed::

      pip install perfetto

  Or from source::

      git clone https://github.com/google/perfetto
      cd perfetto/python
      pip install .

If Perfetto is not available, all methods raise a clear exception with
installation instructions.

Use cases in Host Trace Diagnosis:
  - Cross-validate scheduler events parsed from ftrace.
  - Query thread states to compute CPU utilization metrics.
  - Analyze CPU frequency scaling and its impact on performance.
  - Correlate Host-side scheduling with NPU kernel execution gaps.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("host_trace_diagnosis.ir.perfetto_bridge")


# Optional dependency: perfetto TraceProcessor
try:
    from perfetto.trace_processor import TraceProcessor  # type: ignore
    HAS_PERFETTO = True
except ImportError:
    HAS_PERFETTO = False
    TraceProcessor = None  # type: ignore
    logger.debug(
        "perfetto package not installed. PerfettoBridge will raise "
        "RuntimeError when used. Install with: pip install perfetto"
    )


class PerfettoUnavailableError(RuntimeError):
    """
    Raised when Perfetto TraceProcessor is not available.

    Contains installation instructions in the error message.
    """

    def __init__(self, message: str = "") -> None:
        msg = message or (
            "Perfetto TraceProcessor is not available.\n"
            "Install the Perfetto Python package:\n"
            "    pip install perfetto\n"
            "Or from source:\n"
            "    git clone https://github.com/google/perfetto\n"
            "    cd perfetto/python\n"
            "    pip install .\n"
            "See: https://perfetto.dev/docs/quickstart/trace-analysis"
        )
        super().__init__(msg)


class PerfettoBridge:
    """
    Bridge to Perfetto TraceProcessor for SQL-based trace analysis.

    Wraps the Perfetto Python API to provide a simplified interface for
    common trace analysis queries.  All methods raise
    ``PerfettoUnavailableError`` if the ``perfetto`` package is not
    installed.

    Usage::

        bridge = PerfettoBridge(config)
        bridge.load_trace("trace.perfetto-trace")
        results = bridge.query("SELECT name, dur FROM slice LIMIT 10")
        sched = bridge.query_sched_events()
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        Initialize the Perfetto bridge.

        Args:
            config: Configuration dict.  Expected keys:
                - ``perfetto_path`` (str): Path to perfetto binary
                  (for binary mode).  Leave empty to use Python API.
        """
        self.config: Dict[str, Any] = config or {}
        self.perfetto_path: str = self.config.get("perfetto_path", "")
        self._tp: Optional[Any] = None  # TraceProcessor instance
        self._loaded_trace: str = ""

    # ------------------------------------------------------------------ #
    #  Availability check                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def is_available() -> bool:
        """
        Check if Perfetto TraceProcessor is available.

        Returns:
            bool: True if the ``perfetto`` Python package is installed.
        """
        return HAS_PERFETTO

    def _ensure_available(self) -> None:
        """
        Ensure Perfetto is available; raise if not.

        Raises:
            PerfettoUnavailableError: If perfetto is not installed.
        """
        if not HAS_PERFETTO:
            raise PerfettoUnavailableError()

    # ------------------------------------------------------------------ #
    #  Trace loading                                                      #
    # ------------------------------------------------------------------ #

    def load_trace(self, trace_path: str) -> None:
        """
        Load a Perfetto trace file into TraceProcessor.

        After loading, SQL queries can be executed against the trace data.

        Args:
            trace_path: Path to the Perfetto trace file (``.perfetto-trace``
                or ``.pb`` format).

        Raises:
            PerfettoUnavailableError: If perfetto is not installed.
            FileNotFoundError: If the trace file does not exist.
            RuntimeError: If the trace cannot be loaded.
        """
        self._ensure_available()

        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"Trace file not found: {trace_path}")

        logger.info(f"Loading Perfetto trace: {trace_path}")

        # Close any previously loaded trace
        if self._tp is not None:
            try:
                self._tp.close()
            except Exception:
                pass
            self._tp = None

        try:
            # Use http mode if perfetto_path points to a server,
            # otherwise use file mode
            if self.perfetto_path and self.perfetto_path.startswith("http"):
                self._tp = TraceProcessor(addr=self.perfetto_path)
                # Load the trace file via the HTTP API
                with open(trace_path, "rb") as f:
                    self._tp.query(f.read())
            else:
                # Direct file loading (most common mode)
                self._tp = TraceProcessor(trace=trace_path)

            self._loaded_trace = trace_path
            logger.info(f"Perfetto trace loaded successfully: {trace_path}")

        except Exception as e:
            raise RuntimeError(
                f"Failed to load Perfetto trace '{trace_path}': {e}"
            ) from e

    # ------------------------------------------------------------------ #
    #  Query execution                                                    #
    # ------------------------------------------------------------------ #

    def query(self, sql: str) -> List[Dict[str, Any]]:
        """
        Execute a SQL query against the loaded trace.

        Args:
            sql: SQL query string.  See Perfetto documentation for
                available tables and schema:
                https://perfetto.dev/docs/analysis/sql-tables

        Returns:
            List[Dict[str, Any]]: Query results as a list of dicts,
            where each dict maps column names to values.

        Raises:
            PerfettoUnavailableError: If perfetto is not installed.
            RuntimeError: If no trace is loaded or the query fails.
        """
        self._ensure_available()

        if self._tp is None:
            raise RuntimeError(
                "No trace loaded. Call load_trace() before querying."
            )

        logger.debug(f"Executing SQL: {sql[:200]}")

        try:
            result = self._tp.query(sql)
            # Convert QueryResultIterator to list of dicts
            columns = result.GetColumnNames()
            rows = []
            for row in result:
                row_dict = {}
                for i, col_name in enumerate(columns):
                    row_dict[col_name] = row[i]
                rows.append(row_dict)
            logger.debug(f"Query returned {len(rows)} rows")
            return rows

        except Exception as e:
            raise RuntimeError(f"SQL query failed: {e}\nQuery: {sql}") from e

    # ------------------------------------------------------------------ #
    #  Predefined queries                                                 #
    # ------------------------------------------------------------------ #

    def query_sched_events(
        self,
        cpu: Optional[int] = None,
        ts_start: Optional[int] = None,
        ts_end: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query scheduler events (sched_switch) from the trace.

        Returns context switch events with prev/next task information.

        Args:
            cpu: Filter by CPU core (None for all cores).
            ts_start: Start timestamp filter in nanoseconds (None for no filter).
            ts_end: End timestamp filter in nanoseconds (None for no filter).

        Returns:
            List of dicts with keys: ts, cpu, prev_pid, prev_comm,
            prev_state, next_pid, next_comm, next_prio.
        """
        conditions = []
        if cpu is not None:
            conditions.append(f"cpu = {int(cpu)}")
        if ts_start is not None:
            conditions.append(f"ts >= {int(ts_start)}")
        if ts_end is not None:
            conditions.append(f"ts <= {int(ts_end)}")

        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        sql = f"""
            SELECT
                ts,
                cpu,
                prev_pid AS prev_pid,
                prev_comm AS prev_comm,
                prev_state AS prev_state,
                next_pid AS next_pid,
                next_comm AS next_comm,
                next_prio AS next_prio
            FROM sched_switch
            {where_clause}
            ORDER BY ts
        """
        return self.query(sql)

    def query_thread_states(
        self,
        tid: Optional[int] = None,
        cpu: Optional[int] = None,
        ts_start: Optional[int] = None,
        ts_end: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query thread states (running, runnable, sleeping, etc.).

        This is a derived view that computes thread states from
        sched_switch and sched_wakeup events.

        Args:
            tid: Filter by thread ID (None for all threads).
            cpu: Filter by CPU core (None for all cores).
            ts_start: Start timestamp filter in nanoseconds.
            ts_end: End timestamp filter in nanoseconds.

        Returns:
            List of dicts with keys: ts, dur, cpu, tid, state, name.
        """
        conditions = []
        if tid is not None:
            conditions.append(f"tid = {int(tid)}")
        if cpu is not None:
            conditions.append(f"cpu = {int(cpu)}")
        if ts_start is not None:
            conditions.append(f"ts >= {int(ts_start)}")
        if ts_end is not None:
            conditions.append(f"ts <= {int(ts_end)}")

        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        sql = f"""
            SELECT
                ts,
                dur,
                cpu,
                tid,
                state,
                name
            FROM thread_state
            {where_clause}
            ORDER BY ts
        """
        return self.query(sql)

    def query_cpu_frequency(
        self,
        cpu: Optional[int] = None,
        ts_start: Optional[int] = None,
        ts_end: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query CPU frequency scaling events.

        Returns frequency change events for CPU cores, useful for
        analyzing thermal throttling or frequency scaling effects.

        Args:
            cpu: Filter by CPU core (None for all cores).
            ts_start: Start timestamp filter in nanoseconds.
            ts_end: End timestamp filter in nanoseconds.

        Returns:
            List of dicts with keys: ts, cpu, freq, id.
        """
        conditions = []
        if cpu is not None:
            conditions.append(f"cpu = {int(cpu)}")
        if ts_start is not None:
            conditions.append(f"ts >= {int(ts_start)}")
        if ts_end is not None:
            conditions.append(f"ts <= {int(ts_end)}")

        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        sql = f"""
            SELECT
                ts,
                cpu,
                freq,
                id
            FROM cpu_frequency_counters
            {where_clause}
            ORDER BY ts
        """
        return self.query(sql)

    def query_cpu_utilization(
        self,
        cpu: Optional[int] = None,
        window_ns: int = 1_000_000_000,
    ) -> List[Dict[str, Any]]:
        """
        Query CPU utilization aggregated over time windows.

        Computes the percentage of time each CPU was running a task
        within each time window.

        Args:
            cpu: Filter by CPU core (None for all cores).
            window_ns: Aggregation window size in nanoseconds
                (default 1 second).

        Returns:
            List of dicts with keys: window_start, cpu, utilization_pct.
        """
        cpu_filter = f" AND cpu = {int(cpu)}" if cpu is not None else ""

        sql = f"""
            SELECT
                (ts / {int(window_ns)}) * {int(window_ns)} AS window_start,
                cpu,
                CAST(SUM(dur) AS FLOAT) / {int(window_ns)} * 100.0 AS utilization_pct
            FROM sched_slice
            WHERE dur > 0{cpu_filter}
            GROUP BY window_start, cpu
            ORDER BY window_start, cpu
        """
        return self.query(sql)

    def query_slice_names(self) -> List[str]:
        """
        Query all unique slice (event) names in the trace.

        Useful for discovering what types of events are available.

        Returns:
            List of unique event name strings.
        """
        sql = "SELECT DISTINCT name FROM slice ORDER BY name"
        results = self.query(sql)
        return [r["name"] for r in results]

    def get_table_names(self) -> List[str]:
        """
        Get all available SQL table names in the loaded trace.

        Returns:
            List of table name strings.
        """
        sql = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        results = self.query(sql)
        return [r["name"] for r in results]

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Close the TraceProcessor and release resources."""
        if self._tp is not None:
            try:
                self._tp.close()
            except Exception as e:
                logger.debug(f"Error closing TraceProcessor: {e}")
            finally:
                self._tp = None
                self._loaded_trace = ""

    def __enter__(self) -> "PerfettoBridge":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self):
        """Ensure resources are cleaned up."""
        self.close()
