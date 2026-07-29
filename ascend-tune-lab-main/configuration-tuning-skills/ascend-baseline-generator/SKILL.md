---
name: ascend-baseline-generator
description: >-
  Finds the best vLLM-Ascend deployment config (low-latency or high-throughput)
  from baseline markdown docs by matching device, model, quantization, NPU count,
  and deploy strategy, then adjusts context length by input/output sequence length.
  Use when the user asks to find or generate vLLM baseline configs, reproduce
  Ascend deployment baselines, or provides an MD config file with 基本参数.
---

# ascend-baseline-generator — vLLM-Ascend 基线配置生成

根据 MD 配置文件中的设备/模型标识字段（device_type、model_name、quantization、num_npus、deploy_strategy），
从本 skill 目录下的 `baseline-docs/` 部署文档中查找匹配项。只有所有 5 个标识字段都匹配时，才根据输入输出长度自动匹配最佳配置行并替换上下文长度。

## 参数

用户提供 MD 配置文件路径（相对或绝对路径）：
- 从用户消息中提取 MD 配置文件路径
- 如果未提供参数 → 报错并提示用法：`用法: ascend-baseline-generator <md配置文件路径>`
- 如果文件不存在或无法解析 → 报错并提示用法

### MD 配置文件格式

配置文件为一个 Markdown 文件：**必须** 含 `## 基本参数`（7 项字段）；**可选** 含 `## 服务化配置` bash 代码块（用于覆盖模型路径 / host / port）：

```markdown
# 部署配置

## 基本参数

- 输入长度: 4096
- 输出长度: 1024
- 设备类型: A3
- 模型名称: Qwen3.5-27B
- 量化格式: w8a8
- NPU卡数: 2
- 部署策略: 单机混部

## 服务化配置

```bash
export VLLM_USE_MODELSCOPE=True
...
vllm serve ... \
    --max-model-len 133000 \
    ...
```
```

各参数行的键值对应关系：

| 参数字段 | MD 键名 | 示例值 |
|---------|--------|-------|
| `device_type` | 设备类型 | A3 |
| `model_name` | 模型名称 | Qwen3.5-27B |
| `quantization` | 量化格式 | w8a8 |
| `num_npus` | NPU卡数 | 2 |
| `deploy_strategy` | 部署策略 | 单机混部 |
| `input_seq_len` | 输入长度 | 4096 |
| `output_seq_len` | 输出长度 | 1024 |

参考示例：[config.example.md](config.example.md)（`## 服务化配置` 整节可省略。）

### 基线配置目录结构

部署文档位于 `baseline-docs/` 目录下（相对于本 skill 目录），按模型厂家/版本分层组织：

```
baseline-docs/
├── DeepSeek/DeepSeek-V3.2/
│   └── 基于vLLM-Ascend的DeepSeek-V3.2模型Atlas 800I A3单机混部部署实践.md
├── GLM/GLM-5.1/
│   └── 基于vLLM-Ascend的GLM-5.1模型Atlas 800I A3单机混部部署实践.md
├── MiniMax/MiniMax-M2.5/
│   └── 基于vLLM-Ascend的MiniMax-M2.5模型Atlas 800I A3单机混部部署实践.md
└── Qwen/
    ├── Qwen3.5-122B/（含 A2、A3 两份文档）
    ├── Qwen3.5-27B/（含 A2、A3 两份文档）
    └── Qwen3.5-397B/（含 A2、A3 两份文档）
```

文件名命名规范：`基于vLLM-Ascend的{模型名}模型Atlas {设备名}单机混部部署实践.md`

## 工作流程

### Step 1: 解析 MD 配置文件

1. 获取配置文件路径：
   - 从用户消息中提取 MD 配置文件路径（相对路径或绝对路径）
   - 如果未提供路径，报错并提示：`用法: ascend-baseline-generator <md配置文件路径>`
2. 使用 Bash 检查文件是否存在（`test -f`），不存在则报错「配置文件 [路径] 不存在，请检查路径」
3. 使用 Read 工具读取 MD 配置文件内容
4. 解析「基本参数」列表，提取 7 个字段：
   - `- 输入长度: (\d+)` → `input_seq_len`
   - `- 输出长度: (\d+)` → `output_seq_len`
   - `- 设备类型: (.+)` → `device_type`
   - `- 模型名称: (.+)` → `model_name`
   - `- 量化格式: (.+)` → `quantization`
   - `- NPU卡数: (\d+)` → `num_npus`
   - `- 部署策略: (.+)` → `deploy_strategy`
