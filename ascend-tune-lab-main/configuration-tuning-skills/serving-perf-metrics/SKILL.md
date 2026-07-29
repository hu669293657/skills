---
name: serving-perf-metrics
description: "从服务化日志运行阶段解析性能指标（权重容量、显存、吞吐、命中率等），自动识别日志格式，按指标为行输出 CSV。"
---

# Serving Perf Metrics (服务化运行性能指标) 🦞

从大模型服务化（vLLM Serving）日志的**运行测试阶段**中解析性能指标，
**自动检测日志格式**，按 **指标为行、值为列** 输出 CSV。

**与 `serving-cfg-extract` 的区别：**
- 本脚本提取的是**运行测试阶段**的性能指标（吞吐、显存、命中率等）
- `serving-cfg-extract` 提取的是**启动阶段**的配置参数（模型路径、并行策略等）

## CSV 输出格式

```
序号,指标名称,值1,值2,值3,值4,值5,值6,值7
1,权重占用容量,24.5860 GB,27.0601 GB
2,可分配token显存容量,26.48 GiB
3,kv cache显存利用率,10.9%,11.0%,11.5%,12.1%,8.7%,0.0%,0.0%
4,prefix cache命中率,0.2%,0.2%,0.2%,0.2%,0.2%,0.2%,0.2%
5,MTP命中率,79.6%,85.0%
6,平均接受长度,1.79,2.1
7,平均输出吞吐,272.6 tokens/s,287.9 tokens/s,271.0 tokens/s,285.8 tokens/s,291.4 tokens/s,13.4 tokens/s,0.0 tokens/s
8,草稿模型生成吞吐,3.28 tokens/s,4.0 tokens/s
9,草稿模型有效生成吞吐,4.12 tokens/s,5.0 tokens/s
10,接受的tokens,1420 tokens,2000 tokens
11,草稿模型生成tokens,1420 tokens,1800 tokens
```

- 每行 = 一个指标（按 1~11 固定顺序）
- 各列 = 该指标在日志中出现的**所有值**（按出现顺序）
- 没有的值留空
- CSV 编码 UTF-8 with BOM，Excel/WPS 可直接打开

## 提取的 11 项指标

| # | 中文名称 | 日志匹配关键字 |
|---|---------|--------------|
| 1 | 权重占用容量 | `Loading model weights took ... GB` |
| 2 | 可分配token显存容量 | `Available KV cache memory: ... GiB` |
| 3 | kv cache显存利用率 | `GPU KV cache usage: ...%` |
| 4 | prefix cache命中率 | `Prefix cache hit rate: ...%` |
| 5 | MTP命中率 | `Avg Draft acceptance rate: ...%` |
| 6 | 平均接受长度 | `Mean acceptance length:...` |
| 7 | 平均输出吞吐 | `Avg generation throughput: ... tokens/s` |
| 8 | 草稿模型生成吞吐 | `Accepted throughput: ... tokens/s` |
| 9 | 草稿模型有效生成吞吐 | `Drafted throughput: ... tokens/s` |
| 10 | 接受的tokens | `Accepted:... tokens` |
| 11 | 草稿模型生成tokens | `Drafted:... tokens` |

## 支持的日志格式

| 格式 | 说明 | 检测条件 |
|------|------|----------|
| **格式A** 独立行 | 各指标分散在不同行 | 有权重加载，无聚合行 |
| **格式B** vLLM聚合行 | APIServer 一行包含多指标 | 存在 `Engines Aggregated: ...` 行 |

## 使用方式

```bash
cd ~/Desktop/serving-perf-metrics
python3 scripts/parse_log.py 服务化日志.log
python3 scripts/parse_log.py /path/to/serving.log 结果.csv
```

未指定输出路径时，默认输出到 `<日志文件名>_metrics.csv`。

## 依赖

- Python 3.6+（内置模块，无需额外安装）
