---
name: "host-trace-diagnosis"
description: "Diagnoses host-side performance issues from trace files (perfetto/ftrace/msprof/perf). Invoke when analyzing trace for host bottlenecks in Ascend NPU or general HPC scenarios."
---

# Host Trace 智能诊断 Skill

## 概述

本 Skill 指导 Agent 分析 host 侧 trace 文件，定位 CPU、调度、NUMA、内存、IO 及 Host-NPU 协同等维度的性能瓶颈。适用于昇腾 NPU 训练/推理性能调优场景，也兼容通用 HPC 环境。

**核心原则：Agent 是编排者，不是计算器。** Agent 负责格式识别、流程编排、指标解读和报告生成；数值计算交给 Python 脚本完成。

---

## 对参考方案的评估

### 认可的设计

| 设计点 | 评价 |
|--------|------|
| 三层架构（解析层 + 特征层 + 诊断层） | 正确，职责分离清晰 |
| 问题分类体系（CPU/调度/内存/IO/Runtime/Host-NPU） | 全面，覆盖昇腾场景核心问题 |
| 规则 + LLM 混合诊断 | 务实，规则保证确定性，LLM 负责解释 |
| Host-NPU 关联分析 | 最有价值的设计，这是昇腾场景的核心差异点 |
| 分阶段开发路线 | 合理，先解决数据再叠智能 |

### 关键改进点（本 Skill 的独立判断）

1. **大文件处理需要具体工程方案**：参考方案只说"不要让 LLM 读 trace"但没给落地策略。本 Skill 定义了五级处理管线：二进制转文本 → 流式读取 → 时间窗口聚合 → 事件采样 → 早期过滤，将 GB 级数据压缩到 KB 级指标 JSON。

2. **格式适配需要 Adapter 模式而非简单目录**：perfetto 是 protobuf 二进制，ftrace 是文本行，msprof 是 JSON/CSV，perf 是二进制。四种格式结构完全不同，不能靠一套 parser 通吃。本 Skill 为每种格式定义了独立的检测信号和转换命令。

3. **Agent 是编排者而非系统构建者**：参考方案描述的是"构建一个完整软件系统"。但 Skill 场景下，Agent 本身就是编排者——它检测格式、编写/运行预处理脚本、解读结果、应用规则、生成报告。不需要 LangChain 或 Agent SDK，Skill 内置的脚本模板就够了。

4. **增加预检（Triage）步骤**：参考方案缺少"先看全局再深入"的步骤。分析 GB 级 trace 前必须先做预检：文件大小、格式、时间范围、事件总数、事件类型分布。这决定了后续处理策略。

5. **阈值需要场景校准**：参考方案的阈值（runqueue > core, sched_latency > 5ms）是静态的。训练场景下 DataLoader 多 worker 导致高 runqueue 可能是正常的。本 Skill 的规则支持 `workload_context` 条件，区分训练/推理场景。

6. **Host-NPU 关联需要具体方法**：参考方案只说"要关联"。本 Skill 定义了具体方法：时间线对齐 → NPU idle gap 识别 → idle 期间 host 事件回溯 → 因果链构建。

7. **错误处理和降级策略**：参考方案没有考虑 parser 失败、格式未知、trace 损坏等情况。本 Skill 定义了明确的降级路径。

8. **支持迭代分析**：先快速扫描出明显问题，再深入分析复杂问题。不是一次性全量处理。

---

## 架构设计

```
                    Trace 文件 (.trace/.txt/.json/.csv/.perf/.dat)
                              │
                    ┌─────────▼─────────┐
                    │   Step 0: 预检    │  文件大小/格式/时间范围/事件统计
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Step 1: 格式适配   │  检测格式 → 二进制转文本(如需)
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Step 2: 流式预处理  │  分块读取 → 事件过滤 → 窗口聚合
                    │   (Python 脚本)    │  输出: structured_events.jsonl
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Step 3: 特征提取   │  事件 → 指标 (CPU/sched/numa/...)
                    │   (Python 脚本)    │  输出: host_metrics.json
                    └─────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              │                               │
    ┌─────────▼─────────┐           ┌─────────▼─────────┐
    │ Step 4: 规则诊断   │           │ Step 5: Host-NPU  │
    │ (YAML 规则匹配)    │           │ 关联分析           │
    │ 输出: matched_rules│           │ (如存在 NPU trace) │
    └─────────┬─────────┘           └─────────┬─────────┘
              │                               │
              └───────────────┬───────────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Step 6: 报告生成   │  Agent 综合所有结果
                    │   (Agent 推理)     │  输出: 诊断报告
                    └───────────────────┘
```

