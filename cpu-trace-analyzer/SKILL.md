---
name: "cpu-trace-analyzer"
description: "分析CPU trace数据，定位NPU/GPU训练场景下的Host侧性能瓶颈。当用户提供trace文件(.json/.txt/.csv/.trace)并需要识别CPU调度、DataLoader竞争、H2D拷贝阻塞或kernel launch间隙等host问题时触发。"
---

# CPU Trace 分析器

分析 CPU 侧 trace 数据，定位导致 NPU/GPU 利用率低下的 Host 侧性能瓶颈。采用 Gap 驱动反向溯源方法论：扫描 Device 空闲段，关联 Host 事件，归因到具体 Host 侧原因。

## 触发条件

- 用户提供 trace 文件（`.json`、`.txt`、`.csv`、`.trace`、`.perfetto-trace`）并要求分析
- 用户提到"host 问题"、"CPU 瓶颈"、"NPU 空闲"、"trace 分析"、"性能问题"
- 用户需要诊断训练/推理变慢，且怀疑是 Host 侧问题
- 用户提到昇腾/NPU profiling 产物（msprof、trace_view.json、ftrace、perf）

## 不触发条件

- 用户询问通信瓶颈（HCCL/NCCL）→ 使用通信分析工具
- 用户只需可视化查看 trace → 建议 Perfetto UI 或 MindStudio Insight
- 用户询问模型算法正确性，而非性能问题

## 支持的 Trace 格式

| 格式 | 扩展名 | 探测信号 |
|------|--------|---------|
| Chrome Trace JSON | `.json` `.trace` | 首字符 `[` 或 `{`，含 `traceEvents` |
| ftrace 文本 | `.txt` | 首行含 `tracer:` 或 `entries-in-buffer` |
| msprof 产物 | `.csv` `.db` | CSV 含算子列，或 SQLite 数据库 |
| perf 数据 | `perf.data` | 二进制 `PERFILE2` magic |
| Perfetto protobuf | `.perfetto-trace` | 二进制 protobuf 格式 |

## 核心方法论：Gap 驱动反向溯源

核心思路是**从 Device 空闲段反向追溯到 Host 根因**，而非从 Host 指标正推猜测。

### 分析流水线（5 步）

```
步骤1: 解析 trace → 统一 IR (TraceEvent 流)
步骤2: 提取特征 (Host 指标 + Device Gap 扫描 + 关联分析)
步骤3: 规则引擎评估 (阈值 + 时序模式 + 关联证据)
步骤4: 诊断 (严重度 + 证据链 + 优化建议)
步骤5: 生成报告 (JSON + HTML + 时间线可视化)
```

### 步骤1：解析与归一化

所有 trace 格式转换为统一的 `TraceEvent` 结构，字段包括：`ts`、`dur`、`pid`、`tid`、`cpu`、`name`、`cat`、`ph`、`device_id`、`stream_id`、`args`。

**大文件处理规则：**
- 文件 < 100MB：标准 `json.load()` 可接受
- 文件 100MB - 1GB：使用 `ijson` 流式解析，逐事件 yield
- 文件 > 1GB：流式解析 + 按时间窗口分片（每片 10 秒），各片独立分析后合并
- 内存目标：O(1) ~50MB，与文件大小无关

**事件分类：**
- Host 侧：`cpu_sched`、`cpu_function`、`memory`、`io`、`runtime`、`data_loader`、`python`、`cuda_npu_api`
- Device 侧：`npu_kernel`、`gpu_kernel`、`npu_memcpy`、`stream_sync`

### 步骤2：特征提取

提取三类特征：

**A 类 — 标量汇总指标：**
- `cpu_util_avg/max`：CPU 利用率（非 idle 时间 / 总时间）
- `runqueue_avg/max`：就绪队列长度（sched_switch 中 prev_state=R 的频率）
- `sched_latency_avg/p99/max_us`：调度延迟（wakeup 到 switch 的时间差）
- `ctx_switch_rate`：每秒上下文切换次数
- `cpu_balance_score`：CPU 负载均衡度（1 - std/mean）
- `h2d/d2h_bandwidth_mbs`：内存拷贝带宽
- `launch_count`、`launch_avg_gap_us`：Kernel 提交频率和间隔

**B 类 — 时序序列特征：**
- `cpu_util_per_step[]`：按滑动窗口的 CPU 利用率序列
- `runqueue_timeline[]`：就绪队列时间序列
- `gap_distribution[]`：Device gap 大小分桶直方图

**C 类 — 关联特征（核心创新）：**
- `gap_host_op_pairs[]`：每个 Device gap 对应的 Top Host 事件
- `bottleneck_attribution{}`：各类问题对总空闲时间的贡献占比
- `correlation_score`：Host 活跃度与 Device 空闲度的皮尔逊相关系数

