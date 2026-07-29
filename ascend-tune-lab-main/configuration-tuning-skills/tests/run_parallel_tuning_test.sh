#!/usr/bin/env bash
# Offline tests for parallel-strategy tuning skills.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
TMP="${TMPDIR:-/tmp}/parallel_tuning_test_$$"
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

echo "=== 1) find-possible-parallel-strategy: GLM-5.1 744B W8A8 ==="
GLM_JSON=$($PY "$ROOT/find-possible-parallel-strategy/scripts/find_parallel_strategies.py" \
  --model GLM-5.1 --quantization w8a8 --num-npus 16 --input-len 2048 --output-len 2048 --json)
GLM_MIN=$($PY -c "import json,sys; print(json.loads(sys.argv[1])['min_tp'])" "$GLM_JSON")
assert_eq "glm min_tp >= 16" "$GLM_MIN" "16"

echo "=== 2) find-possible-parallel-strategy: DeepSeek-V4-flash 284B W8A8 ==="
DS_JSON=$($PY "$ROOT/find-possible-parallel-strategy/scripts/find_parallel_strategies.py" \
  --model DeepSeek-V4-flash --quantization w8a8 --num-npus 16 --input-len 4096 --output-len 1024 --json)
DS_MIN=$($PY -c "import json,sys; print(json.loads(sys.argv[1])['min_tp'])" "$DS_JSON")
assert_eq "deepseek-flash min_tp" "$DS_MIN" "8"
DS_N=$($PY -c "import json,sys; print(len(json.loads(sys.argv[1])['combinations']))" "$DS_JSON")
assert_true "deepseek has combos" "$([[ "$DS_N" -ge 1 ]] && echo 1 || echo 0)"
# Expect DP1TP16, DP2TP8 (min_tp=8)
DS_LABELS=$($PY -c "import json,sys; print(','.join(c['label'] for c in json.loads(sys.argv[1])['combinations']))" "$DS_JSON")
echo "  labels: $DS_LABELS"
assert_true "has DP2TP8" "$(echo "$DS_LABELS" | grep -q 'DP2TP8' && echo 1 || echo 0)"
assert_true "has DP1TP16" "$(echo "$DS_LABELS" | grep -q 'DP1TP16' && echo 1 || echo 0)"

echo "=== 3) KV capacity ==="
echo "$DS_JSON" > "$TMP/parallel-strategies.json"
$PY "$ROOT/serving-kv-cache-capacity/scripts/evaluate_kv_capacity.py" \
  --parallel-json "$TMP/parallel-strategies.json" \
  --out "$TMP/kv-capacity.json" --json >/dev/null
