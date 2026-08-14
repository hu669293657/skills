# 诊断规则与 Host-NPU 关联分析指南

本文件定义诊断规则的格式、条件语法、以及 Host-NPU 关联分析方法。

---

## 规则格式规范

### YAML 规则结构

```yaml
rule_id: <唯一标识符>              # 如 CPU001, SCHED002
category: <类别>                   # cpu, sched, numa, memory, io, runtime, host_npu
condition: <条件表达式>            # and/or/not 嵌套
severity: <严重程度>               # CRITICAL, HIGH, MEDIUM, LOW, INFO
workload_context:                  # 可选：场景条件
  match: <工作负载类型>            # training, inference, mixed
  exclude: [<排除的类型>]
  threshold_adjust:                # 阈值调整
    <metric>: <multiplier>
diagnosis: <诊断描述>              # 人话描述问题
evidence_required:                 # 必须包含的证据指标
  - <metric_name>
suggestions:                       # 优化建议
  - <建议1>
  - <建议2>
correlation_hint: <关联提示>       # 可选：提示 Agent 做进一步关联分析
```

### 条件表达式语法

```yaml
# 简单条件
condition:
  metric: cpu_util_avg
  op: ">"
  value: 90

# AND 条件
condition:
  and:
    - metric: cpu_util_avg
      op: ">"
      value: 90
    - metric: runqueue_avg
      op: ">"
      ref: cpu_count      # 引用元数据中的 cpu_count

# OR 条件
condition:
  or:
    - metric: sched_latency_p99_ms
      op: ">"
      value: 20
    - metric: sched_latency_p99_9_ms
      op: ">"
      value: 50

# 嵌套条件
condition:
  and:
    - or:
        - metric: cpu_util_avg
          op: ">"
          value: 90
        - metric: cpu_util_max
          op: ">"
          value: 95
    - metric: runqueue_avg
      op: ">"
      ref: cpu_count

# NOT 条件
condition:
  not:
    metric: cpu_balance
    op: "<"
    value: 0.2
```

### 操作符

| 操作符 | 含义 | 示例 |
|--------|------|------|
| `>` | 大于 | `value: 90` |
| `>=` | 大于等于 | `value: 90` |
| `<` | 小于 | `value: 10` |
| `<=` | 小于等于 | `value: 10` |
| `==` | 等于 | `value: 0` |
| `!=` | 不等于 | `value: 0` |
| `in` | 在列表中 | `value: [1, 2, 3]` |
| `not_in` | 不在列表中 | `value: [0]` |

### 引用元数据

规则中可以使用 `ref` 引用 metrics JSON 中 `metadata` 字段的值：

```yaml
condition:
  metric: runqueue_avg
  op: ">"
  ref: cpu_count     # 引用 metadata.cpu_count
```

---

## Host-NPU 关联分析

### 分析目标

识别 host 侧事件与 NPU idle 之间的因果关系，回答"为什么 NPU 在等待"。

### 因果模式库

#### 模式 A: CPU_SCHED_CONTENTION

```
CPU runqueue 高 → runtime thread 调度延迟 → kernel launch 延迟 → NPU idle
```

**检测条件**：
1. 存在 NPU idle gap > 10ms
2. idle gap 前 50ms 内，CPU runqueue > cpu_count
3. idle gap 前 50ms 内，runtime thread 存在 sched_latency > 5ms

**证据**：
```json
{
  "pattern": "CPU_SCHED_CONTENTION",
  "npu_idle_gap": {"ts": 1234600, "duration_ms": 35},
  "preceding_events": [
    {"ts": 1234590, "type": "runqueue_spike", "value": 28},
    {"ts": 1234595, "type": "sched_wakeup_delay", "pid": 999, "latency_ms": 25}
  ],
  "confidence": 0.85
}
```

#### 模式 B: IO_BLOCK

```
磁盘 IO 等待 → 数据加载阻塞 → DataLoader 线程 D 状态 → NPU idle
```

**检测条件**：
1. 存在 NPU idle gap > 10ms
2. idle gap 前 100ms 内，存在 block_rq_issue 但无对应的 block_rq_complete
3. idle gap 前 100ms 内，存在线程进入 D 状态 (uninterruptible sleep)

**证据**：
```json
{
  "pattern": "IO_BLOCK",
  "npu_idle_gap": {"ts": 1234700, "duration_ms": 50},
  "preceding_events": [
    {"ts": 1234680, "type": "block_rq_issue", "dev": "8,0", "sectors": 1024},
    {"ts": 1234685, "type": "sched_switch_D", "pid": 12345}
  ],
  "confidence": 0.78
}
```

#### 模式 C: MEMORY_PRESSURE

```
major page fault → 内存不足 → 数据准备阻塞 → NPU idle
```

**检测条件**：
1. 存在 NPU idle gap > 10ms
2. idle gap 前 200ms 内，存在 major page fault
3. idle gap 前 200ms 内，minor page fault rate > 10000/s

#### 模式 D: GIL_CONTENTION

```
Python GIL 竞争 → 主线程阻塞 → kernel launch 延迟 → NPU idle
```

**检测条件**：
1. 存在 NPU idle gap > 10ms
2. idle gap 前 50ms 内，存在 GIL acquire 等待事件
3. 主线程 (python main thread) 调度延迟 > 5ms

#### 模式 E: KERNEL_LAUNCH_GAP

```
runtime 调用阻塞 → kernel 下发间隔大 → NPU 计算完成后空闲等待
```

**检测条件**：
1. 存在 NPU idle gap > 10ms
2. idle gap 前一个 kernel 完成后，下一个 kernel 下发间隔 > idle gap 的 80%
3. host 侧无明显的 CPU/IO/内存异常

**这通常意味着**：runtime 层存在同步等待、锁竞争或不必要的 barrier。

