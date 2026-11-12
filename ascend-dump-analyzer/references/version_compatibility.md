# 版本配套检查

## 每份 dump 都检查

```bash
python3 "<SKILL_DIR>/references/check_compatibility.py" "<dump.json>" \
  --scene auto --output "<WORK_DIR>/compatibility.json"
```

已确认业务时用 `torch`、`vllm`、`mindspeed`、`modelink` 或 `mindie`。自动模式根据包证据推断，多个业务栈同时存在时要求指定。对于包在其他解释器、源码部署、存在多个安装版本或需补宿主机 HDK 的场景，用 `--observed <证据.json>` 补充实际观测。每份文件及角色独立核对，按同一角色比较。

```json
{
  "mindspeed_llm": {"version": "26.2.0", "source": "业务源码仓库的发布标签"},
  "hdk": {"version": "26.1.1", "source": "宿主机 HDK 安装记录"}
}
```

只填写现场证据。采集器 Python 和包版本只代表该解释器；对目标进程的结论以确认过的运行证据为准。输出中的 `observed` 包含来源及多版本冲突；`software.checks` 给出整行配套结果；`boards` 与 `host` 独立判断。工作文件中的路径和版本证据进入报告前照常脱敏。

## 规则与口径

| 场景 | 检查口径 |
| --- | --- |
| PyTorch | 按 `torch_npu` 安装包号查 PyTorch、CANN 和适用 Python 范围。产品号另列，保留 RC、post、dev 和构建后缀。通用 CANN 兼容范围和单个发布包基线分别记录。 |
| vLLM Ascend | 按插件的精确发布号查同一行的上游 vLLM、CANN、PyTorch、TorchNPU、Python。存在 Triton Ascend 证据时一并检查。源码主线、特定硬件变体或自行构建包按对应说明核对。 |
| MindSpeed LLM | 按套件发布号查推荐组合。已采到 Megatron、Triton Ascend、FSDPTurbo 时对照该行。另显示 CANN 和 TorchNPU 产品号的单组件支持范围；两个单项命中仍需核对整套组合。 |
| 历史 ModelLink / MindIE | ModelLink 核对同代 TorchNPU、MindSpeed、CANN 和 Python；MindIE 按已收录版本查 CANN、PyTorch 及安装包。其他版本按发布说明补核。 |
| HDK、驱动、固件、OS | 逐卡看 board 的 `Compatibility`。成功回显中的 `NOK` 是明确不兼容；`OK` 只确认该卡驱动和固件。HDK 版本须来自宿主机证据，按 NPU 产品型号、CANN、OS 版本和架构查官方硬件适配表。容器镜像的 OS 不作为宿主机 OS。 |

报告用 **符合配套**（`matched`）、**偏离基线**（`deviation`）、**明确不兼容**（`incompatible`）、**待核验**（`unknown`）。偏离发布基线时先核对特定版本和部署文档；查不到规则、字段缺失、安装候选冲突或官方来源互相矛盾时，列出待补证据。报告结论限于已核对的版本与范围。

点组件卡片可固定该组件的已采集版本，弹窗用表格列出同一场景下其他组件的当前版本和推荐版本。整体建议可以调整场景主组件。检查器的 `recommendations` 按需要改动的已核实版本数升序选择完整配套行。同等改动数时优先覆盖更多已核实版本，再优先保留主组件版本。推荐行须覆盖已确认偏离的组件。驱动、固件和 OS 配套按设备型号和硬件适配表核对。

## 数据来源

[离线矩阵](compatibility_matrix.json) 将参考资料的版本事实与检查器分开，按来源维护。变更时记录发布来源、适用场景和整行约束。未覆盖的版本按官方资料补核。

- [TorchNPU 官方配套表](https://gitcode.com/Ascend/pytorch/blob/master/COMPATIBILITY.md) · [昇腾软件下载与配套查询](https://www.hiascend.com/zh/developer/download/community)
- [vLLM Ascend 发布矩阵](https://docs.vllm.ai/projects/ascend/en/v0.30.0/community/versioning_policy.html#release-compatibility-matrix) · [安装验证基线](https://docs.vllm.ai/projects/ascend/en/v0.30.0/getting_started/installation.html)
- [MindSpeed LLM 发布配套与兼容表](https://gitcode.com/Ascend/MindSpeed-LLM/blob/master/docs/zh/release_notes_llm.md) · [MindIE 安装配套](https://github.com/Ascend/MindIE-LLM/blob/master/docs/zh/user_guide/install/source/preparing_software_and_dependencies.md)
- [驱动固件查询字段](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/920beta2/softwareinst/instg/instg_0064.html) · [昇腾硬件驱动与固件](https://www.hiascend.com/hardware/firmware-drivers/commercial)
