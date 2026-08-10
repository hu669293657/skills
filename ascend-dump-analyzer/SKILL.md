---
name: "ascend-dump-analyzer"
description: "采集、分析和比对昇腾 NPU 环境信息。当用户需要在 Ascend 服务器上采集环境信息、分析 dump JSON、或比对两个 dump 文件以检测配置漂移时触发。"
---

# Ascend 环境分析器

采集、分析和比对昇腾 NPU 环境 dump JSON 文件。本技能包含独立采集脚本（`msprechecker_dump.py`）、比对脚本（`compare_dumps.py`）、分析指南和 HTML 报告模板。

## 触发条件

- 用户需要在 Ascend NPU 服务器上采集环境信息（使用采集脚本）
- 用户提供 `.json` 文件并要求分析/诊断 Ascend 环境
- 用户要求比对两个或多个 dump JSON 文件
- 用户提到"dump 分析"、"环境对比"、"配置漂移"
- 用户问"两个环境之间有什么变化"

## 核心能力

### 0. 环境采集（Dump）

本技能包含一个独立 Python 脚本 `references/msprechecker_dump.py`，可部署到任意 Ascend NPU 服务器采集环境信息。

**核心特性：**
- 零依赖 — 仅需 Python 标准库（psutil 为可选依赖，缺失时自动跳过）
- 单文件 — 拷贝到任意 Linux 服务器即可用 `python3` 运行
- 输出兼容 `msprechecker compare` — JSON 格式与原工具一致

**使用场景：**
- 用户要求"采集环境信息"或"创建 dump"
- 用户需要部署前的基线快照
- 用户在未安装 msprechecker 的机器上采集数据

**部署和使用：**

1. 拷贝脚本到目标服务器：
```bash
scp references/msprechecker_dump.py user@server:/tmp/
```

2. 在目标服务器上运行：
```bash
# 基础采集（系统 + Ascend + 环境变量）
python3 /tmp/msprechecker_dump.py

# 指定输出路径
python3 /tmp/msprechecker_dump.py -o /tmp/snapshot.json

# 仅采集昇腾相关环境变量（分享时更安全）
python3 /tmp/msprechecker_dump.py --filter -o /tmp/snapshot.json

# 全量采集（含配置、网络、权重）
python3 /tmp/msprechecker_dump.py \
  --mies-config-path /usr/local/Ascend/mindie/latest/mindie-service/conf/config.json \
  --rank-table-path /path/to/rank_table.json --scene mindie \
  --weight-dir /path/to/weights \
  --filter -o /tmp/full_dump.json
```

**命令行参数：**

| 参数 | 必选 | 说明 |
|------|------|------|
| `-o, --output-path` | 否 | 输出 JSON 路径。默认：`./msprechecker_dumped.json` |
| `--filter` | 否 | 仅采集昇腾相关环境变量 |
| `--mies-config-path` | 否 | MindIE 服务 config.json 路径 |
| `--user-config-path` | 否 | user_config.json 路径 |
| `--mindie-env-path` | 否 | mindie_env.json 路径 |
| `--rank-table-path` | 否 | Rank table 文件路径（触发 ping/HCCL/link/vnic/tls 采集） |
| `--scene` | 否 | `mindie` 或 `vllm` — 决定 rank table 解析格式 |
| `--weight-dir` | 否 | 模型权重目录（触发 SHA256 哈希采集） |
| `--chunk-size` | 否 | 哈希计算块大小(MB)：32/64/128/256。默认：32 |

**采集内容：**

| Section | 始终采集 | 触发参数 | 内容 |
|---------|---------|---------|------|
| `system` | 是 | — | CPU 型号、高性能模式、内核、THP、内存、虚拟化检测 |
| `ascend` | 是 | — | 7 个组件：driver/toolkit/opp_kernel/atb/mindie/atb-models |
| `env` | 是 | — | 全部环境变量或过滤后的昇腾子集 |
| `mies config` | 否 | `--mies-config-path` | MindIE 服务 config.json |
| `user config` | 否 | `--user-config-path` | user_config.json |
| `mindie env` | 否 | `--mindie-env-path` | mindie_env.json |
| `model config` | 否 | `--weight-dir` | 权重目录下的 config.json |
| `weight` | 否 | `--weight-dir` | .safetensors 文件的 SHA256 哈希 |
| `ping` | 否 | `--rank-table-path` | rank table 中所有主机的 ping 结果 |
| `hccl` | 否 | `--rank-table-path` | NPU 设备间 HCCS ping |
| `link` | 否 | `--rank-table-path` | NPU 链路状态（hccn_tool） |
| `vnic` | 否 | `--rank-table-path` | VNIC 状态（A3 板卡） |
| `tls` | 否 | `--rank-table-path` | TLS 证书状态 |
| `_meta` | 是 | — | 工具名、版本、时间戳、主机名、Python 版本、耗时 |

