# 代码仓驱动的算子路径分析（模型泛化）

## 1. vllm-ascend（必拉）

主仓：https://github.com/vllm-project/vllm-ascend

权威调度：`vllm_ascend/platform.py` → `get_attn_backend_cls`：

- `use_sparse ≈ hasattr(hf_text_config, "index_topk")`
- `use_mla` 来自模型配置 / MLA 结构字段
- 映射到 `AscendAttentionBackend` / `AscendMLABackend` / `AscendSFABackend` / `AscendDSABackend`

本 skill 的 `analyze_vllm_ascend_path.py` **复现同一映射**，并对 Qwen / GLM / DeepSeek / MiniMax / Llama 等做 family 默认，再用 config 覆盖。

## 2. msmodeling（必拉）

https://gitcode.com/Ascend/msmodeling  

`tensor_cast/performance_model/profiling_database/data/{DEVICE}/vllm_ascend|hccl/`

查表与模型无关：只认 `kernel_type`（FIA / SparseFlashAttention / MatMul / HCCL…）。

## 3. 多模型示例期望

| 模型 | family | backend |
| --- | --- | --- |
| Qwen3.5-27B / 122B | qwen | ASCEND |
| Qwen3.5-397B | qwen_moe | ASCEND + MoE kernels |
| GLM-5.1 | glm | ASCEND_SFA |
| DeepSeek-V3 | deepseek_mla | ASCEND_MLA |
| DeepSeek-V3.2 / V4-flash | deepseek_sparse | ASCEND_SFA |
