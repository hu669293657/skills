# 诊断报告模板

本文件定义 Host Trace 诊断报告的标准格式和示例。

---

## 报告结构

```
# Host Trace 诊断报告

## 1. 执行摘要
## 2. 关键指标总览
## 3. 问题诊断
## 4. 时间线分析
## 5. Host-NPU 关联分析（如适用）
## 6. 优化建议
## 7. 分析元数据
## 8. 附录
```

---

## 完整模板

```markdown
# Host Trace 诊断报告

## 1. 执行摘要

- **分析文件**: `<trace_file>` (`<size>`, `<format>`格式)
- **采集时长**: `<duration>s`
- **CPU 核心数**: `<cpu_count>`
- **工作负载**: `<workload_type>` (`<parallel_config>`)
- **分析时间**: `<analysis_datetime>`
- **处理策略**: `<strategy>`
- **发现问题**: `<count>`个 (`<n_critical>` CRITICAL, `<n_high>` HIGH, `<n_medium>` MEDIUM, `<n_low>` LOW)
- **主要瓶颈**: `<primary_bottleneck>`

**一句话结论**: `<conclusion>`

---

## 2. 关键指标总览

| 类别 | 指标 | 值 | 阈值 | 状态 |
|------|------|-----|------|------|
| CPU | CPU利用率(avg) | `<cpu_util_avg>`% | >90% | `<status>` |
| CPU | CPU利用率(max) | `<cpu_util_max>`% | >95% | `<status>` |
| CPU | CPU均衡度 | `<cpu_balance>` | >0.5 | `<status>` |
| CPU | 上下文切换率 | `<cs_rate>`/s | >50000/s | `<status>` |
| 调度 | runqueue(avg) | `<runqueue_avg>` | >`<cpu_count>` | `<status>` |
| 调度 | 调度延迟(p99) | `<sched_p99>`ms | >20ms | `<status>` |
| NUMA | 远端访问比例 | `<numa_remote>`% | >20% | `<status>` |
| 内存 | major page fault | `<major_pf>` | >0 | `<status>` |
| IO | IO等待占比 | `<io_wait>`% | >5% | `<status>` |
| Runtime | 线程阻塞次数 | `<thread_block>` | >0 | `<status>` |
| Host-NPU | NPU空闲率 | `<npu_idle>`% | >10% | `<status>` |
| Host-NPU | NPU最大空闲间隔 | `<npu_idle_max>`ms | >20ms | `<status>` |

状态说明: ✅ 正常 | ⚠ 异常 | ❌ 严重异常 | - 不适用

---

## 3. 问题诊断

### 3.1 [`<severity>`] `<diagnosis_title>`

**规则ID**: `<rule_id>`

**证据**:
- `<metric_name>`: `<value>` (阈值: `<threshold>`)
- `<metric_name>`: `<value>` (阈值: `<threshold>`)
- `<metric_name>`: `<value>` (阈值: `<threshold>`)

**根因推断**:
`<root_cause_analysis>`

**影响**:
`<impact_description>`

**建议**:
1. `<suggestion_1>`
2. `<suggestion_2>`
3. `<suggestion_3>`

---

### 3.2 [`<severity>`] `<diagnosis_title>`
... (重复上述结构)

---

## 4. 时间线分析

### 整体趋势
`<overall_trend_description>`

### 关键时段

| 时间段 | 持续时间 | 现象 | 可能原因 |
|--------|---------|------|----------|
| `<ts_start>~<ts_end>` | `<duration>`ms | `<phenomenon>` | `<cause>` |
| `<ts_start>~<ts_end>` | `<duration>`ms | `<phenomenon>` | `<cause>` |

### 时序模式
`<temporal_pattern_description>`

示例:
```
[0~5s]   正常运行，CPU 利用率 70%，NPU 空闲率 3%
[5~15s]  CPU 压力上升，利用率达 95%，runqueue 从 8 升至 18
[15~20s] NPU 出现空闲 gap，平均 35ms，与 CPU runqueue 峰值对齐
[20~30s] 持续恶化，NPU 空闲率达 25%
```

---

## 5. Host-NPU 关联分析

### NPU 空闲概览
- **空闲率**: `<npu_idle_ratio>`%
- **空闲间隔数**: `<idle_gap_count>`
- **最大空闲间隔**: `<max_gap>`ms
- **平均空闲间隔**: `<avg_gap>`ms

### 关联模式分布

| 模式 | 出现次数 | 平均置信度 | 占比 |
|------|---------|-----------|------|
| `<pattern_name>` | `<count>` | `<confidence>` | `<ratio>`% |
| `<pattern_name>` | `<count>` | `<confidence>` | `<ratio>`% |

### 主要瓶颈模式

**模式**: `<primary_pattern>`

**因果链**:
```
<cause_level_1>
    ↓
