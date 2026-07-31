---
name: "ascend-dump-analyzer"
description: "Collect, analyze, and compare Ascend NPU environment dumps. Invoke when user needs to collect environment info on Ascend servers, analyze a dump JSON, or compare two dumps for config drift."
---

# Ascend Dump Analyzer

Collect, analyze, and compare Ascend NPU environment dump JSON files. The skill includes a standalone collection script (`msprechecker_dump.py`), analysis guidelines, and HTML report templates.

## When to Invoke

- User needs to collect environment info on an Ascend NPU server (use the dump script)
- User provides a `.json` file and asks to analyze/diagnose an Ascend environment
- User asks to compare two or more dump JSON files
- User mentions "dump analysis", "environment comparison", "config drift"
- User asks "what changed" between two environments

## Core Capabilities

### 0. Environment Collection (Dump)

The skill includes a standalone Python script at `references/msprechecker_dump.py` that can be deployed to any Ascend NPU server to collect environment information.

**Key features:**
- Zero dependencies — only requires Python standard library (psutil optional, auto-skipped if missing)
- Single file — copy to any Linux server and run with `python3`
- Output compatible with `msprechecker compare` — JSON format matches the original tool

**When to use:**
- User asks to "collect environment info" or "create a dump" on an Ascend server
- User needs a baseline snapshot before deployment
- User needs to collect data on a machine where `msprechecker` is not installed

**How to deploy and use:**

1. Copy the script to the target server:
```bash
scp references/msprechecker_dump.py user@server:/tmp/
```

2. Run on the target server:
```bash
# Basic collection (system + ascend + env)
python3 /tmp/msprechecker_dump.py

# Specify output path
python3 /tmp/msprechecker_dump.py -o /tmp/snapshot.json

# Filter to Ascend-related env vars only (safer for sharing)
python3 /tmp/msprechecker_dump.py --filter -o /tmp/snapshot.json

# Full collection with config, network, and weights
python3 /tmp/msprechecker_dump.py \
  --mies-config-path /usr/local/Ascend/mindie/latest/mindie-service/conf/config.json \
  --rank-table-path /path/to/rank_table.json --scene mindie \
  --weight-dir /path/to/weights \
  --filter -o /tmp/full_dump.json
```

**CLI parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `-o, --output-path` | No | Output JSON path. Default: `./msprechecker_dumped.json` |
| `--filter` | No | Only collect Ascend-related env vars |
| `--mies-config-path` | No | MindIE service config.json path |
| `--user-config-path` | No | user_config.json path |
| `--mindie-env-path` | No | mindie_env.json path |
| `--rank-table-path` | No | Rank table file path (triggers ping/HCCL/link/vnic/tls) |
| `--scene` | No | `mindie` or `vllm` — determines rank table parse format |
| `--weight-dir` | No | Model weight directory (triggers SHA256 hash collection) |
| `--chunk-size` | No | Hash chunk size in MB: 32/64/128/256. Default: 32 |

**What gets collected:**

| Section | Always | Trigger | Content |
|---------|--------|---------|---------|
| `system` | Yes | — | CPU model, high-perf mode, kernel, THP, memory, VM detection |
| `ascend` | Yes | — | 7 components: driver/toolkit/opp_kernel/atb/mindie/atb-models |
| `env` | Yes | — | All env vars or filtered Ascend subset |
| `mies config` | No | `--mies-config-path` | MindIE service config.json |
| `user config` | No | `--user-config-path` | user_config.json |
| `mindie env` | No | `--mindie-env-path` | mindie_env.json |
| `model config` | No | `--weight-dir` | Model config.json from weight dir |
| `weight` | No | `--weight-dir` | SHA256 hashes of .safetensors files |
| `ping` | No | `--rank-table-path` | Ping results for all hosts in rank table |
| `hccl` | No | `--rank-table-path` | HCCS ping between NPU devices |
| `link` | No | `--rank-table-path` | NPU link status (hccn_tool) |
| `vnic` | No | `--rank-table-path` | VNIC status (A3 boards) |
| `tls` | No | `--rank-table-path` | TLS certificate status |
| `_meta` | Yes | — | Tool name, version, timestamp, hostname, Python version, duration |