**用户指导：**
- 分享 dump 文件时建议加 `--filter`（避免泄露敏感环境变量）
- 多机比对时需在**每台**机器上分别采集
- PD 分离 / K8s 场景使用 `--user-config-path` 和 `--mindie-env-path`
- root 和非 root 均可运行（TLS 检查需 root）

### 0.5. Dump 比对（compare_dumps.py）

本技能包含一个独立 Python 脚本 `references/compare_dumps.py`，用于比对 2 个或多个 dump JSON 文件。

**核心特性：**
- 零依赖 — 仅需 Python 标准库
- 支持 2 个或更多文件同时比对
- 鲁棒 JSON 解析 — 处理 markdown 转义字符（`\_` → `_`、`\:` → `:`）、多余空行等
- 递归扁平化 — 适用于任意 JSON 结构，不依赖固定 schema
- Section 感知分类 — 按 system / ascend / env / config / network / weight / other 分组差异
- 智能截断 — 超长值（LS_COLORS、路径）在显示时截断，JSON 输出中保留完整值
- 多种输出格式：终端文本、HTML 报告、JSON 差异文件

**使用场景：**
- 用户要求比对两个或多个 dump 文件
- 用户需要识别环境间的配置漂移
- 用户需要验证集群一致性

**使用方法：**

```bash
# 终端文本输出（默认）
python3 compare_dumps.py env1.json env2.json

# HTML 报告
python3 compare_dumps.py env1.json env2.json --html report.html

# JSON 差异文件（用于程序化处理）
python3 compare_dumps.py env1.json env2.json --json diff.json

# 三方比对
python3 compare_dumps.py env1.json env2.json env3.json --html report.html

# 同时输出多种格式
python3 compare_dumps.py env1.json env2.json --html report.html --json diff.json
```

**命令行参数：**

| 参数 | 必选 | 说明 |
|------|------|------|
| `files`（位置参数） | 是（2+） | dump JSON 文件路径 |
| `--html PATH` | 否 | 输出 HTML 报告 |
| `--json PATH` | 否 | 输出 JSON 差异文件 |
| `--text` | 否 | 终端文本输出（默认） |

**鲁棒性设计（处理多样化 JSON 结构）：**

1. **递归扁平化**：任意 dict/list/scalar 展平为 `path→value` 对。dict 用 `.key`，list 用 `[index]`。空 `{}` 和 `[]` 保留为特殊标记。
2. **JSON 修复**：尝试直接解析 → 修复 markdown 转义 → 去空行重试。全部失败则跳过该文件并告警。
3. **Section 追踪**：记录每个顶层 section 存在于哪些文件中。报告"仅在文件 X 中存在"。
4. **类别分类**：顶层 key 映射到类别（system/ascend/env/config/network/weight/meta/other）。未知 section 归入"other"。
5. **差异类型**：每项差异分为"modified"（所有文件都有但值不同）或"only_in"（部分文件有、部分没有）。
6. **相似度评分**：`一致路径数 / 总路径数 * 100`，排除 `_meta`。

**用户指导：**
- 用 `--html` 生成人类可读报告，`--json` 用于 CI/CD 集成
- 比对不同采集方法产生的文件时（如 `msprechecker dump` vs `msprechecker_dump.py`），JSON 结构应兼容但 section 名可能略有差异
- 脚本能处理一侧有某 section 而另一侧没有的情况（如一侧有 `ping`/`hccl` 而另一侧没有）

### 1. 单机分析

给定一个 dump JSON 文件，生成结构化报告，包含：

