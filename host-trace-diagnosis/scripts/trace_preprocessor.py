#!/usr/bin/env python3
"""
Host Trace 预处理器
====================
功能：格式检测 → 流式读取 → 事件过滤 → 时间窗口聚合
输入：trace 文件（支持 ftrace/perfetto/msprof/perf 格式）
输出：structured_events.jsonl（结构化事件流）

使用方法：
    python trace_preprocessor.py <input_file> [--output <output_file>]
        [--window-ms 100] [--format auto]
        [--keep-types sched_switch,sched_wakeup,...]
        [--sample-rate 1.0] [--max-memory 500]

注意：此脚本是 Skill 模板，Agent 可根据实际 trace 格式调整解析逻辑。
"""

import sys
import os
import re
import json
import argparse
from collections import defaultdict
from typing import Generator, Dict, List, Optional, Any

# ============================================================
# 格式检测
# ============================================================

class FormatDetector:
    """Trace 文件格式检测器"""

    @staticmethod
    def detect(file_path: str) -> Dict[str, Any]:
        """检测 trace 文件格式，返回格式信息"""
        result = {
            'format': 'unknown',
            'encoding': 'text',
            'needs_conversion': False,
            'conversion_cmd': None,
            'details': {},
        }

        # 检查文件大小
        file_size = os.path.getsize(file_path)
        result['file_size'] = file_size
        result['file_size_mb'] = file_size / 1024 / 1024

        # 读取文件头
        with open(file_path, 'rb') as f:
            header = f.read(1024)

        # 1. 检测 Perfetto (protobuf binary)
        if header[0] == 0x0a and file_path.endswith(('.trace', '.perfetto-trace')):
            result['format'] = 'perfetto'
            result['encoding'] = 'binary'
            result['needs_conversion'] = True
            result['conversion_cmd'] = f'traceconv json "{file_path}" output.json'
            return result

        # 2. 检测 perf data
        if header[:8] == b'PERFILE2':
            result['format'] = 'perf'
            result['encoding'] = 'binary'
            result['needs_conversion'] = True
            result['conversion_cmd'] = f'perf script -i "{file_path}" > output.txt'
            return result

        # 3. 检测 trace-cmd dat
        if header[:12] == b'tracing_data':
            result['format'] = 'trace_cmd'
            result['encoding'] = 'binary'
            result['needs_conversion'] = True
            result['conversion_cmd'] = f'trace-cmd report "{file_path}" > output.txt'
            return result

        # 尝试文本解码
        try:
            text_header = header.decode('utf-8', errors='replace')
        except Exception:
            text_header = header.decode('latin-1', errors='replace')

        lines = text_header.split('\n')

        # 4. 检测 ftrace 文本
        ftrace_pattern = re.compile(
            r'^\s*(\S+)-(\d+)\s+\[(\d+)\]\s+\S+\s+\d+\.\d+:\s+\w+:'
        )
        if any(ftrace_pattern.match(line) for line in lines[:20]):
            result['format'] = 'ftrace'
            result['encoding'] = 'text'
            return result

        # 检测 ftrace raw header
        if '# tracer:' in text_header:
            result['format'] = 'ftrace'
            result['encoding'] = 'text'
            return result

        # 5. 检测 JSON 格式
        if text_header.strip().startswith(('{', '[')):
            try:
                # 尝试解析为 JSON
                if text_header.strip().startswith('['):
                    result['format'] = 'json_array'
                    result['encoding'] = 'text'
                    # 检查是否是 msprof
                    if '"acl"' in text_header or '"Op"' in text_header or '"ge"' in text_header:
                        result['format'] = 'msprof_json'
                    elif '"traceEvents"' in text_header:
                        result['format'] = 'chrome_trace'
                    return result
                elif text_header.strip().startswith('{'):
                    # 可能是 JSONL 格式
                    result['format'] = 'jsonl'
                    result['encoding'] = 'text'
                    if '"name"' in text_header and ('"acl"' in text_header or '"Op"' in text_header):
                        result['format'] = 'msprof_jsonl'
                    return result
            except Exception:
                pass

        # 6. 检测 CSV 格式
        first_line = lines[0] if lines else ''
        if ',' in first_line and any(
            keyword in first_line for keyword in
            ['Op Name', 'Duration', 'Start Time', 'Device ID', 'ACLNN']
        ):
            result['format'] = 'msprof_csv'
            result['encoding'] = 'text'
            return result

        # 7. 检测 perf script 输出
        perf_pattern = re.compile(
            r'^\s*(\S+)\s+(\d+)/(\d+)\s+\[(\d+)\]\s+\d+\.\d+:\s+\w+:'
        )
        if any(perf_pattern.match(line) for line in lines[:20]):
            result['format'] = 'perf_script'
            result['encoding'] = 'text'
            return result

        # 8. 未知格式 - 尝试通用模式
        result['format'] = 'unknown'
        result['encoding'] = 'text'
        result['details']['first_lines'] = lines[:5]
        return result


