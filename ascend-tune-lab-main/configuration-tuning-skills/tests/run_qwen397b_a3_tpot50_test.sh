#!/usr/bin/env bash
# Scenario test: Atlas 800I A3 · Qwen3.5-397B · 单机混部 · TPOT≤50ms
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
TMP="${TMPDIR:-/tmp}/qwen397b_a3_tpot50_$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

PY=python3
PASS=0
FAIL=0

assert_eq() {
  local name="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    echo "  PASS: $name ($got)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name got=$got want=$want"
    FAIL=$((FAIL + 1))
  fi
}

assert_true() {
  local name="$1" cond="$2"
  if [[ "$cond" == "1" || "$cond" == "true" || "$cond" == "True" ]]; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name (cond=$cond)"
    FAIL=$((FAIL + 1))
  fi
}

PROBE="$REPO_ROOT/.tmp/repo_probe"
VLLM_REPO="$PROBE/vllm-ascend"
MS_REPO="$PROBE/msmodeling"

echo "=== Scenario: A3 · Qwen3.5-397B · 单机混部 · TPOT=50ms ==="

# --- fixture deploy-config ---
cat > "$TMP/deploy-config.md" <<'EOF'
# 部署配置

## 基本参数

- 输入长度: 4096
- 输出长度: 1024
- 设备类型: A3
- 模型名称: Qwen3.5-397B
- 量化格式: w8a8
- NPU卡数: 8
- 部署策略: 单机混部

## SLO约束

- TTFT:
- TPOT: <50ms
- 其他约束: A3单机397B吞吐优先
EOF

CASE="$TMP/case"
# Phase-0 model config stub (production: ModelScope download)
mkdir -p "$TMP"
cat > "$TMP/model_config.json" <<'EOF'
{
  "model_type": "qwen3_moe",
  "architectures": ["Qwen3MoeForCausalLM"],
  "hidden_size": 4096,
  "num_hidden_layers": 94,
  "num_attention_heads": 64,
  "num_key_value_heads": 4,
  "vocab_size": 151936,
  "num_experts": 512,
  "num_experts_per_tok": 10
}
EOF
mkdir -p "$CASE/baseline"
cat > "$CASE/baseline/baseline-summary.md" <<'EOF'
# baseline-summary

- model_name: Qwen3.5-397B
- quantization: w8a8
- device_type: A3
- num_npus: 8
- deploy_strategy: 单机混部
- 输入长度: 4096
- 输出长度: 1024
- match_status: matched
- profile_confirmed: yes
- --data-parallel-size 1
- --tensor-parallel-size 16
EOF

echo "--- 1) Parallel strategies ---"
$PY "$ROOT/find-possible-parallel-strategy/scripts/find_parallel_strategies.py" \
  --config "$TMP/deploy-config.md" \
  --out "$TMP/parallel-strategies.json" --json >/dev/null

MIN_TP=$($PY -c "import json; print(json.load(open('$TMP/parallel-strategies.json'))['min_tp'])")
WEIGHT=$($PY -c "import json; print(json.load(open('$TMP/parallel-strategies.json'))['profile']['weight_gb'])")
LABELS=$($PY -c "import json; print(','.join(c['label'] for c in json.load(open('$TMP/parallel-strategies.json'))['combinations']))")
MODEL=$($PY -c "import json; print(json.load(open('$TMP/parallel-strategies.json'))['model'])")
NPUS=$($PY -c "import json; print(json.load(open('$TMP/parallel-strategies.json'))['num_npus'])")
WORLD=$($PY -c "import json; print(json.load(open('$TMP/parallel-strategies.json'))['world_size'])")
DIES=$($PY -c "import json; print(json.load(open('$TMP/parallel-strategies.json'))['dies_per_card'])")

