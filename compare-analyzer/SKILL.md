---
name: "compare-analyzer"
description: "Parses msprof-analyze compare output xlsx, extracts key insights per sheet, generates HTML report and Chinese xlsx. Invoke when user has a compare result xlsx and wants analysis report or Chinese translation."
---

# Compare Analyzer

## 功能概述

本 Skill 用于解析 msprof-analyze compare 工具生成的 `performance_comparison_result_*.xlsx` 输出件，针对每个 Sheet 页提炼关键数据并进行分析，最终生成：

1. **中间件 JSON**：包含所有 Sheet 的结构化分析结果
2. **HTML 报告**：可视化呈现性能比对分析结论（改善亮点/劣化风险双维度）
3. **中文 xlsx**：将原版英文表头翻译为中文的 xlsx 文件（多 Sheet 标签页 + 同步 CSV）

## 使用方法

```bash
# 完整流程：解析 xlsx → 生成中间件 JSON → 生成 HTML 报告 → 生成中文 xlsx
python scripts/main_analyzer.py <xlsx_file_path> [-o <output_dir>]

# 示例
python scripts/main_analyzer.py ./performance_comparison_result_20250101.xlsx -o ./report_output
```

## 文件结构

```
compare-analyzer/
├── SKILL.md                          # Skill 说明文件
├── references/
│   └── analysis_methodology.md       # 分析方法论（各 Sheet 指标含义与分析经验）
└── scripts/
    ├── main_analyzer.py              # 主调度脚本
    ├── html_generator.py             # HTML 报告生成器
    ├── csv_translator.py             # 中文翻译器（xlsx + CSV 同步输出）
    └── parsers/                      # 8 个解析器模块
        ├── __init__.py
        ├── common.py                 # 公共工具：safe_float、safe_int、get_sheet_data
        ├── overall_metrics.py        # 总体性能：维度拆解 + 改善/劣化双方向
        ├── operator.py               # 算子统计+明细（合并）
        ├── module.py                 # 模块统计+明细（合并）
        ├── memory.py                 # 内存统计+明细（合并）
        ├── kernel.py                 # Kernel比对+类型+负载均衡（合并）
        ├── communication.py          # 通信比对
        └── api_compare.py            # API比对
```

## 各 Sheet 分析思路

### 1. OverallMetrics（总体性能比对）
- **分析目标**：定界性能方向（计算/通信/调度），区分改善和劣化
- **关键指标**：E2E Time、Computing Time、Uncovered Communication Time、Free Time
- **分析方法**：提取四大维度的基准/比对值和差异比率，分别识别改善最大维度和劣化最大维度，计算各维度对总变化的贡献占比
- **下钻分析**：提取子类别（FA前向/反向、Conv、Matmul、Vector等），分改善/劣化两组排序
- **特殊检测**：Not minimal profiling 警告

### 2. OperatorCompareStatistic（算子统计比对）
- **分析目标**：定位劣化 TOP 算子
- **关键指标**：Diff Duration(ms)、Diff Ratio
- **分析方法**：按 Diff Duration 降序排列，提取 Top 10，计算累计劣化占比（集中度），判断劣化是否集中于少数算子

### 3. OperatorCompare（算子明细比对）
- **分析目标**：查看 TOP 算子的 Kernel 级详情
- **关键指标**：Device Duration(us)、Kernel Details、Input Shape
- **分析方法**：关联统计页 TOP 算子，提取对应明细行的 Kernel 信息，分析 Kernel 数量变化

### 4. ModuleCompareStatistic（模块统计比对）
- **分析目标**：定位劣化 TOP 模块
- **关键指标**：Device Total Time Diff(ms)、Device Self Time Diff(ms)
- **分析方法**：筛选 [ TOTAL ] 行，按总耗时差异降序，提取 Top 10 模块

### 5. ModuleCompare（模块明细比对）
- **分析目标**：结合调用栈定位代码位置
- **关键指标**：Call Stack、Device Self Time(us)
- **分析方法**：提取劣化模块下算子的调用栈信息