### 步骤3：Gap 扫描算法

最重要的分析步骤：

1. 收集所有 Device kernel 事件（`device_id >= 0`，cat 属于 `npu_kernel/gpu_kernel/npu_memcpy`）
2. 按 `(device_id, stream_id)` 分组，按 `ts` 排序
3. 对每对相邻 kernel：`gap = next.ts - (current.ts + current.dur)`
4. 过滤 gap < 100us（正常 stream 切换开销）
5. 按 gap 时长降序排序
6. Pareto 筛选：累计 gap 时长达到总 gap 的 80% 为止
7. 保留 Top 20 用于详细分析

**关键指标：**
- `device_utilization` = kernel 总执行时间 / 总时间
- 若 device_utilization < 70%，大概率存在 Host 侧瓶颈

### 步骤4：Host-Device 关联

对每个显著 gap `[gap_start, gap_end]`：

1. 查询同时间窗口内的 Host 事件：`WHERE ts <= gap_end AND ts+dur >= gap_start`
2. 按 dur 降序取 Top 5
3. 根据耗时最长的 Host 事件归因：
   - cat=`cpu_sched` 或 name 含 "sched" → **CPU_SCHED**
   - cat=`data_loader` 或 name 含 "DataLoader" → **DATA_LOADER**
   - cat=`npu_memcpy` 或 name 含 "memcpy/copy" → **MEMCPY**
   - cat=`cuda_npu_api` 或 name 含 "launch" → **LAUNCH_GAP**
   - cat=`io` 或 name 含 "io/read/write" → **IO_WAIT**
   - cat=`runtime` 或 name 含 "sync/lock/mutex" → **RUNTIME_BLOCK**
   - 其他 → **OTHER**
4. 计算 `bottleneck_attribution`：各类别占总 gap 时间的百分比
5. 计算 `correlation_score`：按 1 秒窗口的 host_busy_ratio 和 device_idle_ratio 的皮尔逊相关系数

### 步骤5：规则引擎诊断

规则使用三层条件评估 + 置信度评分：

**第一层 — 标量阈值：** 快速筛选（如 `runqueue_avg > cpu_cores`）
**第二层 — 时序模式：** 防止误报（如"NPU 空闲期间 runqueue 出现尖峰"）
**第三层 — 关联证据：** 建立因果关系（如"CPU_SCHED 归因 > 20%"）

置信度 = sum(weight_i × condition_hit_i) / sum(all_weights)。置信度 >= 阈值时规则命中。

**六类诊断类别，11 条规则：**

| 类别 | 规则 ID | 关键条件 |
|------|---------|---------|
| CPU 调度 | CPU001, CPU002 | runqueue > 核数, sched_latency > 5ms, cpu_balance < 0.5 |
| 调度延迟 | SCHED001, SCHED002 | sched_latency_avg > 5ms, ctx_switch > 30k/s |
| 内存拷贝 | MEM001, MEM002 | h2d_bandwidth < 500 MB/s, memcpy_time > 10% |
| IO 等待 | IO001 | IO_WAIT 归因 > 15% |
| Runtime 阻塞 | RT001, RT002 | RUNTIME_BLOCK > 15%, launch_gap > 1ms |
| Host-NPU 协同 | HN001, HN002 | correlation > 0.6, DATA_LOADER > 20% |

## Python 项目使用方法

项目代码位于本 Skill 目录下的 `host_trace_diagnosis/` 子目录。

### CLI 用法

```bash
# 安装依赖
pip install pyyaml ijson pyarrow

# 完整流水线：解析 → 特征 → 诊断 → 报告
python host_trace_diagnosis/cli.py analyze <trace文件> --output reports/

# 分步执行
python host_trace_diagnosis/cli.py detect <trace文件>           # 仅探测格式
python host_trace_diagnosis/cli.py parse <trace文件> --output ir.parquet
python host_trace_diagnosis/cli.py features ir.parquet --output features.json
python host_trace_diagnosis/cli.py diagnose features.json --output reports/
```

### Python API 用法

