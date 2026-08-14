# 大文件处理策略

本文件定义处理 GB 级 trace 文件的具体工程方案。

## 核心挑战

| 挑战 | 描述 | 影响 |
|------|------|------|
| 内存 | GB 级文件无法一次性加载到内存 | OOM / 系统卡死 |
| 解析速度 | 百万级事件逐条解析耗时 | 分析时间过长 |
| LLM 上下文 | LLM 无法直接处理大量原始事件 | 无法直接推理 |
| 数据价值密度 | trace 中大部分事件是正常的 | 需要过滤无关信息 |

## 五级处理管线

### Level 1: 二进制转文本

**触发条件**：文件为二进制格式（perfetto/perf/trace-cmd）

**方法**：使用格式对应的转换工具，输出文本/JSON 文件

**注意事项**：
- 转换后文件可能比原文件大 3~10 倍（二进制→文本展开）
- 转换过程本身可能消耗大量内存，需监控
- 转换后的文本文件用于后续流式处理

```bash
# Perfetto: 转换为文本（比 JSON 更紧凑）
traceconv text input.trace output.txt

# perf: 转换为文本
perf script -i input.perf > output.txt

# trace-cmd: 转换为文本
trace-cmd report input.dat > output.txt
```

**降级策略**：如果转换工具不可用
- Perfetto: 尝试 `pip install perfetto` 使用 Python API
- perf: 尝试 `perf script` 直接输出
- 如果都不可用：告知用户安装对应工具，同时尝试直接读取二进制头部采样

### Level 2: 流式读取

**触发条件**：所有文本格式文件

**核心设计**：使用 Python generator 逐行/逐事件读取，不一次性加载

```python
def stream_lines(file_path, encoding='utf-8', errors='replace'):
    """逐行读取文件，不加载全文件到内存"""
    with open(file_path, 'r', encoding=encoding, errors=errors) as f:
        for line in f:
            yield line.strip()

def stream_events(file_path, parser_func, filter_types=None):
    """逐事件流式读取 + 早期过滤"""
    for line in stream_lines(file_path):
        event = parser_func(line)
        if event is None:
            continue
        if filter_types and event.get('type') not in filter_types:
            continue
        yield event
```

**内存控制**：任何时刻内存中只有当前行 + 聚合缓冲区，不保留历史事件。

### Level 3: 早期过滤

**触发条件**：事件类型已知，且部分类型与诊断无关

**方法**：在流式读取阶段即过滤无关事件类型

**默认保留的事件类型**：

```python
KEEP_EVENTS = {
    # 调度类（必保留）
    'sched_switch', 'sched_wakeup', 'sched_waking', 'sched_wakeup_new',
    'sched_process_exit', 'sched_process_fork',
    
    # CPU 类
    'cpu_frequency', 'cpu_idle',
    
    # NUMA 类
    'numa_hit', 'numa_miss', 'numa_local', 'numa_remote',
    'numa_hint_faults', 'numa_hint_faults_local',
    
    # 内存类
    'mm_page_alloc', 'mm_page_free', 'mm_page_fault',
    
    # IO 类
    'block_rq_issue', 'block_rq_complete',
    
    # 中断类
    'irq_handler_entry', 'irq_handler_exit',
    'softirq_entry', 'softirq_exit',
    
    # NPU 类（昇腾）
    'acl:OpExecute', 'acl:streamSync', 'acl:memcpyH2D', 'acl:memcpyD2H',
    'ge:LaunchOp', 'ge:CompileOp', 'aclmdlExecute', 'aclrtSynchronizeStream',
}

# 默认丢弃的事件类型
DROP_EVENTS = {
    'print', 'print_freerunning', 'tracing_mark_write:traceevent',
    # 用户态打印类事件，通常对 host 诊断无价值
}
```

**过滤效果**：通常可减少 30%~60% 的事件量。

### Level 4: 时间窗口聚合

**触发条件**：文件 > 100MB

