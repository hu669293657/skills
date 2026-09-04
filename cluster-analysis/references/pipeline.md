# 八阶段流水线规范（pipeline.md）

> 本文件是执行链路的**单一事实来源（SSOT）**：所有命令、参数与产物路径均以 `scripts/run_workflow.py` 及各脚本 argparse 实现为准（核实基准：当前 scripts/ 代码）。
> 合并自原 workflow.md 与 vendor_integration.md。

## 1. 目录与产物契约

所有阶段共用一个中间件根目录（run_workflow 创建，默认名 `cluster_analysis_mw`）。下表路径均相对该目录：

| 路径 | 产出者 | 说明 |
|---|---|---|
| `detect_result.json` | detect_prof.py | 输入类型判定（db/text、单卡/多卡/集群），后续阶段的分支依据 |
| `advisor/advisor.md`、`advisor/advisor.json` | advisor_fallback.py（原生 advisor all 产物仅留档） | Advisor 结论 |
| `cluster_analysis_output/cluster_analysis.db` | 原生 msprof-analyze 或 cluster_fallback.py | 集群分析主库（SQLite） |
| `cluster_analysis_output/recipes/<name>/*.csv` | recipe_fallback.py | 各 recipe 明细 CSV |
| `cluster_analysis_output/recipes_summary.{md,json}` | recipe_fallback.py | recipe 汇总（同时写入 DB 表 RecipeSummary） |
| `compare/compare_analysis_result.json` | compare_fallback.py | 卡间对比明细 |
| `compare/performance_comparison_result_*.xlsx` | 原生 msprof-analyze（归一化复制）或 compare_fallback.py | 对比 Excel（vendor compare 消费此文件） |
| `reports/cluster_analysis_report.html` | vendor_bridge（cluster） | vendor 单模式报告（最终报告 iframe 内嵌） |
| `reports/compare_analysis_report.html` | vendor_bridge（compare） | vendor 对比报告（iframe 内嵌） |
| `reports/cluster_data.json`、`reports/chinese_xlsx/` | vendor_bridge 附带产物 | 报告数据与中文表 |
| `swimlane/swimlane_rank{N}.{md,json}` | swimlane_analyzer.py | 每卡泳道分析 |
| `swimlane/swimlane_compare.md` | swimlane_analyzer.py | 泳道跨卡对比 |
| `performance_report.html` | report_generator.py | 最终总报告（相对路径引用 reports/ 子目录） |

## 2. 阶段规范（P0–P7）

所有阶段由 run_workflow.py 顺序编排；任一阶段失败即中止（vendor 的 SKIP 除外）。幂等：输出已存在时默认跳过，`--force` 强制重跑。

### P0 detect — 输入判定
```bash
python scripts/detect_prof.py --path <输入路径> --output <mw>/detect_result.json --skill-dir <skill根>
```
- 失败处理：中止流水线（无输入类型无法继续）。

### P1 advisor（原生留档 + 内置归一化）
探测到 `msprof-analyze` 时先执行原生 `advisor all`（产物 mstt_advisor_* 仅留档），随后**始终**运行内置实现归一化（advisor.md/advisor.json 为总报告契约件，保证两种路径产物一致）：
```bash
python scripts/advisor_fallback.py --root <输入路径> --output <mw>/advisor
```

### P2 cluster（原生优先）
原生：`msprof-analyze cluster analysis -d <输入路径> --top_num N`。原生成功但产物缺 `cluster_analysis.db` 时（下游 recipes/泳道依赖），用内置实现补齐（幂等：db 已存在自动跳过）。原生失败/不可用降级：
```bash
python scripts/cluster_fallback.py --root <输入路径> [--force]
```