# ============================================================
# 事件解析器
# ============================================================

class EventParsers:
    """各种格式的事件解析器"""

    # 预编译正则
    FTRACE_LINE = re.compile(
        r'^\s*(\S+)-(\d+)\s+\[(\d+)\]\s+(\S+)\s+(\d+\.\d+):\s+(\w+):\s*(.*)'
    )
    PERF_LINE = re.compile(
        r'^\s*(\S+)\s+(\d+)/(\d+)\s+\[(\d+)\]\s+(\d+\.\d+):\s+(\w+):(\w+):\s*(.*)'
    )
    FTRACE_SCHED_SWITCH = re.compile(
        r'prev_comm=(\S+)\s+prev_pid=(\d+)\s+prev_prio=(\d+)\s+prev_state=(\S+)\s+'
        r'next_comm=(\S+)\s+next_pid=(\d+)\s+next_prio=(\d+)'
    )
    FTRACE_SCHED_WAKEUP = re.compile(
        r'comm=(\S+)\s+pid=(\d+)\s+prio=(\d+)\s+target_cpu=(\d+)'
    )
    FTRACE_NUMA = re.compile(
        r'pid=(\d+)\s+cpu=(\d+)\s+nid=(\d+)(?:\s+preferred_node=(\d+))?'
    )

    @staticmethod
    def parse_ftrace_line(line: str) -> Optional[Dict]:
        """解析 ftrace 文本行"""
        m = EventParsers.FTRACE_LINE.match(line)
        if not m:
            return None

        task, pid, cpu, flags, ts_str, event_name, data = m.groups()
        ts = float(ts_str)
        cpu = int(cpu)
        pid = int(pid)

        event = {
            'type': event_name,
            'ts': ts,
            'pid': pid,
            'cpu': cpu,
            'task': task,
            'raw': data,
        }

        # 解析特定事件的数据
        if event_name == 'sched_switch':
            sm = EventParsers.FTRACE_SCHED_SWITCH.search(data)
            if sm:
                event['prev_comm'] = sm.group(1)
                event['prev_pid'] = int(sm.group(2))
                event['prev_state'] = sm.group(4)
                event['next_comm'] = sm.group(5)
                event['next_pid'] = int(sm.group(6))
        elif event_name in ('sched_wakeup', 'sched_waking', 'sched_wakeup_new'):
            sm = EventParsers.FTRACE_SCHED_WAKEUP.search(data)
            if sm:
                event['wakeup_comm'] = sm.group(1)
                event['wakeup_pid'] = int(sm.group(2))
                event['target_cpu'] = int(sm.group(4))
        elif event_name in ('numa_hit', 'numa_miss', 'numa_local', 'numa_remote'):
            sm = EventParsers.FTRACE_NUMA.search(data)
            if sm:
                event['numa_pid'] = int(sm.group(1))
                event['numa_cpu'] = int(sm.group(2))
                event['nid'] = int(sm.group(3))
                if sm.group(4):
                    event['preferred_node'] = int(sm.group(4))
        elif event_name in ('block_rq_issue', 'block_rq_complete'):
            # 简化解析: 8,0 WS 0 () 128 + 8 [python]
            parts = data.split()
            if len(parts) >= 2:
                event['dev'] = parts[0]
                event['rwbs'] = parts[1] if len(parts) > 1 else ''
            if '+' in data:
                sector_m = re.search(r'(\d+)\s*\+\s*(\d+)', data)
                if sector_m:
                    event['sector'] = int(sector_m.group(1))
                    event['sectors_count'] = int(sector_m.group(2))

        return event

    @staticmethod
    def parse_perf_line(line: str) -> Optional[Dict]:
        """解析 perf script 输出行"""
        m = EventParsers.PERF_LINE.match(line)
        if not m:
            # 尝试 ftrace 格式（perf script 有时输出类似格式）
            return EventParsers.parse_ftrace_line(line)

        task, pid, tid, cpu, ts_str, subsystem, event_name, data = m.groups()
        return {
            'type': f'{subsystem}:{event_name}',
            'ts': float(ts_str),
            'pid': int(pid),
            'tid': int(tid),
            'cpu': int(cpu),
            'task': task,
            'raw': data,
        }

    @staticmethod
    def parse_jsonl_line(line: str) -> Optional[Dict]:
        """解析 JSONL 格式行"""
        line = line.strip()
        if not line or not line.startswith('{'):
            return None
        try:
            data = json.loads(line)
            # 标准化字段
            event = {
                'type': data.get('name', data.get('type', 'unknown')),
                'ts': data.get('ts', data.get('timestamp', 0)),
                'cat': data.get('cat', ''),
                'pid': data.get('pid', 0),
                'tid': data.get('tid', 0),
                'dur': data.get('dur', 0),
            }
            if 'args' in data:
                event['args'] = data['args']
            if 'cpu' in data:
                event['cpu'] = data['cpu']
            return event
        except json.JSONDecodeError:
            return None

    @staticmethod
    def parse_csv_line(line: str, header: List[str]) -> Optional[Dict]:
        """解析 CSV 格式行"""
        values = line.strip().split(',')
        if len(values) < len(header):
            return None
        row = dict(zip(header, values))
        return {
            'type': row.get('Op Name', row.get('Op Type', 'unknown')),
            'ts': float(row.get('Start Time', row.get('Timestamp', 0))),
            'dur': float(row.get('Duration', 0)),
            'cat': row.get('Task Type', ''),
            'device_id': row.get('Device ID', ''),
            'stream_id': row.get('Stream ID', ''),
        }

    @staticmethod
    def get_parser(format_type: str):
        """根据格式类型获取解析器函数"""
        parsers = {
            'ftrace': EventParsers.parse_ftrace_line,
            'perf_script': EventParsers.parse_perf_line,
            'jsonl': EventParsers.parse_jsonl_line,
            'msprof_jsonl': EventParsers.parse_jsonl_line,
            'chrome_trace': EventParsers.parse_jsonl_line,
        }
        return parsers.get(format_type)


