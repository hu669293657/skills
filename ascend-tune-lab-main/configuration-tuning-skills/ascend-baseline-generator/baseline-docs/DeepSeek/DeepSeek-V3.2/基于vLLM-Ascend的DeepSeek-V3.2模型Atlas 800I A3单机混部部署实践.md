​**作者**​：昇腾实战派
​**知识地图**​：[https://blog.csdn.net/Lumos\_Lovegood/article/details/161601003](https://blog.csdn.net/Lumos_Lovegood/article/details/161601003)

## 背景概述

本文档将介绍基于vLLM-Ascend的DeepSeek-V3.2模型在Atlas 800I A3上的单机混部部署实践，包括支持的特性、特性配置、环境信息以及性能测试典型case

## 基本信息

| 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
| --- | --- | --- | --- | --- |
| 0.18.0 | NPU: Atlas 800I A3-560T, HBM 128G  <br>CPU: Kunpeng 920 (80核-2900MHz)  <br>内存: 32根*64G* 5200MHz  <br>OS: OpenEuler 22.03 LTS-SP4 | Atlas 800I A3单机 | 8 | W8A8 |

## 服务化配置

### 低时延/高吞吐

```bash
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=10
export HCCL_OP_EXPANSION_MODE="AIV"
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libjemalloc.so.2:$LD_PRELOAD
export VLLM_USE_V1=1
export HCCL_BUFFSIZE=256
export ASCEND_AGGREGATE_ENABLE=1
export ASCEND_TRANSPORT_PRINT=1
export ACL_OP_INIT_MODE=1
export ASCEND_A3_ENABLE=1
export VLLM_NIXL_ABORT_REQUEST_TIMEOUT=300000
export TASK_QUEUE_ENABLE=1
export VLLM_ASCEND_ENABLE_MLAPO=1
export VLLM_ASCEND_ENABLE_FLASHCOMM1=1

vllm serve /mnt/share/weights/DeepSeek-V3.2-W8A8 \
    --port 8003 \
    --data-parallel-size 2 \
    --tensor-parallel-size 8 \
    --seed 1024 \
    --served-model-name dsv3 \
    --max-model-len 67000 \
    --max-num-batched-tokens 4096 \
    --max-num-seqs 8 \
    --trust-remote-code \
    --quantization ascend \
    --async-scheduling \
    --no-enable-prefix-caching \
    --enable-expert-parallel \
    --gpu-memory-utilization 0.95 \
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY", "cudagraph_capture_sizes":[1,2,4,8,16,24,32,40,48]}' \
    --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp"}' \
    --tokenizer-mode deepseek_v32 \
    --reasoning-parser deepseek_v3
```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) | 
| --- | --- | --- | --- | --- | --- | --- | --- | 
| 16384 | 1024 | MLA：DP2+TP8 | 67000 | 0 | 4 | 1 | 0 | 
| 16384 | 1024 | MLA：DP2+TP8 | 67000 | 0 | 16 | 4 | 0.5 | 
| 32768 | 512 | MLA：DP2+TP8 | 67000 | 0 | 4 | 1 | 0 | 
| 32768 | 512 | MLA：DP2+TP8 | 67000 | 0 | 8 | 2 | 0.2 | 
| 65536 | 1024 | MLA：DP2+TP8 | 67000 | 0 | 4 | 1 | 0 | 
| 65536 | 1024 | MLA：DP2+TP8 | 67000 | 0 | 8 | 2 | 1 | 
| 2048 | 2048 | MLA：DP2+TP8 | 8000 | 0 | 4 | 1 | 0 | 
| 2048 | 2048 | MLA：DP2+TP8 | 8000 | 0 | 16 | 4 | 0 | 
| 3500 | 1500 | MLA：DP2+TP8 | 8000 | 0 | 4 | 1 | 0 | 
| 3500 | 1500 | MLA：DP2+TP8 | 8000 | 0 | 16 | 4 | 0 |

### 测试命令

参考aisbench官方测试指南。

[aisbench测试命令](https://gitee.com/aisbench/benchmark)

[vllm-ascend社区官网](https://docs.vllm.ai/projects/ascend/en/latest/)

## 特别声明

1. 以上配置均未开启Prefix Cache，若实际生产环境需要使用该特性，参考vLLM-Ascend社区参数指南开启--enable-prefix-caching

