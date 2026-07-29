# 使用示例

## 示例 1: 格式 A (独立行) - 传统服务化日志

```bash
$ cd ~/Desktop/serving-perf-metrics
$ python3 scripts/parse_log.py /var/log/serving.log

📋 检测到日志格式: 格式A (独立行)
✅ CSV 已生成: serving_metrics.csv
   共 3 条记录
```

## 示例 2: 格式 B (聚合行) - vLLM Prefill 基准日志

```bash
$ python3 scripts/parse_log.py /path/to/vllm_prefill.log

📋 检测到日志格式: 格式B (vLLM 聚合行)
✅ CSV 已生成: vllm_prefill_metrics.csv
   共 7 条记录
```

## 示例 3: 指定输出路径

```bash
$ python3 scripts/parse_log.py /var/log/serving.log /tmp/results.csv
```

## 常见日志片段

**格式 A** 示例：
```
Loading model weights took 27.0601 GB
Available KV cache memory: 26.48 GiB
GPU KV cache usage: 10.9%
Prefix cache hit rate: 0.2%
Avg generation throughput: 272.6 tokens/s
```

**格式 B** 示例：
```
1 Engines Aggregated: Avg prompt throughput: 10393.9 tokens/s, Avg generation throughput: 272.6 tokens/s, Running: 96 reqs, Waiting: 0 reqs, GPU KV cache usage: 10.9%, Prefix cache hit rate: 0.2%
```
