# ascend-dump-analyzer

昇腾训练与推理环境采集、单份分析与多份比对。

## 分享与使用

分享整个 `ascend-dump-analyzer/` 文件夹即可。保留 `SKILL.md` 和 `references/` 中的脚本、字段说明及 HTML 模板。仓库的 `tests/ascend-dump-analyzer/` 是维护者回归测试，使用或部署 skill 时无需分发。只需在 Linux 服务器采集时，可单独复制 `references/msprechecker_dump.py`。

```bash
python3 msprechecker_dump.py --filter --no-probes \
  --target launcher=1234 --target worker-0=2345 -o snapshot.json
```

替换为现场可见的 PID。跨环境沿用相同角色名和采集选项。需要 rank table 时追加 `--rank-table-path <路径> --scene mindie`，或按实际格式选择 `vllm`。

## 输出

- **完整 HTML 报告**直接使用 [单份模板](references/template_single_dump.html) 或 [比对模板](references/template_comparison.html)，包含证据、发现和覆盖限制。
- **Markdown 摘要**保留总体结论、最重要的发现和下一步。
- 将现有 dump 或目录交给 skill，说明分析一份还是比对多份即可。历史 dump 可直接使用。