# ============================================================
# 流式处理器
# ============================================================

class StreamProcessor:
    """流式事件处理器：读取 → 过滤 → 聚合"""

    DEFAULT_KEEP_TYPES = {
        'sched_switch', 'sched_wakeup', 'sched_waking', 'sched_wakeup_new',
        'sched_process_exit', 'sched_process_fork',
        'cpu_frequency', 'cpu_idle',
        'numa_hit', 'numa_miss', 'numa_local', 'numa_remote',
        'numa_hint_faults', 'numa_hint_faults_local',
        'block_rq_issue', 'block_rq_complete',
        'irq_handler_entry', 'irq_handler_exit',
        'softirq_entry', 'softirq_exit',
    }

    # NPU 相关事件
    NPU_EVENT_TYPES = {
        'acl:OpExecute', 'acl:streamSync', 'acl:memcpyH2D', 'acl:memcpyD2H',
        'ge:LaunchOp', 'ge:CompileOp', 'aclmdlExecute', 'aclmdlExecuteAsync',
        'aclrtSynchronizeStream', 'aclrtMemcpy', 'aclrtMemAlloc',
    }

    def __init__(
        self,
        window_ms: int = 100,
        keep_types: Optional[set] = None,
        sample_rate: float = 1.0,
        key_event_thresholds: Optional[Dict] = None,
    ):
        self.window_ms = window_ms
        self.keep_types = keep_types or self.DEFAULT_KEEP_TYPES | self.NPU_EVENT_TYPES
        self.sample_rate = sample_rate
        self.sample_counter = 0
        self.key_event_thresholds = key_event_thresholds or {
            'sched_latency_ms': 10,
            'runqueue': 50,
            'npu_idle_gap_ms': 20,
        }

    def should_keep(self, event: Dict) -> bool:
        """判断事件是否保留"""
        event_type = event.get('type', '')

        # 检查是否在保留列表中
        if event_type in self.keep_types:
            return True

        # 检查是否是 NPU 事件
        if any(npu_type in event_type for npu_type in self.NPU_EVENT_TYPES):
            return True

        # 检查是否是关键事件
        if self._is_key_event(event):
            return True

        # 采样
        if self.sample_rate < 1.0:
            self.sample_counter += 1
            if self.sample_counter % int(1 / self.sample_rate) == 0:
                return True

        return False

    def _is_key_event(self, event: Dict) -> bool:
        """判断是否为关键异常事件"""
        # 调度延迟超过阈值
        if 'latency_ms' in event:
            if event['latency_ms'] > self.key_event_thresholds.get('sched_latency_ms', 10):
                return True

        # runqueue 异常高
        if 'runnable' in event:
            if event['runnable'] > self.key_event_thresholds.get('runqueue', 50):
                return True

        # NPU idle gap
        if event.get('type') == 'npu_idle_gap':
            if event.get('duration_ms', 0) > self.key_event_thresholds.get('npu_idle_gap_ms', 20):
                return True

        return False

    def stream_events(
        self, file_path: str, format_type: str
    ) -> Generator[Dict, None, None]:
        """流式读取事件"""
        parser = EventParsers.get_parser(format_type)
        if parser is None:
            raise ValueError(f"Unsupported format: {format_type}")

        # CSV 特殊处理（需要先读 header）
        if format_type == 'msprof_csv':
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                header = f.readline().strip().split(',')
                for line in f:
                    event = parser(line, header)
                    if event and self.should_keep(event):
                        yield event
            return

        # JSON 数组特殊处理
        if format_type in ('msprof_json', 'chrome_trace'):
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                try:
                    data = json.load(f)
                    events = data.get('traceEvents', data) if isinstance(data, dict) else data
                    for event_data in events:
                        event = EventParsers.parse_jsonl_line(json.dumps(event_data))
                        if event and self.should_keep(event):
                            yield event
                except json.JSONDecodeError as e:
                    print(f"ERROR: Failed to parse JSON: {e}", file=sys.stderr)
            return

        # 文本格式流式读取
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                event = parser(line)
                if event and self.should_keep(event):
                    yield event


