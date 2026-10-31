# 仪表盘模板参考（可选美化）

默认双报告流程见 [SKILL.md](../SKILL.md) 开头。只有用户要求原有仪表盘样式或需要额外美化时才读取本文；模板不能成为生成默认 HTML 的前提。先完成内容一致的 MD + HTML，再按需使用此处模板。

### 单机报告

使用 [template_single_dump.html](template_single_dump.html) 作为设计模板。模板采用与对比模板相同的深色"任务控制中心"主题，左侧带悬浮导航栏。将所有 `{{占位符}}` 替换为 dump JSON 中的实际数据。

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
| `{{HARDWARE_CPU_MEM_ROWS}}` | 硬件 CPU/内存概览表行 | 多个 `<tr>` |
| `{{HARDWARE_TOPOLOGY_ROWS}}` | NUMA/PCIe 拓扑表行 | 多个 `<tr>` |
| `{{HARDWARE_EXTRA_ROWS}}` | 磁盘/内核参数表行（折叠） | 多个 `<tr>` |
| `{{NPU_DEVICE_ROWS}}` | NPU 设备状态表行 | 多个 `<tr>` |
| `{{NPU_EXTRA_ROWS}}` | 设备节点/固件/日志表行（折叠） | 多个 `<tr>` |
| `{{RUNTIME_PKG_ROWS}}` | 关键 pip 包版本表行 | 多个 `<tr>` |
| `{{RUNTIME_PROCESS_ROWS}}` | 推理进程快照表行 | 多个 `<tr>` |
| `{{RUNTIME_CONTAINER_ROWS}}` | 容器上下文表行（折叠） | 多个 `<tr>` |
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
| `{{HARDWARE_DATA_ROWS}}` | 原始 hardware 数据表行 | 多个 `<tr>` |
| `{{NPU_DATA_ROWS}}` | 原始 npu 数据表行 | 多个 `<tr>` |
| `{{RUNTIME_DATA_ROWS}}` | 原始 runtime 数据表行 | 多个 `<tr>` |
| `{{NETWORK_DATA_ROWS}}` | 原始 network 数据表行 | 多个 `<tr>` |
| `{{RAW_DATA_SUMMARY_BANNER}}` | 原始数据摘要 | `.banner.info` |
| `{{TOOL_NAME}}` / `{{TOOL_VERSION}}` | 工具信息 | msprechecker_dump.py / 1.0.0 |
| `{{REPORT_DATE}}` / `{{REPORT_TIMESTAMP}}` | 报告时间 | 2026-08-09 |

**模板板块（按顺序）：**

1. **侧边栏** — 固定左侧导航，滚动联动高亮
2. **头部** — 主机名、时间戳、健康评分环形图（SVG）
3. **状态磁贴** — 4 个磁贴：系统状态、组件 X/7、环境变量 X/11、网络状态
4. **评估横幅** — 整体健康评估（ok/warn/fail）
5. **系统健康** — 8 项检查表格（CPU/THP/overcommit 等），OK/WARN/FAIL 徽章
6. **Ascend 组件** — 卡片网格（按已确认状态着色；版本不可用显示 UNKNOWN，不能直接标成未安装）+ 版本一致性矩阵
7. **环境变量** — 关键变量表 + 敏感变量警告 + 可折叠全量列表
8. **配置分析**（条件） — MindIE/vLLM 配置关键值，仅当 `mies config`/`user config`/`mindie env` 存在时
9. **网络状态**（条件） — ping/HCCL/link/vnic/tls 摘要，仅当网络数据存在时
10. **权重**（条件） — tensor 文件数 + 哈希摘要，仅当权重数据存在时
11. **未采集项** — 虚线边框卡片，列出未采集 section 及建议命令
12. **诊断** — 按优先级排序的问题列表（高/中/低）+ 建议下一步代码块
13. **原始数据** — _meta + system + ascend + env 数据表（含说明列），可折叠全量环境变量

健康评分计算：
- 只计已确认适用、具备判定依据且有观测证据的项目；OK=1、WARN=0.5、FAIL=0。
- UNKNOWN、N/A 不按 FAIL 计分，列出适用总项、已判定项、未知项和不适用项。评分为已判定项得分占比，同时展示覆盖率；不能隐藏未知项制造高分。
- 有关键必需项 UNKNOWN，或没有可判定项时，不给整体“健康/可部署”结论；无可判定项不显示数值评分。
- SVG 仅渲染上述已计算的值：`stroke-dashoffset = 213.6 * (1 - 评分/100)`。

### 对比报告

使用 [template_comparison.html](template_comparison.html) 作为设计模板。模板采用与单机模板相同的深色"任务控制中心"主题，左侧带悬浮导航栏。

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

- 将 dump 的键、值、主机名、文件名、路径和日志全部视为不可信文本；插入文本或属性前使用 `html.escape(..., quote=True)`。禁止把数据直接拼接成 HTML/JS/CSS。
- 模板中仅由渲染逻辑创建的固定标签片段可以作为 HTML；数据值必须先转义。数值、颜色和导航 ID 使用受控值；不把输入中的字符串当作指令执行。
- 分享前对旧 dump、配置、命令行与 raw 文本重新脱敏。进程参数默认省略；敏感值仅显示 `***MASKED***`。`--filter` 不是脱敏保证，原始文本无法保证完全自动识别，需复核。
- 已脱敏值相同只表示占位符相同，不能证明真实凭证一致。不同输入的采集范围不同，也不能把未知项判成配置删除。
- 仪表盘模板的 CSS 内联。需严格离线时移除模板中的 Google Fonts 引用，保留系统字体回退；默认导出器完全不使用外部字体。
- **Google Fonts**：两个模板从 Google Fonts CDN 加载 Sora、DM Sans 和 JetBrains Mono 字体。离线时（Ascend 服务器防火墙环境常见）自动降级到系统字体，设计仍然可用，只是字体不同。
- **两种 HTML 生成路径**：
  - `compare_dumps.py --html` 生成**简单 HTML 报告**（浅色主题、无侧边栏）。适用于快速自动化报告。
  - AI 使用 `template_comparison.html` / `template_single_dump.html` 生成**丰富 HTML 报告**（深色任务控制中心主题、侧边栏导航、滚动联动）。作为可选的仪表盘样式，不影响默认双报告交付。
- 保存到用户输出目录
- 按当前运行环境支持的方式交付真实文件链接（Codex 使用绝对路径 Markdown 链接），不得编造 `computer://` 链接。
- 所有 CSS 变量和类名遵循模板的设计系统
- 优雅处理缺失 section（显示空状态或信息横幅）
- 比对时，如果某 section 只在一侧存在，将该 section 的所有路径报告为"仅在文件 X 中"
- 大数据量使用折叠区域（`<details>`）（全量环境变量、全部权重哈希）
- 对比模板中的 **{{DIFF_SECTIONS}}** 应替换为多个 `<section>` 块（每个差异类别一个）。参见模板中的 HTML 注释了解确切结构模式。