### 数据体积变化

```
原始 trace (MB ~ GB)
    │  流式预处理 + 窗口聚合
    ▼
结构化事件 (KB ~ MB)
    │  特征提取
    ▼
指标 JSON (KB)
    │  规则匹配 + Agent 推理
    ▼
诊断报告 (KB)
```

---

## 工作流程

### Step 0: 预检（Triage）

**目标**：在深入分析前快速了解 trace 文件的全貌，决定后续处理策略。

**操作**：

1. 检查文件大小：
   ```bash
   # PowerShell
   Get-Item <trace_file> | Select-Object Name, Length, LastWriteTime
   ```

2. 判断文件大小级别：
   - `< 10MB`：可直接读取分析
   - `10MB ~ 100MB`：需要流式处理
   - `> 100MB`：必须流式处理 + 窗口聚合 + 采样

3. 检测文件格式（详见 `references/trace-formats.md`）：
   - 检查文件扩展名
   - 读取前 1KB 内容判断格式特征
   - 检查 magic bytes（二进制格式）

4. 快速统计（对文本格式，采样前 10000 行）：
   - 时间范围（first_ts ~ last_ts）
   - 事件类型分布
   - CPU 数量
   - PID/TID 数量

**输出**：预检结果 JSON
```json
{
  "file": "trace.txt",
  "size_mb": 850,
  "format": "ftrace",
  "time_range": {"start": 1234567.890, "end": 1234597.890, "duration_s": 30},
  "event_types": {"sched_switch": 500000, "sched_wakeup": 200000, ...},
  "cpu_count": 64,
  "pid_count": 150,
  "processing_strategy": "streaming_aggregation"
}
```

**决策**：根据预检结果选择处理策略，告知用户预期处理时间。

---

### Step 1: 格式检测与适配

**目标**：识别 trace 格式，必要时将二进制格式转换为可解析的文本/JSON。

**支持的格式**：

| 格式 | 扩展名 | 检测信号 | 转换命令 |
|------|--------|----------|----------|
| Perfetto | .trace, .perfetto-trace | magic: `0x0a` + protobuf field | `traceconv json <input> <output>` |
| ftrace (trace-cmd) | .txt, .trace | `trace-cmd report` output 或 `<task>-<pid>` 行格式 | 原生文本，无需转换 |
| msprof | .json, .csv | JSON 含 `"name":"acl"` 或 CSV 含 op 列 | 原生格式，直接解析 |
| perf | .perf, .data | magic: `PERFILE2` | `perf script -i <input> > output.txt` |
| systrace | .html | HTML 含 `linux perf` | 已弃用，建议用 perfetto |

**详细格式规范**：参见 `references/trace-formats.md`

**操作**：

1. 根据预检结果确定格式
2. 如果是二进制格式（perfetto/perf/trace-cmd），先运行转换命令
3. 转换后的文本/JSON 文件作为后续步骤的输入
4. 如果转换工具不可用，降级为"原始事件采样分析"模式

**关键决策**：
- 如果格式无法识别 → 读取前 1KB，尝试正则匹配常见模式
- 如果转换工具缺失 → 告知用户需要安装的工具，同时尝试直接解析
- 如果文件是自定义格式 → 采样分析事件结构，构建 ad-hoc parser

---

### Step 2: 流式预处理

**目标**：将大文件trace转换为结构化事件流，同时大幅降低数据量。

**策略选择**（根据文件大小）：

| 文件大小 | 策略 | 输出大小 |
|----------|------|----------|
| < 10MB | 全量解析 | 原始大小 |
| 10~100MB | 流式读取 + 事件过滤 | ~1/5 原始 |
| 100MB~1GB | 流式读取 + 窗口聚合(100ms) | ~1/100 原始 |
| > 1GB | 流式读取 + 窗口聚合(1s) + 采样 | ~1/1000 原始 |

