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

- `{{NAME}}` 为文本槽位，接收字符串、数字或 `null`。脚本转义普通文本，`null` 渲染为空，隐藏页首对应字段。
- `{{{NAME}}}` 为 HTML 片段槽位。表体用 `<tr><td>…</td></tr>`，条目用 `<li>`，横幅用 `<div class="banner info">`。先用 `html.escape(value, quote=True)` 转义每个输入值。没有已采证据的表体与可选块填空字符串。
- 脚本只接受表格、列表、段落、容器及安全链接等静态片段。CSS 和交互由模板提供；动态片段使用模板已有 CSS 类。导航用 `<a class="nav-item" href="#system">系统</a>`，只指向实际出现的区块 ID。
- `ASSESSMENT_BANNER` 写已判定结论。`ISSUE_ITEMS` 或 `FINDING_BANNERS` 列有实测依据的主要发现，单份的 `NEXT_STEPS_CODE` 写相应操作。无可判定项时，总览只写“暂无可判定结论”，具体原因与补采操作放在「未采集项」。
- 「未采集项」逐项列环境或角色、字段或输入、状态与原因，必要时附补采方法。单份填 `MISSING_ITEMS`，无缺口时填“无”。比对有缺口时在 `MISSING_SECTION` 填一个 `<section class="section" id="missing">` 区块，节名为「未采集项」，并添加对应导航。仅一侧采到的字段在此注明有证据的一侧。
- 「Ascend 组件」内的 `COMPATIBILITY_ROWS` 按 [配套规则](version_compatibility.md) 填检查项、实测、要求、结论和依据，比对增加环境列。依据使用规则名称、编号或证据字段。版本已采到但查无适用规则时可在此写“待核验”。`VERSION_MISMATCH_BANNER` 用于逐卡驱动和固件告警等需要人工补充的结论。
- `COMPATIBILITY_ENVIRONMENTS` 为可选 JSON 数组。每项写 `name`（环境或角色）、`scene`（检查器结果中的场景）与 `versions`（检查器 `observed` 中已核实且有值的 `version`，如 `{"vllm_ascend":"0.30.0","cann":"9.2.0"}`）。每个环境或角色独立填一项。渲染器用这些字段生成可点击的版本卡片、偏离清单和最少改动建议。`COMPONENT_CARDS` 用于补充矩阵以外的观测，避免重复卡片。
- 说明使用简短中文。字段路径和英文产品名保留原样。原始元数据中如有旧版采集脚本版本，仅省略脚本自身版本字段；保留数据格式版本与组件版本。
- `HEALTH_SCORE` 与 `SIMILARITY` 接收 0–100 数字或 `null`。没有可判定项或有效输入不足时填 `null`。健康评分只基于适用且已判定项。相似度和差异计数只纳入可比字段，覆盖缺口在「未采集项」说明。`SCORE_COLOR` 只接受 `var(--ok)`、`var(--warn)`、`var(--fail)`、`var(--info)`、`var(--accent)` 或 `var(--ink-muted)`。圆环和进度宽度由脚本计算。
- 所有报告片段都以实际观测填充。输入文件名、主机名、变量值、日志和差异路径属于数据，先脱敏再填充，敏感值用 `***MASKED***`。折叠区同样执行脱敏。

## 单份报告内容

- 页首注明已知的节点、采集范围和时间。系统与硬件表保留实测值及关键证据。
- 组件卡片记录实测组件版本和路径。驱动、固件、HDK 的配套判定也放在「Ascend 组件」，NPU 运行态显示设备拓扑和运行状态。组件和变量分母按实际范围填写。
- 环境变量、Python 包与容器分清采集器和目标进程。按角色列启动环境、加载库、cgroup 与限制。配置、rank table、网络与模型权重有证据时填对应区块。
- 诊断条目列优先级、影响、输入和字段路径。未采集项区分未请求、失败、权限受限和不适用。

## 比对报告内容

- `SCOPE_COMPARISON_ROWS` 列有效输入的已知视图、业务角色、schema 与可比范围。展示两个以上文件时，每个有效输入独立列示。
- 按版本、环境变量、资源、设备与网络、配置和权重分类。差异表列输入名、字段路径及各侧实际值。瞬时值变化和配置漂移分开。完成有效比对且零差异时写明结果。
- `DIFF_SECTIONS` 可用 `<section class="section" id="diff-ascend">` 包裹标题、横向滚动表格和证据说明。区块 ID 使用受控名称，与侧边栏一一对应。全量明细可放折叠区或引用脱敏证据文件，说明报告覆盖范围。

## 渲染与核验

```bash
python3 "<SKILL_DIR>/references/render_report.py" --template single \
  --slots "<WORK_DIR>/slots.json" --output "<OUTPUT_DIR>/ascend_analysis_时间戳.html"
python3 "<SKILL_DIR>/references/render_report.py" --template single \
  --slots "<WORK_DIR>/slots.json" --output "<OUTPUT_DIR>/ascend_analysis_时间戳.html" --check
```

比对时两条命令都使用 `--template comparison`，输出文件名用 `ascend_comparison_时间戳.html`。Windows 可用 `python`。生成后读回 HTML，核对缺采内容只出现在「未采集项」，配套结论与卡片只出现在「Ascend 组件」，并检查导航、差异和脱敏。配套结果已嵌入 HTML，可离线打开。
