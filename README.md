<div align="center">

<img src="docs/assets/hero.svg" alt="AI Infra Skills — Agent-powered performance analysis" width="100%">

# AI Infra Skills

**AI 智能性能分析技能集**

面向 AI 训练与推理的性能分析、差异归因、故障诊断与优化工作流。

[![Skills](https://img.shields.io/badge/Skills-8-0891b2?style=flat-square)](#技能目录)
[![Ascend](https://img.shields.io/badge/Ascend-NPU-0e7490?style=flat-square)](#核心性能分析)
[![Profiling](https://img.shields.io/badge/Profiling-NPU%20%2F%20GPU-2563eb?style=flat-square)](#核心性能分析)
[![Reports](https://img.shields.io/badge/Reports-HTML%20%2F%20Excel-0f766e?style=flat-square)](#报告与交付)

[快速开始](#快速开始) · [核心性能分析](#核心性能分析) · [技能目录](#技能目录) · [报告与交付](#报告与交付) · [English](README_EN.md)

</div>

## 为什么使用 AI Infra Skills

**让 Agent 执行标准化的性能分析流程，而不仅是给出泛化的优化建议。**

面对训练变慢、通信劣化、GPU → NPU 迁移或推理吞吐不足，不再从大量日志和 Profiling 文件手动拼接分析过程。选择合适的 Skill，提供数据与目标，让 Agent 按定义的工作流执行分析、提取证据并生成交付物。

| 从 | 到 |
| :--- | :--- |
| 手工逐卡排查、反复运行分析命令 | 自动化数据识别、异常 Rank 定位及工作流编排 |
| 仅知道端到端耗时发生变化 | 拆解计算、通信、调度等维度，分析差异贡献 |
| 难以找到具体算子或 Kernel | 从总体指标逐步下钻到热点与执行明细 |
| 人工整理分析材料 | HTML、Markdown、Excel 等结构化交付（视 Skill 而定） |
| 依赖个人经验判断 | 可复用的方法、明确的数据证据与分析边界 |

## 核心性能分析

<img src="docs/assets/workflow.svg" alt="性能分析能力：输入、全景分析、差异归因、微观下钻、报告交付" width="100%">

以上为能力层次示意，并非强制执行顺序。根据输入和问题选用一个或多个 Skill。

<table>
<tr>
<td width="33%" valign="top"><h3><a href="cluster-analysis/README.md">cluster-analysis</a></h3><b>性能全景分析</b><p>一份 Prof，自动识别慢卡、计算、通信与 Host 瓶颈。</p><p><b>输入：</b>单份昇腾 Prof / 集群分析输出</p><p><b>产出：</b>HTML · MD · CSV · Excel</p><a href="cluster-analysis/README.md">查看使用指南 →</a></td>
<td width="33%" valign="top"><h3><a href="cluster-compare/README.md">cluster-compare</a></h3><b>双集群劣化归因</b><p>两份集群数据，拆解差异、判断通信带宽与异常 Rank。</p><p><b>输入：</b>两份昇腾集群数据</p><p><b>产出：</b>HTML · MD</p><a href="cluster-compare/README.md">查看使用指南 →</a></td>
<td width="33%" valign="top"><h3><a href="prof-compare/README.md">prof-compare</a></h3><b>单卡精细化比对</b><p>NPU/GPU 或 NPU/NPU 比对，下钻算子、Kernel、Shape 和单次执行。</p><p><b>输入：</b>两份单卡 Prof / compare XLSX</p><p><b>产出：</b>HTML · 中文 Excel · JSON · CSV</p><a href="prof-compare/README.md">查看使用指南 →</a></td>
</tr>
</table>

- **cluster-analysis：哪里慢？** 支持单卡、多卡与已有集群输出，包含 23 个正式 recipe 和 1 个扩展 recipe；根据实际数据覆盖生成统一 `performance_report.html`。内置 fallback 有明确前置条件，不承诺与原生 CLI 全部能力等价。
- **cluster-compare：为什么变慢？** 比较两个昇腾集群的 Step、Rank、计算、通信、空闲及带宽，输出贡献度归因与分级行动建议。
- **prof-compare：具体慢在哪个算子？** 支持 NPU/GPU 与 NPU/NPU 单卡比对；原始明细可用时支持 Host/Device Top 15 及单次执行 47 项字段展示。

## 快速开始

### 1. 获取技能

```bash
git clone https://github.com/hu669293657/skills.git
cd skills
```

### 2. 交给 Agent

让支持本地文件与命令执行的 Agent 读取对应目录中的 `SKILL.md`，按其步骤执行。也可依照所用 Agent 的技能安装规则导入**完整 Skill 目录**（保留 scripts、references 等文件）。本仓库没有统一安装器；各 Agent 的发现机制、权限和依赖以实际环境为准。

例如：

```text
使用 cluster-analysis 分析 ./prof_data，找出主要性能瓶颈、异常 Rank 及证据，生成完整 HTML 报告。
```

```text
使用 cluster-compare 对比 ./baseline 和 ./current，以 baseline 为基准，分析劣化贡献度、通信带宽与异常 Rank，生成 HTML 和 MD 报告。
```

```text
使用 prof-compare 对比 ./gpu_prof 和 ./npu_prof，以 GPU 为基准，分析总体差异和劣化 Top 算子，生成 HTML 与中文 Excel 报告。
```

替换为实际数据路径。执行前阅读对应 Skill 的输入要求、依赖、权限及数据覆盖说明。

### 3. 不依赖 Agent 的示例

以 cluster-analysis 为例，在仓库根目录：

```bash
cd cluster-analysis
python scripts/run_workflow.py --input "/path/to/prof_data" --no-native
```

满足输入与 Python 依赖条件时，可通过内置工作流生成报告，默认总报告为数据根目录下 `cluster_analysis_mw/performance_report.html`。其他 Skill 的独立运行方式以各自 README 为准。

## 技能目录

| Skill | 适用场景 | 主要输入 | 交付物 |
| :--- | :--- | :--- | :--- |
| [cluster-analysis](cluster-analysis/README.md) | 性能全景分析 | 单份昇腾 Prof / 集群分析输出 | HTML · MD · CSV · Excel |
| [cluster-compare](cluster-compare/README.md) | 双集群劣化归因 | 两份昇腾集群数据 | HTML · MD |
| [prof-compare](prof-compare/README.md) | 单卡精细化比对 | 两份单卡 Prof / compare XLSX | HTML · 中文 Excel · JSON · CSV |
| [cpu-host-performance](cpu-host-performance/README.md) | HostBound 与 CPU 诊断 | ftrace / CPU 采集包 | HTML · MD · 绑核建议 |
| [vllm-ascend-tuning](vllm-ascend-tuning/README.md) | 推理全链路调优 | 模型、硬件、配置及测试目标 | YAML · 调优配置 · 基准结果 |
| [ascend-dump-analyzer](ascend-dump-analyzer/README.md) | 环境与版本配套 | 环境 dump / 版本清单 | HTML · MD |
| [issue-analyzer](issue-analyzer/README.md) | 故障日志取证 | 现网日志及问题描述 | MD · 错误摘要 |
| [ibmc-analyzer](ibmc-analyzer/README.md) | 硬件健康诊断 | iBMC dump_info / 日志包 | HTML · JSON |

<details>
<summary><b>按问题选型</b></summary>

- 一份性能数据，不知道哪里慢 → [cluster-analysis](cluster-analysis/README.md)
- 两个昇腾集群性能差异 → [cluster-compare](cluster-compare/README.md)
- GPU/NPU 或两份单卡 Prof 对比 → [prof-compare](prof-compare/README.md)
- 怀疑 Host/CPU 或绑核问题 → [cpu-host-performance](cpu-host-performance/README.md)
- 推理吞吐、TTFT、TPOT 调优 → [vllm-ascend-tuning](vllm-ascend-tuning/README.md)
- 配置漂移或版本配套 → [ascend-dump-analyzer](ascend-dump-analyzer/README.md)
- 报错、中断或 hang → [issue-analyzer](issue-analyzer/README.md)
- 服务器硬件异常 → [ibmc-analyzer](ibmc-analyzer/README.md)

</details>

## 报告与交付

| 报告 | 输出内容 | 使用指南 |
| :--- | :--- | :--- |
| `performance_report.html` | 集群全景、Rank 趋势、瓶颈证据、建议和可用的双卡比对 | [查看](cluster-analysis/README.md) |
| `cluster_compare_report.html` / `.md` | 双集群差异、劣化归因、带宽判定与分级建议 | [查看](cluster-compare/README.md) |
| `compare_analysis_report.html` / 中文 Excel | 总体指标、热点、算子与 Kernel 差异 | [查看](prof-compare/README.md) |
| Host、环境、硬件诊断报告 | CPU/NUMA/IRQ、版本配套、硬件健康等证据 | [Host](cpu-host-performance/README.md) · [环境](ascend-dump-analyzer/README.md) · [硬件](ibmc-analyzer/README.md) |

公开演示报告与可复现性能基准尚未发布。报告实际覆盖取决于采集数据、设备、框架版本和工具依赖，不以示意图作为实测证据。

## 使用边界与反馈

- 单集群与双集群分析当前主要面向昇腾；跨 GPU/NPU 的单卡比对由 prof-compare 提供。
- 各 Skill 的 `SKILL.md` 与独立 README 是执行定义和技术说明的入口；缺失数据可能导致部分分析不可用。
- 安装依赖、远程访问、绑核或修改服务配置前，请核对权限与影响；生产变更应经过审批、验证及回滚评估。
- 不要公开上传含客户信息、凭据或其他敏感内容的日志、Prof 和报告。仓库当前未提供根级 LICENSE，请在再分发前确认授权及第三方组件条款。

欢迎通过 [Issues](https://github.com/hu669293657/skills/issues) 提交问题与建议。建议提供 Skill 名称、版本/提交、脱敏输入结构、实际结果和预期结果。

<div align="center">

如果这些 Skill 对你的性能分析工作有帮助，欢迎 Star ⭐ 支持项目。

**AI Infra Skills · 从性能数据到可行动的洞察**

</div>