**处理管线**：

```
原始文件
  │
  ▼ ① 流式读取（逐行/逐事件，不一次性加载）
  │
  ▼ ② 早期过滤（丢弃无关事件类型）
  │
  ▼ ③ 时间窗口聚合（按时间桶聚合统计量）
  │
  ▼ ④ 关键事件保留（异常事件完整保留）
  │
  ▼ 输出: structured_events.jsonl
```

**脚本模板**：使用 `scripts/trace_preprocessor.py`，支持：
- 自动格式检测
- 流式读取（generator，不加载全文件到内存）
- 可配置的时间窗口大小
- 可配置的事件类型过滤器
- 关键事件阈值（如 sched_latency > 10ms 的事件完整保留）

**详细策略**：参见 `references/large-file-strategy.md`

**输出格式**（JSONL，每行一个 JSON 对象）：

```jsonl
{"type":"window_summary","ts":1234560,"window_ms":100,"sched_switch_count":350,"runnable_avg":8,"sched_latency_max":15}
{"type":"window_summary","ts":1234660,"window_ms":100,"sched_switch_count":420,"runnable_avg":12,"sched_latency_max":25}
{"type":"key_event","ts":1234700,"event":"sched_wakeup_delay","pid":12345,"latency_ms":35,"cpu":8}
{"type":"npu_event","ts":1234800,"op":"MatMul","duration_ms":5,"device_id":0}
```

---

### Step 3: 特征提取

**目标**：从结构化事件中计算性能指标，生成 Agent 可解读的指标 JSON。

**指标体系**：

| 类别 | 指标 | 计算方法 | 异常阈值 |
|------|------|----------|----------|
| CPU | cpu_util_avg | 1 - idle_time/total_time | > 90% |
| CPU | cpu_util_max | 单核最大利用率 | > 95% |
| CPU | cpu_balance | std(cpu_util_per_core) / mean | > 0.5 |
| CPU | context_switch_rate | sched_switch_count / duration_s | > 50000/s |
| 调度 | runqueue_avg | avg(runnable_threads) | > cpu_count |
| 调度 | runqueue_max | max(runnable_threads) | > 2*cpu_count |
| 调度 | sched_latency_avg | avg(wakeup_to_schedule_time) | > 5ms |
| 调度 | sched_latency_p99 | p99(wakeup_to_schedule_time) | > 20ms |
| NUMA | numa_remote_ratio | remote_access/total_access | > 20% |
| NUMA | numa_local_bw | local_access_bandwidth | workload dependent |
| 内存 | major_page_faults | count(MAJOR page fault) | > 0 |
| 内存 | swap_usage | swap_bytes_used | > 0 |
| 内存 | minor_page_fault_rate | count(MINOR page fault)/duration_s | > 10000/s |
| IO | io_wait_time | iowait_time/total_time | > 5% |
| IO | disk_io_bw | read_bw + write_bw | workload dependent |
| Runtime | thread_block_count | count(blocked threads) | > 0 |
| Runtime | dataloader_time | dataloader_duration/total_duration | > 30% |
| Host-NPU | npu_idle_ratio | idle_time/total_time | > 10% |
| Host-NPU | npu_idle_max_gap | max(idle_duration) | > 20ms |
| Host-NPU | kernel_launch_gap | avg(kernel_submit_gap) | > 1ms |

**脚本模板**：使用 `scripts/feature_extractor.py`

**详细指标定义和计算公式**：参见 `references/metrics-guide.md`