assert_eq "model" "$MODEL" "Qwen3.5-397B"
assert_eq "num_npus(A3总卡数)" "$NPUS" "8"
assert_eq "dies_per_card(A3)" "$DIES" "2"
assert_eq "world_size(A3 dies)" "$WORLD" "16"
assert_eq "weight_gb (W8A8)" "$WEIGHT" "397.0"
assert_eq "min_tp" "$MIN_TP" "8"
echo "  labels: $LABELS"
assert_true "has DP1TP16" "$(echo "$LABELS" | grep -q 'DP1TP16' && echo 1 || echo 0)"
assert_true "has DP2TP8" "$(echo "$LABELS" | grep -q 'DP2TP8' && echo 1 || echo 0)"

echo "--- 2) KV capacity ---"
$PY "$ROOT/serving-kv-cache-capacity/scripts/evaluate_kv_capacity.py" \
  --parallel-json "$TMP/parallel-strategies.json" \
  --out "$TMP/kv-capacity.json" --json >/dev/null
KV_OK=$($PY -c "
import json
d=json.load(open('$TMP/kv-capacity.json'))
ok=all(c['max_concurrency_memory']>=0 for c in d['combinations']) and len(d['combinations'])>=1
print(1 if ok else 0)
")
assert_eq "kv combinations ok" "$KV_OK" "1"

echo "--- 3) SLO resolve (TPOT=50ms) ---"
$PY "$ROOT/serving-parallel-strategy-tuning/scripts/resolve_slo_constraints.py" \
  --config "$TMP/deploy-config.md" \
  --out "$TMP/slo-constraints.json" --json >/dev/null
TPOT=$($PY -c "import json; print(json.load(open('$TMP/slo-constraints.json'))['tpot_ms'])")
TTFT=$($PY -c "import json; print(json.load(open('$TMP/slo-constraints.json'))['ttft_ms'])")
assert_eq "tpot_ms" "$TPOT" "50.0"
assert_eq "ttft unlimited" "$TTFT" "None"

echo "--- 4) Code path + SLO concurrency ---"
SLO_EXTRA=()
ORCH_EXTRA=(--skip-clone)
if [[ -d "$VLLM_REPO/vllm_ascend" && -d "$MS_REPO/tensor_cast/performance_model/profiling_database/data" ]]; then
  SLO_EXTRA=(--vllm-ascend-repo "$VLLM_REPO" --msmodeling-repo "$MS_REPO")
  echo "  repos: probe ($PROBE)"
else
  SLO_EXTRA=(--allow-without-repos)
  ORCH_EXTRA+=(--allow-without-repos)
  echo "  WARN: probe repos missing; --allow-without-repos"
fi

if [[ -d "$VLLM_REPO/vllm_ascend" ]]; then
  $PY "$ROOT/serving-slo-concurrency/scripts/analyze_vllm_ascend_path.py" \
    --model Qwen3.5-397B \
    --vllm-ascend-repo "$VLLM_REPO" \
    --profile-json "$TMP/parallel-strategies.json" \
    --out "$TMP/code-path.json" --json >/dev/null
  BACKEND=$($PY -c "import json; print(json.load(open('$TMP/code-path.json'))['selected_backend'])")
  FAMILY=$($PY -c "import json; print(json.load(open('$TMP/code-path.json'))['family'])")
  assert_eq "family" "$FAMILY" "qwen_moe"
  assert_eq "backend" "$BACKEND" "ASCEND"
fi

$PY "$ROOT/serving-slo-concurrency/scripts/estimate_slo_concurrency.py" \
  --parallel-json "$TMP/parallel-strategies.json" \
  --kv-json "$TMP/kv-capacity.json" \
  --slo-json "$TMP/slo-constraints.json" \
  --out "$TMP/slo-concurrency.json" --json \
  "${SLO_EXTRA[@]}" >/dev/null

SLO_CHECK=$($PY -c "
import json
d=json.load(open('$TMP/slo-concurrency.json'))
assert d['slo']['tpot_ms'] == 50.0
combos=d.get('combinations') or []
assert combos, 'no combinations'
# every feasible combo must report tpot <= 50 at its max_slo point (when feasible)
ok=True
for c in combos:
    if c.get('feasible') and c.get('at_max_slo') and c['at_max_slo'].get('tpot_ms') is not None:
        if float(c['at_max_slo']['tpot_ms']) > 50.0 + 1e-6:
            ok=False
rec=d.get('recommended')
print(json.dumps({
  'ok': ok,
  'n_combos': len(combos),
  'n_feasible': sum(1 for c in combos if c.get('feasible')),
  'recommended': None if not rec else {
    'label': rec.get('label'),
    'max_concurrency_slo': rec.get('max_concurrency_slo'),
    'tpot_ms': (rec.get('at_max_slo') or {}).get('tpot_ms'),
  },
  'perf_db_source': d.get('perf_db_source'),
}, ensure_ascii=False))
")
echo "  slo_summary: $SLO_CHECK"
assert_true "tpot constraint held" "$(echo "$SLO_CHECK" | $PY -c "import json,sys; print(1 if json.load(sys.stdin)['ok'] else 0)")"

echo "--- 5) Full orchestrate ---"
$PY "$ROOT/serving-parallel-strategy-tuning/scripts/orchestrate_parallel_tuning.py" \
  --case-dir "$CASE" \
  --workdir "$TMP" \
  --config "$TMP/deploy-config.md" \
  --baseline-summary "$CASE/baseline/baseline-summary.md" \
  "${ORCH_EXTRA[@]}" --json > "$TMP/orch.json"

for f in slo-constraints.json parallel-strategies.json kv-capacity.json slo-concurrency.json tuning-status.md tuning-process.md tuning-process.json; do
  if [[ -f "$CASE/tuning/$f" ]]; then
    echo "  PASS: artifact $f"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: missing $f"
    FAIL=$((FAIL + 1))
  fi
done

# process report must contain A/B/C sections with concrete values
PROC_OK=$($PY -c "
import json
from pathlib import Path
md=Path('$CASE/tuning/tuning-process.md').read_text(encoding='utf-8')
js=json.loads(Path('$CASE/tuning/tuning-process.json').read_text(encoding='utf-8'))
need_md=['## A. 并行策略过程值','## B. KV 内存并发上限过程值','## C. SLO 并发过程值']
ok_md=all(s in md for s in need_md)
ok_js=all(k in js for k in (
  'parallel_strategy_process','kv_memory_concurrency_process','slo_concurrency_process'))
combos=js['parallel_strategy_process'].get('combinations') or []
kv_c=js['kv_memory_concurrency_process'].get('combinations') or []
slo_c=js['slo_concurrency_process'].get('combinations') or []
ok_vals=bool(combos and kv_c and slo_c and combos[0].get('process') and kv_c[0].get('max_concurrency_memory') is not None)
print(1 if ok_md and ok_js and ok_vals else 0)
")
assert_eq "tuning-process has A/B/C process values" "$PROC_OK" "1"

ORCH_TPOT=$($PY -c "import json; print(json.load(open('$CASE/tuning/slo-constraints.json'))['tpot_ms'])")
ORCH_MODEL=$($PY -c "import json; print(json.load(open('$CASE/tuning/parallel-strategies.json'))['model'])")
assert_eq "orch tpot_ms" "$ORCH_TPOT" "50.0"
assert_eq "orch model" "$ORCH_MODEL" "Qwen3.5-397B"

REC=$($PY -c "
import json
d=json.load(open('$CASE/tuning/slo-concurrency.json'))
r=d.get('recommended') or {}
print(r.get('label'), r.get('max_concurrency_slo'), (r.get('at_max_slo') or {}).get('tpot_ms'))
")
echo "  recommended: $REC"

echo ""
echo "Results: PASS=$PASS FAIL=$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
echo "OK: A3 Qwen3.5-397B TPOT<=50ms scenario passed"
exit 0
