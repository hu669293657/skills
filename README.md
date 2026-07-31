# Skills

个人技能集合，涵盖 vLLM-Ascend 性能调优、环境分析、配置提取等场景。

## 仓库结构

```
skills/
├── ascend-dump-analyzer/          # Ascend NPU 环境信息采集与分析
├── ascend-tune-lab-main/          # vLLM-Ascend 调优实验室（含子 Skills 和 Agents）
│   ├── configuration-tuning-skills/
│   └── configuration-tuning-agents/
├── cluster-analysis/              # Ascend 集群性能分析与比对
├── ibmc_analyzer/                 # iBMC 服务器日志分析与故障定位
└── vllm-ascend-tuning/            # vLLM-Ascend 全链路性能调优
```

## Skills 总览

### 1. ascend-dump-analyzer

**Ascend NPU 环境信息采集与分析工具**

采集 Ascend 服务器的环境信息（系统、驱动、CANN、环境变量、网络、权重等），生成结构化分析报告，支持多环境对比。

- **采集**：零依赖单文件脚本，可在任意 Ascend 服务器上运行
- **单环境分析**：系统健康检查、组件版本校验、关键环境变量检测、网络状态诊断
- **环境对比**：对比两个或多个 dump JSON，识别配置漂移
- **HTML 报告**：内置可视化模板，支持单环境报告和对比报告

### 2. ascend-tune-lab-main

**vLLM-Ascend 调优实验室，包含配置调优 Skills 和编排 Agents**

#### 子 Skills

| Skill | 描述 |
|-------|------|
| **ascend-baseline-generator** | 根据设备/模型/量化/NPU 数量从基线文档匹配最佳 vLLM-Ascend 部署配置 |
| **find-possible-parallel-strategy** | 根据模型参数量与量化类型，枚举合法的 DP×TP×EP 并行组合 |
| **serving-kv-cache-capacity** | 估算各并行组合下的 KV Cache 容量与内存上限最大并发 |
| **serving-slo-concurrency** | 基于 TTFT/TPOT SLO 约束估算最大实际并发，覆盖 Qwen/GLM/DeepSeek/MiniMax 等 |
| **serving-parallel-strategy-tuning** | 并行策略调优入口 Skill，串联上述三个子 Skill 产出推荐配置 |
| **serving-cfg-extract** | 从服务化日志启动阶段提取 non-default args 关键参数，生成 Excel 报告 |
| **serving-perf-metrics** | 从服务化日志运行阶段解析性能指标（吞吐、显存、命中率等），输出 CSV |
| **model-feature-extractor** | 将模型特性支持表 (xlsx) 转换为紧凑 JSON 格式 |
| **vllm-ascend-config-extractor** | 从 vLLM 和 vLLM-Ascend 源码中提取并对比配置开关定义 |

#### Agents

| Agent | 描述 |
|-------|------|
| **serving-perf-optimization** | 两阶段性能优化编排 Agent，Phase 1 基线复现 + Phase 2 调优，串联上述 Skills |

### 3. cluster-analysis

**Ascend 集群性能分析与比对工具**

面向昇腾 NPU 集群 profiling 数据的性能分析工具，支持从 `cluster_analysis_output` 目录提取数据，生成全景总结和 HTML 可视化报告。

- **数据格式**：支持 DB 模式（cluster.db）和 TEXT 模式（CSV + JSON），自动识别
- **全景提取**：解析 Step 时间、通信时间、通信带宽、通信矩阵等核心维度，生成 MD 总结文件
- **单集群分析**：计算时间分布、通信效率、Rank 均衡度，生成 HTML 分析报告
- **双集群比对**：对比正常与异常集群，识别性能差异根因（慢卡定位、通信瓶颈等）
- **HTML 报告**：内置单集群分析和双集群对比两套可视化模板

### 4. ibmc_analyzer

**iBMC 服务器日志分析与故障定位工具**

基于 iBMC 一键收集 (dump_info) 日志，进行故障根因定位、性能瓶颈排查和集群硬件配置一致性校验。

- **故障定位**：分析 MCE/PCIe 错误、内核 Panic，区分软件与硬件故障
- **性能排查**：检查 BIOS/OS 参数、降频诊断、温度异常分析
- **集群对比**：多节点配置 Diff，识别网卡固件、PCIe 带宽等木桶效应

### 5. vllm-ascend-tuning

**vLLM-Ascend 全链路性能调优技能**

提供从并行策略到模型推理的全链路优化工作流，包含 18 种主流模型的已验证 YAML 基准配置库。

- **覆盖模型**：DeepSeek-V3/V3.1/V3.2、Qwen3/3.5 全系列、GLM-4/5.1、Kimi-K2.5、MiniMax-M2.5 等
- **调优阶段**：并行策略 → 编译优化 → OS 调优 → torch_npu → CANN/HCCL → vLLM 参数 → Speculative Decoding → 量化 → PD 分离 → 基准测试
- **快速模板**：内置低延迟 (TPOT ~20ms) 和高吞吐 (TPOT ~50ms) 两套快速配置模板
- **参考文档**：包含 parallel_strategy、graph_mode、quantization、speculative_decoding 等 17 篇技术文档

## 同步方式

```bash
cd C:\Users\66929\Documents\skills

# 本地改动推送到远程
git add -A
git commit -m "描述改动"
git push

# 拉取远程更新到本地
git pull
```
