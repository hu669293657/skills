# 特征提取指标指南

本文件定义所有性能指标的计算公式、阈值和场景校准规则。

## 指标体系总览

```
Host 性能指标
│
├── CPU 指标
│   ├── cpu_util_avg          # CPU 平均利用率
│   ├── cpu_util_max          # 单核最大利用率
│   ├── cpu_balance           # CPU 负载均衡度
│   ├── context_switch_rate   # 上下文切换速率
│   └── cpu_migration_rate    # CPU 间迁移速率
│
├── 调度指标
│   ├── runqueue_avg          # 平均运行队列长度
│   ├── runqueue_max          # 最大运行队列长度
│   ├── runqueue_pressure     # 运行队列压力比
│   ├── sched_latency_avg     # 平均调度延迟
│   ├── sched_latency_p99     # P99 调度延迟
│   ├── sched_latency_p99_9   # P99.9 调度延迟
│   └── wakeup_to_schedule    # 唤醒到调度时间
│
├── NUMA 指标
│   ├── numa_remote_ratio     # 远端访问比例
│   ├── numa_hit_ratio        # 命中率
│   ├── numa_local_count      # 本地访问数
│   ├── numa_remote_count     # 远端访问数
│   └── numa_migration_rate   # NUMA 迁移速率
│
├── 内存指标
│   ├── major_page_faults     # major page fault 数
│   ├── minor_page_fault_rate # minor page fault 速率
│   ├── swap_usage            # swap 使用量
│   ├── oom_kill_count        # OOM kill 次数
│   └── memory_bandwidth      # 内存带宽利用率
│
├── IO 指标
│   ├── io_wait_pct           # IO 等待时间占比
│   ├── disk_read_bw          # 磁盘读带宽
│   ├── disk_write_bw         # 磁盘写带宽
│   ├── io_latency_avg        # IO 平均延迟
│   └── io_latency_p99        # IO P99 延迟
│
├── Runtime 指标
│   ├── thread_block_count    # 线程阻塞次数
│   ├── thread_block_time     # 线程阻塞总时间
│   ├── dataloader_time_pct   # DataLoader 耗时占比
│   ├── gil_contention_count  # GIL 竞争次数
│   └── runtime_call_rate     # runtime 调用速率
│
└── Host-NPU 协同指标
    ├── npu_idle_ratio        # NPU 空闲占比
    ├── npu_idle_max_gap      # NPU 最大空闲间隔
    ├── npu_idle_gap_count    # NPU 空闲间隔数
    ├── kernel_launch_gap     # kernel 下发间隔
    ├── h2d_wait_time         # H2D 拷贝等待时间
    ├── d2h_wait_time         # D2H 拷贝等待时间
    └── sync_wait_time        # 同步等待时间
```

---

## CPU 指标

### cpu_util_avg

**定义**：所有 CPU 核心的平均利用率（%）

**计算方法**：

从 `sched_switch` 和 `cpu_idle` 事件计算：

```python
def calc_cpu_util_avg(events, duration_s, cpu_count):
    """
    方法1: 基于 cpu_idle 事件
    cpu_idle state=0 表示 C0 (running), state>0 表示 idle
    """
    idle_time_per_cpu = defaultdict(float)
    prev_idle_start = {}
    
    for event in events:
        if event['type'] == 'cpu_idle':
            cpu = event['cpu']
            if event['state'] == 0:  # exit idle
                if cpu in prev_idle_start:
                    idle_time_per_cpu[cpu] += event['ts'] - prev_idle_start[cpu]
                    del prev_idle_start[cpu]
            else:  # enter idle
                prev_idle_start[cpu] = event['ts']
    
    total_idle = sum(idle_time_per_cpu.values())
    total_time = duration_s * cpu_count
    return (1 - total_idle / total_time) * 100

def calc_cpu_util_from_sched(sched_switches, duration_s, cpu_count):
    """
    方法2: 基于 sched_switch 事件
    利用 context switch 频率近似 CPU 利用率
    """
    # 有 switch 的时间段认为是 busy
    busy_intervals = set()
    for sw in sched_switches:
        # 将时间分成 10ms 粒度的 bucket
        bucket = int(sw['ts'] * 100)  # 10ms bucket
        busy_intervals.add((sw['cpu'], bucket))
    
    total_buckets = duration_s * 100 * cpu_count  # 10ms buckets
    return len(busy_intervals) / total_buckets * 100
```

