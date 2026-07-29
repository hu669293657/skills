---
name: "model-feature-extractor"
description: "Reads model feature support table (xlsx) and converts to compact JSON. Invoke when user needs to parse or convert model-feature xlsx to JSON, or query model feature support across versions."
---

# Model Feature Extractor

This skill reads a model feature support table (`.xlsx`) and converts it into compact JSON files for agent consumption. Each model is stored in a separate JSON file, with shared definitions in `common.json`.

## When to Invoke

- User asks to convert a model feature support xlsx to JSON
- User asks to query which features a model supports in a specific version
- User provides an xlsx file containing model feature compatibility data

## Input

The user must provide the **absolute path** to the `.xlsx` file. Example:
```
C:\Users\lan\Doc\模型特性支持表.xlsx
```

## Expected XLSX Structure

The xlsx must follow this layout:

| Row | Content |
|-----|---------|
| Row 1 | Category headers (merged cells for groups like 通用优化, attention, 图模式, etc.) |
| Row 2 | Feature sub-headers (one per column, e.g. 异步调度, 绑核, prefix-cache, ...) |
| Row 3+ | Data rows with model name, param type, quant type, version, then feature values |

**Columns A-D**: Model metadata (模型名称, 参数类型, 量化类型, 版本号)
**Columns E+**: Feature values

**Cell value conventions:**
- `√` → fully supported
- `×` → not supported by model architecture
- `-` → not applicable / not tested
- `√xxx` → supported with condition (e.g. `√未入图`, `√结合mtp报错`)

**Merged cells**: Model name, param type, and quant type may span multiple rows (merged vertically) for different versions of the same model variant.

## Output File Structure

The script produces multiple JSON files in `.trae/skills/model-feature-extractor/output/`:

```
output/
├── common.json                     ← Shared definitions (features + categories)
├── model_feature_qwen3_5.json      ← Per-model feature data
├── model_feature_deepseek_v3.json   ← Per-model feature data
└── ...
```

### `common.json` — Shared Definitions

Contains feature key-to-name mapping and category grouping, shared across all models:

```json
{
  "features": {
    "vllm_ascend_balance_scheduling": "异步调度",
    "enable_cpu_binding": "绑核",
    ...
  },
  "categories": {
    "通用优化": ["vllm_ascend_balance_scheduling", "enable_cpu_binding", "vllm_ascend_enable_nz"],
    "attention": ["enable_prefix_caching", "enable_chunked_prefill", "vllm_ascend_enable_mlapo"],
    ...
  }
}
```

### Per-model files — Feature Data

Each model gets its own JSON file with the `model_feature_` prefix. The filename is `model_feature_{normalized_name}.json`, where the model name is normalized by replacing `.` → `_`, `/` → `_`, converting to lowercase. The content structure:

```json
{
  "<参数规格>": {
    "<量化类型>": {
      "<版本号>": {
        "<config_name>": <value>,
        ...
      },
      ...
    },
    ...
  },
  ...
}
```

Example (`model_feature_qwen3_5.json`):

```json
{
  "397B/122B/35B": {
    "bf16/W8A8/W4A8": {
      "0.17.0rc1": { "speculative_config.method=mtp": "未入图" },
      "0.18.0rc1": { "speculative_config.method=mtp": "未入图" },
      "0.18.0":    { "speculative_config.method=mtp": "未入图" },
      "0.19.1rc1": { "speculative_config.method=mtp": "结合prefix-cache报错" },
      "0.20.2rc1": { "speculative_config.method=mtp": 1 }
    }
  }
}
```

### Value Encoding

| Original | JSON Value | Meaning |
|----------|-----------|---------|
| `√` | `1` | Fully supported |
| `×` | `0` | Not supported (architecture limitation) |
| `-` | _(omitted)_ | Not applicable, key is absent from the object |
| `√xxx` | `"xxx"` | Supported with condition described in string |

**Key design principle**: Omit `-` entries entirely to minimize JSON size. An absent key means "not applicable".

## Output File Naming Convention

