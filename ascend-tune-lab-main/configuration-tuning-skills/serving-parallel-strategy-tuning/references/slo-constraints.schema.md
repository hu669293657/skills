# SLO 约束 JSON 契约

由 `resolve_slo_constraints.py` 产出，路径：`{case_dir}/tuning/slo-constraints.json`。

## 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ttft_ms` | number \| null | TTFT 上限（毫秒）；`null` = 不限制 |
| `tpot_ms` | number | TPOT 上限（毫秒）；缺省默认 **50** |
| `other` | string \| null | 其他约束自由文本 |
| `section_present` | bool | deploy-config 是否含 `## SLO约束` |
| `fields_present` | string[] | 已从配置/交互解析到的字段名 |
| `defaults_applied` | string[] | 应用的默认项说明 |
| `needs_user_input` | bool | 解析阶段是否建议向用户询问（应用默认后为 false） |

## deploy-config.md 节示例

```markdown
## SLO约束

- TTFT: <1s
- TPOT: <50ms
- 其他约束: 优先高吞吐
```

缺省或未填 TPOT → `tpot_ms=50`；未填 TTFT → `ttft_ms=null`。
