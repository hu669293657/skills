# Skills

个人技能集合，涵盖 vLLM-Ascend 性能调优、训练性能分析与集群比对、Host 侧瓶颈定位、环境/日志/硬件诊断等场景。

每个 skill 目录下均有 `SKILL.md`（Agent 执行定义）与 `README.md`（人类可读说明：应用场景、解决的问题、输入及获取方式、输出、流程图、示例提示词）。

## 仓库结构

```
skills/
├── ascend-dump-analyzer/          # Ascend NPU 环境信息采集与分析
├── ascend-field-issue-analyzer/   # 现网问题文本日志取证分析
├── cluster-analysis/              # 昇腾 NPU 训练性能一站式分析（含 vendor 子技能）
├── cluster-compare/               # 两集群 profiling 数据比对与劣化归因
├── cpu_host_performance/          # ftrace 采集数据 CPU Host 自动分析（中文报告）
├── cpu-trace-analyzer/            # Host 侧 CPU trace 瓶颈定位
├── host-bound-analyzer/           # CPU/Host Bound 采集与离线诊断（18 章节报告）
├── host-trace-diagnosis/          # Host 侧 trace 规则化智能诊断
├── ibmc_analyzer/                 # iBMC 服务器日志分析与故障定位
├── prof-compare/                  # 双 Prof 数据比对与中文报告生成
└── vllm-ascend-tuning/            # vLLM-Ascend 全链路性能调优
```

## Skills 总览

### 1. ascend-dump-analyzer

**Ascend NPU 环境信息采集与分析工具**（[README](ascend-dump-analyzer/README.md)）

采集 Ascend 服务器的环境信息（系统、驱动、CANN、环境变量、网络、权重等），生成结构化分析报告，支持多环境对比。

- **采集**：零依赖单文件脚本，可在任意 Ascend 服务器上运行
- **单环境分析**：系统健康检查、组件版本校验、关键环境变量检测、网络状态诊断
- **环境对比**：对比两个或多个 dump JSON，识别配置漂移
- **HTML 报告**：内置可视化模板，支持单环境报告和对比报告

### 2. ascend-field-issue-analyzer

**现网问题日志取证分析工具**（[README](ascend-field-issue-analyzer/README.md)）

从 plog/slog/host/device/framework 等现网日志中构建证据链，定位训练/推理中断、算子报错、环境异常的根因。

- **日志分诊**：按问题类型自动分诊并提取关键错误
- **证据链**：所有结论附日志原文与来源文件，杜绝臆测
- **可回验**：每条结论附回验命令
- **输出**：Markdown 分析报告（含时间线与处置建议）

### 3. cluster-analysis

**昇腾 NPU 训练性能一站式分析技能**（[README](cluster-analysis/README.md)）

面向单卡/多卡 Prof 数据的一站式性能分析与瓶颈定位，兼容 msprof-analyze 全部能力：原生 CLI 已安装则优先调用，未安装则使用内置 fallback 脚本（能力等价）。

- **输入自动识别**：单卡/多卡、DB/TEXT 格式、PyTorch/MindSpore 框架，缺失 `cluster_analysis_output` 时自动生成
- **流水线执行**：八阶段流水线（P0–P7），支持 `run_workflow.py` 一键编排与端到端验收
- **进阶分析**：五大类 23 正式 + 1 扩展 recipe 全量执行，含 Advisor 诊断与能力矩阵
- **对比能力**：泳道算子统计与跨卡比对、通信占比最大/最小卡对比（经 vendor_bridge 调用 vendor 子技能）
- **HTML 报告**：左侧固定导航、分区可折叠、vendor 子报告 iframe 内嵌的完整报告

### 4. cluster-compare

**昇腾集群比对分析技能**（[README](cluster-compare/README.md)）

专注比对两个集群的 profiling 数据（DB 或 TEXT 格式），定位性能差异并输出劣化归因 HTML 报告。

- **数据准备**：无现成 `cluster_analysis_output` 时，自动安装并调用 `msprof-analyze cluster` 生成
- **比对维度**：Step 耗时拆解、通信算子耗时、通信带宽、通信矩阵
- **输出**：劣化归因 HTML 对比报告

### 5. prof-compare

**双 Prof 数据比对与中文报告生成技能**（[README](prof-compare/README.md)）

比对两个 Prof 数据集（GPU vs NPU / NPU vs NPU），自动检测并安装 msprof-analyze，支持直比与 xlsx 解析两种工作模式。

- **模式一（直比）**：提供两个 Prof 路径，自动调用 compare 生成比对 xlsx 后进入分析
- **模式二（xlsx 分析）**：直接解析已有的 `performance_comparison_result_*.xlsx`
- **多格式输出**：JSON（结构化）+ HTML 报告（改善亮点/劣化风险）+ 中文 xlsx 与 CSV

### 6. cpu_host_performance

**Linux ftrace CPU Host 性能自动化分析**（[README](cpu_host_performance/README.md)）

解析 ftrace/trace 采集数据，自动计算调度延迟、IRQ/SoftIRQ、CPU 利用率/imbalance、频率等指标，输出中文结论与单文件离线 HTML 报告（inline SVG，无 CDN/外部依赖）。