- **Language**: English only (no Chinese characters in filenames)
- **Style**: `snake_case`
- **Common file**: Always `common.json`
- **Model files**: `model_feature_{model_name_normalized}.json`, where the model name is normalized by replacing `.` → `_`, `/` → `_`, spaces → `_`, and converting to lowercase
  - `qwen3.5` → `model_feature_qwen3_5.json`
  - `DeepSeek-V3` → `model_feature_deepseek-v3.json`
  - `GLM-4.9V` → `model_feature_glm-4_9v.json`
- **No version suffix**: The model feature support table is a **cross-version reference** — it contains feature data across multiple vllm-ascend versions. Version information lives inside the JSON data (per-model, per-version entries), not in the filename.

## Execution Steps

1. **Run the extraction script** located at `.trae/skills/model-feature-extractor/script/extract_model_features.py`
2. Pass the xlsx path as the argument:
   ```
   python .trae/skills/model-feature-extractor/script/extract_model_features.py "<xlsx_path>"
   ```
   Example:
   ```
   python .trae/skills/model-feature-extractor/script/extract_model_features.py "C:\Users\lan\Doc\模型特性支持表.xlsx"
   ```
3. The script writes to `.trae/skills/model-feature-extractor/output/`:
   - `common.json` — shared definitions
   - `model_feature_{model_name}.json` — one file per model
4. **Report** the output paths and file sizes to the user

## Feature Key Mapping

Feature keys use **config_name** from vllm / vllm-ascend to stay consistent with the config extractor output:

| config_name | Chinese Name | Category |
|-------------|-------------|----------|
| `vllm_ascend_balance_scheduling` | 异步调度 | 通用优化 |
| `enable_cpu_binding` | 绑核 | 通用优化 |
| `vllm_ascend_enable_nz` | weight_nz | 通用优化 |
| `enable_prefix_caching` | prefix-cache | attention |
| `enable_chunked_prefill` | chunked-prefill | attention |
| `vllm_ascend_enable_mlapo` | MLAPO | attention |
| `compilation_config.cudagraph_mode=PIECEWISE` | PIECEWISE | 图模式 |
| `compilation_config.cudagraph_mode=FULL_DECODE_ONLY` | FULL_DECODE_ONLY | 图模式 |
| `compilation_config.cudagraph_mode=FULL` | FULL | 图模式 |
| `speculative_config.method=mtp` | mtp | 投机推理 |
| `speculative_config.method=eagle3` | eagle3 | 投机推理 |
| `prefill_context_parallel_size` | pcp | CP并行 |
| `decode_context_parallel_size` | dcp | CP并行 |
| `enable_expert_parallel` | EP并行 | MoE |
| `vllm_ascend_enable_fused_mc2` | MoE大融合算子 | MoE |
| `multistream_overlap_shared_expert` | 共享专家多流 | MoE |
| `enable_shared_expert_dp` | 共享专家dp | MoE |
| `dynamic_eplb` | EPLB | 负载均衡 |
| `data_parallel_size_local` | DPLB | 负载均衡 |
| `kv_transfer_config.connector=MooncakeConnectorV1` | mooncake池化 | 池化 |
| `kv_transfer_config.connector=AscendStoreConnector` | kvcache池化 | 池化 |
| `vllm_ascend_enable_flashcomm1` | flashcomm1 | 其它优化点 |
| `finegrained_tp_config.lmhead_tensor_parallel_size` | lm_head_dp | 其它优化点 |
| `weight_prefetch_config` | 权重预取 | 其它优化点 |

**Note**: mooncake池化 and kvcache池化 are separate columns in the xlsx but both use `kv_transfer_config`. They are distinguished by the connector type: `MooncakeConnectorV1` for mooncake池化 (PD disaggregation), `AscendStoreConnector` for kvcache池化 (KV Cache Pool with memcache/mooncake backend).

## Handling New Features

If the xlsx adds new feature columns, update the `FEATURE_KEYS`, `FEATURE_NAMES`, and `FEATURE_CATEGORIES` arrays in `script/extract_model_features.py`. Use the corresponding `config_name` from vllm / vllm-ascend as the key. If no existing config_name matches, use a descriptive snake_case key. After updating, re-run the script to regenerate `common.json` and all model files.

## Handling New Models

New models in the xlsx will automatically produce a new per-model JSON file on the next script run. Models with empty data rows (no version entries) will not produce a file — check the xlsx directly for such cases.
