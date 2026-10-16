# 验证记录

2026-10-16 在本机 Ubuntu 22.04.5 x86_64 完成。机器可读证据见 [validation-evidence.json](validation-evidence.json)，桌面截图见下方；完整运行记录保留在本机 `.artifacts/`，不纳入 Git。

![Blender 风格的原生桌面工作区](images/workspace.png)

![独立相机窗口](images/camera-preview.png)

## 已验证结果

| 检查 | 实际结果 |
|---|---|
| 单元与 Qt 测试 | **145 passed**，2.96 s |
| RGB + mask + mask_visib | 同场景两个目标 ID 2 / 7，4 / 4 完成，语义检查通过 |
| BOP | 4 / 4 完成；深度、K、位姿、模型毫米坐标、GT 索引和 gt_info 检查通过 |
| 无 BOP toolkit 的轻格式 | toolkit_path=None，离线预检及 4 / 4 渲染通过 |
| 近距离 BOP | 相机半径 35 mm，深度最大 36.3 mm；后处理未被固定 0.1 m 近裁剪面截空 |
| 无法满足的布局 | 恰好 30 次碰撞拒绝；saved=0、skipped=1、partial、退出码 2 |
| 已有输出目录 | 拒绝再次运行，退出码 1；结果树 SHA256 保持一致 |
| SIGINT / SIGTERM | 两者均保存 1 张、跳过 999 张，状态 interrupted，进程退出码分别 130 / 143 |
| 核心与 Blender 投影 | 非对称 fx/fy、偏置 cx/cy；与 bpy.calc_matrix_camera 最大误差 0.0000022 px |
| 实际 GPU 相机视口 | 与 Blender 相机投影误差 0.0301 px，与实际 Blender 标记图误差 0.25 px，均小于 1 px |
| 原生桌面 GUI | 真实 OpenGL，1320×880 和 1024×768 截图；相机重开复用 canvas；切换中心、保存、两种导出通过 |
| 独立任务 | 从 `/tmp`、移除 PYTHONPATH，使用 conda Python 3.8 启动预检及渲染通过 |
| 命令与目录 | 中文、空格、单引号、字面 `$()` 路径；空目录、文件、软链接、断链保护测试通过 |
| 编译与依赖 | compileall、3.8 启动器 py_compile、pip check、git diff --check 通过 |

测试演示由本地 Blender 生成 HDRI，并使用偏置 CAD 原点的 mm / cm 目标及 m 遮挡物；单元测试另覆盖三种单位。演示是合成网格，尚未使用用户自己的生产模型和 Haven 资源批量验证。

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

桌面依赖仅安装进仓库 `.venv`。没有向原 `bop`、Blender Python 或原 `dataset_rendering` 项目安装依赖。渲染使用已有的离线依赖和本地 BOP toolkit。实际 Cycles 验收使用 CPU；CUDA / OPTIX / HIP / ONEAPI / METAL 选项尚未做设备渲染验收。

## 复验

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QT_API=pyside6 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -p pytestqt.plugin tests/unit tests/ui -q

.venv/bin/python tools/create_demo.py .artifacts/demo-new
.venv/bin/python tools/gui_smoke.py \
  --project .artifacts/demo-new/demo.yaml --output .artifacts/gui-new
```

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
