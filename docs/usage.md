# 使用说明

## 安装与启动

Ubuntu 22.04 使用 Python 3.10+，在仓库创建独立 `.venv`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gui,dev]' -c constraints/ui.txt
.venv/bin/blenderproc-ui
```

`bproc-ui` 是同一入口的简写。打开项目：`.venv/bin/blenderproc-ui --project /项目路径/scene.yaml`。UI 与实际 Blender 的依赖分别安装，不把 UI 约束强装进 `bop` 或 Blender Python。

需要可用的桌面 OpenGL。Qt 报 xcb 插件错误时，运行 `ldd .venv/lib/python3.10/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so` 检查缺项；常见系统包为 `libxcb-cursor0`、`libxkbcommon-x11-0`、`libegl1`、`libgl1`，只在确实缺少时安装。`QT_QPA_PLATFORM=offscreen` 仅适用于不建立 GL 的测试。

## 模型、单位与中心

在“资源”工作区选择导入单位，再导入一个或多个 PLY。目标必须有面，点云不能直接渲染；遮挡物支持 PLY / OBJ。每个启用目标在每张图中出现一次，对象 ID 必须是唯一正整数。

对象树和“编辑目标”只切换当前编辑对象。“设为渲染中心”单独改变相机中心。中心目标的包围盒中心位于世界原点；其他目标的包围盒中心在各自 X / Y / Z 范围内随机采样，单位是米，方向沿中心目标局部坐标轴。原始顶点和 CAD 原点保留，首版不随机旋转目标。

要停用或移除中心，先把另一个启用目标设为中心。修改模型单位会重新载入网格。“恢复建议范围”根据全部目标尺度和视野替换位置、距离设置；保留自定义范围时无需使用它。

全部启用目标需要完整入画、各有可见像素，并通过保守 AABB 不穿插检查。该检查可能拒绝实际不相交的凹模型，可调整位置范围。首版处理不透明网格、基础颜色与顶点颜色，透明表面会明确报不支持。

## 相机采样与预览

“采样”工作区设置半径 r、方位角 φ、仰角 α。φ=0 沿中心局部 +X，向 +Y 为正；α=0 水平，+90° 沿 +Z，相机始终朝向中心目标包围盒中心。

- φ=170° 到 −170° 是跨界的 20° 范围。
- 两端相等表示固定角度；勾选“全方位”才表示完整一周。
- 半径均匀采样，方向对 φ 和 sin(α) 均匀采样。
- 多区域均分场景图片总数，余数按列表顺序分配；总数不乘以目标数。

底部选择区域、机位和预览种子，再刷新候选。主视口拖动仅改变观察视角。独立“相机视角”窗口使用相同 K、分辨率和画幅；关闭后可用“视图 → 相机视角”或 F12 重开。

视口只显示几何构图，不执行 Cycles 或实际像素遮挡验收。需要看真实光照、材质和遮挡时，用下一节的方式在 Blender 中打开当前构图。网格、球面线、相机图标只在主视口显示，不会进入数据集。

默认 640×480，fx=572.4114、fy=573.57043、cx=325.2611、cy=242.04899。自动内参缩放按原尺寸比例调整 fx/fy/cx/cy；关闭后保留手动 K。仅支持零 skew 的 pinhole 相机。

## 在 Blender 中查看当前构图

“导出”工作区的“在 Blender 中打开当前构图”先用当前候选机位写出场景与相机，再让 Blender 自己的 Python 按渲染器同一套代码重建：目标与遮挡物、材质与顶点颜色、HDRI、灯光、相机内参与位姿。打开的是真实场景，不是几何线框。

- 重建要几秒，期间按钮上方显示进度；产物在项目同级的 `.blender-view/`。
- 打开后 3D 视图已切到相机视角并使用渲染着色，可以转视角、改采样、直接 F12 出图。
- `scene.blend` 与 `blender.log` 也在该目录；项目里改了机位或渲染参数，再点一次即可覆盖更新。
- 这里只用于查看：不写数据集目录，不替代 `--check-only` 与批量试渲染，在 Blender 里改的机位不会回写到项目。
- 命令行等价入口：`.venv/bin/python tools/open_blender.py --project .artifacts/demo/demo.yaml`。

## 环境、光照与遮挡

选择或扫描 Haven `.hdr` / `.exr`。关闭随机环境时使用列表选中的文件；强度与 Z 旋转仍按范围采样。HDRI 提供背景与照明，本版不导入完整房间或生成地面。

环境图可以先不选：没有 HDRI 时世界背景为中性灰 `(0.05, 0.05, 0.05)`，预览与导出都照常，`run_metadata/manifest.json` 与 `status.json` 的 `world` 记成 `{"kind": "neutral", "color": [...]}`；选定后 `world` 变成 `{"kind": "hdri", "files": [...]}`。

点光源可设置功率、距离、上半球仰角和色温范围。遮挡物在中心视线附近自动摆放，数量及相对尺寸可调，无需手写策略。

遮挡率针对**中心目标**：完整场景的可见像素与只保留该目标的完整轮廓比较，其他目标和专门遮挡物均计入，自遮挡不计入。默认 20% 请求要求无额外遮挡，其余要求 10%–50%。几何和像素拒绝共享每请求最多 30 次预算；不满足就记录并跳过，不扩大用户范围。

## GPU 与表面处理

“脚本”工作区的“Cycles 设置”默认选择 **OPTIX**，适用于本机 RTX 3060 Ti。也可选择 CUDA / HIP / ONEAPI / METAL。CPU 设备会明确禁用；请求的 GPU 后端不可用就报错，不自动回退。旧项目的 CPU 设置在载入时转换为 OPTIX，原文件不会被自动修改。已经导出的旧任务包含旧运行时，需要重新导出。

“表面处理”可以选择平直、平滑或按角度平滑（默认 30°），并启用加权法线。设置应用于目标和遮挡物，仅改变面着色与法线，保留原始几何、原点、单位和 BOP 对齐。平滑后的最终效果由终端 Blender 渲染验证，视口只用于构图。

降噪提供自动 GPU、关闭、OptiX。只有 OPTIX 渲染后端使用 OptiX GPU 降噪；CUDA 等其他后端的自动模式关闭降噪，以避免 Blender 4.2 静默使用 CPU 降噪。显式选择不兼容组合会报错。标签渲染关闭降噪，深度不经过颜色后处理。噪声阈值默认 0.01，设为 0 关闭自适应采样。

更详细的参数与验证见 [GPU 与表面处理](gpu-rendering.md)。

## 保存、导出与终端

Ctrl+S 保存 YAML，草稿允许缺少资源；“检查配置”列出具体阻塞字段。科学参数、目标 ID、单位与采样设置都保存在项目里。

“脚本”工作区填写数据集父目录和名称、Blender 目录、`bop/bin/blenderproc` 和可选 BOP toolkit 路径。Blender 目录应包含 `blender` 和 `4.2/python/bin/python3.11`；本版适配 BlenderProc 2.8.0。

选择**新的任务目录**导出。任务包括 `scene.yaml`、`run_job.py`、`render_scene.py`、`launch.json` 与两个运行时包。资源绝对路径引用，不复制大文件。普通参数可直接编辑 YAML；改变 Blender / BlenderProc 定位后应重新导出。

```bash
conda run --no-capture-output -n bop python /任务目录/run_job.py --check-only
conda run --no-capture-output -n bop python /任务目录/run_job.py --num-samples 4 --output /新的试渲染父目录
conda run --no-capture-output -n bop python /任务目录/run_job.py
```

`--output` 是父目录，最终路径为 `<父目录>/<dataset_name>`。试渲染和完整任务必须采用不同最终目录。`--format rgb_masks|bop` 可以临时切换格式，并重新检查所需依赖。UI 仅复制命令，不执行渲染。

启动器兼容 conda Python 3.8，用实际 Blender Python 3.11 预检真实依赖版本、导入位置与安装缓存。通过后直接启动指定 Blender，沿用 BlenderProc 2.8 的初始化参数，同时准确转发中断信号。渲染入口首行仍为 `import blenderproc as bproc`。

缺项会停止，不联网安装或更改缓存。需要人工准备时，在 `bop` 环境使用 `blenderproc pip install --custom-blender-path /Blender目录 ...`，确认依赖版本与本机 BlenderProc `DefaultConfig.default_pip_packages` 一致，再运行 `--check-only`。BOP 额外需要本地 toolkit 和可用 EGL；轻格式不需要 toolkit。

## 输出与运行状态

`RGB + 双掩码` 默认 RGB PNG，可选 JPEG。每帧每个目标都有二值 PNG（0 / 255）：

```text
dataset/
  rgb/000000.png
  mask/000000_000002.png
  mask/000000_000007.png
  mask_visib/000000_000002.png
  mask_visib/000000_000007.png
  run_metadata/
```

后缀是 `obj_id`。`mask` 是目标单独渲染的轮廓，`mask_visib` 是完整场景中的可见部分。轻格式不输出深度、BOP 位姿或模型。

`BOP` 默认 RGB JPEG，另有深度 PNG、相机、位姿、canonical models、双掩码和 `scene_gt_info.json`。模型坐标与位姿平移用毫米；默认深度恢复公式为 `PNG数值 × 0.1 / 1000` 米。掩码后缀是 **GT 列表索引**，例如对象 ID 2 和 7 对应索引 0 和 1。BOP 可见性使用原生 0.015 m 容差，不等同于采样验收的精确分割遮挡率。本版不生成 COCO / YOLO。

`run_metadata/status.json` 记录 requested、written_frames、saved、skipped 和失败原因；manifest 记录每帧相机、位姿、环境、光源及遮挡率。BOP 只有后处理完成才计入 saved。退出码：完成 0、部分完成 2、失败 1、SIGINT 130、SIGTERM 143。

已有任务或输出目录（包括空目录、软链接、断链）均拒绝覆盖。中断保留已有结果供检查，下次换新目录；不做断点续跑。
