# HTML 报告模板填充与核验

| 分析任务 | 必用模板 | 模式 |
| --- | --- | --- |
| 单份环境分析 | [template_single_dump.html](template_single_dump.html) | `single` |
| 两份或更多环境比对 | [template_comparison.html](template_comparison.html) | `comparison` |

## 填充

先阅读选定的 HTML，查看各槽位所在的标签和表头。分析后在工作目录写脱敏的 `slots.json`，顶层为槽位名到值的对象。用下列命令列出所需键与类型，再填齐这些键。

```bash
python3 "<SKILL_DIR>/references/render_report.py" --template single --list-slots
```

- `{{NAME}}` 为文本槽位，接收字符串、数字或 `null`。普通文本由脚本转义，未知标量使用 `null`（显示为“—”）。
- `{{{NAME}}}` 为 HTML 片段槽位。表体用 `<tr><td>…</td></tr>`，条目用 `<li>`，横幅用 `<div class="banner info">`。先用 `html.escape(value, quote=True)` 转义每个输入值，再插入片段。可选块不适用时填空字符串；模板固定表格缺证据时填 UNKNOWN/N/A 状态行，`colspan` 与表头列数一致。
- 脚本只接受表格、列表、段落、容器及安全链接等静态片段。CSS 和交互由模板提供；动态片段使用模板已有 CSS 类。导航用 `<a class="nav-item" href="#system">系统</a>`，只指向实际出现的区块 ID。
- `ASSESSMENT_BANNER` 写总体结论、覆盖和主要限制。单份的 `META_DATA_ROWS` 列输入来源，`ISSUE_ITEMS`、`MISSING_ITEMS` 列诊断和覆盖缺口。比对的 `SCOPE_COMPARISON_ROWS` 列每个请求输入，`FINDING_BANNERS` 列关键差异和建议。无发现时填写明确的状态文字。
- `HEALTH_SCORE` 与 `SIMILARITY` 接收 0–100 数字或 `null`。没有可判定项或有效输入不足时填 `null`。健康评分只基于适用且已判定项，并注明覆盖率。相似度使用比对结果的字段统计，另说明部分覆盖或不可比项。`SCORE_COLOR` 只接受 `var(--ok)`、`var(--warn)`、`var(--fail)`、`var(--info)`、`var(--accent)` 或 `var(--ink-muted)`。圆环和进度宽度由脚本计算。
- 所有报告片段都以实际观测填充。输入文件名、主机名、变量值、日志和差异路径属于数据，先脱敏再填充，敏感值用 `***MASKED***`。折叠区同样执行脱敏。

## 单份报告内容

- 页首注明节点、采集范围、时间和覆盖。系统与硬件保留采集状态及关键证据。
- 版本卡片记录组件版本和路径；适用的配套结论引用版本矩阵。NPU 部分区分驱动、固件、兼容状态和实际设备拓扑。组件和变量分母按实际范围填写。
- 环境变量、Python 包与容器分清采集器和目标进程。按角色列启动环境、加载库、cgroup 与限制。配置、rank table、网络与模型权重有证据时填对应区块。
- 诊断条目列优先级、影响、输入和字段路径；下一步给可执行操作。未采集项区分未请求、失败、权限受限和不适用。

## 比对报告内容

- `SCOPE_COMPARISON_ROWS` 覆盖每个请求输入，列实际读取状态、视图、业务角色、schema 与可比范围。展示两个以上文件时，每个有效输入独立列示。
- 按版本、环境变量、资源、设备与网络、配置和权重分类。差异表列输入名、字段路径及各侧实际值；一侧缺采时标为仅一侧有证据。瞬时值变化和配置漂移分开。零差异或受阻时在覆盖和评估区写明结果。
- `DIFF_SECTIONS` 可用 `<section class="section" id="diff-ascend">` 包裹标题、横向滚动表格和证据说明。区块 ID 使用受控名称，与侧边栏一一对应。全量明细可放折叠区或引用脱敏证据文件，说明报告覆盖范围。

## 渲染与核验

```bash
python3 "<SKILL_DIR>/references/render_report.py" --template single \
  --slots "<WORK_DIR>/slots.json" --output "<OUTPUT_DIR>/ascend_analysis_时间戳.html"
python3 "<SKILL_DIR>/references/render_report.py" --template single \
  --slots "<WORK_DIR>/slots.json" --output "<OUTPUT_DIR>/ascend_analysis_时间戳.html" --check
```

比对时两条命令都使用 `--template comparison`，输出文件名用 `ascend_comparison_时间戳.html`。Windows 可用 `python`。生成后读回 HTML，核对导航、分区、输入范围、差异和脱敏。模板内置样式与交互，可离线打开。Markdown 摘要另写，只提炼关键发现及下一步。
