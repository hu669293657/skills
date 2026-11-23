---
name: "prof-compare"
description: "比对两份单卡 Prof，支持 GPU JSON trace 与 NPU profiler DB 或文本导出，也支持两侧 NPU 独立选择 DB 或文本。生成含 Host 算子、Device kernel Top 15 和单次执行明细的综合报告，支持张量及 AIC/AIV 指标、异名与拆核匹配和已有 compare xlsx 的分析翻译。"
---

# Prof Compare

## 功能概述

### 基线、比对与采集来源

- 按用户指定的角色传参。 `-bp` 对应基线，`-d` 对应比对，两侧均可用 NPU 或 GPU。用户给了「旧版」「新策略」等名称时，用 `--base-label` 和 `--compare-label` 原样传入。两个报告入口均支持这两个参数。
- 从 HTML 首页「比对来源」、中文 xlsx 首张表或 JSON 的 `metadata.comparison` 核对设备、主机和采集来源。同号卡可按主机区分，同一主机同号卡的多次运行可按不同采集路径区分。设备号是 Prof 记录的编号，rank 单独显示。分析旧 xlsx 时，从原工作表表头恢复可用路径，其余未知字段显示为「未知」。
- 解读差值时引用该章节的表头。总体性能使用 `-d − -bp`。NPU / NPU 热点和单次执行同样使用 `-d − -bp`。NPU / GPU 热点和单次执行固定使用 NPU − GPU，表头随两侧角色变化。

### 综合报告中的 Host 算子、Device kernel Top 15 与单次执行

两份 Prof 默认走 msprof-analyze 的综合报告流程。两侧具备原始明细时，脚本在原 HTML 的「算子统计」和「Kernel 比对」章节中追加实测对照，并在中文 xlsx 中增加两张工作表。

```bash
python scripts/run_compare.py -d "<rank_0/..._ascend_pt>" -bp "<rank_0/...pt.trace.json>" -o "<报告目录>"
python scripts/run_compare.py -d "<待比对NPU/..._ascend_pt>" -bp "<基准NPU/..._ascend_pt>" -o "<报告目录>"
python scripts/run_compare.py -d "<ascend_pytorch_profiler_0.db>" -bp "<gpu.pt.trace.json>" -o "<报告目录>"
# 若用户给了界面导出的 kernel 排名 CSV，用它核对当前 rank 的分组、次数与耗时
python scripts/run_compare.py -d "<NPU目录>" -bp "<GPU trace>" --npu-top-csv "<operator_detail_group_by_Operator Type_....csv>" -o "<报告目录>"
```

每侧 NPU 可指定采集目录、单卡导出目录或 profiler 主库文件。目录中 DB 和文本并存时优先选主库；多个主库时指定目标 rank 的 DB。GPU 可指定 JSON、JSON.gz 或只含一个 rank 的 pt.trace 目录。

- **DB**：从 `PYTORCH_API` 的框架 op 读取 Host 次数、总耗时和输入形状，按嵌套区间计算 self。用 `CONNECTION_IDS` 关联实际下发任务。Device 读取 `TASK` 与计算、通信调度任务表；逻辑通信事件另进入执行明细。无需预先导出 CSV。
- **文本**：从 `kernel_details.csv` 和 `operator_details.csv` 读取明细，从 `trace_view.json` 关联调用路径。缺少 Host CSV 时可用 trace 中的框架事件计算 Host 指标；缺少 trace 时仍可生成 CSV 支持的原始明细报告。完整 msprof 工作表需要 trace 或 profiler 主库。
- **采集范围**：Host 与 Kernel 独立读取。缺少框架事件、张量、PMU 或关联证据时保留其他实测项。等待时间分别取 DB 的 `waitNs` 或 CSV 的 `Wait Time(us)`；不同导出器的等待时间、PMU 舍入精度和时间原点可能不同。
- NPU 对 NPU 时，综合报告和中文工作表用 DB 明细核对 `KernelCompare` 的通信事件、调度任务以及 `KernelTypeCompare` 的调度任务，避免将上游遗漏的次数算作性能变化。`--compare_only` 仍交付 msprof 原始 xlsx。

