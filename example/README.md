# 示例素材（非默认运行目录）

- `jobs/render_job/`：历史导出任务，250 场景 × 4 视角，配置请求 1000 张；不代表已完成 1000 张渲染。
- `renders/preview/dataset/`：已完成的 1 场景 × 4 视角 RGB/完整掩码/可见掩码及运行元数据，saved=4、skipped=0。

以上从仓库原根目录 `jobs/`、`renders/` 迁移，保留历史配置及运行元数据原样。模型、HDRI、Blender/conda 位置仍是原机器的绝对路径，不包含外部资源，不保证其他机器直接运行。

请通过应用配置本机资源并重新导出任务到根目录 `jobs/`，生成结果到根目录 `renders/`（这两个运行目录已忽略）。不要将输出写回此示例目录。

从仓库根目录可只读检查示例结果：

```bash
.venv/bin/python tests/blender/check_rgb_masks.py example/renders/preview/dataset
.venv/bin/python tests/blender/verify_runtime.py example/jobs/render_job --static
```

环境配置、示例图片与详细操作见根目录 [README](../README.md)。
