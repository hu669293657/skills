"""
Feature extraction layer.

Provides extractors for Host-side metrics, Device idle gap scanning,
Host<->Device correlation, auxiliary timeline features, and feature
vector assembly.
"""
from .host_metrics import HostMetricsExtractor
from .gap_scanner import GapScanner
from .correlation import CorrelationEngine
from .timeline_features import TimelineFeatureExtractor
from .vector import FeatureVectorBuilder

__all__ = [
    "HostMetricsExtractor",
    "GapScanner",
    "CorrelationEngine",
    "TimelineFeatureExtractor",
    "FeatureVectorBuilder",
]
