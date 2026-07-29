---
name: serving-slo-concurrency
description: >-
  Estimates max concurrency under TTFT/TPOT SLO for arbitrary vLLM-Ascend models
  (Qwen any size, GLM, DeepSeek, MiniMax, …) by analyzing vllm-ascend attention
  dispatch (use_mla/use_sparse) and msmodeling profiling_database. Requires local
  clones of both repos. Use as sub-skill of serving-parallel-strategy-tuning.
---

# serving-slo-concurrency

在 KV 内存上限内，按 SLO（TTFT / TPOT）估算各并行策略的最大实际并发。

**模型泛化**：不绑定单一模型。按架构特征选型 backend，覆盖：

| 模型族 | 示例 | 默认路径 |
| --- | --- | --- |
| Qwen（任意参数量） | Qwen3.5-27B / 122B / 397B | dense → `ASCEND` + FIA |
| Qwen MoE | Qwen3.5-397B-A17B | dense attn + MoE kernels |
| GLM | GLM-5.1 / 5.2 | sparse MLA → `ASCEND_SFA`（LI/SFA 仅为该路径示例） |
| DeepSeek MLA | DeepSeek-V3 | `ASCEND_MLA` |
| DeepSeek sparse | DeepSeek-V3.2 / V4-flash | `ASCEND_SFA` |
| MiniMax / Llama | MiniMax-M2.5 等 | 默认 `ASCEND` |

调度与源码一致：`vllm_ascend/platform.py` 的 `(use_mla, use_sparse, use_compress)` → MLA / SFA / DSA / ASCEND。若提供 `model_config.json` / profile（含 `index_topk`、`kv_lora_rank` 等），**覆盖**名称启发式。

**强依赖代码仓：**

| 仓库 | URL |
| --- | --- |
| vllm-ascend | https://github.com/vllm-project/vllm-ascend |
| msmodeling | https://gitcode.com/Ascend/msmodeling |

## 算子耗时查询（硬要求）

**必须优先使用 msmodeling 真实 CSV 耗时**（`profiling_database` 中的 `Duration(us)` / `Average Duration(us)`）。

1. **先**运行 `ensure_msmodeling_csv.py`：  
   - `git lfs pull`（若可用）  
   - 否则 GitCode LFS batch API 拉齐 pointer → 真实 CSV  
2. **再** `lookup_op_latency` / `estimate_slo_concurrency.py` 查表  
3. **仅当**真实 CSV 不可用时，才允许 analytic / builtin 回退，且必须写明原因

| `perf_db_source` | 含义 | 是否需 `perf_db_fallback_reasons` |
| --- | --- | --- |
| `msmodeling_csv` | 全部核心算子命中真实 CSV | 否（`used_real_csv=true`） |
| `msmodeling_mixed` | 部分真实 CSV + 部分回退 | **是** |
| `msmodeling_lfs_analytic` | CSV 仍为 LFS pointer | **是** |
| `msmodeling_mapping` | 无 CSV / 无法解析，用映射公式 | **是** |
| `builtin_fallback` | 无 msmodeling（仅测试） | **是** |

回退原因示例：

- `git-lfs unavailable` + GitCode LFS API 失败  
- CSV 文件仍是 `version https://git-lfs.github.com/spec/v1` pointer  
- 设备目录下缺少对应 `kernel_type.csv`  
- CSV 无 Duration/latency 列  
- `--allow-without-repos`（仅测试）

产物中必须包含：

- `used_real_csv: true|false`
- `perf_db_fallback_reasons: [...]`（当 `used_real_csv=false` 时非空）
- `msmodeling-csv-ensure.json`（编排器写入的拉齐尝试记录）

Agent / 编排在 `tuning-status.md` / `tuning-process.md` 中必须展示上述字段；**禁止**在未说明原因的情况下静默使用公式估算。

## 流程

1. `analyze_vllm_ascend_path.py`：分类 family → 解析 arch → 映射 backend → kernel 列表  
2. `ensure_msmodeling_csv.py`：拉齐真实 CSV（硬要求）  
3. 生成 shape → `lookup_op_latency`（优先真实 `Duration(us)`）  
4. ITL / TPOT（decode）：
   - `ITL = (Σ 主要算子 decode 耗时 × num_hidden_layers) / main_op_ratio`
   - `num_hidden_layers` 来自模型 `config.json` / profile
   - `main_op_ratio` 目前参考值 **0.7**（后续按模型/场景替换为标定值）
   - `TPOT = ITL / (1 + mtp_accept_rate)`；二分最大并发  

详见 `references/code_repo_analysis.md`、`references/model_families.md`。

## 运行

```bash
python scripts/analyze_vllm_ascend_path.py \
  --model Qwen3.5-122B \
  --vllm-ascend-repo {workdir}/repos/vllm-ascend \
  --profile-json tuning/parallel-strategies.json \
  --out tuning/code-path.json --json

python scripts/ensure_msmodeling_csv.py \
  --msmodeling-repo {workdir}/repos/msmodeling \
  --kernels FusedInferAttentionScore,MatMulV2,hcom_allReduce_ \
  --out tuning/msmodeling-csv-ensure.json --json

python scripts/estimate_slo_concurrency.py \
  --parallel-json tuning/parallel-strategies.json \
  --kv-json tuning/kv-capacity.json \
  --slo-json tuning/slo-constraints.json \
  --vllm-ascend-repo {workdir}/repos/vllm-ascend \
  --msmodeling-repo {workdir}/repos/msmodeling \
  --out tuning/slo-concurrency.json --json
```

## 输出

含 `family`、`use_mla`/`use_sparse`、`selected_backend`、`core_ops`、`max_concurrency_slo`、`perf_db_source`、`used_real_csv`、`perf_db_fallback_reasons`。

## 约束

- 生产路径禁止 `--allow-without-repos`。
- 不部署、不压测。
- **禁止**在未尝试拉齐真实 CSV、且未写明 `perf_db_fallback_reasons` 的情况下交付 SLO 结果。