**Guidance for users:**
- Always advise `--filter` when the dump will be shared (avoids leaking sensitive env vars)
- For multi-machine comparison, collect dumps on **each** machine separately
- For PD disaggregation / K8s scenarios, use `--user-config-path` and `--mindie-env-path`
- The script is safe to run as root or non-root (root needed for `hccn_tool` TLS check)

### 1. Single Dump Analysis

Given a dump JSON file, produce a structured report covering:

1. **Meta Info** — hostname, timestamp, collection duration
2. **System Health** — CPU model, high-performance mode, kernel version, THP status, memory settings, VM detection
3. **Ascend Components** — driver/toolkit/atb/mindie/atb-models versions, flag missing components, check version consistency
4. **Environment Variables** — highlight key Ascend-related vars, flag missing critical ones, detect sensitive vars
5. **Config Files** (if present) — summarize key MindIE/vLLM config values
6. **Network** (if present) — ping results, HCCL connectivity, Link/VNIC/TLS status
7. **Weights** (if present) — tensor file count and hash summary
8. **Missing Sections** — proactively list what was NOT collected and suggest re-collection commands

### 2. Dump Comparison

Given two or more dump JSON files, identify all differences:

1. **Flatten** each dump into path→value pairs
2. **Diff** by comparing values at every path across all files
3. **Classify** each difference as: version change / env var change / config change / network change
4. **Output** a structured diff report highlighting what changed, added, or removed

## Analysis Guidelines

### System Section

| Key | Expected / Good | Needs Attention | Problem |
|-----|----------------|-----------------|---------|
| `high_performance` | `true` | — | `false` = CPU not in performance mode |
| `virtual_machine` | `false` | `true` (known VM) | — |
| `transparent_hugepage` | `always` | `madvise` | `never` = THP disabled |
| `overcommit_memory` | `0` or `1` | — | `2` = strict overcommit, may cause OOM |
| `page_size` | `4096` | — | non-4096 may cause compatibility issues |

**Note on THP**: The dump value is the string inside `[...]` brackets extracted by the script (e.g. `always`, not `[always]`). The sysfs file format is `[always] madvise never`, and the script extracts the active value without brackets.

**Note on overcommit_memory**: `0` = heuristic (kernel default), `1` = always allow overcommit, `2` = strict. Both `0` and `1` are acceptable for Ascend environments; `2` is the real risk.

### Ascend Components

#### Individual Component Check

Check each component's `version` field:
- Flag any component with empty `version` or empty `{}` as **MISSING**
- Driver version < 24.1 is a known risk for newer NPU features
- RC versions (e.g. `26.0.rc1`, `8.2.RC1`) should be flagged as **non-production** — note this is a release candidate

Key components: `driver`, `toolkit`, `opp_kernel`, `mindstudio_toolkit`, `atb`, `mindie`, `atb-models`

#### Version Consistency Check (Cross-Component)

After checking individual components, compare versions across components that should align:

| Component Pair | Expected | Mismatch Impact |
|---------------|----------|-----------------|
| `toolkit` vs `opp_kernel` | Same version | OPP kernels may not match toolkit APIs |
| `toolkit` vs `atb` | Same major.minor series | ATB may have compatibility issues |
| `toolkit` vs `mindstudio_toolkit` | Same major.minor series | MindStudio toolkit may not support toolkit features |

Extract the version series (e.g. `8.2.RC1` from `8.2.0.0.201 (8.2.RC1)`) and compare. Flag mismatches as WARN.

**Note**: `driver` version uses a different numbering scheme (e.g. `26.0.rc1`) and should NOT be compared with CANN toolkit versions.

### Environment Variables

When `--filter` was used, only Ascend-related vars are present. Otherwise all env vars are dumped.

#### Critical Env Vars Checklist

Always check the presence of these critical variables and report as a table:

| Variable | Purpose | Missing Impact |
|----------|---------|----------------|
| `ASCEND_HOME_PATH` | Ascend driver home | Tools may not find driver |
| `ASCEND_TOOLKIT_HOME` | CANN toolkit home | Tools may use wrong toolkit path |
| `LD_LIBRARY_PATH` | Dynamic library path | Runtime may fail to load Ascend libs |
| `MINDIE_LLM_HOME_PATH` | MindIE-LLM path | MindIE won't be found (aligns with `mindie: {}` in ascend section) |
| `ATB_HOME_PATH` | ATB library path | ATB may not load correctly |
| `ATB_SPEED_HOME_PATH` | ATB-Models path | ATB-Models won't be found (aligns with `atb-models: {}` in ascend section) |
| `HCCL_BUFFSIZE` | HCCL buffer size | Uses default; may need tuning for multi-card |
| `TASK_QUEUE_ENABLE` | Task queue switch | Uses default |
| `RANK_TABLE_FILE` | Rank table path | Distributed scenarios need this |
| `OMP_NUM_THREADS` | OpenMP threads | May affect CPU operator performance |
| `PYTORCH_NPU_ALLOC_CONF` | PyTorch NPU memory config | Uses default allocation strategy |

**Cross-reference**: If `mindie: {}` in ascend section AND `MINDIE_LLM_HOME_PATH` is missing, conclude MindIE is likely not installed. Same logic applies to `atb-models` and `ATB_SPEED_HOME_PATH`.

#### Sensitive Variable Detection

When `--filter` was NOT used (full env dump), scan for variables that may contain sensitive information:

| Pattern | Risk | Action |
|---------|------|--------|
| `*_API_KEY`, `*_SECRET`, `*_TOKEN`, `*_PASSWORD` | High | Flag in report, advise user to use `--filter` or scrub before sharing |
| `SSH_CONNECTION`, `SSH_CLIENT` | Low | Contains IP addresses, note in report |
| `LD_LIBRARY_PATH`, `PATH` | Info | Contains system paths, safe to share |

Report: "This dump was collected without `--filter` and contains N sensitive variables. Consider using `--filter` for safer sharing."

### Network / HCCL

- **ping**: Look for `0% packet loss` (good) vs `100% packet loss` or `ping failed` (bad)
- **hccl**: Look for `3 received` (good) in hccs_ping output; check return code `0`
- **link**: All devices should show `link status: UP`
- **vnic**: A3 boards should have IP configured and link UP
- **tls**: `tls switch[0]` should be `0` (disabled) for non-TLS deployments

### Weights

- Count of tensor files and their SHA256 hashes
- When comparing, any hash difference means weight files changed

### Missing Sections Detection

At the end of each analysis, check which optional sections were NOT collected and proactively suggest re-collection:

| Missing Section | Suggested Command |
|----------------|-------------------|
| `mies config` | `--mies-config-path <path>` |
| `user config` | `--user-config-path <path>` |
| `mindie env` | `--mindie-env-path <path>` |
| `model config` + `weight` | `--weight-dir <path>` |
| `ping` / `hccl` / `link` / `vnic` / `tls` | `--rank-table-path <path> --scene <mindie\|vllm>` |

Provide a ready-to-use command example with the most common paths filled in.

## Output Format

### Analysis Output

Structure the report as a markdown document with these sections:

```
## Environment Summary
- Hostname / Timestamp / Duration / Collection scope

## System Health
| Check | Value | Expected | Status | Notes |
(table with OK/WARN/FAIL indicators)

## Ascend Components
### Individual Versions
| Component | Version | Timestamp/Commit | Status | Notes |
### Version Consistency
| Component Pair | Version A | Version B | Status |
(flag cross-component mismatches)

## Environment Variables
### Critical Ascend Variables
| Variable | Present | Value | Status |
(missing vars flagged as FAIL)
### Sensitive Variables
(flag any vars matching sensitive patterns)
### Diagnosis
(correlate missing env vars with missing ascend components)

## Config Analysis (if present)
(Key config values with observations)

## Network Status (if present)
(Ping / HCCL / Link / VNIC / TLS summary table)

## Missing Sections
(list what was not collected + suggested commands)

## Comprehensive Diagnosis
### Issues Found
| Priority | Issue | Recommendation |
(high/medium/low)
### Suggested Next Steps
(ready-to-use commands)
```

Use these status markers:
- OK = meets expectation
- WARN = needs attention but not blocking
- FAIL = likely causes deployment/runtime issues

### Comparison Output