KV_N=$($PY -c "import json; d=json.load(open('$TMP/kv-capacity.json')); print(len(d['combinations']))")
assert_true "kv combinations > 0" "$([[ "$KV_N" -ge 1 ]] && echo 1 || echo 0)"
KV_POS=$($PY -c "
import json
d=json.load(open('$TMP/kv-capacity.json'))
ok=all(c['max_concurrency_memory']>=0 for c in d['combinations'])
# at least one positive if weight fits
print(1 if ok else 0)
")
assert_eq "kv concurrency non-negative" "$KV_POS" "1"

echo "=== 4) SLO defaults + concurrency ==="
$PY "$ROOT/serving-parallel-strategy-tuning/scripts/resolve_slo_constraints.py" \
  --out "$TMP/slo-constraints.json" --json >/dev/null
TPOT=$($PY -c "import json; print(json.load(open('$TMP/slo-constraints.json'))['tpot_ms'])")
assert_eq "default tpot_ms" "$TPOT" "50.0"
TTFT=$($PY -c "import json; print(json.load(open('$TMP/slo-constraints.json'))['ttft_ms'])")
assert_eq "default ttft unlimited" "$TTFT" "None"

PROBE="$REPO_ROOT/.tmp/repo_probe"
VLLM_REPO="$PROBE/vllm-ascend"
MS_REPO="$PROBE/msmodeling"
SLO_EXTRA=()
if [[ -d "$VLLM_REPO/vllm_ascend" && -d "$MS_REPO/tensor_cast/performance_model/profiling_database/data" ]]; then
  SLO_EXTRA=(--vllm-ascend-repo "$VLLM_REPO" --msmodeling-repo "$MS_REPO")
  echo "  using probe repos under $PROBE"
else
  SLO_EXTRA=(--allow-without-repos)
  echo "  WARN: probe repos missing; using --allow-without-repos"
fi

$PY "$ROOT/serving-slo-concurrency/scripts/estimate_slo_concurrency.py" \
  --parallel-json "$TMP/parallel-strategies.json" \
  --kv-json "$TMP/kv-capacity.json" \
  --slo-json "$TMP/slo-constraints.json" \
  --out "$TMP/slo-concurrency.json" --json \
  "${SLO_EXTRA[@]}" >/dev/null
SLO_SRC=$($PY -c "import json; print(json.load(open('$TMP/slo-concurrency.json')).get('perf_db_source'))")
echo "  perf_db_source=$SLO_SRC"
assert_true "perf source set" "$([[ -n "$SLO_SRC" ]] && echo 1 || echo 0)"

if [[ -d "$VLLM_REPO/vllm_ascend" ]]; then
  $PY "$ROOT/serving-slo-concurrency/scripts/analyze_vllm_ascend_path.py" \
    --model DeepSeek-V4-flash --vllm-ascend-repo "$VLLM_REPO" \
    --out "$TMP/code-path.json" --json >/dev/null
  BACKEND=$($PY -c "import json; print(json.load(open('$TMP/code-path.json')).get('selected_backend'))")
  echo "  selected_backend=$BACKEND"
  assert_true "backend selected" "$([[ -n "$BACKEND" ]] && echo 1 || echo 0)"
  OPS=$($PY -c "import json; print(len(json.load(open('$TMP/code-path.json')).get('core_ops') or []))")
  assert_true "core_ops non-empty" "$([[ "$OPS" -ge 1 ]] && echo 1 || echo 0)"
fi

# With tight TTFT, slo max should be <= without TTFT
$PY -c "
import json
from pathlib import Path
slo=json.load(open('$TMP/slo-constraints.json'))
slo['ttft_ms']=1.0  # 1ms impossible-ish
Path('$TMP/slo-tight.json').write_text(json.dumps(slo))
"
$PY "$ROOT/serving-slo-concurrency/scripts/estimate_slo_concurrency.py" \
  --parallel-json "$TMP/parallel-strategies.json" \
  --kv-json "$TMP/kv-capacity.json" \
  --slo-json "$TMP/slo-tight.json" \
  --out "$TMP/slo-tight-out.json" --json \
  "${SLO_EXTRA[@]}" >/dev/null
CMP=$($PY -c "
import json
a=json.load(open('$TMP/slo-concurrency.json'))
b=json.load(open('$TMP/slo-tight-out.json'))
def mx(d):
  xs=[c['max_concurrency_slo'] for c in d['combinations']]
  return max(xs) if xs else 0
print(1 if mx(b) <= mx(a) else 0)
")
assert_eq "tight TTFT <= default" "$CMP" "1"

echo "=== 4b) Multi-model backend generalization ==="
if [[ -d "$VLLM_REPO/vllm_ascend" ]]; then
  check_backend() {
    local model="$1" want="$2"
    local got
    got=$($PY "$ROOT/serving-slo-concurrency/scripts/analyze_vllm_ascend_path.py" \
      --model "$model" --vllm-ascend-repo "$VLLM_REPO" --json \
      | $PY -c "import json,sys; print(json.load(sys.stdin)['selected_backend'])")
    assert_eq "backend $model" "$got" "$want"
  }
  check_backend "Qwen3.5-27B" "ASCEND"
  check_backend "Qwen3.5-122B" "ASCEND"
  check_backend "Qwen3.5-397B" "ASCEND"
  check_backend "GLM-5.1" "ASCEND_SFA"
  check_backend "DeepSeek-V3" "ASCEND_MLA"
  check_backend "DeepSeek-V3.2" "ASCEND_SFA"
  check_backend "DeepSeek-V4-flash" "ASCEND_SFA"
  check_backend "MiniMax-M2.5" "ASCEND"
fi

echo "=== 5) Orchestrate smoke (--skip-clone, link probe or allow) ==="
CASE="$TMP/case"
mkdir -p "$CASE/baseline"
# Phase-0 model config (stub; production downloads from ModelScope)
cat > "$TMP/model_config.json" <<'EOF'
{
  "model_type": "deepseek_v3",
  "architectures": ["DeepseekV3ForCausalLM"],
  "hidden_size": 7168,
  "num_hidden_layers": 61,
  "num_attention_heads": 128,
  "num_key_value_heads": 128,
  "vocab_size": 129280,
  "index_topk": 2048,
  "kv_lora_rank": 512
}
EOF
cat > "$TMP/deploy-config.md" <<'EOF'
# 部署配置

## 基本参数

- 输入长度: 4096
- 输出长度: 1024
- 设备类型: A3
- 模型名称: DeepSeek-V4-flash
- 量化格式: w8a8
- NPU卡数: 8
- 部署策略: 单机混部

## SLO约束

- TPOT: <50ms
EOF

cat > "$CASE/baseline/baseline-summary.md" <<'EOF'
# baseline-summary

- model_name: DeepSeek-V4-flash
- quantization: w8a8
- device_type: A3
- num_npus: 8
- 输入长度: 4096
- 输出长度: 1024
- match_status: matched
- profile_confirmed: yes
- --data-parallel-size 1
- --tensor-parallel-size 8
EOF

ORCH_EXTRA=(--skip-clone)
if [[ ! -d "$VLLM_REPO/vllm_ascend" || ! -d "$MS_REPO/tensor_cast/performance_model/profiling_database/data" ]]; then
  ORCH_EXTRA+=(--allow-without-repos)
fi

$PY "$ROOT/serving-parallel-strategy-tuning/scripts/orchestrate_parallel_tuning.py" \
  --case-dir "$CASE" \
  --workdir "$TMP" \
  --config "$TMP/deploy-config.md" \
  --baseline-summary "$CASE/baseline/baseline-summary.md" \
  "${ORCH_EXTRA[@]}" --json > "$TMP/orch.json"

for f in slo-constraints.json parallel-strategies.json kv-capacity.json slo-concurrency.json tuning-status.md; do
  if [[ -f "$CASE/tuning/$f" ]]; then
    echo "  PASS: artifact $f"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: missing $f"
    FAIL=$((FAIL + 1))
  fi
done

if [[ -f "$CASE/tuning/tuning-process.md" && -f "$CASE/tuning/tuning-process.json" ]]; then
  echo "  PASS: artifact tuning-process.md/json"
  PASS=$((PASS + 1))
else
  echo "  FAIL: missing tuning-process.md/json"
  FAIL=$((FAIL + 1))
fi

if [[ -f "$CASE/tuning/code-path.json" ]]; then
  echo "  PASS: artifact code-path.json"
  PASS=$((PASS + 1))
elif [[ " ${ORCH_EXTRA[*]} " == *" --allow-without-repos "* ]]; then
  echo "  PASS: code-path skipped (allow-without-repos)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: missing code-path.json"
  FAIL=$((FAIL + 1))
fi
STATUS=$($PY -c "
t=open('$CASE/tuning/tuning-status.md').read()
print('completed' if 'status: completed' in t else 'bad')
")
assert_eq "tuning-status completed" "$STATUS" "completed"

echo "=== 6) Qwen3.5-27B A3 1-card smoke (must include DP1TP2) ==="
$PY "$ROOT/find-possible-parallel-strategy/scripts/find_parallel_strategies.py" \
  --model Qwen3.5-27B --quantization w8a8 --num-npus 1 --device-type A3 \
  --input-len 4096 --output-len 1024 --dp 1 --tp 2 \
  --out "$TMP/qwen-parallel.json" --json >/dev/null
QMIN=$($PY -c "import json; print(json.load(open('$TMP/qwen-parallel.json'))['min_tp'])")
QWORLD=$($PY -c "import json; print(json.load(open('$TMP/qwen-parallel.json'))['world_size'])")
QLABELS=$($PY -c "import json; print(','.join(c['label'] for c in json.load(open('$TMP/qwen-parallel.json'))['combinations']))")
assert_eq "qwen27 min_tp" "$QMIN" "1"
assert_eq "qwen27 A3 world_size" "$QWORLD" "2"
assert_true "qwen27 has DP1TP2" "$(echo "$QLABELS" | grep -q 'DP1TP2' && echo 1 || echo 0)"
assert_true "qwen27 has DP2TP1" "$(echo "$QLABELS" | grep -q 'DP2TP1' && echo 1 || echo 0)"

echo ""
echo "Results: PASS=$PASS FAIL=$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