**阈值**：

| 值 | 含义 | 适用场景 |
|----|------|----------|
| < 50% | 低利用率 | 可能存在 IO 瓶颈 |
| 50~80% | 正常 | 大多数工作负载 |
| 80~90% | 偏高 | 需关注 |
| > 90% | 高利用率 | 可能存在 CPU 瓶颈 |

### cpu_util_max

**定义**：单个 CPU 核心的最大利用率

**计算方法**：逐 CPU 计算 `cpu_util_avg`，取最大值

**阈值**：> 95% 为单核热点

### cpu_balance

**定义**：CPU 负载均衡度，衡量各核心利用率的一致性

**计算方法**：
```python
def calc_cpu_balance(per_core_utils):
    """变异系数: std / mean"""
    import statistics
    mean = statistics.mean(per_core_utils)
    if mean == 0:
        return 0
    std = statistics.stdev(per_core_utils)
    return std / mean
```

**阈值**：
- `< 0.2`：均衡
- `0.2~0.5`：轻度不均
- `> 0.5`：严重不均（可能存在绑核问题）

### context_switch_rate

**定义**：每秒上下文切换次数

**计算方法**：
```python
def calc_context_switch_rate(sched_switches, duration_s):
    return len(sched_switches) / duration_s
```

**阈值**：

| 值 | 含义 |
|----|------|
| < 10000/s | 正常 |
| 10000~50000/s | 偏高 |
| > 50000/s | 异常，可能存在线程竞争 |

### cpu_migration_rate

**定义**：每秒 CPU 间迁移次数（线程从一个 CPU 迁移到另一个）

**计算方法**：
```python
def calc_cpu_migration_rate(sched_switches, duration_s):
    """从 sched_switch 中同一 PID 的 CPU 变化计算"""
    migrations = 0
    prev_cpu = {}
    for sw in sched_switches:
        pid = sw['next_pid']
        if pid in prev_cpu and prev_cpu[pid] != sw['cpu']:
            migrations += 1
        prev_cpu[pid] = sw['cpu']
    return migrations / duration_s
```

**阈值**：`> 1000/s` 可能存在 NUMA 迁移问题

---

## 调度指标

### runqueue_avg

**定义**：平均运行队列长度（runnable 线程数）

**计算方法**：

```python
def calc_runqueue_avg(sched_switches, duration_s, window_ms=100):
    """
    方法1: 从 sched_switch 的 prev_state 推断
    prev_state 以 R 开头表示 Runnable
    通过时间窗口采样 runnable 线程数
    """
    # 按时间窗口聚合
    windows = defaultdict(lambda: {'runnable': 0, 'samples': 0})
    
    for sw in sched_switches:
        window = int(sw['ts'] * 1000 / window_ms)  # ms 级窗口
        prev_state = sw.get('prev_state', '')
        if prev_state.startswith('R'):
            windows[window]['runnable'] += 1
        windows[window]['samples'] += 1
    
    # 计算平均
    runnable_per_window = [w['runnable'] for w in windows.values()]
    return sum(runnable_per_window) / max(1, len(runnable_per_window))

def calc_runqueue_from_sched_stat(trace_path):
    """
    方法2: 从 /proc/schedstat 或 schedstat trace 计算
    更精确但需要额外的 schedstat 数据
    """
    # 如果 trace 中包含 sched_runqueue_stats 事件
    pass
```

**阈值**：

| runqueue_avg / cpu_count | 含义 | 严重程度 |
|--------------------------|------|----------|
| < 1.0 | 正常 | - |
| 1.0~1.5 | 轻度压力 | LOW |
| 1.5~2.0 | 中度压力 | MEDIUM |
| > 2.0 | 严重 oversubscription | HIGH |

