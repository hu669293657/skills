​**作者**​：昇腾实战派
​**知识地图**​：[https://blog.csdn.net/Lumos\_Lovegood/article/details/161601003](https://blog.csdn.net/Lumos_Lovegood/article/details/161601003)

## 背景概述

本文档将介绍基于vLLM-Ascend的GLM-5.1模型在Atlas 800I A3上的单机混部部署实践，包括支持的特性、特性配置、环境信息以及性能测试典型case。

## 基本信息

| 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
| --- | --- | --- | --- | --- |
| 0.18.0 | NPU: Atlas 800I A3-560T, HBM 128G  <br>CPU: Kunpeng 920 (80核-2900MHz)  <br>内存: 32根*64G* 5200MHz  <br>OS: OpenEuler 22.03 LTS-SP4 | Atlas 800I A3单机 | 8 | W8A8C16 |

## 服务化配置

### 低时延/高吞吐

```bash
export HCCL_OP_EXPANSION_MODE="AIV"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=1
export HCCL_BUFFSIZE=200
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export VLLM_ASCEND_BALANCE_SCHEDULING=1
export VLLM_ASCEND_ENABLE_MLAPO=1

vllm serve /mnt/weight/GLM-new-w8a8 \
--host 0.0.0.0 \
--port 8077 \
--data-parallel-size 1 \
--tensor-parallel-size 16 \
--enable-expert-parallel \
--seed 1024 \
--served-model-name glm-5 \
--max-num-seqs 48 \
--max-model-len 20480 \
--max-num-batched-tokens 4096 \
--trust-remote-code \
--gpu-memory-utilization 0.95 \
--quantization ascend \
--enable-chunked-prefill \
--no-enable-prefix-caching \
--async-scheduling \
--additional-config '{"enable_npugraph_ex": true,"fuse_muls_add":true,"multistream_overlap_shared_expert":true}' \
--compilation-config '{"cudagraph_mode": "FULL_DECODE_ONLY"}' \
--speculative-config '{"num_speculative_tokens": 3, "method": "deepseek_mtp"}'
```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) |
| --- | --- | --- | --- | --- | --- | --- | --- | 
| 2048 | 2048 | MLA：DP1+TP16 | 20480 | 0 | 16 | 4 | 0 | 
| 3500 | 1500 | MLA：DP1+TP16 | 20480 | 0 | 96 | 24 | 0 | 
| 3500 | 1500 | MLA：DP1+TP16 | 20480 | 0 | 16 | 4 | 0 | 
| 16384 | 1024 | MLA：DP1+TP16 | 20480 | 0 | 20 | 5 | 0 | 
| 16384 | 1024 | MLA：DP1+TP16 | 20480 | 0 | 4 | 1 | 0 | 
| 2048 | 2048 | MLA：DP2+TP8 | 20480 | 0 | 144 | 36 | 0 | 
| 32768 | 512 | MLA：DP2+TP8 | 33792 | 0 | 8 | 2 | 0 | 
| 32768 | 512 | MLA：DP2+TP8 | 33792 | 0 | 4 | 1 | 0 |

### 测试命令

参考aisbench官方测试指南。

[aisbench测试命令](https://gitee.com/aisbench/benchmark)

[vllm-ascend社区官网](https://docs.vllm.ai/projects/ascend/en/latest/)

## 特别声明

1. 以上配置均未开启Prefix Cache，若实际生产环境需要使用该特性，参考vLLM-Ascend社区参数指南开启--enable-prefix-caching

