# ascend-tune-lab

A repository for deploying agents/skills for parameter tuning, profiling and analysis.

## Skills

| Skill | Description |
|-------|-------------|
| [ascend-baseline-generator](configuration-tuning-skills/ascend-baseline-generator/) | Find best vLLM-Ascend deployment baseline config by model/device/quantization |
| [model-feature-extractor](configuration-tuning-skills/model-feature-extractor/) | Convert model feature support xlsx to JSON |
| [serving-cfg-extract](configuration-tuning-skills/serving-cfg-extract/) | Extract non-default args from serving startup logs |
| [serving-perf-metrics](configuration-tuning-skills/serving-perf-metrics/) | Parse serving performance metrics from logs |
| [vllm-ascend-config-extractor](configuration-tuning-skills/vllm-ascend-config-extractor/) | Extract vLLM-Ascend config definitions from source |

## Agents

| Agent | Description |
|-------|-------------|
| [serving-perf-optimization](configuration-tuning-agents/) | Two-phase vLLM serving pipeline: Phase 1 baseline config (active), Phase 2 tuning (placeholder) |