**场景校准**：
- 训练场景（多 DataLoader worker）：阈值可放宽 50%
- 推理场景（单流）：阈值收紧 30%
- CPU affinity 绑核场景：阈值收紧 20%

### sched_latency_avg / p99 / p99_9

**定义**：线程从 wakeup 到实际被调度的延迟

**计算方法**：

```python
def calc_sched_latency(events):
    """
    从 sched_waking 和 sched_switch 配对计算
    sched_waking: 线程被唤醒（wakeup 发起）
    sched_switch: 线程实际被调度运行
    延迟 = switch.ts - waking.ts (同一 pid)
    """
    wakeup_times = {}  # pid -> wakeup_timestamp
    latencies = []
    
    for event in events:
        if event['type'] == 'sched_waking':
            wakeup_times[event['pid']] = event['ts']
        elif event['type'] == 'sched_switch':
            next_pid = event['next_pid']
            if next_pid in wakeup_times:
                latency = event['ts'] - wakeup_times[next_pid]
                latencies.append(latency * 1000)  # 转为 ms
                del wakeup_times[next_pid]
    
    if not latencies:
        return None
    
    import statistics
    return {
        'avg_ms': statistics.mean(latencies),
        'p50_ms': statistics.median(latencies),
        'p99_ms': sorted(latencies)[int(len(latencies) * 0.99)],
        'p99_9_ms': sorted(latencies)[int(len(latencies) * 0.999)],
        'max_ms': max(latencies),
    }
```

**阈值**：

| 指标 | 正常 | 偏高 | 异常 |
|------|------|------|------|
| avg | < 1ms | 1~5ms | > 5ms |
| p99 | < 5ms | 5~20ms | > 20ms |
| p99.9 | < 10ms | 10~50ms | > 50ms |

**注意**：调度延迟受 CPU 负载、RT 优先级、cgroup 配置影响。高延迟不一定意味着问题，需结合 runqueue 分析。

### wakeup_to_schedule

**定义**：特定线程（如 runtime thread）的唤醒到调度时间

**用途**：专门跟踪关键线程（如 NPU runtime thread）的调度延迟

```python
def calc_thread_sched_latency(events, target_pid):
    """计算指定线程的调度延迟"""
    wakeup_times = {}
    latencies = []
    
    for event in events:
        if event['type'] == 'sched_waking' and event['pid'] == target_pid:
            wakeup_times[target_pid] = event['ts']
        elif event['type'] == 'sched_switch' and event['next_pid'] == target_pid:
            if target_pid in wakeup_times:
                latency = event['ts'] - wakeup_times[target_pid]
                latencies.append(latency * 1000)
                del wakeup_times[target_pid]
    
    return latencies
```

---

## NUMA 指标

### numa_remote_ratio

**定义**：NUMA 远端内存访问占总访问的比例

**计算方法**：

```python
def calc_numa_remote_ratio(events):
    """从 numa_local/numa_remote 事件计算"""
    local_count = 0
    remote_count = 0
    
    for event in events:
        if event['type'] == 'numa_local':
            local_count += 1
        elif event['type'] == 'numa_remote':
            remote_count += 1
    
    total = local_count + remote_count
    if total == 0:
        return None
    
    return remote_count / total * 100
```

**阈值**：

| 值 | 含义 | 严重程度 |
|----|------|----------|
| < 5% | 正常 | - |
| 5~20% | 轻度不均 | LOW |
| 20~40% | 中度不均 | MEDIUM |
| > 40% | 严重 NUMA 问题 | HIGH |

### numa_hit_ratio

**定义**：NUMA 本地访问命中率

**计算方法**：`hit_ratio = numa_hit / (numa_hit + numa_miss)`

**阈值**：`< 80%` 可能存在 NUMA 问题

### numa_hint_faults_local_ratio

**定义**：NUMA hint fault 中本地访问的比例

**计算方法**：`local_ratio = numa_hint_faults_local / numa_hint_faults`

**阈值**：`< 70%` 说明 NUMA 调度策略未生效

---

## 内存指标

### major_page_faults

**定义**：major page fault 数量（需要从磁盘读取）

**计算方法**：

