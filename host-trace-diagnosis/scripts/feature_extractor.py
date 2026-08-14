#!/usr/bin/env python3
"""
Host Trace 特征提取器
======================
功能：从结构化事件流中提取性能指标
输入：structured_events.jsonl（由 trace_preprocessor.py 生成）
输出：host_metrics.json（Agent 可解读的指标 JSON）

使用方法：
    python feature_extractor.py <input.jsonl> [--output <output.json>]
        [--cpu-count 64] [--workload training] [--parallel TP8]
"""

import sys
import os
import json
import argparse
import statistics
from collections import defaultdict
from typing import Dict, List, Optional, Any, Generator

# ============================================================
# 事件流读取器
# ============================================================

def read_events(file_path: str) -> Generator[Dict, None, None]:
    """逐行读取 JSONL 事件文件"""
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ============================================================
# 指标计算器
# ============================================================

class FeatureExtractor:
    """从事件流中提取性能指标"""

    def __init__(self, cpu_count: int = 0, workload: str = 'unknown',
                 parallel: str = 'unknown'):
        self.cpu_count = cpu_count
        self.workload = workload
        self.parallel = parallel

        # 累加器
        self.window_summaries = []
        self.key_events = []
        self.npu_events = []

        # 调度统计
        self.sched_switches = []
        self.sched_wakeups = []
        self.sched_wakings = []

        # 时间范围
        self.first_ts = None
        self.last_ts = None

    def process(self, event: Dict):
        """处理单个事件"""
        event_type = event.get('type', '')

        # 更新时间范围
        ts = event.get('ts', 0)
        if ts:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts

        if event_type == 'window_summary':
            self.window_summaries.append(event)
        elif event_type == 'key_event':
            self.key_events.append(event)
        elif event_type in ('sched_switch',):
            self.sched_switches.append(event)
        elif event_type in ('sched_wakeup', 'sched_waking'):
            if event_type == 'sched_wakeup':
                self.sched_wakeups.append(event)
            else:
                self.sched_wakings.append(event)

        # 收集 NPU 事件
        npu_keywords = ['acl:', 'ge:', 'aclmdl', 'aclrt']
        if any(kw in event_type for kw in npu_keywords):
            self.npu_events.append(event)

    def extract_all(self) -> Dict:
        """提取所有指标"""
        duration_s = 0
        if self.first_ts and self.last_ts:
            duration_s = self.last_ts - self.first_ts

        # 如果没有检测到 CPU 数量，从事件推断
        if self.cpu_count == 0:
            cpu_set = set()
            for sw in self.sched_switches[:10000]:
                if 'cpu' in sw:
                    cpu_set.add(sw['cpu'])
            self.cpu_count = max(len(cpu_set), 1)

        metrics = {
            'metadata': {
                'duration_s': duration_s,
                'cpu_count': self.cpu_count,
                'workload_type': self.workload,
                'parallel_config': self.parallel,
                'total_events': len(self.sched_switches) + len(self.sched_wakeups),
                'total_windows': len(self.window_summaries),
                'total_key_events': len(self.key_events),
            },
            'cpu': self._extract_cpu_metrics(duration_s),
            'sched': self._extract_sched_metrics(duration_s),
            'numa': self._extract_numa_metrics(),
            'memory': self._extract_memory_metrics(),
            'io': self._extract_io_metrics(duration_s),
            'runtime': self._extract_runtime_metrics(),
            'host_npu': self._extract_host_npu_metrics(duration_s),
            'timeline_windows': self._extract_timeline(),
        }

        return metrics

    def _extract_cpu_metrics(self, duration_s: float) -> Dict:
        """提取 CPU 指标"""
        if not self.window_summaries:
            return self._empty_metrics(['cpu_util_avg', 'cpu_util_max',
                                        'cpu_balance', 'context_switch_rate'])

        # 从窗口统计计算
        cs_rates = []
        cpu_distributions = []

        for w in self.window_summaries:
            cs_count = w.get('sched_switch_count', 0)
            window_s = w.get('window_ms', 100) / 1000
            cs_rate = cs_count / max(window_s, 0.001)
            cs_rates.append(cs_rate)

            if 'cpu_distribution' in w:
                cpu_distributions.append(w['cpu_distribution'])

        # CPU 利用率（从 context switch 频率近似）
        # 高 cs_rate 意味着高 CPU 活动
        avg_cs_rate = statistics.mean(cs_rates) if cs_rates else 0
        # 近似: cs_rate / (cpu_count * 1000) * 100, capped at 100
        cpu_util_avg = min(avg_cs_rate / (self.cpu_count * 1000) * 100, 100)

        # CPU 均衡度（从 cpu_distribution 计算）
        cpu_balance = 0
        per_core_utils = []
        if cpu_distributions:
            # 计算每个 CPU 的平均负载
            core_loads = defaultdict(list)
            for dist in cpu_distributions:
                total = sum(dist.values()) or 1
                for cpu, count in dist.items():
                    core_loads[cpu].append(count / total)

            per_core_utils = [statistics.mean(loads) * 100 * self.cpu_count
                              for loads in core_loads.values()]
            if len(per_core_utils) > 1:
                mean_util = statistics.mean(per_core_utils)
                if mean_util > 0:
                    std_util = statistics.stdev(per_core_utils)
                    cpu_balance = std_util / mean_util

        return {
            'cpu_util_avg': round(cpu_util_avg, 2),
            'cpu_util_max': round(max(per_core_utils) if per_core_utils else 0, 2),
            'cpu_balance': round(cpu_balance, 4),
            'context_switch_rate': round(avg_cs_rate, 0),
            'per_core_util': [round(u, 2) for u in per_core_utils],
        }

    def _extract_sched_metrics(self, duration_s: float) -> Dict:
        """提取调度指标"""
        if not self.window_summaries:
            return self._empty_metrics(['runqueue_avg', 'runqueue_max',
                                        'sched_latency_avg_ms', 'sched_latency_p99_ms'])

        # runqueue 从窗口统计
        runnable_values = [w.get('runnable_count', 0) for w in self.window_summaries]

        runqueue_avg = statistics.mean(runnable_values) if runnable_values else 0
        runqueue_max = max(runnable_values) if runnable_values else 0

        # 调度延迟从 key_events 计算
        sched_latencies = []
        for ke in self.key_events:
            if 'latency_ms' in ke:
                sched_latencies.append(ke['latency_ms'])

        # 如果有 wakeup/switch 配对数据，计算延迟
        if not sched_latencies and self.sched_wakings and self.sched_switches:
            sched_latencies = self._calc_sched_latency_from_events()

        result = {
            'runqueue_avg': round(runqueue_avg, 2),
            'runqueue_max': runqueue_max,
            'runqueue_pressure': round(runqueue_avg / max(self.cpu_count, 1), 2),
        }

        if sched_latencies:
            sorted_lat = sorted(sched_latencies)
            n = len(sorted_lat)
            result.update({
                'sched_latency_avg_ms': round(statistics.mean(sorted_lat), 3),
                'sched_latency_p50_ms': round(sorted_lat[n // 2], 3),
                'sched_latency_p99_ms': round(sorted_lat[int(n * 0.99)] if n > 100 else max(sorted_lat), 3),
                'sched_latency_p99_9_ms': round(sorted_lat[int(n * 0.999)] if n > 1000 else max(sorted_lat), 3),
                'sched_latency_max_ms': round(max(sorted_lat), 3),
            })
        else:
            result.update({
                'sched_latency_avg_ms': None,
                'sched_latency_p99_ms': None,
                'note': 'No sched latency data (need sched_waking + sched_switch events)',
            })

        return result

    def _calc_sched_latency_from_events(self) -> List[float]:
        """从 sched_waking 和 sched_switch 配对计算调度延迟"""
        wakeup_times = {}
        latencies = []

        # 合并 waking 和 wakeup 事件
        all_wakeups = self.sched_wakings + self.sched_wakeups

        for event in all_wakeups:
            pid = event.get('wakeup_pid', event.get('pid'))
            if pid:
                wakeup_times[pid] = event['ts']

        for sw in self.sched_switches:
            next_pid = sw.get('next_pid')
            if next_pid and next_pid in wakeup_times:
                latency = (sw['ts'] - wakeup_times[next_pid]) * 1000  # ms
                if 0 < latency < 10000:  # 合理范围过滤
                    latencies.append(latency)
                del wakeup_times[next_pid]

        return latencies

    def _extract_numa_metrics(self) -> Dict:
        """提取 NUMA 指标"""
        local_count = sum(w.get('numa_local', 0) for w in self.window_summaries)
        remote_count = sum(w.get('numa_remote', 0) for w in self.window_summaries)
        total = local_count + remote_count

        if total == 0:
            return {
                'remote_ratio': None,
                'note': 'No NUMA events found. Enable numa events in trace config.',
            }

        return {
            'remote_ratio': round(remote_count / total * 100, 2),
            'local_count': local_count,
            'remote_count': remote_count,
            'hit_ratio': round(local_count / total * 100, 2),
        }

    def _extract_memory_metrics(self) -> Dict:
        """提取内存指标"""
        # 从 key_events 中找 page fault
        major_pf = sum(1 for ke in self.key_events
                       if 'page_fault' in ke.get('type', '') and
                       ke.get('fault_type') == 'major')

        # 从窗口统计找 minor page fault 线索
        # (需要 trace 中有 mm_page_fault 事件)

        return {
            'major_page_faults': major_pf,
            'minor_page_fault_rate': None,  # 需要 mm_page_fault 事件
            'swap_usage_kb': 0,  # 需要额外数据源
            'oom_kill_count': 0,
        }

    def _extract_io_metrics(self, duration_s: float) -> Dict:
        """提取 IO 指标"""
        io_issue_count = sum(w.get('io_issue_count', 0) for w in self.window_summaries)
        io_complete_count = sum(w.get('io_complete_count', 0) for w in self.window_summaries)

        if io_issue_count == 0:
            return {
                'io_wait_pct': None,
                'note': 'No IO events found.',
            }

        # 估算 IO 等待（未完成的 IO 请求占比）
        pending = io_issue_count - io_complete_count
        io_wait_pct = min(pending / max(io_issue_count, 1) * 100, 100)

        return {
            'io_wait_pct': round(io_wait_pct, 2),
            'io_issue_count': io_issue_count,
            'io_complete_count': io_complete_count,
            'io_issue_rate': round(io_issue_count / max(duration_s, 1), 0),
        }

    def _extract_runtime_metrics(self) -> Dict:
        """提取 Runtime 指标"""
        # 从 key_events 找 D 状态线程
        d_state_count = sum(
            1 for ke in self.key_events
            if ke.get('prev_state', '').startswith('D')
        )

        return {
            'thread_block_count': d_state_count,
            'dataloader_time_pct': None,  # 需要 dataloader 标记事件
            'gil_contention_count': None,  # 需要 GIL 事件
        }

    def _extract_host_npu_metrics(self, duration_s: float) -> Dict:
        """提取 Host-NPU 协同指标"""
        if not self.npu_events:
            return {
                'npu_idle_ratio': None,
                'note': 'No NPU events found. Include msprof data for Host-NPU analysis.',
            }

        # NPU busy time
        npu_busy_us = sum(e.get('dur', 0) for e in self.npu_events)
        total_us = duration_s * 1_000_000
        idle_ratio = (1 - npu_busy_us / max(total_us, 1)) * 100

        # NPU idle gaps
        sorted_npu = sorted(self.npu_events, key=lambda e: e.get('ts', 0))
        gaps = []
        for i in range(1, len(sorted_npu)):
            prev_end = sorted_npu[i-1].get('ts', 0) + sorted_npu[i-1].get('dur', 0)
            curr_start = sorted_npu[i].get('ts', 0)
            gap_ms = (curr_start - prev_end) / 1000
            if gap_ms > 10:  # threshold
                gaps.append(gap_ms)

        result = {
            'npu_idle_ratio': round(idle_ratio, 2),
            'npu_op_count': len(self.npu_events),
        }

        if gaps:
            result.update({
                'npu_idle_max_gap_ms': round(max(gaps), 2),
                'npu_idle_avg_gap_ms': round(statistics.mean(gaps), 2),
                'npu_idle_gap_count': len(gaps),
            })
        else:
            result['npu_idle_max_gap_ms'] = 0
            result['npu_idle_gap_count'] = 0

        # Kernel launch gap
        launch_events = [e for e in self.npu_events
                         if 'Execute' in e.get('type', '') or 'Launch' in e.get('type', '')]
        if len(launch_events) > 1:
            launch_times = sorted([e['ts'] for e in launch_events])
            launch_gaps = [
                (launch_times[i] - launch_times[i-1]) / 1000  # ms
                for i in range(1, len(launch_times))
            ]
            result['kernel_launch_gap_avg_ms'] = round(statistics.mean(launch_gaps), 3)
            result['kernel_launch_gap_p99_ms'] = round(
                sorted(launch_gaps)[int(len(launch_gaps) * 0.99)] if len(launch_gaps) > 100
                else max(launch_gaps), 3
            )

        return result

    def _extract_timeline(self) -> List[Dict]:
        """提取时间线数据（用于报告中的时序分析）"""
        timeline = []
        for w in self.window_summaries:
            entry = {
                'ts': w.get('ts'),
                'sched_switch_count': w.get('sched_switch_count', 0),
                'runnable_count': w.get('runnable_count', 0),
                'wakeup_count': w.get('wakeup_count', 0),
                'io_issue_count': w.get('io_issue_count', 0),
                'npu_op_count': w.get('npu_op_count', 0),
            }
            # 只保留非空窗口
            if any(v for k, v in entry.items() if k != 'ts'):
                timeline.append(entry)

        # 限制时间线条目数（避免输出过大）
        if len(timeline) > 500:
            # 降采样
            step = len(timeline) // 500
            timeline = timeline[::step][:500]

        return timeline

    def _empty_metrics(self, keys: List[str]) -> Dict:
        """生成空指标"""
        return {k: None for k in keys} | {'note': 'No data available'}


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Host Trace 特征提取器')
    parser.add_argument('input', help='输入 JSONL 文件路径')
    parser.add_argument('--output', '-o', default=None, help='输出 JSON 文件路径')
    parser.add_argument('--cpu-count', type=int, default=0, help='CPU 核心数')
    parser.add_argument('--workload', default='unknown', help='工作负载类型')
    parser.add_argument('--parallel', default='unknown', help='并行配置')

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if args.output is None:
        base = os.path.splitext(args.input)[0]
        args.output = f'{base}_metrics.json'

    # 提取特征
    print(f"[1/2] 提取特征: {args.input}", file=sys.stderr)
    extractor = FeatureExtractor(
        cpu_count=args.cpu_count,
        workload=args.workload,
        parallel=args.parallel,
    )

    event_count = 0
    for event in read_events(args.input):
        extractor.process(event)
        event_count += 1
        if event_count % 100000 == 0:
            print(f"  已处理 {event_count} 事件", file=sys.stderr)

    metrics = extractor.extract_all()

    # 写入输出
    print(f"\n[2/2] 写入指标: {args.output}", file=sys.stderr)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2, default=str, ensure_ascii=False)

    # 打印摘要
    print(f"\n=== 指标摘要 ===", file=sys.stderr)
    print(f"CPU 利用率(avg): {metrics['cpu'].get('cpu_util_avg', 'N/A')}%", file=sys.stderr)
    print(f"上下文切换率: {metrics['cpu'].get('context_switch_rate', 'N/A')}/s", file=sys.stderr)
    print(f"Runqueue(avg): {metrics['sched'].get('runqueue_avg', 'N/A')}", file=sys.stderr)
    print(f"调度延迟(p99): {metrics['sched'].get('sched_latency_p99_ms', 'N/A')}ms", file=sys.stderr)
    print(f"NUMA远端比例: {metrics['numa'].get('remote_ratio', 'N/A')}%", file=sys.stderr)
    print(f"NPU空闲率: {metrics['host_npu'].get('npu_idle_ratio', 'N/A')}%", file=sys.stderr)
    print(f"\n输出: {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