1. **元数据** — 主机名、时间戳、采集耗时
2. **系统健康** — CPU 型号、高性能模式、内核版本、THP 状态、内存设置、虚拟化检测
3. **Ascend 组件** — driver/toolkit/atb/mindie/atb-models 版本，标记缺失组件，检查版本一致性
4. **环境变量** — 高亮关键昇腾变量，标记缺失的关键变量，检测敏感变量
5. **配置文件**（如有） — 摘要 MindIE/vLLM 配置关键值
6. **网络**（如有） — ping 结果、HCCL 连通性、Link/VNIC/TLS 状态
7. **权重**（如有） — tensor 文件数量和哈希摘要
8. **未采集项** — 主动列出未采集的 section 并建议补采命令

### 2. Dump 比对

给定两个或多个 dump JSON 文件，识别所有差异：

1. **扁平化** 每个 dump 为 path→value 对
2. **比对** 每个路径在所有文件中的值
3. **分类** 每项差异为：版本变更 / 环境变量变更 / 配置变更 / 网络变更
4. **输出** 结构化差异报告，高亮新增、删除、修改

## 分析指南

### 系统检查项

| 键 | 期望值 / 良好 | 需关注 | 问题 |
|----|-------------|--------|------|
| `high_performance` | `true` | — | `false` = CPU 未在高性能模式 |
| `virtual_machine` | `false` | `true`（已知虚拟机） | — |
| `transparent_hugepage` | `always` | `madvise` | `never` = THP 已禁用 |
| `overcommit_memory` | `0` 或 `1` | — | `2` = 严格 overcommit，可能导致 OOM |
| `page_size` | `4096` | — | 非 4096 可能存在兼容性问题 |

**THP 说明**：dump 中的值是脚本从 sysfs 文件 `[...]` 方括号中提取的字符串（如 `always`，不是 `[always]`）。

**overcommit_memory 说明**：`0` = 启发式（内核默认），`1` = 允许 overcommit，`2` = 严格。`0` 和 `1` 在 Ascend 环境中均可接受；`2` 才是真正的风险。

### Ascend 组件

#### 单组件检查

检查每个组件的 `version` 字段：
- 空 `version` 或空 `{}` 标记为 **缺失**
- driver 版本 < 24.1 对较新 NPU 功能有风险
- RC 版本（如 `26.0.rc1`、`8.2.RC1`）标记为**非生产版本** — 注明为候选发布版

关键组件：`driver`、`toolkit`、`opp_kernel`、`mindstudio_toolkit`、`atb`、`mindie`、`atb-models`

#### 版本一致性检查（跨组件）

检查各组件后，对比应保持一致的组件版本：

| 组件对比 | 期望 | 不一致影响 |
|---------|------|-----------|
| `toolkit` vs `opp_kernel` | 版本相同 | OPP 算子可能不匹配 toolkit API |
| `toolkit` vs `atb` | 同一主版本系列 | ATB 可能存在兼容性问题 |
| `toolkit` vs `mindstudio_toolkit` | 同一主版本系列 | MindStudio toolkit 可能不支持 toolkit 功能 |

提取版本系列（如从 `8.2.0.0.201 (8.2.RC1)` 提取 `8.2.RC1`）进行对比。不一致标记为 WARN。

**注意**：`driver` 版本使用不同编号方案（如 `26.0.rc1`），不应与 CANN toolkit 版本直接比较。

### 环境变量

使用 `--filter` 时仅含昇腾相关变量。否则包含全部环境变量。

#### 关键环境变量清单

始终检查以下关键变量是否存在，以表格形式报告：

| 变量 | 用途 | 缺失影响 |
|------|------|---------|
| `ASCEND_HOME_PATH` | Ascend 驱动主路径 | 工具可能找不到驱动 |
| `ASCEND_TOOLKIT_HOME` | CANN toolkit 主路径 | 工具可能使用错误路径 |
| `LD_LIBRARY_PATH` | 动态库搜索路径 | 运行时可能无法加载 Ascend 库 |
| `MINDIE_LLM_HOME_PATH` | MindIE-LLM 路径 | 找不到 MindIE（与 ascend 中 `mindie: {}` 一致） |
| `ATB_HOME_PATH` | ATB 库路径 | ATB 可能无法正确加载 |
| `ATB_SPEED_HOME_PATH` | ATB-Models 路径 | 找不到 ATB-Models（与 `atb-models: {}` 一致） |
| `HCCL_BUFFSIZE` | HCCL 通信缓冲区 | 使用默认值；多卡可能需调优 |
| `TASK_QUEUE_ENABLE` | 任务队列开关 | 使用默认值 |
| `RANK_TABLE_FILE` | Rank table 路径 | 分布式场景需要 |
| `OMP_NUM_THREADS` | OpenMP 线程数 | 可能影响 CPU 算子性能 |
| `PYTORCH_NPU_ALLOC_CONF` | PyTorch NPU 内存配置 | 使用默认分配策略 |