5. 如果缺少任一字段或值无效，报错并提示完整的格式要求
6. 记录这 7 个值供后续步骤使用

### Step 2: 查找并读取 MD 文档文件

- 使用 Glob 工具递归查找 `configuration-tuning-skills/ascend-baseline-generator/baseline-docs/` 目录下所有 `*.md` 文件
  - 查找模式：`configuration-tuning-skills/ascend-baseline-generator/baseline-docs/**/*.md`
  - **排除**配置 MD 文件本身（即 Step 1 读入的那个文件），避免混淆
- 如果没有找到任何 `.md` 文档文件，报错「baseline-docs/ 目录下未找到 .md 文档文件」
- 使用 Read 工具读取所有找到的 `.md` 文档文件内容

### Step 3: 从 MD 文档文件提取 5 个标识字段

对每个 MD 文档文件，从文件路径、文件名、硬件信息表和文档正文中提取以下字段：

#### 1. model_name（模型名称）
按以下优先级提取：
1. **从文件路径的目录名提取**：文档位于 `baseline-docs/<Family>/<ModelName>/xxx.md`，取第二级目录名作为模型名
2. **从文件名提取**：`基于vLLM-Ascend的{ModelName}模型Atlas...` 中的 `{ModelName}`
3. **从文件正文提取**：`本文档将介绍基于vLLM-Ascend的{ModelName}模型在...`
4. 匹配模式：`Qwen[\w.-]+`、`DeepSeek[\w.-]+`、`GLM[\w.-]+`、`MiniMax[\w.-]+`、`Llama[\w.-]+` 等常见模型名

#### 2. device_type（设备类型）
1. **优先从硬件信息表**（`| NPU:` 或 `| NPU：` 或 `| 设备信息` 所在的行）中提取设备描述
   - 注意：A2 文档可能使用全角冒号 `NPU：`
   - 匹配 `Atlas\s+\d+\s*[A-Za-z0-9]*` 模式
2. **其次从文件名提取**：`基于vLLM-Ascend的{ModelName}模型Atlas {Device}单机混部部署实践.md`
3. **最后从文档正文** `## 背景概述` 段落提取
4. 记录完整的设备描述字符串（如 `Atlas 800I A3`、`Atlas 800I A2`）

#### 3. quantization（量化格式）
- 优先从硬件信息表中 `数据格式` 列提取（如 `W8A8C16`）
- 其次从 vllm 命令的模型路径中提取（如 `-w8a8-`）
- 归一化为小写用于比较

#### 4. num_npus（NPU 卡数 / 总卡数）
- 从硬件信息表中提取「总卡数」列的值
- 信息表可能位于以下任一章节下：
  - `## 背景概述`（A3 文档）
  - `## 基本信息`（A2 文档）
- 表格格式示例：
  ```
  | 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
  ```
- 定位到包含「总卡数」的表头列，取对应列的第一个数值行
- 如果找不到「总卡数」列或无法解析，则该字段提取失败，标记为不匹配

#### 5. deploy_strategy（部署策略）
1. **优先从文件名提取**：文件名包含 `单机混部部署` → `单机混部`
2. **其次从文档正文提取**：`## 背景概述` 段落中的部署描述、`## 基本信息` 段落
3. 匹配关键词：`单机混部` > `双机混部` > `PD分离` > `单机` > `多机` > `混部`
4. 取最长匹配的部署策略描述

### Step 4: 匹配标识字段

对每个 MD 文档文件，将提取到的 5 个字段与配置 MD 文件中的值进行逐一比对：

| 字段 | 匹配规则 | 示例 |
|------|---------|------|
| `device_type` | 配置值出现在文档的设备描述中（不区分大小写），如 "A3" 可匹配 "Atlas 800I A3" | 配置: `"A3"` → 文档: `"Atlas 800I A3"` ✅ |
| `model_name` | 配置值出现在文档的文件名或标题中（不区分大小写） | 配置: `"Qwen3.5-27B"` → 文件名: `"Qwen3.5-27B"` ✅ |
| `quantization` | 归一化为小写后，一方包含另一方，如 "w8a8" 可匹配 "w8a8c16" | 配置: `"w8a8"` → 文档: `"W8A8C16"` ✅ |
| `num_npus` | 配置的数值必须等于文档硬件信息表中「总卡数」列的值 | 配置: `2` → 文档: `总卡数=2` ✅ |
| `deploy_strategy` | 配置值出现在文档的文件名或标题中 | 配置: `"单机混部"` → 文件名: `"单机混部部署"` ✅ |

