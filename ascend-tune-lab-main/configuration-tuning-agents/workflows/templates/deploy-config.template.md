# 部署配置

请填写下方 `## 基本参数`（前 7 项均为必填）。`ModelScope模型ID` / `## 服务化配置` / `## SLO约束` 可选。
填写完成后保存，重新向 Agent 发起请求。参考示例：`configuration-tuning-skills/ascend-baseline-generator/config.example.md`

流程开始时会从 ModelScope 下载该模型的 `config.json` 到工作目录 `model_config.json`；失败则需你手动提供。

## 基本参数

- 输入长度:
- 输出长度:
- 设备类型:
- 模型名称:
- 量化格式:
- NPU卡数:
- 部署策略:
- ModelScope模型ID:

## 服务化配置

（本节可选，不需要可整节删除）

```bash
vllm serve /path/to/model \
    --host 0.0.0.0 \
    --port 8000
```

## SLO约束

（本节可选；缺省时 Phase 2 会询问，仍空则默认 TPOT&lt;50ms、TTFT 不限）

- TTFT:
- TPOT:
- 其他约束:
