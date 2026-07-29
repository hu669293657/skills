---
name: serving-parallel-strategy-tuning
description: >-
  Orchestrates vLLM-Ascend single-node parallel strategy tuning from baseline
  config: resolve SLO constraints, clone vllm-ascend/msmodeling, then run
  find-possible-parallel-strategy, serving-kv-cache-capacity, and
  serving-slo-concurrency. Use in Phase 2 serving tuning.
---

# serving-parallel-strategy-tuning

并行策略调优 **入口 skill**。以 Phase 1 `baseline-summary.md` + 工作目录 `deploy-config.md` 为起点，串联三个子 skill，产出推荐 DP/TP/EP 与 SLO 可行并发。

## 子 skill

| 顺序 | Skill | 产出 |
| --- | --- | --- |
| 1 | `find-possible-parallel-strategy` | `parallel-strategies.json` |
| 2 | `serving-kv-cache-capacity` | `kv-capacity.json` |
| 3 | `serving-slo-concurrency` | `slo-concurrency.json` |

## Agent 步骤

1. Read 本 SKILL 与三子 skill 的 `SKILL.md`
2. 确认 `{workdir}/model_config.json` 已由 Phase 0 从 ModelScope 下载（或用户手供）；缺失则先跑 `download_modelscope_config.py`，失败则警告用户手供并停止
3. 确认 `{case_dir}/baseline/baseline-summary.md` 可用
4. **SLO**：运行 `resolve_slo_constraints.py --config deploy-config.md --no-defaults`  
   - 若 `needs_user_input=true` 或关键字段缺失 → **向用户询问** TTFT / TPOT / 其他约束  
   - 用户不提供 → 默认 `TPOT=50ms`，`TTFT` 不限（去掉 `--no-defaults` 或写 interactive JSON 后再跑）
5. Clone（**SLO 强依赖**）：`clone_repos.py` → **`{workdir}/repos/`**（工作目录下独立目录，跨 case 复用；不放在 `case_dir/tuning/`）
   - vllm-ascend：GitHub → GitCode
   - msmodeling：GitCode → GitHub  
   - **失败必须警告用户**：脚本/编排会输出 `warning`（含尝试 URL 与手动 clone 命令），并写 `{workdir}/repos-clone.warning.md`；**停止** Phase 2，要求用户手动放到 `{workdir}/repos/` 后重试  
   - LFS 拉 CSV 失败：输出 `lfs_warning`（可继续，但须在产物中说明非真实 CSV）  
   - 测试可用 `--allow-without-repos`
6. 一键编排（编排内**必须先** `ensure_msmodeling_csv` 再查耗时）：

```bash
python scripts/orchestrate_parallel_tuning.py \
  --case-dir {case_dir} \
  --workdir {workdir} \
  --config {config_md_path} \
  --baseline-summary {case_dir}/baseline/baseline-summary.md
```

7. 验收 `{case_dir}/tuning/tuning-status.md`、**中间过程产物** `tuning-process.md` / `tuning-process.json`，以及 JSON 产物  
   - 检查 `perf_db_source` / `used_real_csv`  
   - 若 `used_real_csv=false`，`perf_db_fallback_reasons` **必须非空**并回报用户

## 产物目录

```text
{workdir}/
  model_config.json           # Phase 0：ModelScope 下载的模型 config（硬门禁）
  model_config.fetch.json     # 下载元数据 / 失败警告
  repos-clone.warning.md      # clone 失败时的用户警告（含手动命令）
  repos/
    vllm-ascend/              # required for SLO（独立目录，跨 case 共享）
    msmodeling/               # required for SLO
  cases/<case>/tuning/        # 或 {case_dir}/tuning/
    slo-constraints.json
    parallel-strategies.json
    kv-capacity.json
    code-path.json
    msmodeling-csv-ensure.json
    slo-concurrency.json
    tuning-process.json
    tuning-process.md
    tuning-status.md
    clone-repos.json          # 记录 clone 结果与 repos_dir 路径
```

## 约束

- 仅单机混部离线估算；**禁止**部署、压测、覆盖 `baseline-launch.sh`
- `serving-slo-concurrency` **必须**基于双仓分析
- **算子耗时必须优先真实 CSV**（`perf_db_source=msmodeling_csv`）；若否，必须在产物中写明 `perf_db_fallback_reasons`（LFS pointer / git-lfs 缺失 / API 失败 / 缺文件等）