**交叉引用**：如果 ascend section 中 `mindie: {}` 且 `MINDIE_LLM_HOME_PATH` 缺失，判定 MindIE 可能未安装。同理适用于 `atb-models` 和 `ATB_SPEED_HOME_PATH`。

#### 敏感变量检测

未使用 `--filter` 时（全量环境变量），扫描可能包含敏感信息的变量：

| 模式 | 风险 | 操作 |
|------|------|------|
| `*_API_KEY`、`*_SECRET`、`*_TOKEN`、`*_PASSWORD` | 高 | 报告中标记，建议使用 `--filter` 或脱敏后分享 |
| `SSH_CONNECTION`、`SSH_CLIENT` | 低 | 含 IP 地址，报告中注明 |
| `LD_LIBRARY_PATH`、`PATH` | 信息 | 含系统路径，可安全分享 |

### 网络 / HCCL

- **ping**：查找 `0% packet loss`（正常）vs `100% packet loss` 或 `ping failed`（异常）
- **hccl**：hccs_ping 输出中查找 `3 received`（正常）；检查返回码 `0`
- **link**：所有设备应显示 `link status: UP`
- **vnic**：A3 板卡应配置 IP 且连接 UP
- **tls**：`tls switch[0]` 应为 `0`（禁用）— 非 TLS 部署场景

### 权重

- tensor 文件数量及 SHA256 哈希
- 比对时任意哈希差异意味着权重文件已变更

### 未采集项检测

分析末尾检查哪些可选 section 未采集，主动建议补采：

| 未采集项 | 建议命令 |
|---------|---------|
| `mies config` | `--mies-config-path <path>` |
| `user config` | `--user-config-path <path>` |
| `mindie env` | `--mindie-env-path <path>` |
| `model config` + `weight` | `--weight-dir <path>` |
| `ping` / `hccl` / `link` / `vnic` / `tls` | `--rank-table-path <path> --scene <mindie\|vllm>` |

## 输出格式

### 分析输出

结构化为 Markdown 文档，包含以下 section：

```
## 环境概要
- 主机名 / 时间戳 / 耗时 / 采集范围

## 系统健康
| 检查项 | 当前值 | 期望值 | 状态 | 说明 |
(带 OK/WARN/FAIL 标记的表格)

## Ascend 组件
### 各组件版本
| 组件 | 版本 | 时间戳/Commit | 状态 | 说明 |
### 版本一致性
| 组件对比 | 版本 A | 版本 B | 状态 |
(标记跨组件不一致)

## 环境变量
### 关键 Ascend 变量
| 变量 | 是否存在 | 值 | 状态 |
(缺失变量标记为 FAIL)
### 敏感变量
(标记匹配敏感模式的变量)
### 诊断
(关联缺失环境变量与缺失 Ascend 组件)

## 配置分析（如有）
(关键配置值及观察)

## 网络状态（如有）
(Ping / HCCL / Link / VNIC / TLS 摘要表)

## 未采集项
(列出未采集项 + 建议命令)

## 综合诊断
### 发现的问题
| 优先级 | 问题 | 建议 |
(高/中/低)
### 建议下一步
(可直接使用的命令)
```

状态标记：
- OK = 符合期望
- WARN = 需关注但不阻塞
- FAIL = 可能导致部署/运行时问题

### 比对输出

```
## 比对概要
- 文件 A: <path> (主机名, 时间戳)
- 文件 B: <path> (主机名, 时间戳)
- 差异总数: N

## 版本变更
| 组件 | 文件 A | 文件 B | 变化 |
(升级 / 降级 / 缺失)

## 环境变量变更
| 变量 | 文件 A | 文件 B | 状态 |
(新增 / 删除 / 修改)

## 系统设置变更
| 设置项 | 文件 A | 文件 B |

## 配置变更（如有）
| 配置路径 | 文件 A | 文件 B |

## 网络变更（如有）
(连通性状态变化)
```

