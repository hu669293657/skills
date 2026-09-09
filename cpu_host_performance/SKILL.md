---
name: "cpu_host_performance"
description: "CPU Host 性能自动化分析 Skill：解析 Linux ftrace/trace 采集数据，自动计算调度延迟、IRQ/SoftIRQ、CPU 利用率等指标并输出中文诊断报告。当用户提供 trace 文件/tar.gz/目录要求分析 CPU Host 性能问题、训练/推理性能下降、NPU 利用率异常、调度延迟、IRQ 热点等场景时调用。"
---

# CPU Host 性能分析 Skill

面向训练/推理场景的 **Host 侧 CPU 根因自动化分析**。客户只负责采集，Skill 负责分析，报告直接给结论，结论必须有证据。

## 何时使用本 Skill

用户出现以下情况时，应主动使用本 Skill：

- 训练/推理性能下降、NPU 利用率异常，怀疑 Host CPU 瓶颈
- CPU 利用率高、Host bound、调度延迟大、dataloader 卡顿
- HCCL/通信线程异常、vLLM Host CPU 瓶颈
- CPU imbalance、IRQ/SoftIRQ 热点（如 NET_RX 软中断打满单核）
- CPU frequency 异常、CPU idle 异常
- 用户直接提供 trace 文件并要求"分析 trace / 分析 CPU / 出报告"

## 输入与输出

**输入**（自动识别格式，无需用户转换）：
- `cpu_trace_*.tar.gz`（本 Skill 采集脚本产物，内含 trace.txt + 系统快照）
- 任意 `.tar.gz` / `.zip` / 目录，内含 ftrace 文本
- 裸 ftrace 文本（`cat /sys/kernel/debug/tracing/trace` 原样输出）
- trace-cmd report 输出的文本
- Chrome Tracing / Perfetto JSON（事件数组格式）
- 精简模式（仅 sched_switch）与全量模式 trace 均可分析

**输出**：
- `report.md` — 中文 Markdown 报告
- `report.html` — 单文件离线 HTML 报告（inline SVG 图表，无 CDN/外部依赖）
- 两个报告内容一致，均存放于工作区 `cpu_host_performance_report/` 目录

## 使用流程（Agent 必须遵守）

1. **确认输入**：用户提供路径后，先用脚本探测文件类型（tar.gz/zip/目录/文本/JSON）。
2. **运行分析**：
   ```bash
   python3 -m cpu_host_performance analyze <输入路径> --output-dir <输出目录>
   # 亦支持: python3 analyzer/analyze_cpu_trace.py <输入路径>
   ```
3. **查看结论**：分析器会打印 Host 状态（CRITICAL/WARNING/DEGRADED/HEALTHY/UNKNOWN）与问题清单。若分析器因数据缺失无法完整运行，Agent 须按 SKILL.md 第 4 节的指标手册人工分析（解析规则一致）。
4. **生成报告**：报告生成后，用浏览器/读取工具检查 report.html 是否正常渲染，再交付给用户。
5. **转述结论**：向用户汇报时遵循 **先结论 → 后证据 → 再建议** 的顺序，不得只罗列指标。

## 报告语言与指标解释（硬性要求）

- **报告正文一律使用中文**（指标英文名保留，如 NET_RX、sched_switch）。
- 报告中出现的**每一个指标都必须附中文解释**，说明它是什么、为什么重要。指标释义统一从 `analyzer/metrics.py` 的 `METRIC_EXPLANATIONS` 取用，例如：
  - `NET_RX` — 网络接收软中断：网卡收包后内核协议栈处理的延迟执行部分，过高说明网络收包压力大。
  - `sched_switch` — 进程切换事件：记录 CPU 上一个任务切换到下一个任务，是计算利用率/调度延迟的基础。
- 若 trace 中出现未知事件名，按 `TASK/IRQ/未知事件` 兜底释义处理并在报告中标注"未收录事件"。

## 分析逻辑要求（证据优先）

1. 不允许"CPU 90% → CPU 有问题"式单指标判断，必须组合判断：
   - CPU>95% + runnable 堆积 + 调度延迟高 → CPU_SATURATION / SCHEDULER_CONTENTION
   - 总利用率中等但个别核 100%、其余空闲 → CPU_IMBALANCE（排查 IRQ affinity/绑核）
   - 利用率正常 + 频率明显偏低 → CPU_FREQUENCY_LOW
   - CPU 空闲但业务差 → HOST_NOT_BOTTLENECK（明确说 CPU 不是主瓶颈，勿强行归因）
   - SoftIRQ 高且集中少数核 → IRQ_HOTSPOT / SOFTIRQ_HOTSPOT
2. 每个结论必须包含：Metric / Value / Threshold / Evidence / Confidence（HIGH/MEDIUM/LOW）。
3. 证据不足时输出 `INSUFFICIENT_EVIDENCE`，置信度不得标 HIGH；能判断 NUMA 则判断，不能则明确写"证据不足"。
4. 时间关联分析：问题发生在哪个时间窗、持续多久、是否周期性，必要时建立"高负载→IRQ↑→调度延迟↑→worker 运行时间下降→NPU 等待 Host"因果链（仅在有证据时）。

## 目录结构

```text
cpu_host_performance/
├── SKILL.md                      # 本文件
├── scripts/cpu_trace_collect.sh  # 独立采集脚本（可单独发给客户）
├── analyzer/
│   ├── __init__.py / __main__.py # CLI: python3 -m cpu_host_performance analyze <path>
│   ├── analyze_cpu_trace.py      # 分析主流程编排
│   ├── parser.py                 # 多格式健壮解析（tar/zip/目录/文本/JSON）
│   ├── metrics.py                # 指标计算 + 中文释义表 METRIC_EXPLANATIONS
│   ├── diagnosis.py              # 13 类问题诊断规则与置信度
│   └── report.py                 # Markdown + 离线 HTML（inline SVG）
├── templates/                    # 报告骨架模板
├── examples/                     # 示例报告
└── README.md                     # 使用说明
```

## 采集脚本使用（发给客户）

```bash
sudo bash cpu_trace_collect.sh --duration 30          # 默认 30s 全量
sudo bash cpu_trace_collect.sh --duration 10 -m       # 精简模式，仅 sched_switch
sudo bash cpu_trace_collect.sh -t 60 -c 0-47          # 指定 CPU 列表
```

产物 `cpu_trace_YYYYMMDD_HHMMSS.tar.gz` 直接回传即可。脚本仅依赖 bash+ftrace+核心utils，不联网、不装包、结束后自动恢复 tracing 原配置。

## 分析器最小依赖

- Python 3.7+，**仅标准库**（tarfile/zipfile/json/re/collections/statistics），无第三方依赖，离线可用。
- 输出 HTML 图表使用 inline SVG，不依赖任何外部 JS/CSS。
