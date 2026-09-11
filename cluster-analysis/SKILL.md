---
name: cluster-analysis
description: 昇腾（Ascend）NPU 训练性能问题分析与定位技能。当用户提供 Prof 采集数据目录（单卡或多卡/集群），要求做性能分析、瓶颈定位、慢卡/慢算子排查、通信问题分析、advisor 诊断、集群分析对比，或要求生成性能分析 HTML/MD 报告时触发。自动识别输入形态（单卡/多卡、db/text、仅含 cluster_analysis_output），缺失时生成 cluster_analysis_output，执行五大类全部进阶 recipe，泳道算子统计与跨卡比对，通信占比最大/最小卡对比，最终产出含左侧固定导航、可折叠分区、子报告嵌入的完整 HTML 报告；若输入仅含 cluster_analysis_output（无任何卡目录），则只调用 vendor cluster-output-analysis 生成独立报告，不生成完整 HTML 报告。
---

# 昇腾 NPU 训练性能分析技能（cluster-analysis）

面向昇腾 NPU 训练场景的一站式性能分析技能，兼容 msprof-analyze 开源项目全部能力（已安装则优先调用原生 CLI，未安装则使用内置 fallback 脚本，能力等价）。

## 必读入口

执行任何分析前，**必须先完整阅读 `references/pipeline.md`**。它是执行链路的单一事实来源（SSOT），涵盖：

- 八阶段流水线规范（P0–P7 命令、参数、目录与产物契约）
- 全局纪律（原生 CLI 优先、单卡/多卡差异、全量 recipe、unit 换算、泳道与报告纪律）
- 并行波次调度（子 agent Wave 0–4，每波最多 3 个并行）
- run_workflow 一键编排与端到端验收清单

## 能力速览

1. **输入自动识别**：单卡/多卡、db/text 格式、框架（PyTorch/MindSpore）、已有 `cluster_analysis_output`。
2. **Advisor 诊断**：单卡做单卡 advisor；多卡对全部卡聚合 advisor，输出 md。
3. **集群分析输出件**：多卡缺失时自动生成（db + 基表），置于单卡 prof 目录同级。
4. **进阶分析**：五大类 23 正式 + 1 扩展 recipe 全量执行，能力矩阵透明呈现（不支持者标注原因）。
5. **性能比对**：通信占比最大 vs 最小两卡对比，vendor prof-compare 生成 HTML 与中文 xlsx。
6. **泳道统计**：每卡 Process / Ascend Hardware / Communication 三类算子统计，多卡跨卡比对。
7. **最终 HTML 报告**：信息概览在前、结论建议随后，左侧固定导航，分区可折叠默认展开，vendor 子报告 iframe 内嵌。
8. **独立报告模式**：输入仅含 `cluster_analysis_output`（无任何卡目录，mode=`cluster_output_only`）时，跳过完整流水线，只调用 vendor cluster-output-analysis 生成独立报告 `reports/cluster_analysis_report.html`，**不生成总报告 performance_report.html**。

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/pipeline.md` | **单一事实来源**：阶段命令、目录契约、全局纪律、并行调度、验收清单、扩展守则 |
| `references/recipes_catalog.md` | 五大类 23 正式 + 1 扩展 recipe 目录 |
| `references/msprof_cli.md` | msprof-analyze 原生 CLI 与 fallback 能力对照 |
| `references/html_template_spec.md` | HTML 模板交互规范（模板改动只改这里） |

> vendor 子技能（`vendor/cluster-output-analysis/`、`vendor/prof-compare/`）**只经 `scripts/vendor_bridge.py` 调用**（参数与返回码见 pipeline.md P5），禁止绕过桥接器直接调用其内部脚本；其内部 SKILL.md 仅供维护者参考，分析任务无需读取。