**输出格式**：
```json
{
  "metadata": {
    "trace_file": "trace.txt",
    "duration_s": 30,
    "cpu_count": 64,
    "workload_type": "training",
    "parallel_config": "TP8"
  },
  "cpu": {
    "cpu_util_avg": 85.2,
    "cpu_util_max": 98.5,
    "cpu_balance": 0.68,
    "context_switch_rate": 65000,
    "per_core_util": [95, 92, 88, ...]
  },
  "sched": {
    "runqueue_avg": 18,
    "runqueue_max": 35,
    "sched_latency_avg_ms": 12.5,
    "sched_latency_p99_ms": 45,
    "sched_latency_p99_9_ms": 120
  },
  "numa": {
    "remote_ratio": 28.5,
    "local_access_count": 850000,
    "remote_access_count": 340000
  },
  "memory": {
    "major_page_faults": 2,
    "minor_page_fault_rate": 3000,
    "swap_usage_kb": 0
  },
  "io": {
    "io_wait_pct": 8.5,
    "disk_read_bw_mbs": 150,
    "disk_write_bw_mbs": 80
  },
  "runtime": {
    "thread_block_count": 15,
    "dataloader_time_pct": 35,
    "gil_contention_count": 0
  },
  "host_npu": {
    "npu_idle_ratio": 15.2,
    "npu_idle_max_gap_ms": 35,
    "kernel_launch_gap_avg_ms": 2.5,
    "correlation_found": true
  },
  "timeline_windows": [
    {"ts": 1234560, "cpu_util": 85, "runqueue": 12, "npu_idle": 10},
    {"ts": 1234660, "cpu_util": 95, "runqueue": 25, "npu_idle": 25},
    ...
  ]
}
```

---

### Step 4: 规则诊断

**目标**：将指标与诊断规则匹配，输出确定性问题清单。

**规则引擎**：使用 `scripts/rule_engine.py` 加载 `rules/` 目录下的 YAML 规则文件。

**规则格式**（YAML）：
```yaml
rule_id: CPU001
category: cpu
condition:
  and:
    - metric: cpu_util_avg
      op: ">"
      value: 90
    - metric: runqueue_avg
      op: ">"
      ref: cpu_count
severity: HIGH
workload_context:
  exclude: ["inference_single_stream"]
diagnosis: "CPU 调度拥塞，runnable 任务数超过 CPU 核心数"
evidence_required:
  - cpu_util_avg
  - runqueue_avg
  - context_switch_rate
suggestions:
  - "检查 OMP_NUM_THREADS 设置"
  - "检查 CPU affinity 绑定"
  - "降低 DataLoader num_workers"
```

**规则文件**：
- `rules/cpu.yaml` — CPU 利用率/均衡性/oversubscription
- `rules/sched.yaml` — 调度延迟/runqueue/context switch
- `rules/numa.yaml` — NUMA 远端访问/带宽
- `rules/memory.yaml` — page fault/swap/内存带宽
- `rules/io.yaml` — IO 等待/磁盘带宽
- `rules/runtime.yaml` — GIL/dataloader/runtime 阻塞
- `rules/host_npu.yaml` — Host-NPU 协同/kernel launch gap

**详细规则定义**：参见 `references/diagnosis-rules.md`

**输出格式**：
```json
{
  "matched_rules": [
    {
      "rule_id": "CPU001",
      "severity": "HIGH",
      "diagnosis": "CPU 调度拥塞",
      "evidence": {"cpu_util_avg": 95, "runqueue_avg": 18, "cpu_count": 16},
      "suggestions": ["检查 OMP_NUM_THREADS", "检查 CPU affinity"]
    },
    {
      "rule_id": "SCHED002",
      "severity": "MEDIUM",
      "diagnosis": "调度延迟偏高",
      "evidence": {"sched_latency_avg_ms": 12.5, "sched_latency_p99_ms": 45},
      "suggestions": ["检查 CPU oversubscription", "检查 rt priority"]
    }
  ],
  "unmatched_but_close": [
    {
      "rule_id": "NUMA001",
      "severity": "LOW",
      "margin": 0.085,
      "diagnosis": "NUMA 远端访问比例接近阈值"
    }
  ]
}
```

---

### Step 5: Host-NPU 关联分析

**目标**：识别 host 事件与 NPU idle 之间的因果关系，这是昇腾场景价值最高的分析。

**前提条件**：trace 中包含 NPU 相关事件（msprof 数据或 host trace 中有 NPU runtime 调用事件）。

**分析方法**：