### P3 recipes（原生优先）
原生：逐个执行 8 个 recipe 子命令（cluster_time_summary / communication_time_sum / slow_rank / slow_link / communication_bottleneck / free_analysis / compute_op_sum / hccl_sum，附 `--top_num`）。原生成功但缺 `recipes_summary.json`（总报告契约件，原生不产出）时，用内置实现补齐全量 23+1 recipe 与汇总件。原生失败/不可用降级：
```bash
python scripts/recipe_fallback.py --root <输入路径> --top-num 20
```
- CATEGORIES 注册的 23 个正式 recipe；extra recipe `communication_bandwidth_sum` 实际执行但未注册（扩展定位，见 recipes_catalog.md）。
- 通信算子判定正则：当前以 recipe_fallback.py 的 `COMM_NAME_PATTERN` 为准（二期收敛至 scripts/lib/config.py）。

### P4 compare（原生优先）
- 原生路径：复用内置选卡（通信占比最小=baseline、最大=compare；`--base-rank/--compare-rank` 可覆盖）→ `msprof-analyze compare -d <baseline卡> -c <compare卡> -o <mw>/compare/native` → 原生 xlsx 归一化复制为 `<mw>/compare/performance_comparison_result_{max}_{min}.xlsx`（vendor glob 契约，历史命名 xlsx 一并清理）→ `compare_fallback.py --json-only` 补产 `compare_analysis_result.json`（仅产 JSON，不触碰 xlsx）。选卡失败/原生命令失败/未找到 xlsx/JSON 补产失败 → 整体回退完整内置实现。
- 降级：
```bash
python scripts/compare_fallback.py --root <输入路径> --output <mw>/compare [--base-rank <N> --compare-rank <N>]
```
- `--base-rank/--compare-rank` 未给时自动选卡；产物 `performance_comparison_result_{tag}.xlsx` + `compare_analysis_result.json`。

### P5 vendor — 集成开源报告
```bash
python scripts/vendor_bridge.py cluster --data-dir <mw>/cluster_analysis_output --reports-dir <mw>/reports
python scripts/vendor_bridge.py compare --compare-dir <mw>/compare --reports-dir <mw>/reports
```
纪律（禁止绕过 vendor_bridge 直接调 vendor 内部脚本）：
- vendor_bridge 固定以 `--mode single` 调 vendor/generate_cluster_report.py；compare 模式 glob `performance_comparison_result_*.xlsx` 取第一个交 main_analyzer.py。
- 返回码：0 = 成功；2 = 输入缺失/不支持 → 跳过并继续（不算失败）；其他 = 失败。
- 产物写入 reports/ 子目录，最终报告以相对路径 iframe 内嵌。

### P6 swimlane
```bash
python scripts/swimlane_analyzer.py --card <卡号> --rank <N> --output <mw>/swimlane [--compare-dir <mw>]
```
- 多卡时每卡产出 swimlane_rank{N}.md/.json，随后产出 swimlane_compare.md。
- Chrome Trace 流式解析，防大文件 OOM。

### P7 report
```bash
python scripts/report_generator.py --middleware <mw> --mode {single|cluster|compare} --output <mw>/performance_report.html
```
- mode 由 detect_result.json 判定决定（run_workflow 自动传递）。
- 图表 ECharts 懒初始化；`<details>` 区块默认展开；样式与模板规范见 html_template_spec.md。

## 3. run_workflow 一键编排

```bash
python scripts/run_workflow.py --input <Prof数据路径> --output-dir <输出根> \
    [--top-num 20] [--base-rank N --compare-rank N] [--report-mode cluster] \
    [--skip-recipes] [--skip-compare] [--skip-swimlane] [--skip-report] [--force] [--no-native]
```
- 默认**原生优先**：探测到 `msprof-analyze`（PATH 存在性）即各阶段优先原生，失败或产物缺口自动回退/由 fallback 补齐；`--no-native` 整体禁用原生，全部使用内置实现；`--msprof-cli` 兼容保留（no-op）。
- 未提供的可选参数均取脚本默认值；json 解析输入异常已由 run_workflow 保护（try/except）。

## 4. 全局纪律

