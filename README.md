# Skills

个人技能集合，涵盖 vLLM-Ascend 性能调优、Prof 性能分析与比对、现网故障取证、Host 侧瓶颈定位、环境/硬件诊断等场景。

每个 skill 目录下均有 `SKILL.md`（Agent 执行定义，frontmatter 的 `description` 决定路由）与 `README.md`（人类可读说明：应用场景、解决的问题、输入及获取方式、输出、流程图、示例提示词）。

## 仓库结构

```
skills/
├── ascend-dump-analyzer/     # 昇腾环境信息采集与分析（环境体检/配置漂移）
├── issue-analyzer/           # 现网故障日志取证分析（报错/中断，非性能问题）
├── cluster-analysis/         # 单份 Prof 性能一站式分析（含 vendor 子技能）
├── cluster-compare/          # 两集群/多卡 Prof 比对与劣化归因
├── prof-compare/             # 两份单卡 Prof 细粒度比对（中文报告）
├── cpu-host-performance/     # HostBound 定位（ftrace 采集 + 证据化诊断）
├── ibmc_analyzer/            # iBMC 服务器硬件健康分析
└── vllm-ascend-tuning/       # vLLM-Ascend 全链路性能调优
```

## 路由决策（先分类，再选技能）

1. **性能问题还是故障问题？**
   - 报错、中断、拉起失败、hang 死等故障类问题 → `issue-analyzer`（日志取证）
   - 性能慢、吞吐低、瓶颈定位 → 进入第 2 步
2. **性能分析有几份数据？**
   - 一份 Prof 数据 → `cluster-analysis`
   - 两份数据对比 → 先判断规模：
     - 多卡/集群级 → `cluster-compare`
     - 单卡/单机 → `prof-compare`
3. **Prof 分析怀疑点不在算子层？**
   - 怀疑 Host/CPU 拖慢 NPU/GPU → `cpu-host-performance`
   - 硬件层（BMC/电源/温度/ECC） → `ibmc_analyzer`
   - 环境配置漂移 → `ascend-dump-analyzer`
4. **推理服务调优**（并行策略/编译/量化/基准测试） → `vllm-ascend-tuning`

## Skills 总览

### 1. ascend-dump-analyzer

**Ascend NPU 环境信息采集与分析工具**（[README](ascend-dump-analyzer/README.md)）

采集 Ascend 服务器的环境信息（系统、驱动、CANN、环境变量、网络、权重等），生成结构化分析报告，支持多环境对比。

- **采集**：零依赖单文件脚本，可在任意 Ascend 服务器上运行
- **单环境分析**：系统健康检查、组件版本校验、关键环境变量检测、网络状态诊断
- **环境对比**：对比两个或多个 dump JSON，识别配置漂移
- **HTML 报告**：内置可视化模板，支持单环境报告和对比报告

### 2. issue-analyzer

**现网问题日志取证分析工具**（[README](issue-analyzer/README.md)）

从 plog/slog/host/device/framework 等现网日志中构建证据链，定位训练/推理中断、算子报错、环境异常的根因（故障类问题，非性能分析）。

- **日志分诊**：按问题类型自动分诊并提取关键错误
- **证据链**：所有结论附日志原文与来源文件，杜绝臆测
- **可回验**：每条结论附回验命令
- **输出**：Markdown 分析报告（含时间线与处置建议）

### 3. cluster-analysis

**昇腾 NPU 训练性能一站式分析技能**（[README](cluster-analysis/README.md)）

面向单份 Prof 数据（单卡或多卡）的一站式性能分析与瓶颈定位，兼容 msprof-analyze 全部能力：原生 CLI 已安装则优先调用，未安装则使用内置 fallback 脚本（能力等价）。

- **输入自动识别**：单卡/多卡、DB/TEXT 格式、PyTorch/MindSpore 框架，缺失 `cluster_analysis_output` 时自动生成
- **流水线执行**：八阶段流水线（P0–P7），支持 `run_workflow.py` 一键编排与端到端验收
- **进阶分析**：五大类 23 正式 + 1 扩展 recipe 全量执行，含 Advisor 诊断与能力矩阵
- **对比能力**：泳道算子统计与跨卡比对、通信占比最大/最小卡对比（经 vendor_bridge 调用 vendor 子技能）
- **HTML 报告**：左侧固定导航、分区可折叠、vendor 子报告 iframe 内嵌的完整报告

### 4. cluster-compare

**昇腾集群比对分析技能**（[README](cluster-compare/README.md)）

专注比对两个集群（多卡）的 profiling 数据（DB 或 TEXT 格式），定位性能差异并输出劣化归因 HTML 报告。