1. **时间线对齐**：确保 host 事件和 NPU 事件使用相同的时间基准
2. **NPU idle gap 识别**：找到 NPU 连续空闲超过阈值的时段（默认 > 10ms）
3. **Host 事件回溯**：对每个 NPU idle gap，回溯前 50ms 的 host 事件
4. **因果模式匹配**：
   - 模式 A：CPU runqueue 高 → runtime thread 调度延迟 → NPU idle
   - 模式 B：IO wait → 数据加载阻塞 → NPU idle
   - 模式 C：内存 page fault → 数据准备阻塞 → NPU idle
   - 模式 D：GIL contention → Python 线程阻塞 → NPU idle
   - 模式 E：kernel launch gap → 提交速率不足 → NPU idle
5. **相关性量化**：计算 NPU idle 时段内 host 异常事件的出现频率 vs 正常时段

**输出**：
```json
{
  "correlation_analysis": {
    "npu_idle_gaps": [
      {
        "ts_start": 1234600,
        "duration_ms": 35,
        "preceding_host_events": [
          {"ts": 1234590, "type": "sched_wakeup_delay", "latency_ms": 25},
          {"ts": 1234595, "type": "runqueue_spike", "value": 28}
        ],
        "matched_pattern": "CPU_SCHED_CONTENTION",
        "confidence": 0.85
      }
    ],
    "pattern_summary": {
      "CPU_SCHED_CONTENTION": {"count": 45, "avg_confidence": 0.82},
      "IO_BLOCK": {"count": 12, "avg_confidence": 0.71},
      "KERNEL_LAUNCH_GAP": {"count": 8, "avg_confidence": 0.65}
    },
    "primary_bottleneck": "CPU_SCHED_CONTENTION",
    "causal_chain": "CPU runqueue 高 → runtime thread 调度延迟 → kernel launch 延迟 → NPU idle 35ms → step time 增加"
  }
}
```

**详细关联分析指南**：参见 `references/diagnosis-rules.md` 的 Host-NPU 关联部分

---

### Step 6: 报告生成

**目标**：Agent 综合所有分析结果，生成结构化诊断报告。

**Agent 输入**：
- `host_metrics.json`（Step 3 输出）
- `matched_rules.json`（Step 4 输出）
- `correlation_analysis.json`（Step 5 输出，如存在）
- 预检结果（Step 0 输出）

**Agent 职责**：
1. 汇总匹配的规则，按严重程度排序
2. 构建证据链：每个问题必须有指标值 + 阈值 + 实际值
3. 进行根因推理：综合多个指标推断根本原因
4. 给出优化建议：基于根因给出可操作的建议
5. 生成时间线分析：描述问题发生的时间模式

**报告格式**：参见 `references/report-template.md`

**报告结构**：
```markdown
# Host Trace 诊断报告

## 1. 执行摘要
- 分析文件: trace.txt (850MB, ftrace格式)
- 分析时长: 30s
- 发现问题: 3个 (1 HIGH, 2 MEDIUM)
- 主要瓶颈: CPU调度拥塞导致NPU空闲

## 2. 关键指标总览
| 指标 | 值 | 阈值 | 状态 |
|------|-----|------|------|
| CPU利用率(avg) | 95% | >90% | ⚠ 异常 |
| runqueue(avg) | 18 | >16(核心数) | ⚠ 异常 |
| 调度延迟(p99) | 45ms | >20ms | ⚠ 异常 |
| NPU空闲率 | 15.2% | >10% | ⚠ 异常 |

## 3. 问题诊断

### 3.1 [HIGH] CPU调度拥塞
**证据**:
- CPU平均利用率 95%，超过90%阈值
- runqueue平均18，超过CPU核心数16
- 调度延迟p99达45ms，超过20ms阈值
- context switch速率 65000/s

**根因推断**:
DataLoader worker线程(8个)与runtime线程竞争CPU，
导致runtime线程无法及时调度，kernel提交延迟

**Host-NPU关联**:
NPU空闲时段(35ms)前50ms内，100%出现CPU runqueue峰值
相关性置信度: 0.85

**建议**:
1. 降低DataLoader num_workers从8降至4
2. 设置CPU affinity: runtime线程绑定独立核心
3. 调整OMP_NUM_THREADS避免oversubscription

### 3.2 [MEDIUM] 调度延迟偏高
...

## 4. 时间线分析
[时间段分析，描述问题的时序模式]

## 5. 优化建议优先级
1. [立即] 设置CPU affinity
2. [立即] 降低num_workers
3. [短期] 调整OMP_NUM_THREADS
4. [中期] 考虑NUMA绑定

## 6. 分析元数据
- 分析时间: 2024-01-15 10:30:00
- 处理策略: streaming_aggregation
- 窗口大小: 100ms
- 规则版本: v1.0
```