**核心思想**：将事件流按时间窗口分桶，每个窗口只保留统计量，丢弃原始事件

**窗口大小选择**：

| 文件大小 | 窗口大小 | 窗口数量(30s trace) | 输出大小(估) |
|----------|----------|---------------------|-------------|
| < 100MB | 不聚合 | - | ~10MB |
| 100~500MB | 100ms | 300 | ~100KB |
| 500MB~1GB | 100ms | 300 | ~100KB |
| 1~5GB | 500ms | 60 | ~20KB |
| > 5GB | 1s | 30 | ~10KB |

**聚合逻辑**：

```python
class WindowAggregator:
    def __init__(self, window_ms=100):
        self.window_ms = window_ms
        self.window_start = None
        self.buffer = defaultdict(list)
        self.key_events = []  # 异常事件完整保留
    
    def process(self, event):
        """处理单个事件，返回完成的窗口或 None"""
        ts = event['ts']
        event_type = event.get('type', event.get('name', ''))
        
        if self.window_start is None:
            self.window_start = ts
        
        # 检查是否是关键事件（需完整保留）
        if self._is_key_event(event):
            self.key_events.append(event)
        
        # 累加到窗口缓冲
        self.buffer[event_type].append(event)
        
        # 检查窗口是否结束
        if ts - self.window_start >= self.window_ms:
            return self._finalize_window()
        return None
    
    def _is_key_event(self, event):
        """判断是否为需完整保留的关键事件"""
        # 调度延迟超过阈值
        if 'latency_ms' in event and event['latency_ms'] > 10:
            return True
        # runqueue 异常高
        if 'runnable' in event and event['runnable'] > 50:
            return True
        # IO 等待超过阈值
        if 'io_wait_ms' in event and event['io_wait_ms'] > 50:
            return True
        # NPU idle gap
        if event.get('type') == 'npu_idle_gap' and event.get('duration_ms', 0) > 20:
            return True
        return False
    
    def _finalize_window(self):
        """生成窗口统计"""
        summary = {
            'type': 'window_summary',
            'ts': self.window_start,
            'window_ms': self.window_ms,
        }
        
        # 调度统计
        if 'sched_switch' in self.buffer:
            switches = self.buffer['sched_switch']
            summary['sched_switch_count'] = len(switches)
            # 计算窗口内 runqueue（从 sched_switch 的 prev_state 推断）
            runnable = sum(1 for s in switches if s.get('prev_state', '').startswith('R'))
            summary['runnable_avg'] = runnable / max(1, len(switches))
        
        if 'sched_wakeup' in self.buffer:
            summary['sched_wakeup_count'] = len(self.buffer['sched_wakeup'])
        
        # CPU 统计
        if 'cpu_idle' in self.buffer:
            idle_count = len(self.buffer['cpu_idle'])
            summary['cpu_idle_pct'] = idle_count / max(1, len(self.buffer.get('sched_switch', [1])))
        
        # NUMA 统计
        local_count = len(self.buffer.get('numa_local', []))
        remote_count = len(self.buffer.get('numa_remote', []))
        if local_count + remote_count > 0:
            summary['numa_remote_ratio'] = remote_count / (local_count + remote_count)
        
        # IO 统计
        if 'block_rq_issue' in self.buffer:
            summary['io_request_count'] = len(self.buffer['block_rq_issue'])
        
        # 重置
        self.window_start = None
        self.buffer = defaultdict(list)
        
        return summary
```

### Level 5: 事件采样

**触发条件**：文件 > 5GB 或事件量 > 10M

**方法**：对非关键事件进行降采样

```python
def sample_events(event_stream, sample_rate=0.01, key_event_func=None):
    """
    对事件流进行采样
    - 关键事件: 100% 保留
    - 普通事件: 按 sample_rate 采样
    """
    counter = 0
    for event in event_stream:
        counter += 1
        
        # 关键事件始终保留
        if key_event_func and key_event_func(event):
            yield event
            continue
        
        # 普通事件采样
        if counter % int(1 / sample_rate) == 0:
            yield event
```

