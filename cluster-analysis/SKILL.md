---
name: cluster-analysis
description: 分析单份昇腾（Ascend）NPU 卡级 Prof 或已有 cluster_analysis_output，生成 performance_report.html。适用于单卡或多卡性能分析、慢卡或慢算子定位、通信诊断、Advisor 和同次采集内的双卡比对。两份集群采集的对比使用 cluster-compare，故障日志排查使用 issue-analyzer，Host 系统级瓶颈诊断使用 cpu-host-performance。
---

# 昇腾 NPU 训练性能分析技能（cluster-analysis）

面向昇腾 NPU 训练与推理场景的性能分析技能。原生 CLI 按子命令预检后优先使用；内置 fallback 支持 TEXT 导出与 DB-only 两种数据源（TEXT 缺失时自动回退 `analysis.db` 适配层，见 [references/db_support.md](references/db_support.md)），能力不等价处均透明标注。没有有效分步标识时，整个 prof 按每卡一个汇总 step（`prof`）分析；TEXT 空 Step 与 DB NULL-step 统一处理。有真实 step 时保留原分步。缺摘要时使用可读事件与时间范围建立整段 step；仍无有效范围或数据时明确报不足。无分步数据必须阅读 [references/whole_prof_step.md](references/whole_prof_step.md)。

TEXT 导出目录可以同时存在 DB。以采集配置和实际产物能力共同路由，不根据 DB 文件存在猜测导出格式。后续阶段使用 detector 给出的实际卡根目录。原始 step 与整段汇总 step（`prof`）分别标记；整段分析只计一个 step，不解释为一次训练迭代或一个推理请求。报告先检查 `workflow_status.json`：`complete` 才算完整完成，`partial`/`failed` 必须说明失败阶段。结论限于已提供 rank 和 step/window，通信占比高或 collective 耗时短不等于已经定位慢卡。

把数据目录交给 `scripts/run_workflow.py --input <路径>`。纯集群输出、集群输出随附部分卡级 Prof、全量卡级 Prof 都用 P7 的 `scripts/report_assets/template.html` 生成 `performance_report.html`。已有集群输出时，集群总览和 KPI 取它覆盖的全部 Rank × Step，原库只读。卡级 Advisor、Insight、双卡比对和泳道使用实际提供的 Prof，首页分别显示集群汇总与卡级覆盖。缺少卡级 Prof 时直接使用集群汇总出报。

P6a Insight 瓶颈定位为「数据概览」提供严重度组合卡，为「结论与建议」提供双栏证据工作台。两处共用归一化严重度。每类最多三条候选，Free 展示整步空闲分布并标记候选位置，计算默认折叠实例级 PMU、输入输出与任务参数。归一化与缺失口径见 [references/insight_localization.md](references/insight_localization.md)。

有卡级数据时执行 P6a，并完整阅读 [references/insight_localization.md](references/insight_localization.md)。`insight_bottlenecks.json` 是总报告使用的定位证据，同名 `.md`/`.html` 是附加摘要，不能代替总报告或被当作完成完整流程的依据。

## 必读入口

执行任何分析前，**必须先完整阅读 `references/pipeline.md`**。它是执行链路的单一事实来源（SSOT），涵盖：

- 八阶段流水线规范（P0–P7 命令、参数、目录与产物契约）
- 全局纪律（原生 CLI 优先、单卡/多卡差异、全量 recipe、unit 换算、泳道与报告纪律）
- 并行波次调度（子 agent Wave 0–4，每波最多 3 个并行）
- run_workflow 一键编排与端到端验收清单

## 能力速览

1. **输入自动识别**：单卡/多卡、db/text 格式、框架（PyTorch/MindSpore）、已有 `cluster_analysis_output`；逐卡输出 db 能力清单（step 摘要/采集窗、通信表、rank-device 映射）。
2. **DB 适配层**：TEXT 导出缺失时自动读取 `analysis.db`（StepTraceTime / CommAnalyzer*）与主 DB（RANK_DEVICE_MAP / HOST_INFO / SESSION_TIME_INFO），WAL 感知只读；单位、窗口语义与来源标记契约见 [references/db_support.md](references/db_support.md)。
3. **Advisor 诊断**：单卡做单卡 advisor；多卡对全部卡聚合 advisor，输出 md。
4. **集群分析输出件**：多卡缺失时自动生成（db + 基表；DB-only 由适配层建库，来源写入 ClusterBaseInfo/status_info），置于单卡 prof 目录同级。
5. **进阶分析**：五大类 23 正式 + 1 扩展 recipe 全量执行，能力矩阵透明呈现（不支持者标注原因）。
6. **性能比对**：默认比较通信占比最大和最小的两卡，也可指定两个 rank。内置模块 `scripts/prof_compare/` 使用比对工作簿及可用的原始明细，生成 HTML 和中文 xlsx。
7. **泳道统计**：每卡 Process / Ascend Hardware / Communication 三类算子统计，多卡跨卡比对；依赖 trace_view.json/kernel_details.csv，DB-only 数据泳道记为 unsupported（P6a 仍可用 DB 原始事件定位）。
8. **最终 HTML 报告**：信息概览在前、结论建议随后，左侧固定导航，分区可折叠默认展开，有双卡比对结果时嵌入 prof-compare 子报告。
   集群总览可按 Step、指标和卡序查看 Rank 耗时走势。通信算子耗时按类型各列 Top 10。指标与排序口径见 [报告模板规范](references/html_template_spec.md#rank-耗时走势和通信榜单)。
   结论章节提供通信实例去重、真实通信组映射、慢卡及 Host 回溯、计算指标与 Free 时间窗定位。

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/pipeline.md` | **单一事实来源**：阶段命令、目录契约、全局纪律、并行调度、验收清单、扩展守则 |
| `references/recipes_catalog.md` | 五大类 23 正式 + 1 扩展 recipe 目录 |
| `references/msprof_cli.md` | msprof-analyze 原生 CLI 与 fallback 能力对照 |
| `references/html_template_spec.md` | P7 唯一主报告模板的布局与交互规范 |
| `references/insight_localization.md` | 原报告结论章节的 Insight 映射、慢卡归因、计算/Free 证据及定位输出契约 |
| `references/whole_prof_step.md` | 无分步 TEXT/DB 整段单 step 规则、来源、时间边界与验收 |
| `references/db_support.md` | DB-only 适配契约：analysis.db/主 DB 已验证 schema、单位、采集窗语义、来源标记与限制 |

> 双卡比对的脚本和模板随本 skill 分发。内置流程使用 Python 标准库生成 HTML、Excel、JSON 和 CSV。输入要求和命令见 `references/pipeline.md` 的 P4–P5。