## 比对算法

比对遵循原始 `msprechecker compare` 的逻辑：

1. **扁平化**：递归遍历每个 dump 的 JSON 树为 `{section.path: value}` 对。dict 用 `.key`，list 用 `[index]`。
2. **收集**：对每个唯一路径，收集所有文件中的值。
3. **过滤**：仅保留值不一致的路径（或在部分文件中存在但其他文件中没有的）。
4. **分类**：按顶层 section 分组差异（`system`、`ascend`、`env` 等）。
5. **报告**：输出带上下文的结构化差异。

扁平化示例：
```json
{"ascend": {"driver": {"version": "24.1"}}}
```
变为：
```
ascend.driver.version = "24.1"
```

## HTML 报告生成

当用户要求可视化报告（或生成交付物）时，使用 `references/` 中的模板生成自包含 HTML 文件。

### 单机报告

使用 `references/template_single_dump.html` 作为设计模板。模板采用与对比模板相同的深色"任务控制中心"主题，左侧带悬浮导航栏。将所有 `{{占位符}}` 替换为 dump JSON 中的实际数据。

**模板占位符：**

| 占位符 | 说明 | 示例 |
|--------|------|------|
| `{{HOSTNAME}}` | 服务器主机名 | worker-66-81 |
| `{{TIMESTAMP}}` | 采集时间戳 | 2026-07-31 11:42:09 |
| `{{DURATION}}` | 采集耗时（秒） | 0.09 |
| `{{PYTHON_VERSION}}` | Python 版本 | 3.13.5 |
| `{{COLLECTION_SCOPE}}` | 采集范围 | 基础采集 (系统+Ascend+环境变量) |
| `{{HEALTH_SCORE}}` | 健康评分 0-100 | 40 |
| `{{SCORE_COLOR}}` | 评分颜色 | var(--fail) / var(--warn) / var(--ok) |
| `{{SCORE_RING_SVG}}` | SVG 环形图代码 | `<svg>...<circle stroke-dashoffset="...">` |
| `{{NAV_ITEMS}}` | 侧边栏导航项 | 多个 `<a class="nav-item">` 标签 |
| `{{STAT_TILES}}` | 健康状态磁贴 | 多个 `.stat-tile` div |
| `{{ASSESSMENT_BANNER}}` | 整体评估横幅 | `.banner.ok/warn/fail` |
| `{{SYSTEM_HEALTH_ROWS}}` | 系统检查表行 | 多个带徽章的 `<tr>` |
| `{{COMPONENT_CARDS}}` | Ascend 组件卡片 | 多个 `.comp-card` div |
| `{{VERSION_MISMATCH_BANNER}}` | 版本不一致警告 | `.banner.warn` 或空 |
| `{{VERSION_CONSISTENCY_ROWS}}` | 版本一致性表行 | 多个 `<tr>` |
| `{{CRITICAL_ENV_ROWS}}` | 关键环境变量表行 | 多个带徽章的 `<tr>` |
| `{{SENSITIVE_VAR_BANNER}}` | 敏感变量警告 | `.banner.warn` 或空 |
| `{{ALL_ENV_ROWS}}` | 全部环境变量行（折叠） | 多个 `<tr>` |
| `{{CONFIG_SECTION}}` | 配置分析板块（条件） | 完整 `<section>` 或空 |
| `{{NETWORK_SECTION}}` | 网络状态板块（条件） | 完整 `<section>` 或空 |
| `{{WEIGHT_SECTION}}` | 权重哈希板块（条件） | 完整 `<section>` 或空 |
| `{{MISSING_ITEMS}}` | 未采集项 | 多个 `.missing-item` div |
| `{{ISSUE_ITEMS}}` | 诊断问题列表项 | 多个 `.issue-item` li 标签 |
| `{{NEXT_STEPS_CODE}}` | 建议下一步 | 含命令的 `.code-block` |
| `{{META_DATA_ROWS}}` | 原始 _meta 数据表行 | 多个 `<tr>` |
| `{{SYSTEM_DATA_ROWS}}` | 原始系统数据表行 | 多个 `<tr>` |
| `{{ASCEND_DATA_ROWS}}` | 原始 Ascend 数据表行 | 多个 `<tr>` |
| `{{ENV_KEY_DATA_ROWS}}` | 原始关键环境变量行 | 多个 `<tr>` |
| `{{ALL_ENV_DATA_ROWS}}` | 全部环境变量原始行（折叠） | 多个 `<tr>` |
| `{{RAW_DATA_SUMMARY_BANNER}}` | 原始数据摘要 | `.banner.info` |
| `{{TOOL_NAME}}` / `{{TOOL_VERSION}}` | 工具信息 | msprechecker_dump.py / 1.0.0 |
| `{{REPORT_DATE}}` / `{{REPORT_TIMESTAMP}}` | 报告时间 | 2026-08-09 |