**如果 5 个字段全部匹配：**
- 输出：「✅ 文档 [文件名] 标识匹配成功：[device_type] / [model_name] / [quantization] / [num_npus]卡 / [deploy_strategy]」
- 进入 Step 4A，根据 deploy_strategy 进入不同分支

**如果有任一字段不匹配：**
- 输出：「❌ 文档 [文件名] 标识不匹配：XXX字段（配置值=[...], 文档值=[...]）」
- 跳过该 MD 文档文件，不参与后续匹配
- 继续处理下一个 MD 文档文件

### Step 4A: 根据部署策略分支

根据配置 MD 文件中的 `deploy_strategy` 判断走哪个流程：

- **如果 deploy_strategy = "单机混部"**：
  - 输出：「➡️ 部署策略为单机混部，进入低时延/高吞吐配置匹配流程」
  - 进入 Step 5，继续匹配输入输出长度

- **如果 deploy_strategy = "双机混部"**：
  - 输出：「⏳ 双机混部配置匹配流程待补充，暂不支持」
  - 跳过该 MD 文档文件，继续处理下一个

- **如果 deploy_strategy = "PD分离"**：
  - 输出：「⏳ PD分离配置匹配流程待补充，暂不支持」
  - 跳过该 MD 文档文件，继续处理下一个

- **其他 deploy_strategy**：
  - 输出：「⚠️ 未知部署策略：[deploy_strategy]，暂不支持」
  - 跳过该 MD 文档文件，继续处理下一个

### Step 5: 解析配置节

仅对标识字段全部匹配通过的 MD 文档文件，识别标题（`##` 或 `###`）和代码块，提取配置节。

根据文档的不同格式分两种情况处理：

#### 情况 A：低时延/高吞吐分离的章节（A3 文档）
- **低时延节**: `### 低时延` 或 `### 低延迟`
  - 包含一个 ````bash` 代码块（vLLM 启动命令和配置）
  - 包含一个 Markdown 表格（`#### 典型测试用例` 下的表格）
  - 表格列：平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率
- **高吞吐节**: `### 高吞吐` 或 `### 高并发`
  - 结构同上

#### 情况 B：低时延/高吞吐合并的章节（A2 文档）
- 标题为 `### 低时延/高吞吐` 或 `### 低时延/高并发`
- 只有一个 ````bash` 代码块和一个 `#### 典型测试用例` 表格
- 此配置同时适用于低时延和高吞吐场景
- 解析时将该表格同时作为低时延节和高吞吐节的匹配候选

### Step 6: 表格行匹配

对每个配置节的表格，按以下逻辑匹配最佳行：

1. **筛选条件**: 行的"平均输入" ≥ 配置 MD 中的 input_seq_len AND "平均输出" ≥ 配置 MD 中的 output_seq_len
2. **评分公式**: `(平均输入 - input_seq_len)² + (平均输出 - output_seq_len)² × 0.8`
   - 分数越低越匹配
3. 每个配置节选一个得分最低的行作为该节的最佳匹配
4. 记录匹配行的：输入长度、输出长度、上下文长度、最大并发数

### Step 7: 处理匹配结果

**情况 A — 只有一个配置节有匹配：**
直接选择该配置，跳到 Step 8。

**情况 B — 两个配置节均有匹配（低时延 + 高吞吐）：**
向用户展示对比信息并询问选择：

| 维度 | 低时延 | 高吞吐 |
|------|--------|--------|
| 上下文长度 | N | M |
| 推荐并发数 | A | B |
| 应用场景 | 延迟敏感 | 高并发批量 |

给出推荐倾向（但不是强制决定）：
- 如果 高吞吐并发数 / 低时延并发数 ≥ 3 → 倾向推荐 **高吞吐**
- 否则倾向推荐 **低时延**

注意：当文档为合并章节（情况 B）时，低时延和高吞吐的匹配结果相同，推荐低时延。

等待用户确认或选择具体方向。

**情况 C — 两个配置节均无匹配：**
提示用户「该文档的配置中未找到能容纳输入=X 输出=Y 的配置行，请减小输入/输出长度或尝试其他配置文档」

