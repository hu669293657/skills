# host-bound-analyzer 总体设计文档

> 版本：v0.1 设计基线 | 设计原则：正确性 > 可解释性 > 可扩展性 > 易用性 > UI美观
> 约束：全链路 Python 标准库零依赖；HTML 报告完全离线；客户侧最小权限、最低开销。

---

## 1. 总体架构

```
┌─────────────────────────────── 客户机器 (Linux) ───────────────────────────────┐
│                                                                                │
│  训练/推理进程 (PyTorch / MindSpore / vLLM / ...)   NPU / GPU   OS/Runtime     │
│                        │                                            │          │
│                        ▼                                            │          │
│  ┌──────────────────────────────────────────────────────────────┐  │          │
│  │  Collector  (host-bound collect)                             │  │          │
│  │  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐  │  │          │
│  │  │ Env Detection  │→ │ Capabilities │→ │ L1/L2/L3 采样器  │  │  │          │
│  │  │ (/proc,/sys,   │  │  .json       │  │ 并行窗口采样     │  │  │          │
│  │  │  工具探测)      │  └──────────────┘  │ L1: proc/sys/top │  │  │          │
│  │  └────────────────┘                    │ L2: vmstat/pidstat│ │  │          │
│  │                                        │     /iostat/mpstat│ │  │          │
│  │                                        │     /perf stat    │  │  │          │
│  │                                        │ L3: perf record / │  │  │          │
│  │                                        │     ftrace(需授权)│  │  │          │
│  │                                        └────────┬─────────┘  │  │          │
│  └───────────────────────────────────────────────────┼────────────┘  │          │
│                                                      ▼               │          │
│                                        collection/  (manifest.json  │          │
│                                          + system/process/thread/…  │          │
│                                          + framework/runtime/logs)  │          │
└────────────────────────────────────────────────────────┼───────────────────┘
                                                         │  case_001.tar.gz
                                                         ▼
┌────────────────────────── 分析端 (工程师本地 / Agent Skill) ─────────────────────────┐
│                                                                                      │
│  ┌─────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐   ┌──────────────────┐   │
│  │ Discover│ → │ Quality  │ → │  Parser    │ → │ Normalizer│ → │  Feature 层      │   │
│  │ 文件发现 │   │ Check    │   │ 逐源解析   │   │ 统一事件  │   │  指标提取        │   │
│  │ 类型识别 │   │ 完整度   │   │ (容错)     │   │ 模型      │   │  (Metrics)       │   │
│  └─────────┘   └──────────┘   └───────────┘   └──────────┘   └────────┬─────────┘   │
│                                                                        ▼             │
│  ┌─────────────────────── Diagnosis Engine ──────────────────────────────────────┐   │
│  │  Rule Engine (rules/*.yaml)  →  Evidence Engine (证据链可追溯)                 │   │
│  │  →  Scoring Engine (Host Bound Score 0-100)  →  根因树  →  优先级建议          │   │
│  └───────────────────────────────────────────────┬──────────────────────────────┘   │
│                                                  ▼                                  │
│                                     report.json (schema 版本化)                     │
│                                                  ▼                                  │
│                            固定 HTML Template + 内嵌 SVG 图表                       │
│                                                  ▼                                  │
│                                            report.html (离线双击可开)                │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

**两条分析路径**：
- 路径 A（Skill 直接采集）：`host-bound run` 在客户机一键 collect→analyze→report。
- 路径 B（离线回传）：客户执行 `host-bound collect` → 回传 tar.gz → 工程师 `host-bound analyze` / Agent Skill 自动走 SKILL.md 工作流。

**核心设计决策**：
| 决策点 | 选择 | 理由 |
|---|---|---|
| 运行依赖 | 仅 Python ≥3.6 标准库（客户机可无 Python 时用打包单文件工具，同一套逻辑） | 客户环境可能无网/无 sudo/pip 受限 |
| 规则载体 | rules/*.yaml + 自研 mini-YAML 解析子集（约 80 行，规则限定语法），解析失败整体 fail-safe 降级为内置默认规则 | 满足"规则不写死"，又不引入 PyYAML 依赖 |
| 图表 | Python 端生成内嵌 SVG（折线/柱状/雷达/热力/时间线/根因树），不引第三方 JS | 完全离线、无 CDN、体积小、可审计 |
| 报告生成 | Analyzer → report.json → 固定模板字符串渲染（无模板引擎） | 章节结构永不漂移，可 schema 校验 |
| 采集权限 | 默认只读 /proc、/sys 与无特权工具；L3 需 `--level 3` 显式授权 | 现场安全 |
| 失败隔离 | 任一 Parser/Analyzer 异常被捕获并记录 `data_quality.warnings`，不中断全流程 | 日志不完整时仍可初步诊断 |

---

## 2. 目录结构

```
host-bound-analyzer/
├── SKILL.md                     # Agent 工作流定义（触发条件/禁令/步骤）
├── README.md                    # 部署、使用、扩展说明
├── DESIGN.md                    # 本文档
├── pyproject.toml               # 元数据（零运行依赖）
├── bin/
│   └── host-bound               # 无 Python 环境时占位说明；有 Python 用 python -m
├── host_bound/                  # 主包（python -m host_bound）
│   ├── __init__.py              # VERSION / SCHEMA_VERSION / REPORT_VERSION / RULE_VERSION
│   ├── __main__.py              # CLI 入口（argparse）
│   ├── cli.py                   # 子命令实现
│   ├── models/
│   │   ├── __init__.py
│   │   ├── events.py            # Event 统一事件模型 / Timeline
│   │   ├── metrics.py           # MetricSeries / FeatureSet
│   │   ├── evidence.py          # Evidence(source, path, line, snippet, value)
│   │   ├── diagnosis.py         # Finding / RootCauseNode / Recommendation / Diagnosis
│   │   └── case.py              # CaseManifest / CollectionInfo / DataQuality
│   ├── collector/
│   │   ├── main.py              # CollectorOrchestrator（环境探测→能力→采样编排）
│   │   ├── environment.py       # OS/arch/容器/工具探测 → capabilities.json
│   │   ├── system.py            # /proc /sys：cpuinfo,lscpu,meminfo,loadavg,stat,vmstat…
│   │   ├── process.py           # ps/top/pstree（无工具时 /proc 遍历降级）
│   │   ├── thread.py            # /proc/<pid>/task/*：线程列表/亲和/状态
│   │   ├── numa.py              # numactl/numastat 或 /sys/devices/system/node
│   │   ├── tools.py             # L2 工具采样：vmstat/pidstat/iostat/mpstat
│   │   ├── perf.py              # perf stat(L2) / perf record+report(L3)
│   │   ├── framework.py         # PyTorch/MindSpore/vLLM 探测 + DataLoader 线程识别
│   │   ├── npu.py               # NPU/GPU：npu-smi / nvidia-smi（存在才采）
│   │   ├── sanitize.py          # 脱敏（IP/hostname/user/path/token）
│   │   └── packer.py            # manifest 生成 + tar.gz 打包
│   ├── parser/
│   │   ├── registry.py          # 解析器注册表 + 自动类型识别 + 未知日志 GenericLog
│   │   ├── proc_parser.py       # cpuinfo/lscpu/meminfo/loadavg/stat/vmstat/proc顶部快照
│   │   ├── ps_parser.py         # ps/top/pidstat 文本解析
│   │   ├── thread_parser.py     # 线程列表/亲和/env（OMP_NUM_THREADS 等）
│   │   ├── numa_parser.py       # numastat/numa_maps/affinity
│   │   ├── perf_parser.py       # perf stat / perf report --stdio
│   │   ├── io_parser.py         # iostat/pidstat -d
│   │   ├── device_parser.py     # npu-smi/nvidia-smi 快照
│   │   ├── timeline_parser.py   # (V0.2) profiler trace → 统一时间线
│   │   └── generic_parser.py    # 未知日志：编码/时间戳/关键字扫描 → GenericLog
│   ├── analyzer/
│   │   ├── base.py              # Analyzer 基类（features 输入输出、异常隔离）
│   │   ├── cpu.py               # 利用率/饱和/不均衡(CV)/iowait/steal
│   │   ├── scheduler.py         # ctx switch/迁移/run queue 比率
│   │   ├── numa.py              # locality 评分/跨节点访问
│   │   ├── thread.py            # 线程数/超订阅/绑定异常/热点线程
│   │   ├── memory.py            # 可用内存/回收压力/swap
│   │   ├── io.py                # iowait/磁盘 util
│   │   ├── perf_analyzer.py     # IPC/cache-miss/热点归因(python/omp/runtime)
│   │   ├── host_bound.py        # 多维证据合成 Host Bound 评估 + 分类
│   │   └── timeline.py          # (V0.2) host-device gap
│   ├── diagnosis/
│   │   ├── miniyaml.py          # 规则专用 YAML 子集解析器
│   │   ├── rules.py             # 规则加载/条件求值(表达式 DSL)
│   │   ├── evidence.py          # 证据登记 → 可回溯 source+line
│   │   ├── scoring.py           # Host Bound Score 加权评分
│   │   ├── engine.py            # Feature→Rule→Evidence→Score→Finding→根因树
│   │   └── recommendation.py    # 建议模板（P0-P3 / 明确 vs 需实验验证）
│   ├── report/
│   │   ├── charts.py            # SVG 生成：line/bar/radar/heatmap/timeline/tree
│   │   ├── template.py          # 固定 18 章节 HTML 模板渲染
│   │   └── generator.py         # report.json → report.html
│   ├── util/
│   │   ├── fsio.py              # 安全读文件/写文件/时间戳
│   │   └── stats.py             # 均值/百分位/CV/趋势
│   └── schemas/                 # report_schema.json / collection_manifest.schema
├── rules/
│   ├── host_bound.yaml          # HB-001.. 总评分规则
│   ├── cpu_saturation.yaml      # CPU-001..
│   ├── thread.yaml              # THR-001.. (OMP 超订阅等)
│   ├── numa.yaml                # NUMA-001..
│   ├── scheduler.yaml           # SCH-001..
│   ├── memory.yaml              # MEM-001..
│   ├── io.yaml                  # IO-001..
│   └── dataloader.yaml          # DL-001..
├── plugins/
│   └── README.md                # 插件接口说明（V0.3 激活 Plugin API）
├── examples/
│   └── cases/                   # 8 个黄金测试案例 input/+expected/
├── tests/
│   ├── test_miniyaml.py
│   ├── test_parsers.py
│   ├── test_rules_engine.py
│   ├── test_scoring.py
│   ├── test_analyzers.py
│   ├── test_e2e_cases.py        # 回归：遍历 examples/cases 断言 Golden
│   └── fixtures/                # 合成日志片段
└── tools/
    └── make_synthetic_case.py   # 合成采集包生成器（开发/回归用）