- 打开 `compare_analysis_report.html`，从侧栏进入 Host 框架算子、Device kernel Top 15 或单次执行比对。主 JSON 的 `host_kernel` 保存热点，`host_kernel.execution` 保存可供交互选择的执行序列。Top 15 以左侧 NPU 的累计 device 耗时排序，按 **(Type, Accelerator Core)** 分组。类型相同、核心不同的行分别排名。点击类型可进入单次明细。
- Device 表按名称、累计耗时、次数和均值成对排列两侧数据。后台用实际 kernel 名、host 标注、输入输出形状与 CUDA launch 的 correlation 处理异名和拆核。多个核心共用的形状不参与逐核心匹配。部分覆盖按已对齐子集计算差值，并标注左侧完整规模；待定位的对端数值留空。
- 两份 NPU 时按 `-d` 的 Top 15 排序，与 `-bp` 的对应类型及核心逐组对齐。差值统一为左侧减右侧，缺少对端数据时留空。
- NPU 对 GPU 的热点章节差值为 NPU − GPU，倍率为 NPU / GPU。比较总量时也说明调用次数差异。MoE 的主核与 epilogue 先分别比较，再看这两个阶段的合计；跨 stream 的 kernel 累计耗时与 step 墙钟时间分别解读。
- Host 以左侧 NPU 的 self 耗时排序，每项指标连续显示两侧的名称、total、self、次数和均值。已核实的 host 异名可用 `--host-aliases aliases.json` 传入，格式为 `{"左侧原名":"右侧原名"}`。

**单次执行比对**

1. 在 HTML 中依次选择算子类型、具体实现、执行序号。例如选择 `GroupedMatmulAdd`、`aclnnGroupedMatmulAddV2_GroupedMatmulAdd_GroupedMatmulAdd`、第 1 次。选择范围涵盖左侧具有可排序记录的实现，也可直接输入序号或逐次翻页。
2. 两端在各自的对应实现内，按 device 开始时间升序独立编号，从 1 起算。时间相同按源文件顺序。按相同序号并排，张量形状作为实测字段展示。某侧次数较少时，超出其长度的序号显示缺失。原始热点和执行序列均覆盖输入的完整采样窗口。
3. 两侧 NPU 优先按类型与实现名对齐，异名时结合核心及张量签名检查唯一候选。GPU 按可独立归属的完整序列编号。一次逻辑调用拆出多个 CUDA kernel 时，在同一序号下列出各核及累计耗时。仅能定位部分热点、无法确定完整序列时，保留左侧记录，对端标为“序列未定位”。
4. 明细共 47 项，包括执行信息、输入输出张量和 34 项 AIC/AIV 计数器。开始时间以各自报告标明的时间原点计算（ms），时长与等待以 μs 计，PMU time 以 ns 计。指定 DB 文件时读取该库的任务 PMU；混合目录可从同一任务的 CSV 补充缺项，纯文本读取 CSV。保留实测零值，区分未采集字段与 GPU 不适用的 Ascend PMU。
5. 桌面侧栏可收起，窄窗口从顶部打开目录。「逐字段对照」按行展示，「每项并列」让每个字段的各侧数值相邻，字段随正文宽度换行。

### 综合比对与 xlsx 分析

- **模式一（综合比对）**：要求总体性能、内存、通信、API 或完整 xlsx 时，使用 `--msprof` 运行原有 msprof-analyze 流程。默认命令也生成综合报告。`--compare_only` 仅生成 msprof 原始 xlsx。若透传 step 筛选，原始热点附表仍统计完整采样窗口，报告中标明两种范围。
- **模式二（xlsx 分析）**：用户已持有 compare 输出的 `performance_comparison_result_*.xlsx`，直接解析分析

分析流程针对每个 Sheet 页提炼关键数据并进行分析，最终生成：

1. **中间件 JSON**：包含所有 Sheet 的结构化分析结果
2. **HTML 报告**：可视化呈现性能比对分析结论（改善亮点/劣化风险双维度）
3. **中文 xlsx**：将原版英文表头翻译为中文的 xlsx 文件（多 Sheet 标签页 + 同步 CSV）

## 使用方法

### 模式一：直接比对两张 Prof（自动调用 compare）

```bash
# 最简用法：默认开启全部比对能力（msprof-analyze 未安装时自动从 PyPI 安装）
python scripts/run_compare.py --msprof -d <待比对prof路径> -bp <基准prof路径>

# 指定输出目录和业务名称
python scripts/run_compare.py -d ./ascend_pt -bp ./base_ascend_pt --base-label "旧版" --compare-label "新版" -o ./out

# 透传 compare 的额外参数（开关、step 过滤等）
python scripts/run_compare.py -d ./ascend_pt -bp ./gpu_trace.json --compare_args "--enable_operator_compare --use_input_shape"

# 只生成 compare xlsx，不生成分析报告
python scripts/run_compare.py -d ./ascend_pt -bp ./gpu_trace.json --compare_only
```

**参数说明**

