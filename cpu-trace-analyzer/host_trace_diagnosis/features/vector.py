"""
Feature Vector Builder
=======================
Assembles all extracted features into a :class:`FeatureVector` and provides
deserialization from dict/JSON for loading saved feature vectors.

The :class:`FeatureVectorBuilder` serves as the final assembly point in the
feature extraction pipeline:

    reader -> HostMetricsExtractor    -> scalars, timelines
           -> GapScanner              -> gaps
           -> CorrelationEngine       -> gap_host_pairs, attribution, corr_score
           -> FeatureVectorBuilder    -> FeatureVector (final output)

The builder also handles:
  - Converting :class:`GapRecord` objects to dicts for ``top_gaps``.
  - Building the ``critical_path`` from top gaps and their host events.
  - Serializing :class:`TraceMetadata` into the feature vector's metadata dict.
  - Tolerant deserialization (``from_dict``) that handles missing or
    extra fields gracefully.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ir.schema import FeatureVector, GapRecord, TraceMetadata

logger = logging.getLogger("host_trace_diagnosis.features.vector")


class FeatureVectorBuilder:
    """Assemble feature extraction outputs into a :class:`FeatureVector`.

    This class is stateless; each method call is independent. It serves
    as a namespace for feature vector construction and deserialization
    logic.
    """

    # Number of top gaps to include in top_gaps and critical_path
    DEFAULT_TOP_N: int = 20
    CRITICAL_PATH_N: int = 5

    # ------------------------------------------------------------------ #
    #  Build (serialize)                                                  #
    # ------------------------------------------------------------------ #

    def build(
        self,
        scalars: Dict[str, Any],
        timelines: Dict[str, List[Tuple[int, float]]],
        gaps: List[GapRecord],
        gap_host_pairs: List[Dict[str, Any]],
        attribution: Dict[str, float],
        corr_score: float,
        metadata: TraceMetadata,
    ) -> FeatureVector:
        """Assemble all features into a :class:`FeatureVector`.

        Parameters
        ----------
        scalars : dict
            A-class scalar metrics from :class:`HostMetricsExtractor`.
        timelines : dict
            B-class timeline features from :class:`HostMetricsExtractor`.
        gaps : list of GapRecord
            Device idle gaps from :class:`GapScanner`. The top-N gaps
            (by duration) are converted to dicts and stored in
            ``top_gaps``.
        gap_host_pairs : list of dict
            Gap-host operation pairs from :class:`CorrelationEngine`.
        attribution : dict
            Bottleneck attribution distribution from
            :class:`CorrelationEngine`.
        corr_score : float
            Host-Device correlation score from :class:`CorrelationEngine`.
        metadata : TraceMetadata
            Trace metadata from the IR reader.

        Returns
        -------
        FeatureVector
            The fully assembled feature vector.
        """
        # Convert top-N gaps to dicts
        top_gaps = self._gaps_to_dicts(gaps, self.DEFAULT_TOP_N)

        # Build critical path from top gaps with host events
        critical_path = self._build_critical_path(gaps, self.CRITICAL_PATH_N)

        # Serialize metadata
        metadata_dict = self._metadata_to_dict(metadata)

        fv = FeatureVector(
            scalars=dict(scalars),
            timelines=self._normalize_timelines(timelines),
            gap_host_op_pairs=list(gap_host_pairs),
            bottleneck_attribution=dict(attribution),
            correlation_score=corr_score,
            critical_path=critical_path,
            top_gaps=top_gaps,
            metadata=metadata_dict,
        )

        logger.info(
            "FeatureVector built: %d scalars, %d timelines, %d top_gaps, "
            "%d gap_host_pairs, corr_score=%.3f",
            len(fv.scalars),
            len(fv.timelines),
            len(fv.top_gaps),
            len(fv.gap_host_op_pairs),
            fv.correlation_score,
        )

        return fv

    # ------------------------------------------------------------------ #
    #  From dict (deserialize)                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> FeatureVector:
        """Deserialize a :class:`FeatureVector` from a dict.

        This method is tolerant of missing or extra fields, making it
        suitable for loading feature vectors saved as JSON that may
        have been produced by different versions of the extractor.

        Parameters
        ----------
        d : dict
            Dictionary representation of a FeatureVector (e.g. from
            ``json.load``).

        Returns
        -------
        FeatureVector
            The deserialized feature vector.
        """
        # Extract each field with a safe default
        scalars = cls._safe_get(d, "scalars", default={})
        timelines = cls._safe_get(d, "timelines", default={})
        gap_host_op_pairs = cls._safe_get(d, "gap_host_op_pairs", default=[])
        bottleneck_attribution = cls._safe_get(d, "bottleneck_attribution", default={})
        correlation_score = cls._safe_get(d, "correlation_score", default=0.0)
        critical_path = cls._safe_get(d, "critical_path", default=[])
        top_gaps = cls._safe_get(d, "top_gaps", default=[])
        metadata = cls._safe_get(d, "metadata", default={})

        # Normalize scalar values to float where possible
        scalars = cls._normalize_scalars(scalars)

        # Normalize timeline tuples: JSON deserialization may produce
        # lists [ts, value] instead of tuples (ts, value)
        timelines = cls._normalize_timelines(timelines)

        # Ensure correlation_score is a float
        try:
            correlation_score = float(correlation_score)
        except (ValueError, TypeError):
            correlation_score = 0.0

        fv = FeatureVector(
            scalars=scalars,
            timelines=timelines,
            gap_host_op_pairs=gap_host_op_pairs,
            bottleneck_attribution=bottleneck_attribution,
            correlation_score=correlation_score,
            critical_path=critical_path,
            top_gaps=top_gaps,
            metadata=metadata,
        )

        logger.debug(
            "FeatureVector loaded from dict: %d scalars, %d top_gaps",
            len(fv.scalars),
            len(fv.top_gaps),
        )

        return fv

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _gaps_to_dicts(gaps: List[GapRecord], top_n: int) -> List[Dict[str, Any]]:
        """Convert the top-N GapRecords to dicts, sorted by duration.

        Parameters
        ----------
        gaps : list of GapRecord
            Device idle gaps.
        top_n : int
            Maximum number of gaps to include.

        Returns
        -------
        list of dict
            Top-N gaps as dicts, sorted by ``gap_dur`` descending.
        """
        # Sort by gap_dur descending
        sorted_gaps = sorted(gaps, key=lambda g: g.gap_dur, reverse=True)
        top = sorted_gaps[:top_n]

        result: List[Dict[str, Any]] = []
        for gap in top:
            result.append({
                "gap_start": gap.gap_start,
                "gap_end": gap.gap_end,
                "gap_dur": gap.gap_dur,
                "device_id": gap.device_id,
                "stream_id": gap.stream_id,
                "prev_kernel_name": gap.prev_kernel_name,
                "next_kernel_name": gap.next_kernel_name,
                "host_events": gap.host_events,
                "attribution": gap.attribution,
                "attribution_confidence": gap.attribution_confidence,
            })

        return result

    @staticmethod
    def _build_critical_path(
        gaps: List[GapRecord], top_n: int
    ) -> List[Dict[str, Any]]:
        """Build the critical path from the top-N gaps.

        The critical path is a causal chain of the most impactful gaps
        with their associated host events and attribution info.

        Parameters
        ----------
        gaps : list of GapRecord
            Device idle gaps.
        top_n : int
            Number of gaps to include.

        Returns
        -------
        list of dict
            Critical path entries.
        """
        sorted_gaps = sorted(gaps, key=lambda g: g.gap_dur, reverse=True)
        top = sorted_gaps[:top_n]

        critical_path: List[Dict[str, Any]] = []
        for gap in top:
            entry: Dict[str, Any] = {
                "gap_start": gap.gap_start,
                "gap_end": gap.gap_end,
                "gap_dur": gap.gap_dur,
                "device_id": gap.device_id,
                "stream_id": gap.stream_id,
                "prev_kernel": gap.prev_kernel_name,
                "next_kernel": gap.next_kernel_name,
                "attribution": gap.attribution,
                "attribution_confidence": gap.attribution_confidence,
                "host_events": gap.host_events,
            }
            critical_path.append(entry)

        return critical_path

    @staticmethod
    def _metadata_to_dict(metadata: TraceMetadata) -> Dict[str, Any]:
        """Serialize :class:`TraceMetadata` to a dict for storage.

        Parameters
        ----------
        metadata : TraceMetadata
            Trace metadata object.

        Returns
        -------
        dict
            Metadata as a serializable dict.
        """
        try:
            return metadata.to_dict()
        except Exception:
            # Fallback: manually extract key fields
            return {
                "source": str(metadata.source),
                "file_path": metadata.file_path,
                "total_events": metadata.total_events,
                "ts_start": metadata.ts_start,
                "ts_end": metadata.ts_end,
                "duration_us": metadata.duration_us,
                "devices": list(metadata.devices) if metadata.devices else [],
                "cpu_cores": metadata.cpu_cores,
            }

    @staticmethod
    def _normalize_timelines(
        timelines: Dict[str, Any],
    ) -> Dict[str, List[Tuple[int, float]]]:
        """Normalize timeline values to lists of (int, float) tuples.

        When deserializing from JSON, timeline values may be lists of
        ``[ts, value]`` lists instead of ``(ts, value)`` tuples. This
        method converts them to the expected tuple format and ensures
        proper types.

        Parameters
        ----------
        timelines : dict
            Timeline dict, potentially with list-of-lists values.

        Returns
        -------
        dict
            Normalized timeline dict with list-of-tuples values.
        """
        normalized: Dict[str, List[Tuple[int, float]]] = {}

        for key, value in timelines.items():
            if not isinstance(value, list):
                # Non-list value; wrap as single-entry timeline
                normalized[key] = [(0, float(value))] if _try_float(value) is not None else []
                continue

            points: List[Tuple[int, float]] = []
            for point in value:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    ts = _try_int(point[0])
                    val = _try_float(point[1])
                    if ts is not None and val is not None:
                        points.append((ts, val))
                elif isinstance(point, (int, float)):
                    # Single value without timestamp
                    points.append((0, float(point)))
                # Skip malformed entries

            normalized[key] = points

        return normalized

    @staticmethod
    def _normalize_scalars(scalars: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize scalar values, converting numeric strings to floats.

        Preserves non-scalar values (e.g. ``cpu_util_per_core`` dict)
        as-is.

        Parameters
        ----------
        scalars : dict
            Scalar metrics dict.

        Returns
        -------
        dict
            Normalized scalars dict.
        """
        normalized: Dict[str, Any] = {}

        for key, value in scalars.items():
            if isinstance(value, (int, float)):
                normalized[key] = float(value)
            elif isinstance(value, str):
                # Try to convert numeric strings
                float_val = _try_float(value)
                if float_val is not None:
                    normalized[key] = float_val
                else:
                    normalized[key] = value
            elif isinstance(value, dict):
                # Preserve dicts like cpu_util_per_core
                normalized[key] = value
            elif isinstance(value, list):
                # Preserve lists
                normalized[key] = value
            else:
                normalized[key] = value

        return normalized

    @staticmethod
    def _safe_get(d: Dict[str, Any], key: str, default: Any = None) -> Any:
        """Safely get a value from a dict, returning default if missing.

        Parameters
        ----------
        d : dict
            Source dictionary.
        key : str
            Key to look up.
        default : Any
            Default value if key is missing or value is None.

        Returns
        -------
        Any
            The value or default.
        """
        value = d.get(key, default)
        if value is None:
            return default
        return value


# ------------------------------------------------------------------ #
#  Module-level helper functions                                      #
# ------------------------------------------------------------------ #

def _try_float(value: Any) -> Optional[float]:
    """Try to convert a value to float, returning None on failure.

    Parameters
    ----------
    value : Any
        Value to convert.

    Returns
    -------
    float or None
        Converted float, or None if conversion fails.
    """
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _try_int(value: Any) -> Optional[int]:
    """Try to convert a value to int, returning None on failure.

    Parameters
    ----------
    value : Any
        Value to convert.

    Returns
    -------
    int or None
        Converted int, or None if conversion fails.
    """
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
