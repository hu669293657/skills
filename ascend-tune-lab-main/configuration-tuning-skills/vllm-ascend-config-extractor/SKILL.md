---
name: "vllm-ascend-config-extractor"
description: "Extract and compare configuration switches between vLLM and vLLM-Ascend repos. Invoke when user needs to audit, compare, or document config options across vLLM and vLLM-Ascend."
---

# vLLM & vLLM-Ascend Configuration Switch Extractor

This skill extracts detailed configuration information from the vLLM upstream repository and the vLLM-Ascend plugin repository. It takes a predefined list of target configurations (`extraction_targets.json`) as input, and for each listed config item, enriches it with information traced from documentation and source code, producing two structured JSON output files.

---

## Step 1: Input Parameter Validation & Initialization

### Required Inputs

| Parameter | Description | Example |
|-----------|-------------|---------|
| `vllm_repo_path` | Local path to vLLM repository | `c:\Users\lan\Code\vllm-0.18.0` |
| `vllm_ascend_repo_path` | Local path to vLLM-Ascend repository | `c:\Users\lan\Code\vllm-ascend-0.18.0` |
| `target_version` | Target version number for the extraction | `0.18.0` |
| `output_dir` | Directory for output files | `./config_extraction_output` |

### Validation Checklist

1. **Repository path validity**: Verify both paths exist and are directories
2. **Core directory existence**:
   - vLLM: `vllm/`, `docs/`
   - vLLM-Ascend: `vllm_ascend/`, `docs/`
3. **Key file existence**:
   - vLLM: `vllm/envs.py`, `vllm/engine/arg_utils.py`, `vllm/config/vllm.py`
   - vLLM-Ascend: `vllm_ascend/envs.py`, `vllm_ascend/ascend_config.py`, `vllm_ascend/platform.py`
4. **Output directory**: Create if not exists; verify write permission
5. **Reference files**: Load both files from the skill's `reference/` directory:
   - `config_source_map.json` — Source file locations for code-level tracing
   - `extraction_targets.json` — Predefined list of config items to extract (the extraction scope)

If any validation fails, report the specific issue and halt execution.

### 1.1 Load Extraction Targets

Load `extraction_targets.json` and parse its hierarchical structure. This file defines **which** configuration items need to be extracted — only items present in this file are in scope. The file is organized by source and type:

| Top-Level Key | Sub-Category | Content |
|---------------|-------------|---------|
| `vllm` | `env_vars` | vLLM upstream environment variables to extract |
| `vllm` | `cli_args` | vLLM upstream CLI arguments to extract |
| `vllm_ascend` | `env_vars` | vLLM-Ascend plugin environment variables to extract |
| `vllm_ascend` | `cli_args` | vLLM-Ascend relevant CLI arguments to extract |
| `vllm_ascend` | `additional_config` | vLLM-Ascend additional_config items to extract |
| `vllm_ascend` | `deprecated_env_vars` | Deprecated vLLM-Ascend environment variables to document |
| `ascend_runtime` | `hccl` | HCCL communication environment variables to extract |
| `ascend_runtime` | `npu` | NPU device runtime environment variables to extract |
| `ascend_runtime` | `omp` | OpenMP runtime environment variables to extract |
| `system` | `kernel` | Linux kernel parameters to extract |
| `system` | `env` | System-level environment variables to extract |

Each item in the file has the form `{ "name": "...", "value": "...", "description": "..." }`. The `name` field is the key used to locate the config in source code and documentation.

### 1.2 Sync Extraction Targets with Model Deployment Docs

`extraction_targets.json` is a manually curated input file. Over time, as new models are added or existing model deployment docs are updated, new env vars and CLI args may appear that are not yet tracked. This step scans the model deployment documentation to detect such gaps and update the file accordingly.

**Source directory**: `{vllm_ascend_repo_path}/docs/source/tutorials/models/`

**Scan procedure**:

1. **Enumerate model docs**: List all `.md` files in the models directory (excluding `index.md`)
2. **Extract config references from code blocks**: For each model doc, find all shell code blocks (```` ```shell ... ``` ````) and extract:
   - **Env vars**: Lines matching `export <NAME>=<VALUE>` — extract `<NAME>` and `<VALUE>`
   - **CLI args**: Lines within `vllm serve` commands matching `--<flag> <value>` or `--<flag>` (boolean) — extract `--<flag>` and `<value>` if present
   - **Kernel params**: Lines matching `sysctl -w <key>=<value>` — extract `<key>` and `<VALUE>`
   - **System commands**: Lines matching `echo performance | tee .../scaling_governor` — extract as `cpu cpufreq scaling_governor`
3. **Classify each extracted config**: Determine which category it belongs to based on naming conventions:

| Pattern | Category in `extraction_targets.json` |
|---------|--------------------------------------|
| `VLLM_ASCEND_*` | `vllm_ascend.env_vars` |
| `VLLM_*` (non-ASCEND) | `vllm.env_vars` |
| `HCCL_*` | `ascend_runtime.hccl` |
| `GLOO_SOCKET_IFNAME`, `TP_SOCKET_IFNAME` | `ascend_runtime.hccl` |
| `PYTORCH_NPU_*`, `ASCEND_*`, `ACL_*`, `TRITON_*`, `USE_MULTI_BLOCK_POOL`, `TASK_QUEUE_ENABLE`, `ASCEND_BUFFER_POOL` | `ascend_runtime.npu` |
| `OMP_*` | `ascend_runtime.omp` |
| `LD_PRELOAD`, `LD_LIBRARY_PATH`, `unset *` | `system.env` |
| `sysctl -w vm.*`, `sysctl -w kernel.*` | `system.kernel` |
| `echo performance .../scaling_governor` | `system.kernel` |
| `--*` (CLI args in `vllm serve`) | `vllm.cli_args` or `vllm_ascend.cli_args` (see note below) |
| `--additional-config` JSON sub-keys | `vllm_ascend.additional_config` |

   **CLI arg classification note**: Most CLI args belong to `vllm.cli_args` (they are vLLM upstream args). The following args belong to `vllm_ascend.cli_args` instead:
   - `--quantization ascend` (Ascend-specific quantization)
   - `--additional-config` (vllm-ascend specific config entry)
   - `--mamba-cache-mode` (Ascend-specific prefix caching mode)
   - `--runner` (Ascend-specific runner selection)
   - `--prefill-context-parallel-size`, `--decode-context-parallel-size` (Ascend context parallel)

4. **Diff against existing targets**: For each extracted config, check if its `name` already exists in the corresponding category of `extraction_targets.json`. Report:
   - **New items**: Configs found in docs but not in `extraction_targets.json` — these are candidates for addition
   - **Existing items**: Configs already tracked — no action needed
5. **Update `extraction_targets.json`**: For each new item, add it to the appropriate category with:
   ```json
   {
     "name": "<config_name>",
     "value": "<value_from_doc_or_placeholder>",
     "description": "<brief_description_inferred_from_context>"
   }
   ```
   The `value` and `description` fields are initial placeholders — they will be enriched during Steps 3-4. If the user declines to add certain items, skip them and note the omission in the report.

6. **Report sync results**: Print a summary:
   - Total configs found across all model docs
   - Number of new items added to `extraction_targets.json`
   - List of new items by category
   - List of skipped items (if any), with reason

**Important**: This step is **idempotent** — running it multiple times produces the same result. It only adds missing items, never removes or modifies existing ones.

---

## Step 2: Version Compatibility Check

### 2.1 Extract Version Numbers

**From vLLM**:
- Read `vllm/version.py` or `pyproject.toml` to extract the vLLM version
- Fallback: Check `git describe --tags` if available

**From vLLM-Ascend**:
- Read `vllm_ascend/version.py` or `pyproject.toml` to extract the vLLM-Ascend version

### 2.2 Version Mapping via Dockerfile

The authoritative version mapping is found in vLLM-Ascend's Dockerfiles:

