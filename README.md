# BlenderProc 数据集工作区

Ubuntu 22.04 本地桌面工具：导入多个 PLY 目标与遮挡物，选择渲染中心，用球坐标设定相机范围、HDRI、光照和实际像素遮挡率，导出脚本后在 conda `bop` 中运行。环境图可以先空着，渲染会退回中性灰世界背景。

界面采用 Blender 风格的灰色编辑器、对象树、三维视口与独立相机窗口。UI 负责配置和几何构图预览；要看真实光照与材质，可在 Blender 中打开当前构图；数据集渲染仍在终端完成。目标物体的表面参数（粗糙度、金属度、高光、可选统一基色）按场景随机，遮挡物不受影响。

```bash
cd /home/kepler/rm_2027/blenderproc-ui
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gui,dev]' -c constraints/ui.txt
.venv/bin/blenderproc-ui
```

已有项目可用 `.venv/bin/blenderproc-ui --project /绝对路径/scene.yaml` 打开。UI 依赖使用独立环境，不安装进原 conda `bop`。

- 多目标同场景随机布局，编辑选择与渲染中心独立。
- 多个球坐标区域，支持固定角度、跨 ±180° 和完整一周。
- 固定机位：把 r/φ/α 钉成单值，每张都用同一台相机；与随机区域各自计数，导出时固定机位在前。
- “适配距离”按模型真实包围盒重算机位距离，毫米级零件不会被放到半米外。
- Haven `.hdr` / `.exr` 环境；明确 m / cm / mm 单位并保留模型原点。
- 默认 OptiX GPU 渲染，禁止 CPU 回退；支持平滑着色、按角度平滑、加权法线和 GPU 降噪。
- 输出 **RGB + mask + mask_visib** 或 **BOP**，YOLO 标签由外部工具转换。
- 新任务与输出目录拒绝覆盖；有限重试，部分完成如实记录并返回非零退出码。
- 导出工作区可用当前候选构图在 Blender 中打开真实场景，转视角、改参数、F12 出图，不写数据集目录。

导出的任务包含配置、启动器和渲染运行时，关闭 UI 后仍可运行。资源采用绝对路径引用，保留原文件位置。

```bash
conda run --no-capture-output -n bop python /任务目录/run_job.py --check-only
conda run --no-capture-output -n bop python /任务目录/run_job.py --num-samples 4 --output /新的试渲染父目录
conda run --no-capture-output -n bop python /任务目录/run_job.py
```

详细操作见 [使用说明](docs/usage.md)、[GPU 与表面处理](docs/gpu-rendering.md)，实际测试与限制见 [验证记录](docs/validation.md)。设计和完整计划位于 `docs/superpowers/`。