```python
def calc_major_page_faults(events):
    """从 mm_page_fault 事件统计"""
    count = 0
    for event in events:
        if event['type'] == 'mm_page_fault':
            if event.get('fault_type') == 'major':
                count += 1
    return count
```

**阈值**：`> 0` 即为异常，说明存在内存不足导致磁盘换入

### minor_page_fault_rate

**定义**：每秒 minor page fault 数量

**计算方法**：`rate = minor_pf_count / duration_s`

**阈值**：
- `< 1000/s`：正常
- `1000~10000/s`：偏高
- `> 10000/s`：异常，可能存在大量内存分配/释放

### swap_usage

**定义**：swap 使用量

**计算方法**：从 trace 中的 swap 事件或 `/proc/meminfo` 快照获取

**阈值**：`> 0` 即为异常

---

## IO 指标

### io_wait_pct

**定义**：CPU IO 等待时间占比

**计算方法**：

```python
def calc_io_wait_pct(events, duration_s, cpu_count):
    """从 cpu_idle 事件中 iowait 状态计算"""
    iowait_time = 0
    prev_state = {}
    
    for event in events:
        if event['type'] == 'cpu_idle':
            cpu = event['cpu']
            if event['state'] == 'iowait':  # 取决于 trace 格式
                if cpu in prev_state:
                    iowait_time += event['ts'] - prev_state[cpu]
                prev_state[cpu] = event['ts']
    
    total_time = duration_s * cpu_count
    return iowait_time / total_time * 100
```

**阈值**：
- `< 5%`：正常
- `5~15%`：IO 压力
- `> 15%`：严重 IO 瓶颈

### disk_io_bw

**定义**：磁盘读写带宽

**计算方法**：

```python
def calc_disk_io_bw(block_events, duration_s):
    """从 block_rq_complete 事件计算"""
    read_bytes = 0
    write_bytes = 0
    
    for event in block_events:
        if event['type'] == 'block_rq_complete':
            sectors = event.get('sectors', 0)
            bytes_io = sectors * 512
            if event.get('rwbs', '').startswith('W'):
                write_bytes += bytes_io
            else:
                read_bytes += bytes_io
    
    return {
        'read_bw_mbs': read_bytes / duration_s / 1024 / 1024,
        'write_bw_mbs': write_bytes / duration_s / 1024 / 1024,
    }
```

### io_latency

**定义**：IO 请求从 issue 到 complete 的延迟

**计算方法**：

```python
def calc_io_latency(events):
    """从 block_rq_issue 和 block_rq_complete 配对计算"""
    pending = {}  # (dev, sector) -> issue_ts
    latencies = []
    
    for event in events:
        if event['type'] == 'block_rq_issue':
            key = (event['dev'], event['sector'])
            pending[key] = event['ts']
        elif event['type'] == 'block_rq_complete':
            key = (event['dev'], event['sector'])
            if key in pending:
                latency = event['ts'] - pending[key]
                latencies.append(latency * 1000)  # ms
                del pending[key]
    
    if not latencies:
        return None
    
    return {
        'avg_ms': statistics.mean(latencies),
        'p99_ms': sorted(latencies)[int(len(latencies) * 0.99)],
    }
```

**阈值**：
- avg `> 10ms`：IO 延迟偏高
- p99 `> 50ms`：存在 IO 抖动

---

## Runtime 指标

### thread_block_count

**定义**：线程阻塞次数（进入 D 状态）

**计算方法**：
```python
def calc_thread_block_count(sched_switches):
    """从 sched_switch 的 prev_state=D 统计"""
    count = 0
    for sw in sched_switches:
        if sw.get('prev_state', '').startswith('D'):
            count += 1
    return count
```

### dataloader_time_pct

**定义**：DataLoader 耗时占 iteration 总时间比例

**计算方法**：需要 trace 中包含 dataloader 相关的标记事件
```python
def calc_dataloader_time(events, total_duration_s):
    """从 trace 中 dataloader 标记事件计算"""
    dataloader_time = 0
    in_dataloader = False
    dataloader_start = 0
    
    for event in events:
        if event.get('name') == 'dataloader_start':
            in_dataloader = True
            dataloader_start = event['ts']
        elif event.get('name') == 'dataloader_end':
            if in_dataloader:
                dataloader_time += event['ts'] - dataloader_start
                in_dataloader = False
    
    return dataloader_time / total_duration_s * 100
```

