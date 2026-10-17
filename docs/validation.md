# 验证记录

2026-10-16 在本机 Ubuntu 22.04.5 x86_64 完成。机器可读证据见 [validation-evidence.json](validation-evidence.json)，桌面截图见下方；完整运行记录保留在本机 `.artifacts/`，不纳入 Git。

![Blender 风格的原生桌面工作区：脚本页展示 GPU 后端、降噪与表面处理](images/workspace.png)

![独立相机窗口](images/camera-preview.png)

![导出页：BOP 任务目录与可复制的终端命令](images/export-bop.png)

## 已验证结果

| 检查 | 实际结果 |
|---|---|
| 单元与 Qt 测试 | **261 passed**，3.9 s（含 GPU、PLY、无 HDRI、Blender 查看、固定机位、场景/视角、目标材质与视口材质预览用例） |
| RGB + mask + mask_visib | 同场景两个目标 ID 2 / 7，4 / 4 完成，语义检查通过 |
| BOP | 4 / 4 完成；深度、K、位姿、模型毫米坐标、GT 索引和 gt_info 检查通过 |
| 无 BOP toolkit 的轻格式 | toolkit_path=None，离线预检及 4 / 4 渲染通过 |
| 目标材质 | 粗糙度 0.03 / 0.95 各渲染 1 帧：中心像素 40 → 58，`candidate.material` 写入 manifest；遮挡物未变 |
| 视口材质预览 | 同一机位、四种表面参数在插件视口内可区分（见下）：高光宽度与整体亮度各自不同 |
| 近距离 BOP | 相机半径 35 mm，深度最大 36.3 mm；后处理未被固定 0.1 m 近裁剪面截空 |
| 无法满足的布局 | 恰好 30 次碰撞拒绝；saved=0、skipped=1、partial、退出码 2 |
| 已有输出目录 | 拒绝再次运行，退出码 1；结果树 SHA256 保持一致 |
| SIGINT / SIGTERM | 两者均保存 1 张、跳过 999 张，状态 interrupted，进程退出码分别 130 / 143 |
| 核心与 Blender 投影 | 非对称 fx/fy、偏置 cx/cy；与 bpy.calc_matrix_camera 最大误差 0.0000022 px |
| 实际 GPU 相机视口 | 与 Blender 相机投影误差 0.0301 px，与实际 Blender 标记图误差 0.25 px，均小于 1 px |
| 原生桌面 GUI | 真实 OpenGL，1320×920 和 1024×768 截图；相机重开复用 canvas；切换中心、保存、两种导出通过 |
| 独立任务 | 从 `/tmp`、移除 PYTHONPATH，使用 conda Python 3.8 启动预检及渲染通过 |
| 命令与目录 | 中文、空格、单引号、字面 `$()` 路径；空目录、文件、软链接、断链保护测试通过 |
| 编译与依赖 | compileall、3.8 启动器 py_compile、pip check、git diff --check 通过 |
| GPU 设备选择 | 真实 OPTIX 渲染；日志 `以 GPU 渲染` 且 CPU 设备被关闭，未回退 CPU |
| BOP EGL 硬件校验 | 两个 EGL 上下文均读到 NVIDIA GeForce RTX 3060 Ti；软件渲染器拒绝由注入身份的用例覆盖 |
| GPU BOP 端到端 | 独立导出的 BOP 任务 4 / 4 完成，退出码 0，掩码、深度、GT 有效 |
| Blender 查看当前构图 | 后台 Blender 重建并写 `scene.blend`，另起 GUI 进程打开；相机点位与候选一致，视图为渲染着色 |
| 未选环境图的项目 | 去掉 HDRI 的同一演示项目通过校验、导出、运行：1 / 1 帧写出，退出码 0，世界背景为中性灰 0.05，不是黑屏 |
| 无 HDRI 的运行记录 | `run_metadata/manifest.json` 与 `status.json` 的 `world` 为 `{"kind":"neutral","color":[0.05,0.05,0.05]}`，候选 `hdri_path` 为 null |
| n 边形与带边 PLY | `~/文档/cores.ply` 读到 1035 顶点、1837 面，不再读成空网格 |
| 视口 HUD 比例 | 网格与坐标架按导入几何的包围盒缩放（0.1 mm 下限），复用同一组 visual 更新数据，缩放后不再残留旧坐标架 |
| 滚轮与数值 | 数值框 `StrongFocus` + 事件过滤器：未聚焦时滚轮滚动面板而不改数值；聚焦后才步进 |
| 打开既有项目 | 相机距离与模型尺度相差 1.6 倍以上时自动适配到 `0.9–1.4 ×` 适配距离，用户随后手改的值不会被再次覆盖 |
| 固定机位模式 | `camera_mode: fixed` + 2 个机位 + 2 场景 = 4 张：每场景两个视角，视角 0 恒为机位 A、视角 1 恒为机位 B，r/φ/α 与设定完全一致 |
| 场景内摆放固定、机位随机 | `scene_count: 2`、`views_per_scene: 3`、1 个区域：6 张全部 saved，同场景三帧的 `placements` 与 `light` 完全一致，`azimuth_deg` 分别为 −22.0° / 137.4° / 154.7°，退出码 0 |
| 无遮挡物时的遮挡要求 | 修掉空遮挡物清单下“ratio 永远到不了下限”的死循环：5 / 5 帧写出，`rejections.jsonl` 为空 |
| 机位距离随模型缩放 | `fit_radius_m` 与归一化包围盒成正比；`suggest_ranges` 不再把 1 mm 模型放到 5 cm 之外 |