### 关联分析算法

```python
def analyze_host_npu_correlation(host_metrics, npu_events, host_events):
    """
    Host-NPU 关联分析主函数
    """
    # 1. 识别 NPU idle gaps
    idle_gaps = identify_npu_idle_gaps(npu_events, threshold_ms=10)
    
    if not idle_gaps:
        return {"correlation_found": False, "reason": "no_npu_idle_gaps"}
    
    # 2. 对每个 idle gap，回溯 host 事件
    correlations = []
    for gap in idle_gaps:
        preceding = get_preceding_events(host_events, gap['ts_start'], lookback_ms=100)
        
        # 3. 匹配因果模式
        pattern = match_causal_pattern(preceding, gap, host_metrics)
        
        if pattern:
            correlations.append({
                'npu_idle_gap': gap,
                'preceding_events': preceding,
                'matched_pattern': pattern['name'],
                'confidence': pattern['confidence'],
            })
    
    # 4. 统计模式分布
    pattern_counts = Counter(c['matched_pattern'] for c in correlations)
    
    # 5. 确定主要瓶颈
    primary = pattern_counts.most_common(1)[0] if pattern_counts else None
    
    return {
        'correlation_found': len(correlations) > 0,
        'total_idle_gaps': len(idle_gaps),
        'correlated_gaps': len(correlations),
        'pattern_summary': dict(pattern_counts),
        'primary_bottleneck': primary[0] if primary else None,
        'correlations': correlations,
    }

def match_causal_pattern(preceding_events, gap, host_metrics):
    """匹配因果模式"""
    patterns = []
    
    # 模式 A: CPU 调度竞争
    runqueue_high = any(
        e.get('runnable', 0) > host_metrics['metadata']['cpu_count']
        for e in preceding_events
    )
    sched_delay = any(
        e.get('latency_ms', 0) > 5
        for e in preceding_events
        if e.get('type') == 'sched_wakeup_delay'
    )
    if runqueue_high and sched_delay:
        patterns.append({
            'name': 'CPU_SCHED_CONTENTION',
            'confidence': 0.85,
        })
    
    # 模式 B: IO 阻塞
    io_pending = any(
        e.get('type') == 'block_rq_issue' for e in preceding_events
    )
    thread_d_state = any(
        e.get('prev_state', '').startswith('D') for e in preceding_events
    )
    if io_pending and thread_d_state:
        patterns.append({
            'name': 'IO_BLOCK',
            'confidence': 0.78,
        })
    
    # 模式 C: 内存压力
    major_pf = any(
        e.get('type') == 'mm_page_fault_major' for e in preceding_events
    )
    if major_pf:
        patterns.append({
            'name': 'MEMORY_PRESSURE',
            'confidence': 0.72,
        })
    
    # 模式 E: Kernel launch gap（无其他异常时）
    if not patterns and gap['duration_ms'] > 20:
        patterns.append({
            'name': 'KERNEL_LAUNCH_GAP',
            'confidence': 0.60,
        })
    
    # 返回置信度最高的模式
    if patterns:
        return max(patterns, key=lambda p: p['confidence'])
    return None
```

### 置信度计算

置信度基于以下因素：
- **模式匹配强度**：满足的条件数 / 总条件数
- **时间相关性**：host 事件与 NPU idle 的时间接近程度
- **频率一致性**：该模式在所有 idle gap 中的出现频率

```python
def calc_confidence(pattern_matches, gap, preceding_events):
    """计算关联置信度"""
    base_confidence = pattern_matches['confidence']
    
    # 时间相关性加成
    if preceding_events:
        closest_ts = max(e['ts'] for e in preceding_events)
        time_diff = gap['ts_start'] - closest_ts
        if time_diff < 10:  # 10ms 内
            base_confidence += 0.05
        elif time_diff > 50:
            base_confidence -= 0.10
    
    # 频率一致性加成
    frequency = pattern_matches.get('frequency', 0)
    if frequency > 0.8:  # 80% 以上的 idle gap 都匹配此模式
        base_confidence += 0.10
    
    return min(base_confidence, 1.0)
```

---

## 规则优先级与冲突处理

当多个规则同时匹配时：

1. **严重程度优先**：CRITICAL > HIGH > MEDIUM > LOW > INFO
2. **同级别时**：类别优先级 host_npu > cpu > sched > memory > io > runtime
3. **因果关系优先**：如果规则 A 的根因可以解释规则 B 的现象，优先报告 A
4. **不隐藏现象**：即使有根因规则，仍报告被解释的现象规则，但标注 "caused by <rule_id>"

---

## 规则版本管理

规则文件支持版本标注：

```yaml
# rules/cpu.yaml
version: "1.0"
rules:
  - rule_id: CPU001
    # ...
```

Agent 在报告中应标注使用的规则版本，便于结果追溯。

---

## 自定义规则扩展

用户可以添加自定义规则文件到 `rules/` 目录。规则引擎会自动加载所有 `.yaml` 文件。

自定义规则示例：

```yaml
# rules/custom_dataloader.yaml
version: "1.0"
rules:
  - rule_id: CUST001
    category: runtime
    condition:
      and:
        - metric: dataloader_time_pct
          op: ">"
          value: 40
        - metric: npu_idle_ratio
          op: ">"
          value: 10
    severity: MEDIUM
    diagnosis: "DataLoader 耗时过长导致 NPU 空闲"
    evidence_required:
      - dataloader_time_pct
      - npu_idle_ratio
    suggestions:
      - "增加 num_workers"
      - "使用 pin_memory"
      - "启用 prefetch_factor"
      - "检查数据存储路径是否在高速存储上"
    correlation_hint: "检查 NPU idle 时段是否与 DataLoader fetch 时段对齐"
```