### 6. CommunicationCompare（通信比对）
- **分析目标**：定位通信改善/劣化算子，区分等待 vs 传输
- **关键指标**：Total Duration(us)、Diff Ratio、Wait/Transmit 拆解
- **分析方法**：按差异绝对值排序，分别识别改善最大和劣化最大的算子，标记极端改善（ratio < 0.1）

### 7. MemoryCompareStatistic（内存统计比对）
- **分析目标**：定位内存增长 TOP 算子
- **关键指标**：Diff Memory(MB)、Diff Ratio
- **分析方法**：按内存差异降序，提取 Top 10。检测全零内存场景（NPU vs NPU 无内存差异）

### 8. MemoryCompare（内存明细比对）
- **分析目标**：查看 TOP 算子内存分配详情
- **关键指标**：Size(KB)、Allocated Details
- **分析方法**：关联统计页 TOP 算子，提取分配明细

### 9. KernelCompare（Kernel比对）
- **分析目标**：定位劣化 Kernel，分析负载均衡
- **关键指标**：Total/Avg/Max/Min Duration(us)、Calls、Diff Total/Avg Ratio
- **分析方法**：
  - 全局统计：劣化/改善总量、净趋势判断
  - 显著劣化：ratio > 1.05 的 Kernel 单独标记
  - **通信/计算分离**：`hcom_` 前缀的通信算子排除出 Top10 和负载均衡分析，单独折叠展示
  - **两卡数据量比对**：计算每个算子的输入数据量（Shape 各维度相乘后所有 tensor 求和），汇总两卡总数据量和总 calls，判断负载是否均衡
  - **Top 10 计算算子数据量明细**：展示单次数据量、基准/比对 calls、基准/比对总数据量、耗时对比
  - **负载均衡分析**：仅对 calls > 10 的计算算子做 max/min 方差分析，方差 > 2x 标记预警
  - **多 Shape 分布**：同名 Kernel 不同 Shape 的数据量和耗时对比

### 10. KernelTypeCompare（Kernel类型比对）
- **分析目标**：按类型汇总 Kernel 劣化
- **关键指标**：Kernel Type、Core Type、Total Duration
- **分析方法**：按类型聚合，识别劣化集中的 Kernel 类型

### 11. ApiCompare（API比对）
- **分析目标**：定位劣化 Host 侧 API
- **关键指标**：Total Duration(ms)、Self Time(ms)、Calls
- **分析方法**：按总耗时差异排序，区分纯耗时变化（calls_ratio=1.0）vs 调用次数变化，统计净趋势

## 异常处理

所有解析器均处理以下异常情况：
- Sheet 不存在（比对开关未开启）
- Sheet 为空（无数据行）
- 列数不匹配（数据格式变化）
- 数值解析失败（非数字字段）
- 比率为 inf 或 NaN（基准值为 0）
- 全零内存场景（NPU vs NPU 无内存差异）
- 通信算子空 Shape（排除出负载均衡分析）

## 输出件

| 输出件 | 文件名 | 说明 |
|---|---|---|
| 中间件 JSON | `compare_analysis_result.json` | 所有 Sheet 的结构化分析结果 |
| HTML 报告 | `compare_analysis_report.html` | 改善/劣化双维度可视化报告 |
| 中文 xlsx | `compare_chinese_result.xlsx` | 英文表头翻译为中文，多 Sheet 标签页 |
| 中文 CSV | `chinese_csv/*.csv` | 每个 Sheet 一个独立 CSV（与 xlsx 同步生成） |

## 分析方法论

详细的分析方法论请参考 `references/analysis_methodology.md`，包含：
- 各 Sheet 指标详解（每个字段的含义和分析要点）
- 综合分析决策树（先定界方向 → 再定位瓶颈 → 最后下钻根因）
- 常见场景与解读（GPU→NPU 迁移、版本升级、MoE 通信瓶颈、KV Cache 优化）
- 指标单位速查表和比率解读速查表