```

---

## 3. CLI 设计

```
host-bound <command> [options]
```

| 命令 | 语法 | 说明 |
|---|---|---|
| collect | `host-bound collect [--duration 60] [--interval 1] [--pid P \| --auto-detect] [--level 1/2/3] [-o DIR] [--sanitize] [--no-tar]` | 客户机采集；L3 需显式授权；产出 collection/ 或 case_xxx.tar.gz |
| analyze | `host-bound analyze <collection.tar.gz\|DIR> [-o analysis.json] [--sanitize]` | 解析→特征→诊断；输出 report.json（含 diagnosis 全量数据） |
| report | `host-bound report <analysis.json> [-o report.html]` | 固定模板渲染 HTML |
| run | `host-bound run <collection> [-o report.html]` | analyze+report 一键（路径 B） |
| run-collect | `host-bound run-collect [--duration 60] [-o report.html]` | 采集+分析+报告一键（路径 A，客户机） |
| validate | `host-bound validate <collection>` | 仅数据质量检查，输出完整度分级 |
| version | `host-bound version` | 打印四元版本号 |

约定：所有命令 `--help` 可用；退出码 0 成功 / 2 部分数据缺失仍出报告 / 1 致命错误。

---

## 4. 统一数据模型

### 4.1 事件层（所有时间序列的最终归一形态）

```json
{
  "ts": 1234.5,            // 相对采集窗口起点秒（或 epoch）
  "duration": 1.0,
  "event": "cpu_sample",   // cpu_sample|thread_snapshot|ctx_switch|io_sample|device_sample|perf_stat|host_span|device_span
  "pid": 12345, "tid": 12350, "cpu": 4, "node": 0,
  "source": "vmstat",      // 来源文件相对路径可回溯
  "metrics": { "usr": 30.1, "sys": 12.0, "iowait": 5.0, "idle": 52.9 }
}
```

### 4.2 特征层（Analyzer → Diagnosis 的唯一接口）

FeatureSet（键 → {value, unit, evidence[]}，全部带证据指针）核心键：

```
cpu.count_logical / cpu.count_physical / cpu.sockets / cpu.model
cpu.avg_util / cpu.max_util / cpu.iowait_pct / cpu.steal_pct
cpu.user_pct / cpu.sys_pct / cpu.run_queue_ratio   # load1/cores
cpu.ctx_switch_per_sec / cpu.migrations_per_sec
cpu.core_util_cv / cpu.cores_over90_pct            # 不均衡与饱和核占比
mem.available_pct / mem.swap_in_out
numa.node_count / numa.remote_pct / numa.crossnode_process
thread.total / thread.target_threads / thread.omp_num_threads
thread.oversub_ratio / thread.hot_thread_pct / thread.affinity_skew
io.iowait_pct / io.disk_util_max
perf.ipc / perf.cache_miss_pct / perf.top_symbols[] / perf.hotspot_category
device.util_pct / device.name                      # 存在 NPU/GPU 时
timeline.device_idle_gap_pct                       # V0.2
dq.sources{} / dq.grade                            # 数据质量
```

### 4.3 诊断层

```json
{
  "version": {"tool_version":"0.2.0","schema_version":"1.0","rule_version":"1.0"},
  "conclusion": {"status":"HIGH RISK","host_bound_score":87,"confidence":0.94,
                 "classification":"CPU Compute Bound / DataLoader",
                 "statement":"存在明显 Host Bound，CPU 预处理供给不足"},
  "findings":[
    {"id":"F-001","rule_id":"CPU-001","severity":"HIGH","confidence":0.96,
     "title":"CPU 饱和","evidence":[
        {"metric":"cpu.avg_util","value":94.2,"unit":"%",
         "source":"system/vmstat.txt","lines":"12-62","snippet":"us=..."}],
     "impact":"设备侧等待 Host 供给，Device 利用率受限",
     "recommendation":{"level":"P0","action":"...","expected":"...","verify":"...",
                       "certainty":"明确建议|建议实验验证"}}
  ],
  "root_cause_tree":{"name":"Host Bound","children":[...按 impact*confidence 排序]},
  "priorities":[{"level":"P0",...}],
  "data_quality":{"grade":"partial","sources":{"perf":"missing"},"limitations":[...]}
}
```

### 4.4 版本化

`tool_version`（代码）、`schema_version`（report.json 结构）、`report_version`（HTML 模板）、`rule_version`（规则集）四元独立版本，写入 manifest 与报告页脚；schema 变更必须同步 `schemas/` 与回归基线。

---

## 5. Collector 设计

### 5.1 启动流程

```
解析参数 → 环境探测(environment.py) → capabilities.json
→ 目标进程解析(--pid / --auto-detect 按 cmdline 关键词 python|torch|mindspore|vllm|train|infer)
→ 按 level 组装采样器 → 并行窗口采样(duration) → 逐文件落盘
→ sanitize(可选) → manifest.json → tar.gz
```

### 5.2 能力探测（capabilities.json）

探测项：os/kernel/arch、cpu 拓扑（/proc/cpuinfo+lscpu）、NUMA（/sys/devices/system/node 或 numactl）、容器（/.dockerenv、/proc/1/cgroup）、工具（ps top vmstat pidstat iostat mpstat numastat numactl perf trace-cmd lscpu）、设备（npu-smi→/dev/davinci*；nvidia-smi）、Python/框架（进程 cmdline + pip list 探测 PyTorch/MindSpore/vLLM）。全部探测失败不致命，逐项记录。

### 5.3 三级采集

| 级别 | 内容 | 开销/权限 |
|---|---|---|
| L1 默认 | /proc、/sys 全量快照 ×interval；/proc/<pid>/task 线程表与 affinity；environ 关键变量；进程树；框架与运行时目录发现 | 极低 / 无特权 |
| L2 工具存在 | vmstat/pidstat -t/iostat -x/mpstat -P ALL/numastat/perf stat -a 按 interval 循环采样 duration 秒 | 低 / 无特权 |
| L3 显式 | perf record -F 99 -g -- <pid>（默认 cap 至 60s）+ perf report --stdio；trace-cmd（存在才列） | 高 / 需 root 或 perf_event_paranoid |

采集目录结构对齐需求文档第五节；`不要求所有文件存在`，缺工具即跳过并在 manifest.capabilities 标注。

### 5.4 线程级信息（第九条对齐）

/proc/<pid>/task/<tid>/：stat（utime/stime/state）、status（线程名）、comm、affinity（Cpus_allowed_list）。识别重点：持续高 CPU 线程、热线程集中度、OMP 线程池规模（线程名 libgomp/openmp/pool）。

### 5.5 敏感信息与打包

`--sanitize` 在打包前对全部文本文件执行规则替换：IP→`10.0.0.x`、hostname→`host-1`、user→`user`、绝对路径→`/path/to/...`、token/key/password 行值→`***`。manifest 记录 `sanitized: true`。

---

## 6. Parser 设计

- `registry.py`：discover(collection) → 逐文件探测类型（文件名/首行指纹/编码/chardet 式启发（标准库实现））→ 分派 Parser → 产出 `(events, metrics, notes)`；未知类型走 `generic_parser` 注册为 GenericLog（扫描时间戳格式、PID 模式、CPU 关键字），绝不静默丢弃。
- 每个 Parser 只做"文本→结构化"，不做诊断；全部容错：单文件异常 → 记录 `dq.warnings`，继续。
- 关键解析：
  - proc：vmstat（us/sy/id/wa/st、cs、in）、loadavg、stat（per-cpu jiffies 差分）、meminfo、cpuinfo/lscpu（拓扑/频率/governor）。
  - ps/pidstat/top：进程/线程 CPU%、RSS、线程名、状态。
  - perf stat：cycles/instructions/branches/branch-misses/cache-* /context-switches/cpu-migrations → IPC、cache_miss_pct。
  - perf report：Overhead+Symbol+共享库 → 热点归类（python 解释器/libgomp/mkl/runtime/通信库/memcpy/其他）。
  - numastat：numa_hit/numa_miss/numa_foreign → remote_pct。
  - device：npu-smi info / nvidia-smi → util%、显存。
  - timeline(V0.2)：msprof/trace JSON → host_span/device_span 事件。

---

## 7. Analyzer 设计

每个 Analyzer 输入 FeatureSet+events，输出新特征并登记证据（文件+行号+原始片段）：

| Analyzer | 关键逻辑 |
|---|---|
| cpu | 全核均值/峰值；per-core CV（不均衡）；iowait、steal；user/sys 比例 |
| scheduler | run_queue_ratio=load1/cores；cs/in 差分速率；voluntary/involuntary（pidstat -w 可得时） |
| numa | numastat remote 比例；进程/线程 affinity 跨 node 检测；locality score |
| thread | 线程总数 vs 核数；OMP_NUM_THREADS vs 可用核 → oversub_ratio；热线程 CPU 集中度 top1_pct |
| memory | MemAvailable%；swap in/out 速率；页回收 |
| io | iowait 均值/峰值；iostat util%/await 峰值 |
| perf_analyzer | IPC<1.0 且 cache-miss 高 → Memory Bound 倾向；热点 top 类别占比 |
| host_bound | 汇总多维信号：cpu 饱和、run queue、device util、gap、热线程 → HB Score 与分类（CPU Compute / Scheduling / Threading / Memory / NUMA / IO / Sync / DataPipeline / Runtime / Python / Unknown） |

Host Bound 判定（第十二条对齐）：不只看 CPU>90%。合成信号：cpu_saturation、run_queue_ratio>1、device_util<70 且 host 侧忙、热线程集中、perf 热点。分类树按置信度排序输出。

---

## 8. Diagnosis 引擎设计

```
FeatureSet → [Rule 求值(条件 DSL)] → 命中规则 → Evidence 登记
           → Scoring(Σ weight×severity_factor → 0-100) → Findings(去重/排序)
           → 根因树(按分类聚合, impact×confidence 排序)
           → Recommendation(规则自带模板+参数注入, 区分 明确/需实验验证)