<cause_level_2>
    ↓
<cause_level_3>
    ↓
NPU idle <gap>ms
    ↓
step time 增加 <delta>ms
```

**典型案例**:
```
时间: <ts>
NPU idle: <duration>ms
前序 host 事件:
  - [<ts>] <event_1>
  - [<ts>] <event_2>
  - [<ts>] <event_3>
置信度: <confidence>
```

---

## 6. 优化建议

### 优先级排序

| 优先级 | 建议 | 预期效果 | 实施难度 |
|--------|------|----------|----------|
| 🔴 立即 | `<suggestion>` | `<expected_effect>` | `<difficulty>` |
| 🟡 短期 | `<suggestion>` | `<expected_effect>` | `<difficulty>` |
| 🟢 中期 | `<suggestion>` | `<expected_effect>` | `<difficulty>` |
| 🔵 长期 | `<suggestion>` | `<expected_effect>` | `<difficulty>` |

### 建议详情

#### 1. [立即] `<suggestion_title>`
- **操作**: `<detailed_action>`
- **原因**: `<reason>`
- **预期效果**: `<expected_effect>`
- **验证方法**: `<verification_method>`

#### 2. [短期] `<suggestion_title>`
...

---

## 7. 分析元数据

- **分析时间**: `<datetime>`
- **Skill 版本**: host-trace-diagnosis v1.0
- **规则版本**: `<rule_version>`
- **处理策略**: `<strategy>`
- **时间窗口**: `<window_ms>`ms
- **采样率**: `<sample_rate>`
- **脚本版本**: `<script_version>`
- **数据完整性**: `<completeness>` (完整/部分/截断)
- **已知限制**: `<limitations>`

---

## 8. 附录

### A. 完整指标 JSON
<details>
<summary>点击展开</summary>

```json
<host_metrics_json>
```
</details>

### B. 匹配规则详情
<details>
<summary>点击展开</summary>

```json
<matched_rules_json>
```
</details>

### C. 关联分析详情
<details>
<summary>点击展开</summary>

```json
<correlation_analysis_json>
```
</details>

### D. 术语表

| 术语 | 含义 |
|------|------|
| runqueue | 运行队列，即处于 Runnable 状态的线程数 |
| sched latency | 调度延迟，从 wakeup 到实际被调度的等待时间 |
| NUMA remote | NUMA 远端访问，跨 NUMA 节点访问内存 |
| major page fault | 需要从磁盘读取数据的页错误 |
| NPU idle gap | NPU 连续空闲的时间间隔 |
| kernel launch gap | 连续两个 kernel 下发之间的时间间隔 |
| H2D/D2H | Host 到 Device / Device 到 Host 的内存拷贝 |
```

---

## 报告生成指南

### Agent 生成报告时的注意事项

1. **禁止无依据猜测**：每个结论必须有指标数据支撑。如果缺少数据，明确标注"数据不足"。

2. **证据链完整性**：每个问题诊断必须包含：
   - 规则ID
   - 至少一个超阈值的指标（值 + 阈值）
   - 根因推断（基于多指标综合分析）
   - 可操作的建议

3. **严重程度标注**：
   - CRITICAL: 导致系统不可用或严重性能下降（如 major page fault、OOM）
   - HIGH: 明确的性能瓶颈（如 CPU oversubscription、NPU idle > 20%）
   - MEDIUM: 需要关注但非紧急（如调度延迟偏高、NUMA 不均）
   - LOW: 轻微异常，建议监控（如 context switch 偏多）
   - INFO: 正常但值得记录的信息

4. **时间线分析**：必须基于 `timeline_windows` 数据，不能编造时序模式。

5. **优化建议**：必须具体可操作，不能是"优化你的代码"这类泛泛建议。每条建议应包含：
   - 具体操作（如"设置 OMP_NUM_THREADS=8"）
   - 预期效果（如"降低 CPU oversubscription"）
   - 验证方法（如"重新采集 trace 并检查 runqueue"）

6. **Host-NPU 关联**：如果存在 NPU trace 数据，必须包含关联分析。如果不存在，标注"无 NPU trace 数据，建议补充采集"。

7. **不确定性标注**：当置信度 < 0.7 时，标注"推测性结论，需进一步验证"。
