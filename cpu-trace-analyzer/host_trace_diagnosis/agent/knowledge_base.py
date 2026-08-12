"""
Knowledge Base
==============
Historical case library for pattern matching and suggestion enhancement.

Provides:
- ``KnowledgeBase``: Manages a collection of historical diagnosis cases.
  Supports similarity search based on weighted feature distance (no
  embedding required), case persistence, and suggestion lookup.

Built-in default cases cover common Host-side performance issues:
1. DataLoader CPU contention causing NPU idle.
2. Synchronous H2D copy blocking computation.
3. OMP_NUM_THREADS oversubscription.
4. Python GIL causing large kernel launch gaps.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ir.schema import FeatureVector

logger = logging.getLogger("host_trace_diagnosis.kb")


# ---------------------------------------------------------------------------
# Built-in default cases
# ---------------------------------------------------------------------------

_DEFAULT_CASES: List[Dict[str, Any]] = [
    {
        "case_id": "CASE_001",
        "title": "DataLoader竞争CPU导致NPU空闲",
        "description": (
            "DataLoader使用过多worker线程与Runtime线程竞争CPU资源，"
            "导致Runtime线程调度延迟增大，kernel提交频率下降，"
            "NPU出现大量空闲gap。"
        ),
        "features": {
            "cpu_util_avg": 88.0,
            "runqueue_avg": 14.0,
            "ctx_switch_rate": 45000,
            "sched_latency_avg_us": 6000,
            "launch_avg_gap_us": 1200,
            "correlation_score": 0.72,
        },
        "bottleneck_attribution": {
            "CPU_SCHED": 0.28,
            "DATA_LOADER": 0.35,
            "LAUNCH_GAP": 0.15,
            "OTHER": 0.05,
        },
        "diagnosis": "DataLoader worker线程与Runtime线程CPU竞争导致NPU饥饿",
        "suggestions": [
            "降低num_workers至CPU核心数的一半",
            "设置CPU affinity隔离Runtime与DataLoader",
            "使用persistent_workers减少线程创建开销",
        ],
        "severity": "HIGH",
    },
    {
        "case_id": "CASE_002",
        "title": "H2D同步拷贝阻塞计算",
        "description": (
            "训练循环中使用同步aclrtMemcpy进行Host到Device数据传输，"
            "每次拷贝阻塞CPU线程，导致计算流水线断裂，"
            "NPU在拷贝期间完全空闲。"
        ),
        "features": {
            "cpu_util_avg": 45.0,
            "h2d_bandwidth_mbs": 320.0,
            "memcpy_total_us": 2500000,
            "launch_avg_gap_us": 800,
            "correlation_score": 0.65,
        },
        "bottleneck_attribution": {
            "MEMCPY": 0.45,
            "LAUNCH_GAP": 0.10,
            "OTHER": 0.05,
        },
        "diagnosis": "同步H2D内存拷贝阻塞计算流水线",
        "suggestions": [
            "将同步拷贝替换为aclrtMemcpyAsync异步拷贝",
            "使用多stream实现拷贝与计算重叠",
            "使用pinned memory提升拷贝带宽",
        ],
        "severity": "HIGH",
    },
    {
        "case_id": "CASE_003",
        "title": "OMP_NUM_THREADS过大导致CPU oversubscription",
        "description": (
            "OMP_NUM_THREADS设置为超过物理核心数，导致OpenMP线程与"
            "Runtime线程、DataLoader worker严重oversubscription，"
            "上下文切换急剧增加，调度延迟飙升。"
        ),
        "features": {
            "cpu_util_avg": 92.0,
            "runqueue_avg": 20.0,
            "ctx_switch_rate": 80000,
            "sched_latency_avg_us": 12000,
            "sched_latency_p99_us": 45000,
            "launch_avg_gap_us": 2000,
            "correlation_score": 0.78,
        },
        "bottleneck_attribution": {
            "CPU_SCHED": 0.50,
            "LAUNCH_GAP": 0.20,
            "OTHER": 0.05,
        },
        "diagnosis": "OMP_NUM_THREADS设置过大导致CPU过度订阅",
        "suggestions": [
            "将OMP_NUM_THREADS设置为物理核心数",
            "设置OMP_PROC_BIND=CLOSE绑定线程",
            "减少DataLoader num_workers数量",
        ],
        "severity": "CRITICAL",
    },
    {
        "case_id": "CASE_004",
        "title": "Python GIL导致kernel launch间隙过大",
        "description": (
            "训练循环中存在大量Python层操作（如数据预处理、loss计算），"
            "受GIL限制无法与kernel提交并行，导致launch间隙增大，"
            "NPU利用率偏低。"
        ),
        "features": {
            "cpu_util_avg": 65.0,
            "runqueue_avg": 6.0,
            "ctx_switch_rate": 15000,
            "launch_avg_gap_us": 3500,
            "launch_count": 50000,
            "correlation_score": 0.55,
        },
        "bottleneck_attribution": {
            "LAUNCH_GAP": 0.40,
            "RUNTIME_BLOCK": 0.10,
            "OTHER": 0.08,
        },
        "diagnosis": "Python GIL限制导致kernel launch间隙过大",
        "suggestions": [
            "使用graph capture将计算图编译为静态图",
            "将Python层热点操作下沉到C++实现",
            "使用torch.compile或JIT加速",
            "减少每个step的Python开销",
        ],
        "severity": "MEDIUM",
    },
    {
        "case_id": "CASE_005",
        "title": "IO等待阻塞数据加载",
        "description": (
            "训练数据存储在慢速磁盘上，DataLoader的IO等待时间长，"
            "导致数据供给速度低于NPU消费速度，NPU频繁等待数据。"
        ),
        "features": {
            "cpu_util_avg": 35.0,
            "cpu_idle_avg": 45.0,
            "h2d_bandwidth_mbs": 200.0,
            "launch_avg_gap_us": 1500,
            "correlation_score": 0.60,
        },
        "bottleneck_attribution": {
            "IO_WAIT": 0.45,
            "DATA_LOADER": 0.20,
            "MEMCPY": 0.08,
            "OTHER": 0.05,
        },
        "diagnosis": "磁盘IO等待阻塞数据加载流水线",
        "suggestions": [
            "使用SSD或NVMe存储训练数据",
            "启用数据预取和缓存",
            "使用内存映射文件(mmap)读取",
            "将数据集转换为更高效的格式(如HDF5)",
        ],
        "severity": "MEDIUM",
    },
]


# Key metrics used for similarity matching with their weights.
_MATCH_METRICS: Dict[str, float] = {
    "cpu_util_avg": 1.0,
    "runqueue_avg": 1.0,
    "ctx_switch_rate": 0.5,
    "sched_latency_avg_us": 0.5,
    "h2d_bandwidth_mbs": 0.8,
    "launch_avg_gap_us": 0.8,
    "correlation_score": 1.0,
}

# Attribution keys used for similarity matching.
_MATCH_ATTR_KEYS: List[str] = [
    "CPU_SCHED",
    "DATA_LOADER",
    "MEMCPY",
    "LAUNCH_GAP",
    "IO_WAIT",
    "RUNTIME_BLOCK",
    "OTHER",
]


class KnowledgeBase:
    """Historical case library for pattern matching and suggestion enhancement.

    Maintains an in-memory list of cases (seeded with built-in defaults)
    and optionally persists new cases to a directory as JSON files.

    Attributes:
        case_dir: Directory for persisting cases as JSON files (may be empty).
        _cases: In-memory list of case dicts.
    """

    def __init__(self, case_dir: str = "") -> None:
        """Initialize the knowledge base.

        Loads built-in default cases, then attempts to load any additional
        cases from *case_dir* if it exists and is non-empty.

        Args:
            case_dir: Path to a directory containing historical case JSON
                files.  If the directory does not exist or is empty, only
                built-in cases are used.  No error is raised.
        """
        self.case_dir: str = case_dir
        self._cases: List[Dict[str, Any]] = list(_DEFAULT_CASES)

        if case_dir:
            self._load_from_dir(case_dir)

    # ------------------------------------------------------------------
    # Case loading
    # ------------------------------------------------------------------

    def _load_from_dir(self, case_dir: str) -> None:
        """Load additional cases from a directory of JSON files.

        Args:
            case_dir: Path to the directory.
        """
        dir_path = Path(case_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            return

        json_files = sorted(list(dir_path.glob("*.json")))
        loaded: int = 0

        for json_file in json_files:
            try:
                with open(json_file, "r", encoding="utf-8") as fh:
                    case = json.load(fh)
                if isinstance(case, dict) and "case_id" in case:
                    self._cases.append(case)
                    loaded += 1
                elif isinstance(case, list):
                    for c in case:
                        if isinstance(c, dict) and "case_id" in c:
                            self._cases.append(c)
                            loaded += 1
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load case file %s: %s", json_file, exc)

        if loaded:
            logger.info("Loaded %d additional cases from %s", loaded, dir_path)

    # ------------------------------------------------------------------
    # Similarity search
    # ------------------------------------------------------------------

    def search_similar(
        self, fv: FeatureVector, top_k: int = 3
    ) -> List[dict]:
        """Find the most similar historical cases to *fv*.

        Uses weighted feature distance (no embedding required).
        Distance is computed as the normalized absolute difference of
        key scalar metrics plus the L1 distance of bottleneck attribution
        ratios.

        Args:
            fv: FeatureVector to match against.
            top_k: Maximum number of similar cases to return.

        Returns:
            List of case dicts sorted by similarity (most similar first).
            Each dict is augmented with a ``similarity_score`` field
            (0-1, higher is more similar).  Returns an empty list if
            no cases are available.
        """
        if not self._cases or top_k <= 0:
            return []

        scored: List[tuple] = []
        for case in self._cases:
            distance = self._compute_distance(fv, case)
            # Convert distance to similarity score (0-1).
            similarity = 1.0 / (1.0 + distance)
            scored.append((similarity, case))

        scored.sort(key=lambda x: x[0], reverse=True)

        results: List[dict] = []
        for similarity, case in scored[:top_k]:
            result = dict(case)
            result["similarity_score"] = round(similarity, 4)
            results.append(result)

        return results

    def _compute_distance(
        self, fv: FeatureVector, case: Dict[str, Any]
    ) -> float:
        """Compute weighted feature distance between *fv* and *case*.

        Args:
            fv: FeatureVector to compare.
            case: Historical case dict.

        Returns:
            Non-negative float distance. Lower means more similar.
        """
        case_features: Dict[str, Any] = case.get("features", {})
        distance: float = 0.0

        # Scalar metric distance
        for metric, weight in _MATCH_METRICS.items():
            fv_val = float(fv.scalars.get(metric, 0.0))
            case_val = float(case_features.get(metric, 0.0))
            diff = abs(fv_val - case_val)
            # Normalize by the larger of the two values (or 1.0 to avoid div-by-zero).
            ref = max(abs(fv_val), abs(case_val), 1.0)
            normalized = diff / ref
            distance += weight * normalized

        # Bottleneck attribution distance
        case_attr: Dict[str, Any] = case.get("bottleneck_attribution", {})
        for attr_key in _MATCH_ATTR_KEYS:
            fv_attr = float(fv.bottleneck_attribution.get(attr_key, 0.0))
            case_attr_val = float(case_attr.get(attr_key, 0.0))
            distance += abs(fv_attr - case_attr_val)

        # Correlation score distance (direct field)
        fv_corr = float(fv.correlation_score)
        case_corr = float(case_features.get("correlation_score", 0.0))
        distance += abs(fv_corr - case_corr)

        return distance

    # ------------------------------------------------------------------
    # Case management
    # ------------------------------------------------------------------

    def add_case(self, case: dict) -> None:
        """Add a new case to the knowledge base.

        Persists the case as a JSON file in *case_dir* (if configured)
        and also adds it to the in-memory list.

        Args:
            case: Case dict.  Should contain at least a ``case_id``;
                if missing, one is auto-generated.
        """
        case_id = case.get("case_id", "")
        if not case_id:
            case_id = f"CASE_{len(self._cases) + 1:03d}"
            case["case_id"] = case_id

        self._cases.append(case)

        if self.case_dir:
            case_path = Path(self.case_dir) / f"{case_id}.json"
            try:
                case_path.parent.mkdir(parents=True, exist_ok=True)
                with open(case_path, "w", encoding="utf-8") as fh:
                    json.dump(case, fh, ensure_ascii=False, indent=2)
                logger.info("Persisted case %s to %s", case_id, case_path)
            except OSError as exc:
                logger.warning("Failed to persist case %s: %s", case_id, exc)
        else:
            logger.info("Added case %s to in-memory knowledge base", case_id)

    # ------------------------------------------------------------------
    # Suggestion lookup
    # ------------------------------------------------------------------

    def get_suggestion(self, diagnosis: str) -> Optional[str]:
        """Look up an enhancement suggestion for a given diagnosis.

        Searches built-in and loaded cases for one whose diagnosis text
        is similar to *diagnosis*.  If found, returns the first suggestion
        from that case.

        Args:
            diagnosis: Diagnosis text to match.

        Returns:
            Suggestion string if a similar case is found, otherwise ``None``.
        """
        if not diagnosis or not self._cases:
            return None

        diagnosis_lower = diagnosis.lower()

        # Exact substring match first
        for case in self._cases:
            case_diag = str(case.get("diagnosis", "")).lower()
            if case_diag and case_diag in diagnosis_lower:
                suggestions = case.get("suggestions", [])
                if suggestions:
                    first = suggestions[0]
                    if isinstance(first, dict):
                        return str(first.get("text", ""))
                    return str(first)

        # Keyword-based match
        keywords_map: Dict[str, str] = {
            "datloader": "降低num_workers至CPU核心数的一半，设置CPU affinity隔离Runtime与DataLoader",
            "data_loader": "降低num_workers至CPU核心数的一半，设置CPU affinity隔离Runtime与DataLoader",
            "拷贝": "将同步拷贝替换为aclrtMemcpyAsync异步拷贝，使用多stream实现重叠",
            "memcpy": "将同步拷贝替换为aclrtMemcpyAsync异步拷贝，使用多stream实现重叠",
            "omp": "将OMP_NUM_THREADS设置为物理核心数，设置OMP_PROC_BIND=CLOSE",
            "gil": "使用graph capture将计算图编译为静态图，减少Python层开销",
            "launch": "使用graph capture减少launch开销，批量化操作减少launch次数",
            "io": "使用SSD或NVMe存储训练数据，启用数据预取和缓存",
            "sched": "提升Runtime线程调度优先级，使用SCHED_FIFO策略",
        }

        for keyword, suggestion in keywords_map.items():
            if keyword in diagnosis_lower:
                return suggestion

        return None