1. Search all Dockerfile patterns listed in `config_source_map.json` → `version_mapping.vllm_ascend_dockerfile_patterns`
2. Extract the line matching `ARG VLLM_TAG=` to find the pinned vLLM version
3. Compare the pinned vLLM version against the actual vLLM repository version

Example Dockerfile pattern:
```dockerfile
ARG VLLM_TAG=v0.18.0
```

### 2.3 Compatibility Modes

| Mode | Behavior |
|------|----------|
| `strict` (default) | Halt with error if versions don't match |
| `force` | Log a warning but continue extraction |

When versions don't match, output:
- vLLM repo version
- vLLM-Ascend Dockerfile pinned version
- vLLM-Ascend repo version
- Recommendation on whether to proceed

---

## Step 3: Documentation-Level Information Extraction

For each config item listed in `extraction_targets.json`, search the documentation to extract initial information.

### 3.1 vLLM Documentation Extraction

**Primary doc files**:
- `docs/configuration/env_vars.md` — Environment variables (uses `--8<--` markers to include `vllm/envs.py`)
- `docs/configuration/engine_args.md` — Engine/CLI arguments
- `docs/configuration/serve_args.md` — Server arguments
- `docs/configuration/optimization.md` — Optimization configurations
- `docs/configuration/conserving_memory.md` — Memory-related configurations

**Extraction approach**:
1. For each target config item from `extraction_targets.json` → `vllm` section, search the above docs
2. Extract the 7 output fields (see Step 5.1 for schema):
   - **config_name**: Internal code name (e.g., `max_model_len`)
   - **env_var_name**: Environment variable name (e.g., `VLLM_ALLOW_LONG_MAX_MODEL_LEN`), or `null`
   - **cli_arg**: CLI parameter name (e.g., `--max-model-len`), or `null`
   - **value_type**: Value type from doc (e.g., `int`, `bool`, `str`)
   - **default_value**: Documented default value
   - **scope**: Applicable scopes from `cli`, `env_var`, `config_file`, `code_internal`
   - **description**: Functional description from documentation

### 3.2 vLLM-Ascend Documentation Extraction

**Primary doc files**:
- `docs/source/user_guide/configuration/env_vars.md` — Ascend environment variables (uses `literalinclude` to embed `vllm_ascend/envs.py`)
- `docs/source/user_guide/configuration/additional_config.md` — Additional config options (tables with Name/Type/Default/Description)
- Feature guide docs under `docs/source/user_guide/feature_guide/` — May contain config references
- Model deployment docs under `docs/source/tutorials/models/` — Contain model-specific env vars, CLI args, and runtime configs used in practice

**Extraction approach**: Same as 3.1, but for items from `extraction_targets.json` → `vllm_ascend` section.

### 3.3 Ascend Runtime & System Documentation Extraction

**Source files**:
- Model deployment docs under `docs/source/tutorials/models/` — Contain HCCL, NPU, OMP, and system-level configs in deployment scripts
- `docs/source/user_guide/` — May contain runtime tuning guides

**Extraction approach**: Scan deployment scripts and code blocks for `export`, `sysctl`, and other system-level patterns. For items from `extraction_targets.json` → `ascend_runtime` and `system` sections, extract the same 7 output fields.

**Important**: `ascend_runtime` and `system` items are **not defined in vllm or vllm-ascend source code**. They are external runtime/system-level configurations that only appear in deployment documentation. Therefore, Step 4 (code-level tracing) does **not** apply to these items — documentation extraction in this step is the sole information source.

### 3.4 Generate Initial Tables

Produce initial (doc-level) tables for all target config items. Items not found in documentation should be marked as "doc-missing" for later code-level enrichment (applies to `vllm` and `vllm_ascend` sections only; `ascend_runtime` and `system` items have no code-level enrichment step).

---

## Step 4: Code-Level Tracing & Information Enrichment

**Scope**: This step applies **only** to target items from `extraction_targets.json` → `vllm` and `vllm_ascend` sections. Items from `ascend_runtime` and `system` sections are external runtime configurations with no corresponding source code in either repository; they are fully handled by Step 3 documentation extraction and require no code-level enrichment.

