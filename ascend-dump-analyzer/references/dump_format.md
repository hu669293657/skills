# Dump JSON Format Reference

Complete schema for dump JSON files produced by `msprechecker_dump.py` or `msprechecker dump`.

## Top-Level Structure

```json
{
  "_meta": { ... },
  "system": { ... },
  "ascend": { ... },
  "env": { ... },
  "mies config": { ... },
  "user config": { ... },
  "mindie env": { ... },
  "model config": { ... },
  "weight": { ... },
  "ping": { ... },
  "hccl": { ... },
  "link": [ ... ],
  "vnic": [ ... ],
  "tls": [ ... ]
}
```

Only `_meta`, `system`, `ascend`, `env` are always present. Other sections appear only when the corresponding CLI argument was provided.

---

## _meta

Collection metadata. Exclude from comparison, include in report header.

| Field | Type | Description |
|-------|------|-------------|
| `tool` | string | Tool name (`msprechecker_dump.py` or `msprechecker`) |
| `version` | string | Tool version |
| `timestamp` | string | Collection time `YYYY-MM-DD HH:MM:SS` |
| `hostname` | string | Server hostname |
| `python_version` | string | Python version |
| `collect_duration_seconds` | float | Collection duration in seconds |

---

## system

System information collected from `lscpu`, `/proc/cpuinfo`, `platform.uname()`, sysfs.

| Field | Type | Description | Expected |
|-------|------|-------------|----------|
| `model_name` | string | CPU model name | Any valid CPU |
| `virtual_machine` | bool | Whether running in VM | `false` for bare metal |
| `high_performance` | bool | CPU in performance mode | `true` |
| `system` | string | Kernel name (e.g. `Linux`) | `Linux` |
| `node` | string | Hostname | — |
| `release` | string | Kernel release (e.g. `5.10.0-xxx`) | — |
| `version` | string | Kernel version string | — |
| `machine` | string | Architecture (`aarch64` / `x86_64`) | — |
| `processor` | string | CPU architecture | — |
| `transparent_hugepage` | string | THP status | `[always]` |
| `page_size` | int | Memory page size in bytes | `4096` |
| `overcommit_memory` | string | Memory overcommit policy | `0` |

---

## ascend

Ascend component versions. Each component is a dict with version/timestamp/commit fields.

```json
{
  "driver": {"version": "24.1.0"},
  "toolkit": {"version": "8.0.0", "timestamp": "2025-01-01 00:00:00"},
  "opp_kernel": {"version": "8.0.0"},
  "mindstudio_toolkit": {"version": "8.0.0"},
  "atb": {"version": "x.x.x", "commit": "abc123"},
  "mindie": {"version": "x.x.x", "timestamp": "..."},
  "atb-models": {"version": "x.x.x", "time": "...", "commit": "abc123"}
}
```

### Components

| Component | version.info Path | Version Keys | Notes |
|-----------|-------------------|--------------|-------|
| `driver` | `/usr/local/Ascend/driver/version.info` | `version` | Absolute path, always at fixed location |
| `toolkit` | `$ASCEND_TOOLKIT_HOME/toolkit/version.info` | `version`, `version_dir` | Default: `/usr/local/Ascend/ascend-toolkit/latest/` |
| `opp_kernel` | `$ASCEND_TOOLKIT_HOME/opp_kernel/version.info` | `version`, `version_dir` | Same base as toolkit |
| `mindstudio_toolkit` | `$ASCEND_TOOLKIT_HOME/mindstudio-toolkit/version.info` | `version` | Same base as toolkit |
| `atb` | `$ATB_HOME_PATH/../../version.info` | `ascend-cann-atb version` | Default: `.../atb/latest/atb/cxx_abi_0` |
| `mindie` | `$MINDIE_LLM_HOME_PATH/../version.info` | `ascend-mindie` | Default: `.../mindie/latest/mindie-llm` |
| `atb-models` | `$ATB_SPEED_HOME_PATH/version.info` | `atb-models version` | Default: `/usr/local/Ascend/atb-models` |

An empty `{}` for a component means the version.info file was not found.

---

## env

All environment variables (`dict(os.environ)`) or filtered Ascend-related subset.