# ============================================================
# 窗口聚合器
# ============================================================

class WindowAggregator:
    """时间窗口聚合器"""

    def __init__(self, window_ms: int = 100):
        self.window_ms = window_ms
        self.window_start = None
        self.buffer = defaultdict(list)
        self.key_events = []
        self.total_events = 0
        self.total_windows = 0

    def process(self, event: Dict) -> Optional[Dict]:
        """处理事件，返回完成的窗口或 None"""
        self.total_events += 1
        ts = event['ts']

        if self.window_start is None:
            self.window_start = ts

        # 关键事件完整保留
        if self._is_key_event(event):
            self.key_events.append(event)

        # 累加到窗口
        event_type = event.get('type', 'unknown')
        self.buffer[event_type].append(event)

        # 检查窗口是否结束
        window_duration = (ts - self.window_start) * 1000  # 转为 ms
        if window_duration >= self.window_ms:
            return self._finalize_window()

        return None

    def _is_key_event(self, event: Dict) -> bool:
        """判断关键事件"""
        if event.get('latency_ms', 0) > 10:
            return True
        if event.get('prev_state', '').startswith('D'):  # D 状态
            return True
        return False

    def _finalize_window(self) -> Dict:
        """生成窗口统计"""
        summary = {
            'type': 'window_summary',
            'ts': self.window_start,
            'window_ms': self.window_ms,
        }

        # 调度统计
        sched_switches = self.buffer.get('sched_switch', [])
        if sched_switches:
            summary['sched_switch_count'] = len(sched_switches)
            runnable = sum(
                1 for s in sched_switches
                if s.get('prev_state', '').startswith('R')
            )
            summary['runnable_count'] = runnable
            # CPU 分布
            cpu_dist = defaultdict(int)
            for s in sched_switches:
                cpu_dist[s.get('cpu', 0)] += 1
            summary['cpu_distribution'] = dict(cpu_dist)

        # 唤醒统计
        wakeups = self.buffer.get('sched_wakeup', []) + self.buffer.get('sched_waking', [])
        if wakeups:
            summary['wakeup_count'] = len(wakeups)

        # NUMA 统计
        local = len(self.buffer.get('numa_local', []))
        remote = len(self.buffer.get('numa_remote', []))
        if local + remote > 0:
            summary['numa_local'] = local
            summary['numa_remote'] = remote

        # IO 统计
        io_issues = self.buffer.get('block_rq_issue', [])
        io_completes = self.buffer.get('block_rq_complete', [])
        if io_issues or io_completes:
            summary['io_issue_count'] = len(io_issues)
            summary['io_complete_count'] = len(io_completes)

        # NPU 统计
        npu_events = []
        for etype, events in self.buffer.items():
            if any(npu in etype for npu in StreamProcessor.NPU_EVENT_TYPES):
                npu_events.extend(events)
        if npu_events:
            summary['npu_op_count'] = len(npu_events)
            summary['npu_busy_time'] = sum(e.get('dur', 0) for e in npu_events)

        # 重置
        self.window_start = None
        self.buffer = defaultdict(list)
        self.total_windows += 1

        return summary

    def get_remaining(self) -> Optional[Dict]:
        """获取最后一个未完成的窗口"""
        if self.buffer:
            return self._finalize_window()
        return None

    def get_key_events(self) -> List[Dict]:
        """获取所有关键事件"""
        return self.key_events


