---
name: serving-cfg-extract
description: "从服务化日志启动阶段提取 non-default args 关键参数（模型路径、量化、并行策略、配置、特性开关等），生成 Excel 报告。"
---

# Serving Config Extract (服务化启动配置提取) 🦞

从服务化（vLLM）日志的启动阶段，提取 `non-default args:` 行的关键参数，
按分类生成结构化的 Excel 文件。

**与 `serving-perf-metrics` 的区别：**
- 本脚本只在**启动阶段**提取一次配置参数（如模型路径、并行策略等）
- `serving-perf-metrics` 提取的是**运行测试阶段**的性能指标（吞吐、显存等）

## 提取的参数分类

| 分类 | 关注的字段 |
|------|-----------|
| **模型路径** | `model` |
| **量化方式** | `quantization` |
| **并行策略** | `tensor_parallel_size`, `enable_expert_parallel`, `data_parallel_size`, `pipeline_parallel_size` |
| **配置参数** | `max_model_len`, `max_num_batched_tokens`, `max_num_seqs` |
| **特性开关** | 所有 `enable_*` / `fuse_*` / `cudagraph_*` / `multistream_*` / `recompute_*` 开头;<br>`speculative_config`, `num_spec_tokens`, `eagle3` 相关 |

## 使用方式

```bash
cd ~/Desktop/serving-cfg-extract
python3 scripts/extract_log_params.py <日志文件路径>
```

输出：Excel 文件到桌面 `<日志文件名>_non_default_args.xlsx`

## 环境要求

```bash
pip3 install openpyxl
```