When `--filter` is used, only variables containing these substrings are kept:
`ASCEND`, `MINDIE`, `ATB_`, `HCCL_`, `MIES`, `RANKTABLE`, `GE_`, `TORCH`, `ACL_`, `NPU_`, `LCCL_`, `LCAL_`, `OPS`, `INF_`

### Critical Environment Variables

| Variable | Purpose |
|----------|---------|
| `ASCEND_HOME_PATH` | Ascend driver home |
| `ASCEND_TOOLKIT_HOME` | CANN toolkit home |
| `LD_LIBRARY_PATH` | Dynamic library search path (must include Ascend libs) |
| `MINDIE_LLM_HOME_PATH` | MindIE-LLM installation path |
| `ATB_HOME_PATH` | ATB library path |
| `ATB_SPEED_HOME_PATH` | ATB-Models installation path |
| `HCCL_BUFFSIZE` | HCCL communication buffer size |
| `TASK_QUEUE_ENABLE` | Task queue feature switch |
| `RANK_TABLE_FILE` | Path to rank table file |
| `OMP_NUM_THREADS` | OpenMP thread count |
| `PYTORCH_NPU_ALLOC_CONF` | PyTorch NPU memory allocation config |

---

## mies config

MindIE service `config.json` file content (raw JSON object).

Typically located at `/usr/local/Ascend/mindie/latest/mindie-service/conf/config.json`.

Key fields to analyze:
- `BackendConfig.ModelDeployConfig.ModelConfig[*].modelWeightPath`
- `BackendConfig.ModelDeployConfig.ModelConfig[*].modelType`
- `BackendConfig.ModelDeployConfig.ModelConfig[*].npuDeviceIds`
- `ServeConfig.ip` / `ServeConfig.port`
- `SchedulerConfig.maxBatchSize`
- `SchedulerConfig.maxPrefillBatchSize`

---

## user config

`user_config.json` for large EP / PD disaggregation scenarios.

---

## mindie env

`mindie_env.json` for PD disaggregation / large EP scenarios.

---

## model config

`config.json` from the model weight directory (HuggingFace-style model config).

Key fields:
- `model_type` — model architecture type
- `torch_dtype` — should be `float16`
- `transformers_version` — must not exceed installed version
- `hidden_size`, `num_attention_heads`, `num_hidden_layers`

---

## weight

SHA256 hashes of `.safetensors` weight files.

```json
{
  "00001": "abc123def456...",
  "00002": "789abc012def...",
  "model": "fff000eee111..."
}
```

Key is the tensor ID extracted from filename pattern `(\d{5})-of-\d{5}.safetensors`, or the full basename if pattern doesn't match.

---

## ping

Ping results for each host in the rank table.

```json
{
  "192.168.1.1": "PING 192.168.1.1 ... 3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n...",
  "192.168.1.2": "ping failed"
}
```

Good: contains `0% packet loss` and `3 received`
Bad: contains `100% packet loss`, `ping failed`, or error message

---

## hccl

HCCL HCCS ping results between NPU devices.

```json
{
  "/usr/local/Ascend/driver/tools/hccn_tool -i 0 -hccs_ping -g address 192.168.1.2": [0, "output..."],
  "/usr/local/Ascend/driver/tools/hccn_tool -i 0 -ping -g address 192.168.1.2": [1, "output..."]
}
```

Key is the full command string. Value is `[return_code, output_string]`.

Good: return code `0` and output contains `3 received`
Bad: non-zero return code or output contains `100% packet loss`

---

## link

Link status for each NPU device (output of `hccn_tool -i N -link -g`).

```json
[
  "link status: UP\n...",
  "link status: UP\n...",
  "link status: DOWN\n...",
  "link status: UP\n..."
]
```

Array index = device ID. Good: all entries contain `link status: UP`.

---

## vnic

VNIC status for each NPU device (output of `hccn_tool -i N -vnic -g`).

Only relevant for A3 boards. Each entry should have:
- `link status: UP`
- IP address configured
- Netmask configured

---

## tls

TLS certificate status for each NPU device (output of `hccn_tool -i N -tls -g`).

```json
[
  "tls switch[0] : 0\n...",
  "tls switch[0] : 0\n..."
]
```

`tls switch[0]` value:
- `0` = TLS disabled (expected for non-TLS deployments)
- `1` = TLS enabled