```
## Comparison Summary
- File A: <path> (hostname, timestamp)
- File B: <path> (hostname, timestamp)
- Total differences: N

## Version Changes
| Component | File A | File B | Change |
(upgrade / downgrade / missing)

## Environment Variable Changes
| Variable | File A | File B | Status |
(added / removed / modified)

## System Setting Changes
| Setting | File A | File B |

## Config Changes (if present)
| Config Path | File A | File B |

## Network Changes (if present)
(Connectivity status changes)
```

## Comparison Algorithm

The comparison follows the same logic as the original `msprechecker compare`:

1. **Flatten**: Recursively traverse each dump's JSON tree into `{section.path: value}` pairs. Dicts use `.key`, lists use `[index]`.
2. **Collect**: For each unique path, gather values from all dump files.
3. **Filter**: Keep only paths where values differ across files (or exist in some but not all).
4. **Classify**: Group differences by top-level section (`system`, `ascend`, `env`, etc.).
5. **Report**: Output structured diff with context.

Path flattening example:
```json
{"ascend": {"driver": {"version": "24.1"}}}
```
becomes:
```
ascend.driver.version = "24.1"
```

## HTML Report Generation

When the user requests a visual report (or when producing a deliverable), generate a self-contained HTML file using the templates in `references/`.

### Single Dump Report

Use `references/template_single_dump.html` as the design template. Replace all `{{PLACEHOLDER}}` variables with actual data from the dump JSON. Key sections:

1. **Header** — hostname, timestamp, duration, health score (computed as OK_count / total_checks * 100)
2. **Health Cards** — 4 stat cards: System status, Components installed (X/7), Critical env vars (X/11), Network status
3. **System Health Table** — one row per check, with OK/WARN/FAIL badges
4. **Ascend Components** — card grid (green border = installed, red border = missing) + version consistency matrix
5. **Environment Variables** — critical vars table + sensitive vars banner + collapsible full list
6. **Missing Sections** — dashed-border cards with suggested commands
7. **Diagnosis** — priority-sorted issue list (high/medium/low) with recommendations

Health score calculation:
- Count total checks across system (8) + ascend components (7) + critical env vars (11) = 26
- Each OK = 1 point, WARN = 0.5, FAIL = 0
- Score = round(points / 26 * 100)

### Comparison Report

Use `references/template_comparison.html` as the design template. Key sections:

1. **Header** — two file cards side-by-side with VS badge, similarity score bar
2. **Stats Cards** — 5 cards: version changes, env changes, system changes, other changes, identical sections
3. **Identical Banner** — if 0 differences, show prominent "完全一致" banner instead of diff tables
4. **Diff Tables** — for each category, color-coded rows: green (added/upgraded), red (removed/downgraded), amber (modified)
5. **Drift Assessment** — overall evaluation with per-dimension status

Similarity score calculation:
- Flatten both JSON files into path→value pairs (excluding `_meta`)
- Count total unique paths
- Count paths with identical values across all files
- Similarity = round(identical / total * 100)

### HTML Generation Rules

- Output a **self-contained** HTML file (inline CSS, no external dependencies)
- Save to the user's output directory
- Use `computer://` links to share the file
- All CSS variables and class names follow the template's design system
- Handle missing sections gracefully (show empty state or info banner)
- For comparison, if sections exist in one file but not the other, show all paths as "only in File X"
- Collapsible sections (`<details>`) for large data (full env vars, all weight hashes)

## Dump JSON Structure Reference

See `references/dump_format.md` for the complete JSON schema with all possible keys, value types, and field descriptions.

## Important Notes

- The `_meta` key contains collection metadata (tool version, timestamp, hostname) — exclude it from comparison logic but include in the report header.
- If a section is missing from the JSON, it means that collector was not run (e.g., no `--rank-table-path` means no `ping`/`hccl`/`link`/`vnic`/`tls` sections).
- Empty `{}` for an Ascend component means the `version.info` file was not found — flag this as MISSING and cross-reference with the corresponding env var to determine if the component is truly not installed vs. just not configured.
- Environment variable values may contain sensitive information (API keys, passwords, tokens) — always scan and flag sensitive variables, advise user to use `--filter` or scrub before sharing dump files.
- When comparing, if a section exists in one file but not the other, report all paths in that section as "only in File X".
- RC (Release Candidate) versions should be noted as non-production — flag in the report but do not treat as FAIL.