For each in-scope target config item, use source code analysis to verify, correct, and supplement the documentation-level information.

### 4.1 vLLM Code-Level Analysis

#### 4.1.1 Discovery Method

Rather than maintaining a hardcoded list of config files (which becomes stale as vLLM evolves), use the following dynamic discovery approach:

**Entry points** (defined in `config_source_map.json`):

| Entry Point | Discovery Method |
|-------------|-----------------|
| `vllm/envs.py` → `environment_variables` dict | Parse all dict keys to discover env var names; extract lambda logic for default values; extract `TYPE_CHECKING` block for type annotations |
| `vllm/engine/arg_utils.py` → `EngineArgs` class + `add_cli_args()` | Parse dataclass fields for config names and types; parse `add_cli_args()` for CLI argument names and mapping to config fields |
| `vllm/config/` directory | Scan all `.py` files for `*Config` classes (inheriting from dataclass or using `@dataclass`); extract field names, types, defaults, and docstrings from each class |

**Dynamic file discovery for `vllm/config/`**:
1. List all `.py` files in `vllm/config/` directory (excluding `__init__.py` and `utils.py`)
2. For each file, find all class definitions matching `*Config` pattern
3. For each class, extract all dataclass fields with their types, defaults, and docstrings
4. Cross-reference with `VllmConfig` in `vllm/config/vllm.py` to understand which sub-configs are actually used

This approach automatically adapts when vLLM adds or removes config files/classes.

#### 4.1.2 Enrichment Rules

For each target config item from `extraction_targets.json` → `vllm` section:

1. **Correct default values**: Compare doc-stated defaults against code defaults. Use the code default as the authoritative `default_value`.
2. **Fill missing CLI arg names**: From `add_cli_args()` in `arg_utils.py`, map `--xxx-yyy` to the corresponding config field.
3. **Fill missing env var names**: From `environment_variables` dict in `envs.py`.
4. **Refine value types**: Use type annotations from `TYPE_CHECKING` block or dataclass field types.
5. **Fill missing descriptions**: Use docstrings or inline comments from code when doc is missing.
6. **Verify existence**: Confirm the target config item actually exists in code. Flag items in `extraction_targets.json` that cannot be found in code.

### 4.2 vLLM-Ascend Code-Level Analysis

#### 4.2.1 Discovery Method

**Entry points** (defined in `config_source_map.json`):

| Entry Point | Discovery Method |
|-------------|-----------------|
| `vllm_ascend/envs.py` → `env_variables` dict | Parse all dict keys to discover env var names; extract lambda logic for default values; extract inline comments for documentation |
| `vllm_ascend/ascend_config.py` → `AscendConfig` class | Parse `__init__` for `additional_config.get()` calls to discover top-level additional_config fields and their defaults; locate all sub-config class instantiations to discover sub-config names |
| `vllm_ascend/ascend_config.py` → sub-config classes | For each sub-config class listed in `config_source_map.json` → `vllm_ascend_config_sources.additional_config.sub_configs`, parse `__init__` signature for field names, types, and defaults |
| `vllm_ascend/platform.py` → `check_and_update_config()` | Parse for config override logic that may modify defaults or add constraints |
| `vllm_ascend/profiling_config.py` | Parse for profiling-related configurations |

**Resolving dotted names**: When a target item from `extraction_targets.json` has a dotted name (e.g., `ascend_compilation_config.fuse_qknorm_rope`), resolve it by:
1. Splitting on `.` — the first part is the sub-config key, the second is the field name
2. Locating the corresponding sub-config class (e.g., `ascend_compilation_config` → `AscendCompilationConfig`)
3. Finding the field in that class's `__init__` signature

**Dynamic sub-config discovery**: Rather than hardcoding sub-config class names, discover them by parsing `AscendConfig.__init__` for patterns like:
```python
xxx_config = additional_config.get("xxx_config", {})
self.xxx_config = XxxConfig(**xxx_config)
```
This automatically adapts when vllm-ascend adds or removes sub-config classes.