- **输入零转换**：自动识别 tar.gz / zip / 目录 / 裸 ftrace 文本 / trace-cmd report / Chrome Tracing JSON，精简与全量模式均可分析
- **组合判定**：拒绝单指标下结论，CPU_SATURATION / SCHEDULER_CONTENTION / CPU_IMBALANCE 等问题均需多指标交叉验证
- **中文报告**：每个指标附中文释义（取自 `analyzer/metrics.py` 的 `METRIC_EXPLANATIONS`），未知事件按兜底释义标注
- **现场采集**：`scripts/cpu_trace_collect.sh` 客户侧一键采集，仅使用 tracefs/debugfs、/proc、/sys，采集后保存并恢复 ftrace 配置
- **运行**：`python3 -m cpu_host_performance analyze <输入路径> --output-dir <输出目录>`

### 7. cpu-trace-analyzer

**Host 侧 CPU trace 瓶颈定位工具**（[README](cpu-trace-analyzer/README.md)）

以"Gap 驱动反向溯源"方式定位 NPU/GPU 训练场景下的 Host 侧性能瓶颈。

- **Gap 扫描**：自动发现计算/通信空隙并按影响排序
- **根因归因**：CPU 调度、DataLoader 竞争、H2D 拷贝阻塞、kernel launch 间隙
- **多格式支持**：Chrome JSON / ftrace / msprof / perf / Perfetto 五种 trace 格式统一解析，内置完整 Python 包（CLI 一键运行）
- **输出**：JSON 诊断结果 + HTML 诊断报告

### 8. host-trace-diagnosis

**Host 侧 trace 规则化智能诊断工具**（[README](host-trace-diagnosis/README.md)）

对 perfetto/ftrace/msprof/perf trace 进行规则库驱动的逐条匹配诊断。

- **多格式支持**：perfetto / ftrace / msprof / perf 统一解析
- **规则可扩展**：内置 7 个 YAML 规则文件（CPU / Host NPU / IO / 内存 / NUMA / 运行时 / 调度），可自定义扩展
- **结论可复核**：命中结果关联规则 ID 与证据事件

### 9. host-bound-analyzer

**CPU/Host Bound 性能瓶颈离线诊断技能**（[README](host-bound-analyzer/README.md)）

对深度学习训练/推理进程做"宿侧（CPU/Host）是否拖慢加速器"的证据化诊断，覆盖线程过订阅、OMP 线程过多、NUMA 远端访问、iowait/磁盘饱和、内存回收、DataLoader 供数不足等场景。

- **三种使用路径**：解析采集包（tar.gz / 采集目录）、Linux 训练机现场采集诊断（L1/L2 只读，L3 perf 采样须显式授权）、单文件 standalone 脚本交付
- **规则引擎**：9 个 YAML 规则文件驱动，缺数据的规则自动 SKIP 绝不虚构；加权评分输出根因树与 P0–P3 建议
- **HTML 报告**：固定 18 章节，零 JS、零外部资源、内嵌 SVG，离线可看
- **安全合规**：分析只读、默认脱敏（`--sanitize`），结论均挂证据（metric/value/来源文件行号可回溯）
- **质量保障**：内置冒烟与金标用例测试，仅依赖 Python ≥3.6 标准库

### 10. ibmc_analyzer

**iBMC 服务器日志分析与故障定位工具**（[README](ibmc_analyzer/README.md)）

基于 iBMC 一键收集 (dump_info) 日志，进行故障根因定位、性能瓶颈排查和集群硬件配置一致性校验。

- **故障定位**：分析 MCE/PCIe 错误、内核 Panic，区分软件与硬件故障
- **性能排查**：检查 BIOS/OS 参数、降频诊断、温度异常分析
- **集群对比**：多节点配置 Diff，识别网卡固件、PCIe 带宽等木桶效应
- **输出**：结构化 JSON + HTML 诊断报告（单机深度分析 + 多机集群对比）

### 11. vllm-ascend-tuning

**vLLM-Ascend 全链路性能调优技能**（[README](vllm-ascend-tuning/README.md)）

提供从并行策略到模型推理的全链路优化工作流（Phase 0–11），包含主流模型的已验证 YAML 基准配置库。

- **覆盖模型**：DeepSeek-V3/V3.1/V3.2、Qwen3/3.5 全系列、GLM-4/5.1、Kimi-K2.5、MiniMax-M2.5 等
- **调优阶段**：并行策略 → 编译优化 → OS 调优 → torch_npu → CANN/HCCL → vLLM 参数 → Speculative Decoding → 量化 → PD 分离 → 基准测试
- **快速模板**：内置低延迟 (TPOT ~20ms) 和高吞吐 (TPOT ~50ms) 两套快速配置模板
- **参考文档**：包含 parallel_strategy、graph_mode、quantization、speculative_decoding 等 17 篇技术文档

## 技能选型速查

| 你想做什么 | 推荐技能 |
|-----------|---------|
| 体检/比对服务器环境 | ascend-dump-analyzer |
| 从现网日志定位问题根因 | ascend-field-issue-analyzer |
| 训练性能一站式分析、找慢卡 | cluster-analysis |
| 比对两个集群（正常 vs 异常） | cluster-compare |
| 对比两组 Prof 数据（GPU vs NPU / 调优前后） | prof-compare |
| ftrace/trace 采集数据自动出中文诊断报告（MD/HTML） | cpu_host_performance |
| 定位 Host 侧瓶颈（Gap 归因） | cpu-trace-analyzer |
| trace 规则化健康检查 | host-trace-diagnosis |
| 采集并诊断 CPU/Host Bound（训练慢、供数不足） | host-bound-analyzer |
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