| 参数 | 必选 | 说明 |
|---|---|---|
| `-d / --profiling_path` | 是 | 比对侧 Prof 路径 |
| `-bp / --benchmark_profiling_path` | 是 | 基线侧 Prof 路径 |
| `--base-label` / `--compare-label` | 否 | 写入分析报告的基线、比对业务名称 |
| `-o / --output` | 否 | 输出目录，默认 `./compare_output_<时间戳>` |
| `--compare_args` | 否 | 透传给 compare 的额外参数字符串，详见 `references/compare_quickstart.md` |
| `--host-kernel` | 否 | 显式要求原始热点对照并校验输入 |
| `--npu-top-csv` | 否 | 用导出榜单核对排名侧 Top 15 |
| `--host-aliases` | 否 | JSON 对象，键和值分别为排名侧和对照侧的 host 原名 |
| `--compare_only` | 否 | 只执行 compare 生成 xlsx，跳过后续分析 |
| `--skip_install` | 否 | 跳过 msprof-analyze 检测与自动安装 |

**msprof-analyze 安装策略**（`run_compare.py` 自动执行，按序尝试，任一成功即止）：

1. 已安装则直接使用（检测 `msprof-analyze` 命令或 `msprof_analyze` 包）
2. 未安装则在线安装：`pip install msprof-analyze`——msprof-analyze 官方发布于 PyPI（MindStudio 官方维护，Python ≥ 3.7，建议 3.9+），在线环境开箱即用

**Prof 数据要求**（详见 `references/compare_quickstart.md`）：

- NPU：指定到 `*_ascend_pt`（TorchNPU）或 `*_ascend_ms`（MindSpore）层级，目录内含 `ASCEND_PROFILER_OUTPUT/`（Text 或 Db 格式均可，同时存在时优先 Db）
- GPU：torch profiler 导出的目录（含 `*.pt.trace.json`）
- 建议只采集一个 step；多 step 时用 `--compare_args "--base_step=1 --comparison_step=1"` 固定

### 模式二：分析已有 compare 结果 xlsx

```bash
# 完整流程：解析 xlsx → 生成中间件 JSON → 生成 HTML 报告 → 生成中文 xlsx
python scripts/main_analyzer.py <xlsx_file_path> [-o <output_dir>]

# 示例
python scripts/main_analyzer.py ./performance_comparison_result_20250101.xlsx --base-label "旧版" --compare-label "新版" -o ./report_output
```

## 文件结构

```
prof-compare/
├── SKILL.md                          # Skill 说明文件
├── references/
│   ├── analysis_methodology.md       # 分析方法论（各 Sheet 指标含义与分析经验）
│   └── compare_quickstart.md         # compare 参数速查与 Prof 数据准备要求
└── scripts/
    ├── run_compare.py                # 直比编排：检测/安装 msprof-analyze → compare → 自动分析
    ├── main_analyzer.py              # 主调度脚本
    ├── html_generator.py             # HTML 报告生成器
    ├── npu_source.py                 # 定位单卡导出，读取 DB 框架事件和设备任务
    ├── kernel_execution.py           # 按类型与实现构建两端执行序列
    ├── execution_panel.html          # 单次执行交互表
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

## compare 常用参数速查（用于 --compare_args）

完整参数说明与数据准备要求见 `references/compare_quickstart.md`。

| 参数 | 用途 |
|---|---|
| （不设任何开关） | 默认开启全部比对能力（总体/算子/内存/通信/API/Kernel） |
| `--enable_profiling_compare` | 只看总体性能（计算/通信/调度/E2E 拆解） |
| `--enable_operator_compare` | 只看算子性能（较耗时，建议单 step 数据） |
| `--enable_communication_compare` | 只看通信性能 |
| `--enable_memory_compare` | 只看内存差异（较耗时） |
| `--enable_kernel_compare` | 只看 Kernel 比对（仅 NPU vs NPU） |
| `--enable_api_compare` | 只看 Host 侧 API 比对 |
| `--base_step=N --comparison_step=N` | 固定比对某个 step（需成对配置） |
| `--disable_details` | 隐藏明细 Sheet，只输出统计级比对（更快） |
| `--use_input_shape` | 算子按输入 Shape 精准匹配 |
| `--use_kernel_type` | 按类型和核心生成 KernelTypeCompare；DB 读取任务记录，文本读取 op_statistic.csv，缺少统计表时用 kernel_details.csv 临时汇总 |
| `--gpu_flow_cat=<id>` | GPU trace 的 CPU-kernel 连线标识（GPU Device Duration 全为 0 时使用） |
| `--force` | 跳过文件属主 / 大小校验，强制执行 |

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
- **分析方法**：按差异绝对值提取 Top 10。直比时核查 NPU 的算子分配记录（DB 的 `OP_MEMORY`、文本的 `operator_memory.csv`）；缺少记录时说明原因，保留原始工作表。

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
- 区分缺少内存分配记录与实测差异为零
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