#### 4.2.2 Enrichment Rules

For each target config item from `extraction_targets.json` → `vllm_ascend` section:

1. **Correct default values**: Use code defaults as authoritative.
2. **Fill missing env var names**: From `env_variables` dict.
3. **Fill CLI arg names**: Ascend additional_config items are passed via `--additional-config` JSON, not individual CLI args.
4. **Refine value types**: From `AscendConfig` and sub-config class `__init__` signatures.
5. **Fill missing descriptions**: From class docstrings and inline comments.
6. **Verify existence**: Confirm the target config item actually exists in code. Flag items in `extraction_targets.json` that cannot be found in code.

### 4.3 Doc-vs-Code Consistency Check

Cross-reference documentation and code to detect inconsistencies for target items:

1. **Doc-only items**: Target configs referenced in documentation but not defined in code. Example: `VLLM_ASCEND_ENABLE_TOPK_OPTIMIZE` appears in `docs/source/tutorials/models/GLM4.x.md` but is not in `vllm_ascend/envs.py`. These should be flagged in the output.
2. **Code-only items**: Target configs defined in code but not documented. These should be flagged in the output.
3. **Deprecated items**: Target configs in the `deprecated_env_vars` category that have been superseded by newer mechanisms. Verify the replacement is correctly documented.

---

## Step 5: Structured Output Generation

### 5.1 Config Item Schema

Each configuration item in the output uses the following 7-field structure:

```json
{
  "config_name": "enable_nz",
  "env_var_name": "VLLM_ASCEND_ENABLE_NZ",
  "cli_arg": null,
  "value_type": "int",
  "default_value": "1",
  "scope": ["env_var"],
  "description": "Whether to enable weight cast format to FRACTAL_NZ. 0: close nz; 1: only quant case enable nz; 2: enable nz as long as possible."
}
```

**Field definitions**:

| Field | Type | Description |
|-------|------|-------------|
| `config_name` | string | Internal code-level name (e.g., dataclass field name, dict key) |
| `env_var_name` | string \| null | Environment variable name, `null` if not an env var |
| `cli_arg` | string \| null | CLI parameter name (e.g., `--max-model-len`), `null` if not a CLI arg |
| `value_type` | string | Value type: `int`, `bool`, `str`, `float`, `list`, `dict`, or `Literal[...]` |
| `default_value` | string | Default value as a string representation (from code, not doc) |
| `scope` | list[string] | Applicable scopes, subset of: `cli`, `env_var`, `config_file`, `code_internal` |
| `description` | string | Functional description (prefer code comments; fallback to doc) |

### 5.2 Output JSON Structure

Two output files are produced, one per repository:

**vLLM output file** (`vllm_config_{vllm_version}.json`):

```json
{
  "metadata": {
    "extraction_timestamp": "2026-06-16T10:00:00Z",
    "vllm_version": "0.18.0"
  },
  "configs": [
    {
      "config_name": "max_model_len",
      "env_var_name": null,
      "cli_arg": "--max-model-len",
      "value_type": "int",
      "default_value": "8192",
      "scope": ["cli"],
      "description": "Model maximum context length."
    }
  ]
}
```

**vLLM-Ascend output file** (`vllm_ascend_config_{ascend_version}.json`):

```json
{
  "metadata": {
    "extraction_timestamp": "2026-06-16T10:00:00Z",
    "vllm_ascend_version": "0.18.0"
  },
  "configs": [
    {
      "config_name": "enable_nz",
      "env_var_name": "VLLM_ASCEND_ENABLE_NZ",
      "cli_arg": null,
      "value_type": "int",
      "default_value": "1",
      "scope": ["env_var"],
      "description": "Whether to enable weight cast format to FRACTAL_NZ. 0: close nz; 1: only quant case enable nz; 2: enable nz as long as possible."
    }
  ]
}
```

**Note**: The output format (7-field flat array per repo) is independent of the input format (`extraction_targets.json` uses 3-field hierarchical structure). The input defines **what** to extract; the output provides the **enriched result**.

### 5.3 Output Files