- **数据准备**：无现成 `cluster_analysis_output` 时，自动安装并调用 `msprof-analyze cluster` 生成
- **比对维度**：Step 耗时拆解、通信算子耗时、通信带宽、通信矩阵
- **输出**：劣化归因 HTML/MD 对比报告

### 5. prof-compare

**双 Prof 数据比对与中文报告生成技能**（[README](prof-compare/README.md)）

比对两份单卡 Prof 数据集（GPU vs NPU / NPU vs NPU / 调优前后），自动检测并安装 msprof-analyze，支持直比与 xlsx 解析两种工作模式。

- **模式一（直比）**：提供两个 Prof 路径，自动调用 compare 生成比对 xlsx 后进入分析
- **模式二（xlsx 分析）**：直接解析已有的 `performance_comparison_result_*.xlsx`
- **多格式输出**：JSON（结构化）+ HTML 报告（改善亮点/劣化风险）+ 中文 xlsx 与 CSV

### 6. cpu-host-performance

**Linux ftrace CPU Host 性能自动化分析**（[README](cpu-host-performance/README.md)）

面向训练/推理场景的 Host 侧 CPU 根因自动化分析：解析 ftrace/trace-cmd 采集数据，自动计算调度延迟、IRQ/SoftIRQ、CPU 利用率/频率等指标，输出全中文诊断报告（Markdown + 离线 HTML）。

- **多格式健壮解析**：tar.gz / zip / 目录 / 裸 ftrace 文本 / trace-cmd report / Chrome Tracing / Perfetto JSON
- **组合式诊断**：拒绝"单指标定罪"，10 类具体诊断 + 证据不足兜底，每条结论带置信度
- **现场采集**：`scripts/cpu_trace_collect.sh` 客户侧一键采集，不联网、不装包、结束自动恢复 tracing 配置
- **运行**：`python3 -m analyzer analyze <输入路径> --output-dir <输出目录>`（在 skill 根目录）

### 7. ibmc_analyzer

**iBMC 服务器日志分析与故障定位工具**（[README](ibmc_analyzer/README.md)）

基于 iBMC 一键收集 (dump_info) 日志，进行故障根因定位、性能瓶颈排查和集群硬件配置一致性校验。

- **故障定位**：分析 MCE/PCIe 错误、内核 Panic，区分软件与硬件故障
- **性能排查**：检查 BIOS/OS 参数、降频诊断、温度异常分析
- **集群对比**：多节点配置 Diff，识别网卡固件、PCIe 带宽等木桶效应
- **输出**：结构化 JSON + HTML 诊断报告（单机深度分析 + 多机集群对比）

### 8. vllm-ascend-tuning

**vLLM-Ascend 全链路性能调优技能**（[README](vllm-ascend-tuning/README.md)）

提供从并行策略到模型推理的全链路优化工作流（Phase 0–11），包含主流模型的已验证 YAML 基准配置库。

- **覆盖模型**：DeepSeek-V3/V3.1/V3.2、Qwen3/3.5 全系列、GLM-4/5.1、Kimi-K2.5、MiniMax-M2.5 等
- **调优阶段**：并行策略 → 编译优化 → OS 调优 → torch_npu → CANN/HCCL → vLLM 参数 → Speculative Decoding → 量化 → PD 分离 → 基准测试
- **快速模板**：内置低延迟 (TPOT ~20ms) 和高吞吐 (TPOT ~50ms) 两套快速配置模板
- **参考文档**：包含 parallel_strategy、graph_mode、quantization、speculative_decoding 等 17 篇技术文档

## 技能选型速查

| 你想做什么 | 推荐技能 |
|-----------|---------|
| 体检/比对服务器环境、排查配置漂移 | ascend-dump-analyzer |
| 现网报错/中断/拉起失败等故障排查（非性能） | issue-analyzer |
| 分析一份 Prof 数据的性能/瓶颈/慢卡 | cluster-analysis |
| 对比两个集群或多卡 Prof 数据 | cluster-compare |
| 对比两份单卡 Prof（GPU vs NPU / 调优前后） | prof-compare |
| 怀疑 Host/CPU 拖慢 NPU/GPU（ftrace 采集与诊断） | cpu-host-performance |
| 判断服务器/BMC 硬件健康 | ibmc_analyzer |
| 端到端 vLLM-Ascend 调优 | vllm-ascend-tuning |

## 同步方式

```powershell
cd D:\skills

# 本地改动推送到远程
git add -A
git commit -m "描述改动"
git push

# 拉取远程更新到本地
git pull
```