---

## 大文件处理策略

### 核心原则

**永远不要一次性加载整个文件到内存。** 使用流式处理（generator/逐行读取），仅在内存中保留聚合结果。

### 五级处理管线

1. **二进制转文本**（如需要）：perfetto/perf/trace-cmd → 文本/JSON
2. **流式读取**：逐行/逐事件读取，使用 Python generator
3. **早期过滤**：在读取时即过滤无关事件类型，减少处理量
4. **时间窗口聚合**：按时间桶（100ms/1s）聚合统计量，丢弃原始事件
5. **关键事件保留**：异常事件（如延迟超过阈值）完整保留，不聚合

### 具体实现

使用 `scripts/trace_preprocessor.py`，核心设计：

```python
# 流式读取，不加载全文件
def stream_events(file_path, format_type):
    """逐事件 yield，不加载全文件到内存"""
    with open(file_path, 'r') as f:
        for line in f:
            event = parse_line(line, format_type)
            if event and should_keep(event, filter_types):
                yield event

# 时间窗口聚合
def aggregate_windows(events, window_ms=100):
    """将事件流聚合为时间窗口统计"""
    window = []
    window_start = None
    for event in events:
        if window_start is None:
            window_start = event['ts']
        if event['ts'] - window_start >= window_ms:
            yield summarize_window(window, window_start)
            window = []
            window_start = event['ts']
        window.append(event)
    if window:
        yield summarize_window(window, window_start)
```

### 内存估算

| 原始文件大小 | 策略 | 峰值内存 | 输出大小 | 处理时间(估) |
|-------------|------|---------|---------|-------------|
| 10MB | 全量解析 | ~50MB | ~10MB | < 5s |
| 100MB | 流式+过滤 | ~10MB | ~5MB | ~15s |
| 500MB | 流式+聚合(100ms) | ~5MB | ~500KB | ~60s |
| 1GB | 流式+聚合(1s) | ~5MB | ~100KB | ~120s |
| 5GB | 流式+聚合(1s)+采样 | ~5MB | ~50KB | ~600s |

**详细策略文档**：参见 `references/large-file-strategy.md`

---

## Trace 格式速查

### Perfetto Trace (.trace)

- **格式**：protobuf 二进制
- **检测**：文件头 `0x0a` 后跟 trace pacet 结构
- **转换**：`traceconv json trace.json` 或 `traceconv text trace.txt`
- **事件结构**：
  ```json
  {"name":"sched_switch", "ts":1234567890, "dur":0, "cat":"sched",
   "args":{"prev_pid":123, "next_pid":456, "cpu":8}}
  ```

### ftrace 文本 (.txt)

- **格式**：文本行
- **检测**：行格式 `<task>-<pid> [<cpu>] <flags> <ts>: <event>: <data>`
- **无需转换**：直接解析
- **事件示例**：
  ```
  python-1234 [008] d... 1234567.890: sched_switch: prev_pid=1234 next_pid=5678
  python-1234 [008] d... 1234567.891: sched_wakeup: pid=5678 target_cpu=008
  ```

### msprof (.json/.csv)

- **格式**：JSON 或 CSV
- **检测**：JSON 含 `"name":"acl"` 或 CSV 含 op 列
- **无需转换**：直接解析
- **事件结构**（JSON）：
  ```json
  {"name":"MatMul", "ts":1234567890, "dur":5000, "cat":"Op",
   "args":{"device_id":0, "stream_id":0}}
  ```

### perf data (.perf/.data)

- **格式**：二进制
- **检测**：magic `PERFILE2`
- **转换**：`perf script -i trace.perf > trace.txt`
- **事件结构**（转换后）：
  ```
  python  1234 [008] 1234567.890: sched:sched_switch
  python  1234 [008] 1234567.891: sched:sched_wakeup
  ```

**详细格式规范**：参见 `references/trace-formats.md`

---

## 错误处理与降级策略

