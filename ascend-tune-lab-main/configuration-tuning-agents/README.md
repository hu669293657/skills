# Configuration Tuning Agents

vLLM-Ascend 服务化性能优化编排，架构 **Plugin → Agent → Skill**，目录命名与 [`configuration-tuning-skills/`](../configuration-tuning-skills/) 对称。

- **Primary**：[`AGENTS.md`](AGENTS.md) — `serving-perf-optimization`
- **Subagents**：[`agents/`](agents/) — Phase 1 `serving-baseline-reproduce-subagent`；Phase 2 `serving-tuning-subagent`（并行策略调优）
- **工作流**：[`workflows/`](workflows/) — 编排步骤与派发模板
- **安装**：[`init.sh`](init.sh) — 挂载 skills / agents / **workflows** 到目标项目

## 安装（推荐）

在**目标服务项目**根目录执行（或指定 `install_path`）：

```bash
/path/to/ascend-tune-lab/configuration-tuning-agents/init.sh project cursor
# 或
/path/to/ascend-tune-lab/configuration-tuning-agents/init.sh project cursor /path/to/your/project
```

`init.sh` 会安装：

| 挂载项 | 目标位置（Cursor project 示例） |
| --- | --- |
| Primary | `./AGENTS.md`、`.cursor/AGENTS.md` |
| Subagents | `.cursor/agents/*.md` |
| Skills | `.cursor/ascend-tune-lab/skills/*` |
| **Workflows** | `.cursor/workflows/` **与** `./workflows/`（符号链接到本插件 `workflows/`） |
| 仓库路径 | `./configuration-tuning-skills/`、`./configuration-tuning-agents/` |

安装后 primary 读取的工作流入口为：**`workflows/serving-perf-optimization-workflow.md`**。

支持 `level`：`project`（默认）、`global`；支持 `tool`：`opencode`、`claude`、`trae`、`cursor`、`copilot`、`codearts`。详见 `./init.sh --help`。

## 手动使用（不运行 init）

1. 将 `AGENTS.md` 复制或链接到项目编排入口。
2. 将 `workflows/` 链接到项目根 `workflows/`，保证 AGENTS 内相对路径可解析。
3. 准备 MD 配置文件：未指定工作目录时 Agent 会在当前路径创建 `workspace/`；在 `{workdir}/deploy-config.md` 填写（首次运行会自动生成模板），或参考 `configuration-tuning-skills/ascend-baseline-generator/config.example.md`；`## 基本参数` 必填，`## 服务化配置` / `## SLO约束` 可选。

## 目录结构

```
configuration-tuning-agents/
├── init.sh                      # CANNBot 风格安装脚本
├── AGENTS.md                    # primary orchestrator
├── README.md
├── agents/
│   ├── serving-baseline-reproduce-subagent.md   # Phase 1 · 基线配置生成
│   └── serving-tuning-subagent.md               # Phase 2 · 并行策略调优
└── workflows/
    ├── serving-perf-optimization-workflow.md
    ├── templates/
    │   ├── deploy-config.template.md
    │   ├── baseline-summary-template.md
    │   └── tuning-process-template.md   # Phase 2 唯一模板（中间过程+状态）
    └── references/
        ├── user-config-format.md
        └── subagent-prompt-templates.md
```

## 与 Skills 的对应关系

| Phase | Subagent | Skill | 当前 |
| --- | --- | --- | --- |
| 1 基线配置生成 | `serving-baseline-reproduce-subagent` | `ascend-baseline-generator` | 已实现 |
| 2 并行策略调优 | `serving-tuning-subagent` | `serving-parallel-strategy-tuning`（入口） | 已实现（离线） |
| 2 子步骤 | （由入口编排） | `find-possible-parallel-strategy` | 已实现 |
| 2 子步骤 | （由入口编排） | `serving-kv-cache-capacity` | 已实现 |
| 2 子步骤 | （由入口编排） | `serving-slo-concurrency` | 已实现 |

Phase 2 为离线估算；线上部署/压测相关 skill（`serving-cfg-extract`、`serving-perf-metrics` 等）仍可后续接入。
