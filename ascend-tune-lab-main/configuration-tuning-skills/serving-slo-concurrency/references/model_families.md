# 模型族与 backend 映射

路径选择**不依赖参数量**（如 Qwen 27B 与 397B 同为 dense `ASCEND`，除非 HF 配置声明 MLA/sparse）。

## 决策优先级

1. `model_config.json` / profile 字段（`index_topk`、`kv_lora_rank`、`use_mla`、experts…）
2. 模型名 → family 默认（见下表）
3. 与 `platform.get_attn_backend_cls` 相同的三元组映射

```text
(use_mla, use_sparse, use_compress)
  (F, F, F) → ASCEND          # Qwen / Llama / 多数 dense
  (T, F, F) → ASCEND_MLA      # DeepSeek-V3 等
  (T, T, F) → ASCEND_SFA      # DeepSeek-V3.2 / GLM-5 / V4-flash
  (T, F, T) → ASCEND_DSA      # compress 路径
```

## Family 表

| family | 名称匹配 | use_mla | use_sparse | is_moe |
| --- | --- | --- | --- | --- |
| `qwen` | `qwen*`（非 MoE 标记） | F | F | F |
| `qwen_moe` | `*397*A*B*` / `*moe*` | F | F | T |
| `glm` | `glm-5*` / `glm5*` | T | T | T |
| `glm_mla` | 其他 `glm*` | T | F | T |
| `deepseek_sparse` | `*v3.2*` / `*v4-flash*` | T | T | T |
| `deepseek_mla` | 其他 `deepseek*` | T | F | T |
| `minimax` | `minimax*` | F | F | T |
| `llama` | `llama*` / `eagle*` | F | F | F |

GLM-5 的 LI/SFA 只是 **ASCEND_SFA** 路径上的算子示例，不是唯一支持的模型。