### gil_contention_count

**定义**：Python GIL 竞争次数

**计算方法**：从 Python trace 中 `gil_acquire`/`gil_release` 事件计算

---

## Host-NPU 协同指标

### npu_idle_ratio

**定义**：NPU 空闲时间占总时间比例

**计算方法**：

```python
def calc_npu_idle_ratio(npu_events, duration_s):
    """从 NPU 算子执行事件计算空闲时间"""
    busy_time = 0
    for event in npu_events:
        if event.get('cat') in ('Op', 'ACLNN'):
            busy_time += event.get('dur', 0)
    
    total_time = duration_s * 1_000_000  # 转为微秒
    idle_time = total_time - busy_time
    return idle_time / total_time * 100
```

**阈值**：
- `< 5%`：正常
- `5~10%`：轻微空闲
- `> 10%`：存在 NPU 空闲，需排查 host 侧原因

### npu_idle_max_gap

**定义**：NPU 连续空闲的最大时间间隔

**计算方法**：

```python
def calc_npu_idle_gaps(npu_events, threshold_ms=10):
    """识别 NPU 空闲间隔"""
    # 按时间排序 NPU 事件
    sorted_events = sorted(npu_events, key=lambda e: e['ts'])
    
    gaps = []
    for i in range(1, len(sorted_events)):
        prev_end = sorted_events[i-1]['ts'] + sorted_events[i-1].get('dur', 0)
        curr_start = sorted_events[i]['ts']
        gap = curr_start - prev_end
        if gap > threshold_ms * 1000:  # 转为微秒
            gaps.append({
                'ts_start': prev_end,
                'ts_end': curr_start,
                'duration_ms': gap / 1000,
            })
    
    if not gaps:
        return None
    
    return {
        'max_gap_ms': max(g['duration_ms'] for g in gaps),
        'count': len(gaps),
        'avg_gap_ms': sum(g['duration_ms'] for g in gaps) / len(gaps),
        'gaps': gaps,
    }
```

### kernel_launch_gap

**定义**：连续两个 kernel 下发之间的间隔

**计算方法**：

```python
def calc_kernel_launch_gap(host_events, npu_events):
    """从 host 侧 runtime 调用和 NPU 侧算子执行计算"""
    # 找到 host 提交 kernel 的时间序列
    launch_times = sorted([
        e['ts'] for e in host_events 
        if e.get('name') in ('aclmdlExecuteAsync', 'ge:LaunchOp')
    ])
    
    if len(launch_times) < 2:
        return None
    
    gaps = [
        (launch_times[i] - launch_times[i-1]) / 1000  # ms
        for i in range(1, len(launch_times))
    ]
    
    return {
        'avg_ms': statistics.mean(gaps),
        'p99_ms': sorted(gaps)[int(len(gaps) * 0.99)],
        'max_ms': max(gaps),
    }
```

**阈值**：
- avg `> 1ms`：kernel 下发间隔偏大
- p99 `> 10ms`：存在 kernel launch 抖动

---

## 场景校准

不同工作负载下，相同指标可能有不同的"正常"范围。规则引擎支持 `workload_context` 条件：

```yaml
# 训练场景（多 worker）
workload_context:
  match: "training"
  threshold_adjust:
    runqueue_avg_multiplier: 1.5    # 训练时 runqueue 阈值放宽 50%
    context_switch_rate_multiplier: 2.0  # 训练时 cs 阈值放宽 100%

# 推理场景（低延迟）
workload_context:
  match: "inference"
  threshold_adjust:
    sched_latency_max_ms: 2.0       # 推理时调度延迟阈值收紧到 2ms
    npu_idle_ratio_max: 5.0         # 推理时 NPU idle 阈值收紧到 5%
```

Agent 应在分析前询问或推断工作负载类型，并在报告中标注使用的阈值版本。