**采样率选择**：

| 事件量 | 采样率 | 输出事件量 | 统计误差 |
|--------|--------|-----------|----------|
| 1M | 1.0 (全量) | 1M | 0% |
| 10M | 0.1 | 1M | < 3% |
| 100M | 0.01 | 1M | < 5% |
| 1B | 0.001 | 1M | < 10% |

**注意**：采样仅用于统计类指标（如 cpu_util, context_switch_rate）。关键事件（调度延迟异常、NPU idle gap）始终全量保留。

---

## 分片处理方案

当单机内存不足以处理时，使用分片处理：

### 方案 1: 按行数分片

```bash
# 将大文件分割为小文件（每 1000 万行一个）
split -l 10000000 -d --additional-suffix=.part large_trace.txt trace_part_

# 并行处理各分片
for f in trace_part_*.part; do
    python trace_preprocessor.py "$f" --output "${f%.part}_metrics.json" &
done
wait

# 合并结果
python merge_results.py trace_part_*_metrics.json --output final_metrics.json
```

### 方案 2: 按时间分片

```python
def split_by_time(file_path, parser_func, split_interval_s=5):
    """按时间间隔分割事件流"""
    current_split = None
    split_files = {}
    
    for event in stream_events(file_path, parser_func):
        ts = event['ts']
        split_idx = int(ts) // split_interval_s
        
        if split_idx != current_split:
            current_split = split_idx
            split_files[split_idx] = open(f'trace_split_{split_idx}.jsonl', 'w')
        
        split_files[split_idx].write(json.dumps(event) + '\n')
    
    for f in split_files.values():
        f.close()
    return list(split_files.keys())
```

### 方案 3: 使用 Polars 高效处理

```python
import polars as pl

# 流式扫描大文件
lf = pl.scan_csv("large_trace.csv", separator='\t')

# 聚合计算（惰性执行，不加载全文件到内存）
result = (
    lf.group_by("event_type")
    .agg([
        pl.count().alias("count"),
        pl.col("ts").min().alias("first_ts"),
        pl.col("ts").max().alias("last_ts"),
    ])
    .collect()  # 此刻才真正执行
)
```

---

## 内存监控

处理大文件时应监控内存使用：

```python
import psutil
import os

def check_memory():
    process = psutil.Process(os.getpid())
    mem_mb = process.memory_info().rss / 1024 / 1024
    return mem_mb

# 在处理循环中定期检查
for i, event in enumerate(stream_events(file_path, parser)):
    if i % 1000000 == 0:
        mem = check_memory()
        if mem > 500:  # 超过 500MB 告警
            print(f"WARNING: Memory usage {mem:.0f}MB, consider smaller window")
    # ... process event
```

---

## 性能优化技巧

1. **使用 `__slots__`**：事件对象使用 `__slots__` 减少内存
2. **避免字符串拼接**：使用 f-string 或 format，不用 +
3. **批量写入**：输出使用批量写入而非逐行写入
4. **使用 mmap**：对固定格式文件使用内存映射
5. **多进程处理**：对 CPU 密集型解析使用 multiprocessing
6. **正则编译**：预编译所有正则表达式
7. **避免 JSON 重复解析**：对 JSON 行格式，使用 rapidjson 或 orjson

```python
# 预编译正则
SCHED_SWITCH_RE = re.compile(
    r'(\S+)-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+\.\d+):\s+sched_switch:\s+'
    r'prev_comm=(\S+)\s+prev_pid=(\d+)\s+prev_prio=(\d+)\s+prev_state=(\S+)\s+'
    r'next_comm=(\S+)\s+next_pid=(\d+)\s+next_prio=(\d+)'
)

# 使用 orjson 加速 JSON 解析
try:
    import orjson
    def parse_json_line(line):
        return orjson.loads(line)
except ImportError:
    import json
    def parse_json_line(line):
        return json.loads(line)
```