| 场景 | 降级策略 |
|------|----------|
| 格式无法识别 | 采样前1KB，正则匹配常见模式，构建 ad-hoc parser |
| 二进制转换工具缺失 | 告知用户需安装的工具；尝试直接采样解析二进制 |
| 文件过大(>5GB)且无流式工具 | 使用 `head/tail/split` 分片处理；告知用户可能耗时较长 |
| trace 损坏/截断 | 从可解析部分提取信息；在报告中标注数据完整性 |
| NPU trace 缺失 | 跳过 Step 5，仅输出 host 侧分析；建议用户补充 NPU trace |
| 规则全不匹配 | 输出指标 JSON 供人工分析；Agent 尝试基于指标做自由推理 |
| 脚本执行失败 | Agent 回退到"采样读取+人工推理"模式 |

---

## 迭代分析模式

本 Skill 支持渐进式分析，避免一次性全量处理：

1. **快速扫描**（< 30s）：采样前 10000 行，快速判断是否存在明显异常
2. **标准分析**（1~5min）：流式处理全文件，提取完整指标
3. **深度分析**（5~15min）：在标准分析基础上，对异常时段做局部全量解析

Agent 应根据用户需求和文件大小选择合适的分析深度。如果快速扫描发现明显问题，可直接报告并建议是否需要深度分析。

---

## 参考文件索引

| 文件 | 内容 | 何时使用 |
|------|------|----------|
| `references/trace-formats.md` | 各格式详细规范、解析正则、字段定义 | Step 1 格式检测与适配时 |
| `references/large-file-strategy.md` | 大文件处理策略详解、分片方案、内存优化 | Step 2 流式预处理时 |
| `references/metrics-guide.md` | 指标计算公式、阈值定义、场景校准 | Step 3 特征提取时 |
| `references/diagnosis-rules.md` | 规则定义、条件语法、Host-NPU 关联模式 | Step 4/5 诊断与关联时 |
| `references/report-template.md` | 报告格式模板、示例报告 | Step 6 报告生成时 |
| `scripts/trace_preprocessor.py` | 流式预处理脚本（格式检测+流式读取+窗口聚合） | Step 2 执行时 |
| `scripts/feature_extractor.py` | 特征提取脚本（事件→指标） | Step 3 执行时 |
| `scripts/rule_engine.py` | 规则匹配引擎（YAML 规则加载+条件评估） | Step 4 执行时 |
| `rules/*.yaml` | 诊断规则定义文件 | Step 4 规则匹配时 |

---

## 使用示例

### 场景 1：分析 ftrace 文件

```
用户：帮我分析这个 trace.txt 文件，看看 host 侧有没有问题
文件：trace.txt (500MB, ftrace格式)

Agent 执行流程：
1. 预检：检测文件大小 500MB，格式 ftrace，时间范围 30s
2. 格式适配：ftrace 是文本格式，无需转换
3. 流式预处理：运行 trace_preprocessor.py，窗口 100ms
4. 特征提取：运行 feature_extractor.py，计算 CPU/sched/numa 指标
5. 规则诊断：运行 rule_engine.py，匹配 CPU001/SCHED002
6. 报告生成：Agent 综合结果，生成诊断报告
```

### 场景 2：分析 perfetto trace（含 NPU 数据）

```
用户：这个 perfetto trace 有 NPU idle，帮我看看 host 侧什么原因
文件：trace.perfetto-trace (1.2GB, perfetto格式)

Agent 执行流程：
1. 预检：检测文件大小 1.2GB，格式 perfetto，需转换
2. 格式适配：运行 traceconv json 转换为文本
3. 流式预处理：窗口 1s 聚合 + 关键事件保留
4. 特征提取：计算完整指标体系（含 host_npu 指标）
5. 规则诊断：匹配规则 + Host-NPU 关联分析
6. 报告生成：输出含因果链的完整报告
```

### 场景 3：快速诊断

```
用户：快速看看这个 trace 有没有明显问题
文件：trace.txt (50MB, ftrace格式)

Agent 执行流程：
1. 预检：检测文件大小 50MB
2. 采样分析：读取前 10000 行，快速提取关键指标
3. 规则诊断：快速匹配明显异常规则
4. 报告：输出快速诊断结果，建议是否需要全量分析
```
