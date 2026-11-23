# 版本配套检查

## 每份 dump 都检查

```bash
python3 "<SKILL_DIR>/references/check_compatibility.py" "<dump.json>" \
  --scene auto --output "<WORK_DIR>/compatibility.json"
```

已确认业务时用 `torch`、`vllm`、`mindspeed`、`modelink` 或 `mindie`。自动模式根据包证据推断，多个业务栈同时存在时按业务角色分别运行。对于包在其他解释器、源码部署、存在多个安装版本或需补宿主机 HDK 的场景，用 `--observed <证据.json>` 补充实际观测。每份文件及角色独立核对，按同一角色比较。

```json
{
  "mindspeed_llm": {"version": "26.2.0", "source": "业务源码仓库的发布标签"},
  "hdk": {"version": "26.1.1", "source": "宿主机 HDK 安装记录"},
  "torchnpu_release": {"version": "26.2.0", "source": "业务部署使用的 TorchNPU 发行记录"}
}
```

只填写现场证据。采集器 Python 和包版本只代表该解释器。目标进程用已确认的运行证据和对应视图评估，无法确认的版本按待核验处理。用 `--scope <范围.json>` 补充已确认的范围，例如 `{"hardware":"A2","hardware_variant":"训练","cann_edition":"community"}`。字段见[清单约定](compatibility_inventory.md)。自动识别要求所有可见设备的 `name` 都精确匹配到同一系列。型号未映射、身份查询失败或混用系列时，按已核实型号及范围分别检查。混用环境的范围结论只适用于指定系列。

## 解读结果

| 字段 | 可回答的问题 |
| --- | --- |
| `observed` | 采到了哪些版本、来自哪里、是否有多个候选。 |
| `software`、`component_support`、`recommendations` | 所选业务发布基线的差异与最少改动建议。 |
| `boards`、`host` | 逐卡观测到的驱动固件状态、当前宿主机和业务基线证据。 |
| `catalog.checks` | 在 `catalog.scope` 范围内，CANN/HDK、TorchNPU 安装包、已采 CANN 子包、适用的平台和组件组合，以及 vLLM Ascend 或 MindSpeed LLM 依赖配置的逐项结果。 |

每项按 `status`、`reason`、`evidence`、`missing` 和适用时的 `differences` 解读。报告注明快照日期、范围与来源字段或规则 ID。范围以 `catalog.scope_sources` 追溯。工作文件中的路径和版本证据进入报告前照常脱敏。

## 规则与口径

| 场景 | 检查口径 |
| --- | --- |
| PyTorch | 按 `torch_npu` 安装包号查 PyTorch、CANN 和适用 Python 范围。产品号另列，保留 RC、post、dev 和构建后缀。通用 CANN 兼容范围和单个发布包基线分别记录。 |
| vLLM Ascend | 按插件的精确发布号查同一行的上游 vLLM、CANN、PyTorch、TorchNPU、Python。存在 Triton Ascend 证据时一并检查。源码主线、特定硬件变体或自行构建包按对应说明核对。 |
| MindSpeed LLM | 按套件发布号查推荐组合。已采到 Megatron、Triton Ascend、FSDPTurbo 时对照该行。另显示 CANN 和 TorchNPU 产品号的单组件支持范围；两个单项命中仍需核对整套组合。 |
| 历史 ModelLink / MindIE | ModelLink 核对同代 TorchNPU、MindSpeed、CANN 和 Python；MindIE 按已收录版本查 CANN、PyTorch 及安装包。其他版本按发布说明补核。 |
| HDK、驱动、固件、OS | 逐卡看 board 的 `Compatibility`。成功回显中的 `NOK` 是明确不兼容，`OK` 的结论限于该卡驱动和固件。HDK 发行号来自宿主机证据。用 `catalog.checks` 核对硬件系列下的 CANN/HDK 发布关系，用实际 NPU 型号、宿主机 OS、版本和架构对应的条目补核部署条件。容器镜像的 OS 只回答容器条件。 |

业务基线结果使用 **符合配套**（`matched`）、**偏离基线**（`deviation`）和 **待核验**（`unknown`）。知识快照结果按[状态约定](compatibility_inventory.md#结果)输出，单独标记 **仅有候选**（`candidate`）、**明确不配套**（`incompatible`）和 **不适用**（`not_applicable`）。同一环境可同时命中硬件发布配套和不同的业务镜像基线，分别列出规则适用范围。对同一范围的冲突依据，标作待核验并列双方规则。明确平台限制时，把相关建议标为业务基线参考，部署建议须先满足该平台约束。

点组件卡片可固定该组件的已采集版本，弹窗用表格列出同一场景下其他组件的当前版本和推荐版本。整体建议可以调整场景主组件。检查器的 `recommendations` 按需要改动的已核实版本数升序选择完整配套行。同等改动数时优先覆盖更多已核实版本，再优先保留主组件版本。推荐行须覆盖已确认偏离的组件。驱动、固件和 OS 配套按设备型号和硬件适配表核对。

## 数据来源

[版本配套快照](ascend-compatibility.json) 来自随本次整合提供的 `ascend-compatibility` 1.0.0（2026-11-23），由检查器按需读取。规则含硬件发布、查询候选、安装包、工作负载、下载与参考条目。快照的 `coverage` 记录查询覆盖和限制。行级引用使用规则 ID、类型、原有标题及适用范围。

[业务基线矩阵](compatibility_matrix.json) 提供历史版本、发布基线和最少改动计算。更新两类资料时保留发布来源、日期、适用范围和整行约束。快照未覆盖的条件按任务所需复核。

- [TorchNPU 官方配套表](https://gitcode.com/Ascend/pytorch/blob/master/COMPATIBILITY.md) · [昇腾软件下载与配套查询](https://www.hiascend.com/zh/developer/download/community)
- [vLLM Ascend 发布矩阵](https://docs.vllm.ai/projects/ascend/en/v0.30.0/community/versioning_policy.html#release-compatibility-matrix) · [安装验证基线](https://docs.vllm.ai/projects/ascend/en/v0.30.0/getting_started/installation.html)
- [MindSpeed LLM 发布配套与兼容表](https://gitcode.com/Ascend/MindSpeed-LLM/blob/master/docs/zh/release_notes_llm.md) · [MindIE 安装配套](https://github.com/Ascend/MindIE-LLM/blob/master/docs/zh/user_guide/install/source/preparing_software_and_dependencies.md)
- [驱动固件查询字段](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/920beta2/softwareinst/instg/instg_0064.html) · [昇腾硬件驱动与固件](https://www.hiascend.com/hardware/firmware-drivers/commercial)