```

- 条件 DSL：`cpu.avg_util > 90 and cpu.run_queue_ratio > 1.0`（标识符=特征键，and/or/not/比较/括号；求值缺失特征→规则 skip 并记录 dq 而非误判）。
- severity_factor：CRITICAL=1.0 HIGH=0.85 MEDIUM=0.6 LOW=0.35 INFO=0.15；规则带 `weight`（0-1）。
- Score 分档：0-20 基本不存在 / 20-40 轻微 / 40-60 疑似 / 60-80 较明显 / 80-100 强 Host Bound。
- 置信度：命中证据条数、证据源多样性（≥2 独立来源加分）、数据质量降权（grade=insufficient 时全体 confidence×0.6）。

---

## 9. 规则设计（rules/*.yaml）

规则文件限定 mini-YAML 子集：2 空格缩进、`key: value`、`- list`，禁锚点/flow/多行块。

```yaml
rule_id: CPU-001
name: CPU Saturation
group: cpu
condition: "cpu.avg_util > 90 and cpu.run_queue_ratio > 1.0"
requires: ["cpu.avg_util", "cpu.run_queue_ratio"]
weight: 1.0
severity: HIGH
contribution: host_bound
diagnosis:
  type: CPU_COMPUTE_BOUND
  statement: "CPU 计算资源饱和，Host 侧处理能力不足"
