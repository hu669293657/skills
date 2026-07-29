​**作者**​：昇腾实战派
​**知识地图**​：[https://blog.csdn.net/Lumos\_Lovegood/article/details/161601003](https://blog.csdn.net/Lumos_Lovegood/article/details/161601003)

## 背景概述

本文档将介绍基于vLLM-Ascend的Qwen3.5-397B模型在Atlas 800I A2上的单机混部部署实践，包括支持的特性、特性配置、环境信息以及性能测试典型case。

## 基本信息

| 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
| --- | --- | --- | --- | --- |
| 0.18.0 | NPU：Atlas 800I A2-280T, HBM 64G<br>CPU：Kunpeng 920（48核-2600MHz）<br>内存：24根*32G*3200MHZ<br>OS：Ubuntu 22.04 LTS | Atlas 800I A2单机 | 8 | W4A8C16 |

## 服务化配置

### 低时延/高吞吐

```bash
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 
export PYTORCH_NPU_ALLOC_CONF="expandable_segments:True"
export HCCL_IF_IP=xxx 
export HCCL_OP_EXPANSION_MODE="AIV"
export HCCL_BUFFSIZE=1024
export OMP_NUM_THREADS=1
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libjemalloc.so.2:$LD_PRELOAD
export TASK_QUEUE_ENABLE=1
export VLLM_ASCEND_ENABLE_FUSED_MC2=0
export VLLM_ASCEND_ENABLE_FLASHCOMM1=1

vllm serve /home/Qwen3.5-397B-A17B-w4a8-mtp \
    --served-model-name "qwen35-397b-zz" \
    --host xxx \
    --port 10888 \
    --data-parallel-size 1 \
    --tensor-parallel-size 8 \
    --max-model-len 133120 \
    --max-num-batched-tokens 16384 \
    --max-num-seqs 128 \
    --gpu-memory-utilization 0.9 \
    --enable-expert-parallel \
    --compilation-config '{"cudagraph_capture_sizes":[1,4,8,12,16,20,24,28,32,36,40,44,48,52,56,60,64,68,72,76,80,84,88,92,96,100,108,112,128,160,172,196,200,212,232,256,260,288,320,360,400], "cudagraph_mode":"FULL_DECODE_ONLY"}' \
    --speculative_config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
    --trust-remote-code \
    --no-enable-prefix-caching \
    --async-scheduling \
    --allowed-local-media-path / \
    --quantization ascend \
    --mm-processor-cache-gb 0 \
    --additional-config '{"enable_cpu_binding":true, "multistream_overlap_shared_expert": true}'

```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2048 | 2048 | MLA：DP1+TP8 | 262144 | 0 | 288 | 72 | 0 | 
| 2048 | 2048 | MLA：DP1+TP8 | 262144 | 0 | 56 | 14 | 0 | 
| 3500 | 1500 | MLA：DP1+TP8 | 262144 | 0 | 256 | 64 | 0 |
| 3500 | 1500 | MLA：DP1+TP8 | 262144 | 0 | 48 | 12 | 0 | 
| 16384 | 1024 | MLA：DP1+TP8 | 262144 | 0 | 80 | 20 | 0 |
| 16384 | 1024 | MLA：DP1+TP8 | 262144 | 0 | 20 | 5 | 0 |
| 32768 | 512 | MLA：DP1+TP8 | 262144 | 0 | 32 | 8 | 0 |
| 32768 | 512 | MLA：DP1+TP8 | 262144 | 0 | 12 | 3 | 0 |
| 65536 | 1024 | MLA：DP1+TP8 | 262144 | 0 | 28 | 7 | 0 |
| 65536 | 1024 | MLA：DP1+TP8| 262144 | 0 | 8 | 2 | 0 |
| 131072 | 1024 | MLA：DP1+TP8 | 262144 | 0 | 16 | 4 | 0 |
| 131072 | 1024 | MLA：DP1+TP8 | 262144 | 0 | 4 | 1 | 0 |

### 测试命令

参考aisbench官方测试指南。

[aisbench测试命令](https://gitee.com/aisbench/benchmark)

[vllm-ascend社区官网](https://docs.vllm.ai/projects/ascend/en/latest/)

## 特别声明

1. 以上配置均未开启Prefix Cache，若实际生产环境需要使用该特性，参考vLLM-Ascend社区参数指南开启--enable-prefix-caching