**模板板块（按顺序）：**

1. **侧边栏** — 固定左侧导航，滚动联动高亮
2. **头部** — 主机名、时间戳、健康评分环形图（SVG）
3. **状态磁贴** — 4 个磁贴：系统状态、组件 X/7、环境变量 X/11、网络状态
4. **评估横幅** — 整体健康评估（ok/warn/fail）
5. **系统健康** — 8 项检查表格（CPU/THP/overcommit 等），OK/WARN/FAIL 徽章
6. **Ascend 组件** — 卡片网格（绿色=已安装、红色=缺失、黄色=警告）+ 版本一致性矩阵
7. **环境变量** — 关键变量表 + 敏感变量警告 + 可折叠全量列表
8. **配置分析**（条件） — MindIE/vLLM 配置关键值，仅当 `mies config`/`user config`/`mindie env` 存在时
9. **网络状态**（条件） — ping/HCCL/link/vnic/tls 摘要，仅当网络数据存在时
10. **权重**（条件） — tensor 文件数 + 哈希摘要，仅当权重数据存在时
11. **未采集项** — 虚线边框卡片，列出未采集 section 及建议命令
12. **诊断** — 按优先级排序的问题列表（高/中/低）+ 建议下一步代码块
13. **原始数据** — _meta + system + ascend + env 数据表（含说明列），可折叠全量环境变量

健康评分计算：
- 系统检查（8 项）+ Ascend 组件（7 项）+ 关键环境变量（11 项）= 总计 26 项
- 每项 OK = 1 分，WARN = 0.5 分，FAIL = 0 分
- 评分 = round(得分 / 26 * 100)
- 评分环形图 SVG：`stroke-dashoffset = 213.6 * (1 - 评分/100)`

### 对比报告

使用 `references/template_comparison.html` 作为设计模板。模板采用与单机模板相同的深色"任务控制中心"主题，左侧带悬浮导航栏。

**模板占位符（28 个）：**

| 占位符 | 说明 | 示例 |
|--------|------|------|
| `{{HOST_A}}` / `{{HOST_B}}` | 文件 A 和 B 的主机名 | worker-66-81 / worker-66-83 |
| `{{FILE_COUNT}}` | 比对文件数 | 2 |
| `{{DIFF_COUNT}}` | 差异总数 | 204 |
| `{{SIMILARITY}}` | 相似度百分比 | 6 |
| `{{IDENTICAL_COUNT}}` | 一致路径数 | 14 |
| `{{SCORE_COLOR}}` | 评分颜色 | var(--fail) / var(--warn) / var(--ok) |
| `{{SCORE_RING_SVG}}` | SVG 环形图代码 | `<svg>...<circle stroke-dashoffset="...">` |
| `{{NAV_ITEMS}}` | 侧边栏导航项 | 多个 `<a class="nav-item">` 标签 |
| `{{FILE_PAIR_CARDS}}` | 文件信息卡片含 VS 徽章 | 多个 `.file-pill` + `.vs-orb` |
| `{{STAT_TILES}}` | 分类统计磁贴 | 多个 `.stat-tile` div |
| `{{ASSESSMENT_BANNER}}` | 整体评估横幅 | `.banner.ok/warn/fail` |
| `{{SECTION_STAT_ROWS}}` | Section 统计表行 | 多个 `<tr>` |
| `{{DIFF_SECTIONS}}` | 差异详情板块（每类别一个） | 多个 `<section>` 块 |
| `{{SENSITIVE_SECTION}}` | 敏感变量板块（条件） | 完整 `<section>` 或空 |
| `{{MISSING_SECTION}}` | 未采集项板块（条件） | 完整 `<section>` 或空 |
| `{{DRIFT_BANNER}}` | 漂移评估横幅 | `.banner.ok/warn/fail` |
| `{{DRIFT_ROWS}}` | 漂移评估表行 | 多个 `<tr>` |
| `{{FINDING_BANNERS}}` | 关键发现横幅 | 多个 `.banner` div |
| `{{RAW_DATA_TABLES}}` | 原始数据对比表 | 多个子表 |
| `{{ALL_ENV_VAR_ROWS}}` | 全部环境变量行（折叠） | 多个 `<tr>` |
| `{{FILE_COL_HEADERS}}` | 表格文件列表头 | `<th>env1</th><th>env2</th>` |
| `{{ENV_TOTAL_COUNT}}` | 环境变量总数 | 191 |
| `{{RAW_DATA_SUMMARY_BANNER}}` | 原始数据摘要 | `.banner.info` |
| `{{TOOL_NAME}}` / `{{TOOL_VERSION}}` | 工具信息 | compare_dumps.py / 1.0.0 |
| `{{REPORT_DATE}}` / `{{REPORT_TIMESTAMP}}` | 报告时间 | 2026-08-09 |