# ============================================================
# 预检工具
# ============================================================

def triage(file_path: str) -> Dict:
    """快速预检 trace 文件"""
    info = FormatDetector.detect(file_path)

    # 采样前 10000 行快速统计
    if info['encoding'] == 'text' and not info['needs_conversion']:
        stats = {
            'total_lines_sampled': 0,
            'event_types': defaultdict(int),
            'cpu_set': set(),
            'pid_set': set(),
            'first_ts': None,
            'last_ts': None,
        }

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for i, line in enumerate(f):
                    if i >= 10000:
                        break
                    stats['total_lines_sampled'] += 1
                    parser = EventParsers.get_parser(info['format'])
                    if parser:
                        event = parser(line)
                        if event:
                            etype = event.get('type', 'unknown')
                            stats['event_types'][etype] += 1
                            if 'cpu' in event:
                                stats['cpu_set'].add(event['cpu'])
                            if 'pid' in event:
                                stats['pid_set'].add(event['pid'])
                            ts = event.get('ts')
                            if ts:
                                if stats['first_ts'] is None:
                                    stats['first_ts'] = ts
                                stats['last_ts'] = ts
        except Exception as e:
            stats['error'] = str(e)

        # 转换 set 为可序列化格式
        stats['cpu_count'] = len(stats['cpu_set'])
        stats['pid_count'] = len(stats['pid_set'])
        stats['cpus'] = sorted(list(stats['cpu_set']))
        stats['event_types'] = dict(
            sorted(stats['event_types'].items(), key=lambda x: -x[1])
        )
        del stats['cpu_set']
        del stats['pid_set']

        if stats['first_ts'] and stats['last_ts']:
            stats['sampled_duration_s'] = stats['last_ts'] - stats['first_ts']

        info['triage'] = stats

    # 选择处理策略
    size_mb = info.get('file_size_mb', 0)
    if size_mb < 10:
        info['processing_strategy'] = 'full_parse'
    elif size_mb < 100:
        info['processing_strategy'] = 'streaming_filter'
    elif size_mb < 1000:
        info['processing_strategy'] = 'streaming_aggregation'
    else:
        info['processing_strategy'] = 'streaming_aggregation_sampling'

    return info


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Host Trace 预处理器')
    parser.add_argument('input', help='输入 trace 文件路径')
    parser.add_argument('--output', '-o', default=None, help='输出文件路径')
    parser.add_argument('--window-ms', type=int, default=100, help='时间窗口大小(ms)')
    parser.add_argument('--format', default='auto', help='格式 (auto/ftrace/perfetto/...)')
    parser.add_argument('--sample-rate', type=float, default=1.0, help='采样率 (0~1)')
    parser.add_argument('--triage-only', action='store_true', help='仅执行预检')
    parser.add_argument('--keep-types', default=None, help='保留的事件类型(逗号分隔)')

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # 设置输出路径
    if args.output is None:
        base = os.path.splitext(args.input)[0]
        args.output = f'{base}_structured.jsonl'

    # Step 1: 预检
    print(f"[1/4] 预检: {args.input}", file=sys.stderr)
    info = triage(args.input)
    print(f"  格式: {info['format']}", file=sys.stderr)
    print(f"  大小: {info.get('file_size_mb', 0):.1f}MB", file=sys.stderr)
    print(f"  策略: {info.get('processing_strategy', 'unknown')}", file=sys.stderr)

    if args.triage_only:
        print(json.dumps(info, indent=2, default=str))
        return

    # 检查是否需要格式转换
    if info.get('needs_conversion'):
        cmd = info.get('conversion_cmd', '')
        print(f"\n[!] 需要格式转换: {cmd}", file=sys.stderr)
        print(f"    请先运行转换命令，然后对转换后的文件重新运行本脚本", file=sys.stderr)
        sys.exit(2)

    # Step 2: 流式处理
    format_type = args.format if args.format != 'auto' else info['format']
    keep_types = None
    if args.keep_types:
        keep_types = set(args.keep_types.split(','))

    processor = StreamProcessor(
        window_ms=args.window_ms,
        keep_types=keep_types,
        sample_rate=args.sample_rate,
    )

    aggregator = WindowAggregator(window_ms=args.window_ms)

    print(f"\n[2/4] 流式处理 (格式: {format_type}, 窗口: {args.window_ms}ms)", file=sys.stderr)

    event_count = 0
    window_count = 0
    key_event_count = 0

    with open(args.output, 'w', encoding='utf-8') as out_f:
        for event in processor.stream_events(args.input, format_type):
            event_count += 1
            result = aggregator.process(event)

            if result:
                window_count += 1
                out_f.write(json.dumps(result, default=str) + '\n')

            if event_count % 1000000 == 0:
                print(f"  已处理 {event_count} 事件, {window_count} 窗口", file=sys.stderr)

        # 写入最后一个窗口
        remaining = aggregator.get_remaining()
        if remaining:
            window_count += 1
            out_f.write(json.dumps(remaining, default=str) + '\n')

        # 写入关键事件
        key_events = aggregator.get_key_events()
        key_event_count = len(key_events)
        print(f"\n[3/4] 写入 {key_event_count} 个关键事件", file=sys.stderr)
        for ke in key_events:
            out_f.write(json.dumps({'type': 'key_event', **ke}, default=str) + '\n')

    # Step 4: 输出统计
    output_size = os.path.getsize(args.output)
    print(f"\n[4/4] 完成!", file=sys.stderr)
    print(f"  输入: {args.input} ({info.get('file_size_mb', 0):.1f}MB)", file=sys.stderr)
    print(f"  输出: {args.output} ({output_size / 1024:.1f}KB)", file=sys.stderr)
    print(f"  事件: {event_count}", file=sys.stderr)
    print(f"  窗口: {window_count}", file=sys.stderr)
    print(f"  关键事件: {key_event_count}", file=sys.stderr)
    print(f"  压缩比: {info.get('file_size_mb', 0) * 1024 / max(output_size / 1024, 1):.1f}x", file=sys.stderr)


if __name__ == '__main__':
    main()