```python
import sys; sys.path.insert(0, 'host_trace_diagnosis')

from parsers.detector import FormatDetector
from ir.writer import IRWriter
from ir.reader import IRReader
from features.host_metrics import HostMetricsExtractor
from features.gap_scanner import GapScanner
from features.correlation import CorrelationEngine
from features.vector import FeatureVectorBuilder
from rules.engine import RuleEngine
from agent.diagnosis_agent import DiagnosisAgent
from report.generator import ReportGenerator

# 步骤1：探测与解析
detector = FormatDetector({})
fmt = detector.detect("trace.json")
parser = detector.get_parser(fmt, {})
writer = IRWriter("trace_ir.jsonl", {"format": "jsonl"})
for event in parser.parse("trace.json"):
    writer.write_event(event)
writer.finalize(parser.get_metadata())

# 步骤2：提取特征
reader = IRReader("trace_ir.jsonl", {})
host_ext = HostMetricsExtractor({})
scalars, timelines = host_ext.extract(reader)

gap_scanner = GapScanner({})
gaps = gap_scanner.scan(reader)

corr_engine = CorrelationEngine({})
pairs, attribution, corr_score = corr_engine.correlate(reader, gaps)

# 步骤3：构建特征向量
builder = FeatureVectorBuilder()
fv = builder.build(scalars, timelines, gaps, pairs, attribution, corr_score, reader.read_metadata())

# 步骤4：诊断
rule_engine = RuleEngine({"rules_dir": "host_trace_diagnosis/rules"})
matched = rule_engine.evaluate(fv)

agent = DiagnosisAgent({})
result = agent.diagnose(fv, matched)

# 步骤5：报告
report_gen = ReportGenerator({})
report_gen.generate(result, "reports/")
```

## 报告解读

### 报告结构

1. **摘要**：严重度（NONE/LOW/MEDIUM/HIGH/CRITICAL）+ 主要诊断
2. **指标表**：每个指标的值、阈值、通过/失败状态
3. **瓶颈归因**：各类别对 Device 空闲时间的贡献占比
4. **证据链**：Host 事件与 Device gap 的时间线叙述
5. **根因**：主导瓶颈类别及其支撑证据
6. **优化建议**：按优先级排序

### 关键指标参考

| 指标 | 健康 | 警告 | 危险 |
|------|------|------|------|
| device_utilization | > 85% | 70-85% | < 70% |
| cpu_util_avg | 40-70% | 70-85% 或 < 30% | > 85% 或 < 20% |
| runqueue_avg | < CPU 核数 | 1-2x 核数 | > 2x 核数 |
| sched_latency_avg | < 1ms | 1-5ms | > 5ms |
| correlation_score | < 0.3 | 0.3-0.6 | > 0.6 |
| top gap duration | < 1ms | 1-10ms | > 10ms |

**重要：** `correlation_score` > 0.6 表示 Host 活动与 Device 空闲强相关，确认存在 Host 侧瓶颈。

## 常见 Host 问题与解决方案

### CPU 调度瓶颈
- **症状**：高 runqueue、高 sched_latency、CPU 峰值期间 NPU 空闲
- **原因**：过多线程竞争 CPU（OMP_NUM_THREADS、DataLoader workers）
- **修复**：减少线程数、设置 CPU affinity、隔离 Runtime 线程

### DataLoader 竞争
- **症状**：DATA_LOADER 归因 > 20%、step 边界处 NPU 空闲
- **原因**：数据预处理慢于 NPU 计算
- **修复**：增大 prefetch、使用 pinned memory、CPU 饱和时减少 num_workers

### H2D 拷贝阻塞
- **症状**：MEMCPY 归因 > 20%、低 h2d_bandwidth
- **原因**：同步 memcpy 阻塞 kernel 提交
- **修复**：使用异步拷贝、重叠计算与传输、使用 pinned memory

### Kernel Launch 间隙
- **症状**：LAUNCH_GAP 归因 > 20%、launch_avg_gap > 1ms
- **原因**：CPU 提交 kernel 太慢（Python GIL、慢分发）
- **修复**：使用 CUDA Graphs / NPU graph capture、合并小算子、减少 Python 开销

### Runtime 同步阻塞
- **症状**：RUNTIME_BLOCK 归因 > 15%、gap 窗口内有 sync 事件
- **原因**：显式 stream 同步阻塞流水线
- **修复**：移除不必要的 sync、使用异步 API、重叠独立 stream

## 局限性

- 不分析通信瓶颈（HCCL/NCCL）
- 不分析 NPU kernel 内部性能（使用 msprof op_metrics）
- LLM 自然语言报告需配置 API key（无 key 时降级为结构化报告）
- Perfetto TraceProcessor SQL 引擎为可选（无 perfetto 时降级为 Python 原生查询）
- 对于 >1GB 文件，时间窗口分片可能遗漏跨窗口模式

## 知识库

项目内置 5 个诊断案例用于模式匹配：
1. DataLoader 竞争 CPU 导致 NPU 空闲
2. H2D 同步拷贝阻塞计算
3. OMP_NUM_THREADS 过大导致 CPU oversubscription
4. Python GIL 导致 kernel launch 间隙过大
5. IO 等待阻塞数据管道

分析新 trace 时，知识库会自动搜索相似历史案例，增强诊断和建议。
