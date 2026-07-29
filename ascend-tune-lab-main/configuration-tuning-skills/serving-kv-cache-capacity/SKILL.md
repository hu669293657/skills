---
name: serving-kv-cache-capacity
description: >-
  Estimates available KV cache capacity and memory-bound max concurrency for
  each DP×TP×EP parallel combination under vLLM-Ascend. Use after
  find-possible-parallel-strategy or when evaluating KV headroom vs context length.
---

# serving-kv-cache-capacity

对 `parallel-strategies.json` 中每个并行组合，估算可用 KV Cache 容量与内存上限下的最大并发。

## 理论公式

```text
kv_bytes_per_token = 2 × layers × kv_heads × head_dim × kv_dtype_bytes
  （MLA 模型优先 references/memory_defaults.json 覆盖表）

workspace_gb = workspace_gb_base + context_k × per_1k
budget_per_npu = npu_hbm − weight_per_npu − workspace − hccl − system_reserved
available_kv_gb = max(0, budget_per_npu) × TP
per_request_kv_gb = (input + output) × kv_bytes_per_token / 1e9
max_concurrency_memory = floor(available_kv / per_request_kv) × DP
```

默认：`npu_hbm=64`，`system_reserved=3`，`hccl_buffsize=512MB`。

## 运行

```bash
python scripts/evaluate_kv_capacity.py \
  --parallel-json /path/to/tuning/parallel-strategies.json \
  --out /path/to/tuning/kv-capacity.json \
  --json
```

可选：`--log-file` 解析 `Available KV cache memory:` 做校准标注（不覆盖理论值）。

## 输出

`kv-capacity.json`：每个组合的 `available_kv_gb`、`per_request_kv_gb`、`max_concurrency_memory`。

## 约束

- 本轮以理论计算为主；不强制依赖线上日志。
- 由 `serving-parallel-strategy-tuning` 在子 skill 1 之后调用。