测试演示由本地 Blender 生成 HDRI，并使用偏置 CAD 原点的 mm / cm 目标及 m 遮挡物；单元测试另覆盖三种单位。演示是合成网格，尚未使用用户自己的生产模型和 Haven 资源批量验证。

## GPU 验收（2026-10-17）

任务要求：渲染只用 GPU，并保证 BOP 那套独立的 EGL 后处理也不会落到软件光栅化上。两件事分开检查，因为 Cycles 设备和 pyrender 的 EGL 上下文没有共用状态。

规则与参数说明见 [GPU 与表面处理](gpu-rendering.md)。

| 检查 | 实际结果 |
|---|---|
| Cycles 设备 | `GPU rendering: OPTIX — NVIDIA GeForce RTX 3060 Ti; CPU devices disabled`；初始化与每个渲染 pass 前复检 |
| OptiX 降噪 | 本机支持并实际渲染；CUDA + OptiX 降噪在 Blender 4.2 会走 CPU，已被禁止 |
| BOP EGL 探测 | 直接用原生 `_pyrender_init` 函数体建两个上下文：`NVIDIA Corporation / NVIDIA GeForce RTX 3060 Ti/PCIe/SSE2 / 4.1.0 NVIDIA 580.178.04`，深度像素 121 / 1122 |
| GPU RGB 任务 | 独立导出的 rgb_masks 任务 4 / 4 完成，退出码 0 |
| GPU BOP 任务 | 独立导出的 BOP 任务 4 / 4 完成，退出码 0；深度 0–6336（0.1 mm 单位）、mask 57–78 像素、mask_visib 24–60 像素、`visib_fract` 与 GT 一致 |
| 软件渲染器拒绝 | 单元测试注入 llvmpipe / softpipe / swrast / lavapipe / SwiftShader / GDI Generic 与缺失身份，全部拒绝；本机 NVIDIA 驱动不接受 Mesa 软件覆盖，因此没有真实 llvmpipe 上下文的拒绝实测 |

证据：`.artifacts/gpu-probe/`（EGL 探测、Cycles 设备、OptiX 降噪）、`.artifacts/gpu-e2e/`（两个独立任务与日志）、`.artifacts/gpu-quality*/`（表面处理对照）。`tests/blender/check_render_quality.py` 对同一场景比对平直 / 平滑 / 按角度平滑的渲染差异。上方截图均为 GPU 版界面，在真实桌面 OpenGL 会话中捕获。

## Blender 查看（2026-10-17）
目标：导入的文档模型在视口里不显示时，不必等终端试渲染才能判断建模与光照。做法是后台用 Blender 自身的 Python 重建场景并保存 `.blend`，再另起一个 Blender 打开它；打开文件也会跳过启动画面，否则画面被 splash 挡住。

| 检查 | 实际结果 |
|---|---|
| 后台重建 | `--background` 进程跑通，日志为 OptiX 设备选择结果，`scene.blend` 写入成功 |
| 视图设置 | `viewport: rendered camera view in 1 area(s)`；相机视角 + 渲染着色，无启动画面 |
| UI 链路 | 导出工作区按钮走线程池重建后再开窗，成功/失败都更新面板文字 |
| 失败路径 | 构建失败抛 `Blender 构建场景失败（退出码 N）` 并带日志尾部；脚本异常不再关掉 GUI 窗口 |
| 缺少 HDRI | `str(None)` 不再进入 `images.load`；没有环境图时断开环境贴图连线、背景改中性灰，面板追加提示 |

![Blender 中的当前构图](images/blender-view.png)

图中的场景来自演示项目 `.artifacts/demo/demo.yaml`：相机、灯光、三个遮挡物与两个目标都在对象树里，机位是 UI 当前选中的候选。命令行的等价入口是 `tools/open_blender.py`。

## 视口材质预览（2026-10-17）

目标：调材质时不用渲染、也不用开 Blender 就能判断粗糙度与金属度。视口把采样到的 `MaterialState` 映射到 vispy 的 Phong 着色：粗糙度决定高光指数（0.05 → 220，0.9 → 2），金属度压暗漫反射与环境项并让高光带上基色，基色进入每一项。

| 检查 | 实际结果 |
|---|---|
| 四种状态可区分 | `r=0.05/m=0`、`r=0.9/m=0`、`r=0.05/m=1`、`r=0.9/m=1` 同一机位渲染，高光宽度与整体亮度各异（下图） |
| 参数映射 | `phong_params` / `specular_strength` / `shininess` 单测覆盖单调性、边界夹取与基色传递 |
| 只作用于目标 | 遮挡物走原有平灰着色，不接受材质采样 |
| 真机证据 | `tools/gui_smoke.py` 截图：视口内目标按 `#c8ccd0` 基色 + 采样粗糙度着色 |