1. **原生 CLI 优先（默认）**：run_workflow 以 `shutil.which('msprof-analyze')` 探测；可用则 P1 `advisor all`（留档）、P2/P3 `cluster` 子命令、P4 `compare` 均优先原生，失败或产物缺口（db/summary/json）自动回退或由 fallback 补齐；`--no-native` 整体禁用（能力对照见 msprof_cli.md）。
2. **单卡模式**：P2/P3/P4/P6 跳过；advisor 只做单卡；P7 `--mode single`。**多卡模式**：P1 对**所有卡**聚合 advisor（不是每卡一个）；比对卡对 = **通信占比最大 vs 最小**两张卡。
3. **进阶分析全量执行**：23 正式 + 1 扩展 recipe 全跑；部分数据缺失的 recipe 输出「不支持/数据缺失」说明，不算失败。
4. **unit 纪律**：db 内时间为 μs，text-CSV 为 μs，text-JSON 为 ms；写报告时统一换算为 ms 并标注。
5. **泳道分类纪律**：Thread xxx / Stream xxx / Communication 域（Group 内非 Plane 泳道，如 dp:xx）三类汇总；Plane 泳道不单独成节。
6. **报告纪律**：信息概览最前 → 结论与建议随后 → 详情分区；左侧固定导航；`<details>` 可折叠且**默认全部展开（含子报告）**；集群总览按 Step 区分；无「卡片清单（cluster 模式）」分区；vendor 报告以 iframe 内嵌。模板实现细节统一见 html_template_spec.md（模板规范的修改只更新该文件）。

## 5. 并行波次调度（子 agent）

分析任务多且相互独立，按「波次（Wave）」调度子 agent，每波最多 3 个并行：

- **Wave 0**：主 agent 运行 P0 检测，确定模式（单卡/多卡）与后续步骤清单。
- **Wave 1**（互不依赖，3 个子 agent 并行）：
  - 子 agent A：P1 全卡 advisor
  - 子 agent B：P2 集群输出件生成（多卡且缺失时；单卡跳过）
  - 子 agent C：P4 比对 xlsx（多卡时；单卡跳过）
- **Wave 2**：
  - 子 agent D：P3 全量 recipe（依赖 P2）
  - 子 agent E：P5 vendor compare 子报告（依赖 P4）
  - 子 agent F：P6 泳道统计（若卡数多，按卡拆分为多个子 agent，每个负责若干卡）
- **Wave 3**：
  - 子 agent G：P5 vendor cluster 子报告（依赖 P2）
- **Wave 4**：主 agent 运行 P7 汇总报告，嵌入全部子报告。

> 泳道统计（P5）逐卡独立、互不依赖，是最佳并行点；trace_view.json 较大（数百 MB），每个子 agent 用流式解析脚本处理，不要整文件读入。

## 6. 端到端验收清单

1. `detect_result.json` 存在且含类型判定字段。
2. advisor.md 与 advisor.json 至少其一存在（始终由内置实现归一化；原生 advisor all 产物仅留档）。
3. `cluster_analysis.db` 为有效 SQLite 文件。
4. `recipes_summary.md` 覆盖全部执行过的 recipe（对应 summary JSON 存在）。
5. 多卡时 `performance_comparison_result_*.xlsx` 与 `compare_analysis_result.json` 成对存在。
6. `reports/` 下至少一份 vendor HTML；vendor SKIP 属预期时最终报告应有标注。
7. 多卡时 swimlane_rank{N}.md 覆盖全部参与卡，swimlane_compare.md 存在。
8. `performance_report.html` 可直接打开，内嵌子报告可见（相对路径未断）。

## 7. 扩展守则（新增阶段 / recipe 的固定模式）

- 新增阶段：脚本只做「输入路径 → 产物路径」的单向变换，参数命名对齐现有惯例（--root/--output），并在 run_workflow 注册阶段号与产物检查。
- 新增 recipe：函数返回统一 `(rows, reason)` —— rows 为 list[dict]，不支持时 `(None, "原因")`；正式 recipe 在 CATEGORIES 注册，extra recipe 需在 recipes_catalog.md 标注。
- 跨脚本共享常量（目录名、正则、阈值）只允许放 scripts/lib/config.py。