### Step 8: 替换配置中的上下文长度

从选中的配置块中，找到 `--max-model-len <旧值>`，替换为匹配行的"上下文长度"列的值。

注意:
- 仅替换 `vllm serve` 命令中的 `--max-model-len` 参数
- export 类环境变量中的其他值保持不变
- 保持配置块的其他参数不变

### Step 9: 替换用户自定义参数

在输出最终配置前，检查配置 MD 文件的「服务化配置」代码块中是否有用户提供的参数，如果有则替换到匹配到的配置中：

1. 从配置 MD 文件（Step 1 读取的文件）中提取「服务化配置」bash 代码块的内容
2. 从该代码块中提取以下三个参数值：
   - **模型权重路径**: `vllm serve` 后面的第一个参数（路径或模型名）
   - **IP 地址**: `--host` 参数的值
   - **端口号**: `--port` 参数的值
3. **如果三个参数都提取成功**（即用户提供了自定义配置）：
   - 将匹配到的配置 bash 代码块中的对应值替换：
     - `vllm serve <原路径>` → `vllm serve <用户路径>`
     - `--host <原IP>` → `--host <用户IP>`
     - `--port <原端口>` → `--port <用户端口>`
   - 输出：「🔄 已应用用户自定义配置：模型路径、IP、端口」
4. **如果缺少任一参数**（用户未提供完整自定义配置）：
   - 保持匹配到的配置不变
   - 输出：「ℹ️ 未检测到用户自定义配置，使用文档默认参数」
5. 进入 Step 10 输出最终配置

### Step 10: 输出最终配置

以清晰友好的格式输出：

```
════════════════════════════════════════════
  🎯 最终配置 ([低时延/高吞吐])
  📏 上下文长度: [匹配值]
  📊 输入: [N] → 输出: [M]
  🔄 推荐并发: [并发数]
  🖥️  设备: [device_type]
  🧠 模型: [model_name]
  🔢 量化: [quantization] | NPU: [num_npus] | 策略: [deploy_strategy]
════════════════════════════════════════════

```bash
[完整的 bash 配置命令]
```
```

## 边界处理

- **所有标识字段不匹配**: 如果没有任何 MD 文档的 5 个标识字段能匹配上，提示用户「baseline-docs/ 目录下未找到与设备/模型信息匹配的配置文档，请检查配置 MD 文件中的 device_type、model_name、quantization、num_npus、deploy_strategy」
- **无匹配表格行**: 如果标识字段匹配但表格中没有能容纳输入/输出长度的行，提示用户「未找到能容纳输入=X 输出=Y 的配置行，请减小输入/输出长度」
- **未提供参数**: 提示 "用法: ascend-baseline-generator <md配置文件路径>"
- **MD 配置文件不存在**: 提示 "配置文件 [路径] 不存在，请检查路径"
- **配置格式错误**: 提示 "配置文件格式错误，请确保包含「基本参数」列表（输入长度、输出长度、设备类型、模型名称、量化格式、NPU卡数、部署策略）"
- **无可解析表格**: 提示 "未找到包含标准配置表格的章节"
- **无 MD 文档文件**: 提示 "baseline-docs/ 目录下未找到 .md 文档文件"

## 示例

**用户输入:**
`使用 ascend-baseline-generator 处理 config.example.md`

**执行过程:**
1. 读取 `config.example.md`，从基本参数列表解析到：device_type="A3", model_name="Qwen3.5-27B", quantization="w8a8", num_npus=2, deploy_strategy="单机混部", input_seq_len=4096, output_seq_len=1024
2. 搜索 `baseline-docs/**/*.md`，读取所有部署文档
3. 根据文件路径 `baseline-docs/Qwen/Qwen3.5-27B/xxx.md` 得到 model_name="Qwen3.5-27B"；从文件名提取 device_type="Atlas 800I A3"、deploy_strategy="单机混部"
4. 从硬件信息表提取 quantization="w8a8c16"、num_npus=2
5. 5 个字段全部匹配 ✅
6. 部署策略为"单机混部"，进入低时延/高吞吐配置匹配流程
7. 在两个配置节的表格中按评分公式匹配最佳行
8. 替换上下文长度
9. 检测到 config 中有自定义配置，替换模型路径、IP、端口
10. 输出最终配置