![视口内的四种材质状态](images/material-preview.png)

从左到右、从上到下：光亮电介质、粗糙电介质、抛光金属、粗糙金属。视口没有环境反射，金属会比 Blender 里更暗，最终以渲染为准。

## 环境

| 部分 | 版本 |
|---|---|
| UI Python | 3.10.12，独立 `.venv` |
| PySide6 / VisPy | 6.8.3 / 0.15.2 |
| NumPy / trimesh | 1.26.4 / 4.12.2 |
| PyYAML / Pillow | 6.0.3 / 11.3.0 |
| pytest / pytest-qt | 8.4.2 / 4.5.0 |
| conda bop Python | 3.8.20 |
| BlenderProc / Blender | 2.8.0 / 4.2.1（内部 Python 3.11.7） |

桌面依赖仅安装进仓库 `.venv`。没有向原 `bop`、Blender Python 或原 `dataset_rendering` 项目安装依赖。渲染使用已有的离线依赖和本地 BOP toolkit。CPU 路径已被产品逻辑禁用，实际渲染只走 GPU；AMD / Intel / Apple 后端有选项和启动检查，但没有对应硬件可测。

## 复验

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QT_API=pyside6 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -p pytestqt.plugin tests/unit tests/ui -q

.venv/bin/python tools/create_demo.py .artifacts/demo-new
.venv/bin/python tools/gui_smoke.py \
  --project .artifacts/demo-new/demo.yaml --output .artifacts/gui-new

# GPU 端到端：导出独立任务并直接渲染（两个格式各跑一次）
env -u PYTHONPATH .venv/bin/python tools/render_demo.py \
  --project .artifacts/demo/demo.yaml --destination .artifacts/e2e/rgb \
  --format rgb_masks --num-samples 4 --run
env -u PYTHONPATH .venv/bin/python tools/render_demo.py \
  --project .artifacts/demo/demo.yaml --destination .artifacts/e2e/bop \
  --format bop --num-samples 4 --run
```

`render_demo.py` 拒绝已存在的任务目录，输出 `任务目录` 与 `运行命令`；`--device` 可覆盖后端，不传时沿用项目设置（`CPU` 在载入时迁移为 OPTIX）。BOP 运行后看日志里两行 `BOP EGL renderer:` 确认没落到软件光栅器。

本机 shell 的 ROS PYTHONPATH 会自动加载无关 pytest 插件，测试命令显式隔离它；没有为解决此问题安装 ROS 依赖。GUI smoke 必须在真实桌面运行，不能使用 Qt offscreen。

在 UI 导出新任务后，复制其命令先 `--check-only`，再以 4 张小样本和独立输出父目录试渲染。只读检查器可用于实际结果：

```bash
.venv/bin/python tests/blender/check_rgb_masks.py /数据集路径
.venv/bin/python tests/blender/check_bop.py /BOP数据集路径
.venv/bin/python tests/blender/verify_runtime.py /任务目录
.venv/bin/python tests/blender/check_failures.py \
  --project .artifacts/demo/demo.yaml --destination .artifacts/acceptance-new
```

投影证据分别由 `tools/render_projection_fixture.py`（通过 BlenderProc 执行）和 `tools/check_viewport_projection.py`（真实 Qt OpenGL）生成；命令参数见各工具 `--help`。渲染入口必须在已通过离线预检的环境执行。

## 实施与审查修复

独立审查发现并已复验关闭四项问题：大整数种子导致 Qt 溢出、修改其他字段造成微小数值精度丢失、点云未在导出前拒绝、fork 后处理进程继承父级信号处理导致无法终止。回归测试分别覆盖 2**72 种子、微米级范围、导出后资源被替换，以及真实 fork 初始化阻塞后的终止。

真实中断测试还暴露了 BlenderProc 原生进度线程与 bpy 中途抛异常的问题。后端避开其进度线程，并将中断延迟到当前原生渲染调用结束，再完成已接受帧的提交与后处理。因此 Ctrl+C 可能需要等待当前渲染 pass 结束；不会声称立即取消，也不会把未完成的 BOP 后处理计为成功。

实现使用原生协作工具并行落实模块，接口稳定后集成验证。初始依赖下载期间先实现后补齐测试，没有执行计划中每一项“先红后绿”的历史命令；提交按模块合并，不伪造逐任务红测试或逐任务提交记录。

## 当前边界

视口是几何预览；真实光照和遮挡依赖终端 Blender 渲染。首版保守 AABB 检查可能拒绝可行的凹模型布局，且不自动随机旋转目标。仅支持不透明面网格和基础颜色；不导入完整三维背景、不做 YOLO / COCO 转换、断点续跑或渲染队列。BOP 适配器针对 2.8.0 的原生接口，升级 BlenderProc 后需重新验证。