recommendation:
  level: P0
  certainty: 明确建议
  action: "降低 Host 侧 CPU 计算：检查热点函数（见热点分析），调整 OMP/worker 线程与亲和"
  verify: "重采后对比 cpu.avg_util、cpu.run_queue_ratio、device.util_pct、Host Bound Score"
```

规则集覆盖：CPU 饱和/不均衡/steal、超订阅（THR-001：`thread.omp_num_threads > cpu.count_logical`）、绑定异常、NUMA 远端、内存压力、iowait、ctx-switch 爆炸、DataLoader workers、perf 热点、device gap（V0.2）。

---

## 10. HTML 报告设计

**固定 18 章节**（顺序永不变化）：Executive Summary / Overall Diagnosis / Host Bound Assessment / CPU Analysis / Thread Analysis / Scheduler Analysis / NUMA Analysis / Memory Analysis / I/O Analysis / Host-Device Timeline / Framework Analysis / Profiler Analysis / Root Cause / Evidence / Optimization Suggestions / Risk and Impact / Collection Information / Raw Data Appendix。

**布局**：左侧固定导航（章节锚点+风险徽标）+ 主内容区；顶部报告头（Case/Host/OS/CPU/Date）；首屏 Executive Summary 含 9 项健康仪表盘（Host Bound Score、CPU Util、Device Util、CPU Saturation、Memory Pressure、NUMA Risk、Thread Risk、I/O Risk、Scheduler Risk）+ 问题雷达图 + 结论卡。

**视觉**：专业性能分析工具风格；状态色固定 CRITICAL=#d32f2f / HIGH=#e65100 / MEDIUM=#f9a825 / LOW=#1976d2 / INFO=#616161；等宽字体展示指标与日志证据。

**图表（全部内嵌 SVG，零 JS 依赖）**：CPU 利用率折线（含 iowait/steal 堆叠）、per-core 热力条、线程 TOP 柱状、NUMA 拓扑示意、上下文切换折线、雷达图、根因树、（V0.2）Host/Device 时间线甘特、（V0.3）Before/After 对比。

**证据交互**：纯 CSS `<details>` 展开原始日志片段；每条证据标注 `source: lines`。

---

## 11. Plugin 设计（V0.3 接口，本次预留）

```python
class HostBoundPlugin:
    name: str
    def detect(self, collection) -> bool: ...     # 判断该插件是否适用
    def parse(self, files) -> ParseResult: ...    # 产出 events/metrics
    def features(self, fs) -> None: ...           # 注入特征
    def rules(self) -> list[Rule]: ...            # 追加规则(可选)