**`{{DIFF_SECTIONS}}` 生成**：替换为每个有差异的类别生成一个 `<section>` 块。参见模板文件中的 HTML 注释了解确切结构。每个块包含 section 头部、可选横幅和差异表格。

相似度计算：
- 将两个 JSON 文件扁平化为 path→value 对（排除 `_meta`）
- 统计总唯一路径数
- 统计所有文件中值一致的路径数
- 相似度 = round(一致数 / 总数 * 100)

### HTML 生成规则

- 输出**自包含** HTML 文件（内联 CSS，无外部依赖，除 Google Fonts）
- **Google Fonts**：两个模板从 Google Fonts CDN 加载 Sora、DM Sans 和 JetBrains Mono 字体。离线时（Ascend 服务器防火墙环境常见）自动降级到系统字体，设计仍然可用，只是字体不同。
- **两种 HTML 生成路径**：
  - `compare_dumps.py --html` 生成**简单 HTML 报告**（浅色主题、无侧边栏）。适用于快速自动化报告。
  - AI 使用 `template_comparison.html` / `template_single_dump.html` 生成**丰富 HTML 报告**（深色任务控制中心主题、侧边栏导航、滚动联动）。适用于用户要求可视化报告或生成交付物时。
- 保存到用户输出目录
- 使用 `computer://` 链接分享文件
- 所有 CSS 变量和类名遵循模板的设计系统
- 优雅处理缺失 section（显示空状态或信息横幅）
- 比对时，如果某 section 只在一侧存在，将该 section 的所有路径报告为"仅在文件 X 中"
- 大数据量使用折叠区域（`<details>`）（全量环境变量、全部权重哈希）
- 对比模板中的 **{{DIFF_SECTIONS}}** 应替换为多个 `<section>` 块（每个差异类别一个）。参见模板中的 HTML 注释了解确切结构模式。

## Dump JSON 结构参考

参见 `references/dump_format.md` 获取完整的 JSON schema，包括所有可能的键、值类型和字段说明。

## 重要注意事项

- `_meta` 键包含采集元数据（工具版本、时间戳、主机名）— 比对逻辑中排除，但报告头部中包含。
- 如果 JSON 中缺失某 section，表示该采集器未运行（如未提供 `--rank-table-path` 则无 `ping`/`hccl`/`link`/`vnic`/`tls` section）。
- Ascend 组件为空 `{}` 表示未找到 `version.info` 文件 — 标记为缺失，并交叉引用对应环境变量以判断组件是真正未安装还是仅未配置。
- 环境变量值可能包含敏感信息（API Key、密码、令牌）— 始终扫描并标记敏感变量，建议用户使用 `--filter` 或脱敏后分享。
- 比对时，如果某 section 只在一侧存在，将该 section 的所有路径报告为"仅在文件 X 中"。
- RC（候选发布）版本应标注为非生产版本 — 报告中标记但不判为 FAIL。
