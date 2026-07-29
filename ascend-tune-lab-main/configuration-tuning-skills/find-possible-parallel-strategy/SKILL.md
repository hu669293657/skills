---
name: find-possible-parallel-strategy
description: >-
  Enumerates legal DP×TP×EP parallel combinations for vLLM-Ascend single-node
  mixed deployment from model weight size and NPU count. Use when computing
  min TP, listing parallel strategy candidates, or as sub-skill of
  serving-parallel-strategy-tuning.
---

# find-possible-parallel-strategy

单机混部场景下，根据模型参数量与量化类型估算权重大小，计算最小 TP，并枚举合法 `DP×TP×EP` 组合。

支持多模型族（参数量表 + 名称 `\d+B` 回退）：Qwen3.5（多档参数量）、GLM、DeepSeek、MiniMax 等。

## 算法（单机混部）

1. 量化字节：`BF16=2`，`W8A8=1`，`W4A8=0.5`
2. 参数量：优先 `references/model_params_b.json`；否则 HF `model_config.json` 估算或名称中的 `\d+B`
3. `weight_gb = params_b × quant_bytes`
4. `min_tp = ceil_pow2(weight_gb / 57.6)`，其中 `57.6 = 64×0.95−3`（文档口径）
5. 读取物理卡数 `NPU卡数`（与 baseline「总卡数」同口径）及 `设备类型`
6. 拓扑换算：`world_size = num_cards × dies_per_card`
   - **A3**：每卡 2 die（`ASCEND_RT_VISIBLE_DEVICES` 成对），`dies_per_card=2`
   - **A2** / 未知：`dies_per_card=1`
7. `DP = world_size / TP`，`EP = DP × TP`（合法：`TP` 为 ≥`min_tp` 的 2 幂次且整除 `world_size`）

**示例（A3 · 1 卡）**：`world_size=2` → 合法组合含 `DP1TP2EP2`、`DP2TP1EP2`（与 baseline `DP1+TP2` 一致）。

**公式说明**：`ceil_pow2(284/57.6)≈ceil_pow2(4.93)=8`。口头示例若按 `Ceiling(284/57.6)=4` 再列 DP4TP4，与本实现不同；**以本公式为准**。

## 运行

```bash
python scripts/find_parallel_strategies.py \
  --config /path/to/deploy-config.md \
  --model-config /path/to/model_config.json \
  --out /path/to/tuning/parallel-strategies.json \
  --json
```

或显式参数：

```bash
python scripts/find_parallel_strategies.py \
  --model DeepSeek-V4-flash --quantization w8a8 --num-npus 16 \
  --json
```

## 输出

`parallel-strategies.json`：`profile`、`min_tp`、`num_cards` / `dies_per_card` / `world_size`、`combinations[]`（`dp/tp/ep/label/weight_per_npu_gb`）、`current`（baseline DP/TP）。

## 约束

- 仅单机混部；不处理双机 / PD 分离。
- `NPU卡数` 必须是物理卡数（总卡数），**不要**填 die/device 数；A3 由本 skill 自动 ×2。
- 由入口 skill `serving-parallel-strategy-tuning` 调用；也可独立运行。