```

注册：`plugins/` 下 python 包自动发现；核心分析框架不因新增中间件而修改。首期内置走 parser registry 同一接口。

---

## 12. 测试设计

- **单元**：miniyaml（合法/非法/边界）、各 Parser（合成文本→期望结构）、规则求值器、评分、脱敏。
- **集成**：`tools/make_synthetic_case.py` 生成 8 个黄金案例 → 全管线 analyze → report.json 断言。
- **黄金案例（examples/cases/，各含 input/ 与 expected/）**：
  1. cpu_bound_case：CPU 94%、rq>1、perf 热点=预处理 → 期望 HB≥80、分类 CPU_COMPUTE_BOUND
  2. thread_oversub_case：OMP=128/核 64、cs 高 → 期望 THR-001 命中 P0
  3. numa_case：numa_miss 21% → NUMA-001 命中
  4. io_bound_case：iowait 35% → IO-001 命中
  5. memory_bound_case：IPC 0.6、cache-miss 12% → MEM 倾向
  6. normal_case：CPU 35%、device 95% → HB<20、结论健康
  7. incomplete_case：仅部分文件 → grade=partial，仍出报告并列限制
  8. unknown_log_case：含未知日志 → GenericLog 注册、不崩溃
- **回归（Golden Result）**：`pytest tests/test_e2e_cases.py` 断言 diagnosis 分类、score 区间、根因 top1、关键建议存在；规则修改导致 Golden 变化必须显式更新 expected/ 并在 CHANGELOG 记录。
- **异常路径**：空文件/乱码/超大行/缺 manifest → 全部不崩溃。

---

## 13. 版本路线图

| 版本 | 范围 | 状态 |
|---|---|---|
| V0.1 | Collector(L1/L2) + 拓扑 + 线程 + perf stat + 全套基础 Analyzer + 规则/证据/评分引擎 + 固定 HTML + CLI + 8 黄金案例回归 | 本次交付 |
| V0.2 | Timeline(host-device gap) + msprof/perfetto 解析 + DataLoader 深度 + L3 perf record | 规划 |
| V0.3 | Plugin API + Before/After 对比 + 未知日志自动发现增强 | 规划 |
| V0.4 | LLM Agent 自动根因叙述（仅解释层，不产生数据） | 规划 |