- **Format**: JSON
- **vLLM naming**: `vllm_config_{vllm_version}.json` (e.g., `vllm_config_0.18.0.json`)
- **vLLM-Ascend naming**: `vllm_ascend_config_{ascend_version}.json` (e.g., `vllm_ascend_config_0.18.0.json`)
- **Location**: In the specified `output_dir`

---

## Step 6: Result Validation & Wrap-up

### 6.1 JSON Structure Validation

Verify each output JSON:
1. Top-level keys `metadata` and `configs` exist
2. Each config item has all 7 required fields: `config_name`, `env_var_name`, `cli_arg`, `value_type`, `default_value`, `scope`, `description`
3. `scope` values are subset of: `cli`, `env_var`, `config_file`, `code_internal`
4. No duplicate `config_name` entries within the same output file
5. Every item from `extraction_targets.json` has a corresponding entry in the output (or is listed as unresolved)

### 6.2 Extraction Statistics

Compute and report:

| Metric | Description |
|--------|-------------|
| Total target items | Count of all items in `extraction_targets.json` |
| vLLM items extracted | Count of vLLM config items in output |
| vLLM-Ascend items extracted | Count of vLLM-Ascend config items in output |
| Items with env_var scope | Count of configs accessible via environment variable |
| Items with cli scope | Count of configs accessible via CLI argument |
| Items with both env_var and cli | Count of configs accessible via both |
| Doc-only items | Count of target configs found in docs but not in code |
| Code-only items | Count of target configs found in code but not in docs |
| Unresolved items | Count of target configs not found in either docs or code |

### 6.3 Missing Items Report

List:
1. **Doc-missing items**: Target configs found in code but not in docs
2. **Code-missing items**: Target configs found in docs but not in code (e.g., `VLLM_ASCEND_ENABLE_TOPK_OPTIMIZE` in model docs but not in `envs.py`)
3. **Unresolved items**: Target configs not found in either docs or code
4. **Deprecated items**: Target configs in `deprecated_env_vars` with their replacement status

### 6.4 Final Output

Print a summary report including:
- Version compatibility result
- Key statistics from 6.2
- Path to the generated JSON files
- List of items requiring manual review (unresolved, doc-code inconsistencies, deprecated)

---

## Reference Files

The skill uses two reference files in the `reference/` directory:

### `config_source_map.json`
- **Role**: Defines the entry points for code-level discovery — where to start looking for config definitions
- **Contents**:
  - File paths and dict/class names for env var and config extraction (e.g., `vllm/envs.py` → `environment_variables`, `vllm_ascend/ascend_config.py` → `AscendConfig`)
  - Marker patterns for extracting env var definition blocks from code
  - Sub-config class names as a starting reference (not exhaustive — actual classes are discovered dynamically)
  - Dockerfile patterns for version mapping
- **Extensibility**: When vllm or vllm-ascend adds new config source files, update the corresponding entry in this file. The discovery methods in Step 4 will automatically find new fields within those files without SKILL.md changes.

### `extraction_targets.json`
- **Role**: Input file that defines the extraction scope — only items listed in this file will be extracted
- **Format**: Hierarchical structure by source (`vllm`/`vllm_ascend`/`ascend_runtime`/`system`) and type (`env_vars`/`cli_args`/`additional_config`/`deprecated_env_vars`/`hccl`/`npu`/`omp`/`kernel`/`env`)
- **Item schema**: `{ "name": "...", "value": "...", "description": "..." }` (3 fields, simplified)
- **Usage**: Loaded at Step 1; each item's `name` field is used as the key to locate the config in code and documentation
- **Note**: This file's format is independent of the output format. It serves as the "what to extract" definition, while the output provides the "enriched extraction result" in a different schema.
- **Extensibility**: To add new config items for extraction, either:
  - Manually add entries to the appropriate category in this file, or
  - Run Step 1.2 to automatically scan model deployment docs and detect missing items
- **Maintenance**: Run Step 1.2 periodically (especially after vllm-ascend releases with new model docs) to keep the extraction targets in sync with actual deployment practices
