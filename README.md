# BlenderProc 数据集工作区

当前版本：**v1.1**。新增应用与 Blender 内的场景/相机视角切换，几何预览与严格 GPU 渲染预览分离。

面向 Ubuntu 22.04 的本地桌面工具：导入目标与遮挡物，设置相机、光照和遮挡率，导出独立脚本，在 conda `bop` 环境中用 GPU 批量渲染。输出 **RGB + mask + mask_visib** 或 **BOP**；用于 YOLO 时，由外部工具转换标签。

界面采用 Blender 风格，包含对象树、三维视口和独立相机窗口。基本流程是：**导入资源 → 设置采样 → 检查构图 → 导出任务 → 终端预检 → 小批量试渲染 → 完整渲染**。

## 安装与启动

需要 Python 3.10+、可用的桌面 OpenGL 和 GPU 渲染环境。本机已验证的渲染组合为 **BlenderProc 2.8.0 + Blender 4.2.1 + NVIDIA OPTIX**；Blender 使用自己的 Python 3.11。UI 使用独立虚拟环境：

```bash
cd /home/kepler/rm_2027/blenderproc-ui
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gui]' -c constraints/ui.txt
.venv/bin/blenderproc-ui
```

如果仓库位于其他位置，替换第一行路径。已有项目可直接打开：

```bash
.venv/bin/blenderproc-ui --project /绝对路径/scene.yaml
```

`bproc-ui` 是相同入口的简写。UI 依赖安装到 `.venv`；实际渲染使用已有的 conda `bop`、Blender 和对应依赖。在“脚本”页填写环境路径，导出后用 `--check-only` 检查，启动器不会联网补装依赖。

## 第一次生成数据集

### 1. 导入目标，指定渲染中心

在“资源”页选择正确的 **m / cm / mm** 单位，再导入一个或多个 PLY。目标必须包含三角面或可三角化的面，纯点云不能直接渲染。导入后核对显示的实际尺寸；单位不对时修改单位并等待重新加载。

- 每个启用目标在场景中出现一次，`obj_id` 是唯一正整数，用于输出类别映射。
- 选中一个目标并“设为渲染中心”。对象树的编辑选择与渲染中心分别控制。
- 中心目标的包围盒中心位于世界原点；其他目标的包围盒中心按各自 X / Y / Z 范围随机摆放，数值单位为米。
- 保留模型的 CAD 原点与原始朝向；当前不随机旋转目标。需要某几个方向的数据时，限制下一步的相机角度。

所有启用目标都需要完整入画且有可见像素。布局使用保守包围盒碰撞检查，凹模型也可能因包围盒重叠而被拒绝。

### 2. 设置球坐标相机

进入“采样”页，先用“适配距离”按中心目标的尺寸设置距离，再在相机窗口核对所有目标是否入画。多个目标分散时，需要进一步扩大距离或调整摆放范围；“恢复建议范围”会重设布局和机位范围。

| 参数 | 含义 |
|---|---|
| 半径 `r` | 相机到中心目标包围盒中心的距离，单位米 |
| 方位角 `φ` | `0°` 沿目标 +X，`90°` 沿 +Y |
| 仰角 `α` | `0°` 水平，`90°` 从 +Z 上方拍摄 |

例如，只需要 +X 一侧的斜上方视图，可设 `φ = -45° … 45°`、`α = 10° … 45°`，距离按模型尺寸调整。相机始终看向中心目标。`170° → -170°` 表示跨界的 20°；角度两端相等表示固定值，完整一周需要勾选“全方位”。随机方向对 `φ` 和 `sin(α)` 均匀采样，半径均匀采样。

**固定机位与随机采样二选一：**

- 固定机位：列表中每个机位各拍一张，可通过“用当前视角添加”保存正在看的机位。
- 随机采样：填写每场景视角数，在一个或多个球坐标区域抽样；多个区域均分视角数，余数按列表顺序分配。

总请求张数 = 场景数 × 每场景视角数。固定模式的每场景视角数等于机位数量。例如，10 个场景、每场景 4 个视角，共请求 40 张。同一场景共享目标位置、光照、HDRI 和目标材质，每个视角重新生成相机与遮挡物；固定机位的相机数值保持固定。

“无遮挡样本”的分类也按场景确定，小批量会取整。因此只试 1 个场景时，不一定同时包含无遮挡和有遮挡样本。

### 3. 导入遮挡物，选择尺寸策略

遮挡物支持 PLY / OBJ，**导入单位独立于目标，默认 mm**。按源文件选择单位，查看换算后的尺寸。同一路径重复导入只保留一个资源。

| 策略 | 尺寸与姿态 | 适用方式 |
|---|---|---|
| 按物理尺寸 `physical_view` | 实际米制尺寸 × 倍率；随机三维旋转；每帧无放回选取资源 | 新建项目默认。倍率 1–1 保持实物尺寸，近远机位不会自动改变物体大小 |
| 按视角表观尺寸 `aimed_view` | 按相对目标的表观尺寸调整物理大小；不随机旋转；允许重复使用同一资源 | 需要控制画面中遮挡物大小时使用；未写策略字段的旧 YAML 保留此行为 |

新项目默认每帧 1 个物理遮挡物。数量上限不能超过不同资源数；需要遮挡时，下限至少为 1。物体放不下或发生碰撞会重试，不会自动缩小物体或减少数量。

在“中心目标的遮挡约束”中设置：

- **无遮挡样本**：要求中心目标完全没有额外遮挡的场景比例，默认 20%。首次只验证模型与相机、尚未导入遮挡物时，设为 **100%** 即可。
- **其余遮挡率**：有遮挡场景的中心目标像素遮挡率，默认 10%–50%。其他目标与专门遮挡物都计入，自遮挡不计入。
- **重试预算**：每个视角的几何提案与实际像素验收共享预算，默认 30 次。目标布局有独立的场景重试预算。

遮挡率按 `1 − 中心目标可见像素数 / 该目标单独渲染的轮廓像素数` 计算，只约束渲染中心。其他目标必须有可见像素，但不要求落在同一个遮挡率区间。几何预览估算构图，最终以实际渲染的像素验收为准。

### 4. 设置环境、材质与 GPU

可添加或扫描 Poly Haven 等来源的 `.hdr` / `.exr`，设置环境强度、旋转，以及点光源功率、距离和色温。HDRI 用作背景与照明；当前不导入完整房间或生成实体地面。不选 HDRI 时使用中性灰世界背景。

目标材质支持粗糙度、金属度、高光的随机范围或指定值；基色可跟随模型原色，也可统一指定。材质按场景共享。

“脚本”页的 Cycles 设置默认使用 **OPTIX**，可选 CUDA / HIP / ONEAPI / METAL。渲染禁用 CPU 设备；指定后端不可用时停止。当前实际硬件验收为 NVIDIA OPTIX，其他后端需要在对应机器上预检。

表面处理可选择平直、平滑、按角度平滑，并启用加权法线。降噪的“自动 GPU”仅在 OPTIX 后端启用 OptiX 降噪，其他后端自动模式关闭降噪。详见 [GPU 与表面处理](docs/gpu-rendering.md)。

### 5. 预览、保存与导出

在底部“采样机位”栏选择 **场景编号、视角编号和预览种子**，总览与独立相机窗口同步显示同一个候选。固定模式的视角编号对应机位列表，随机模式保留全部区域的视角分配；切换编号不改变项目参数。独立相机窗口关闭后，用“视图 → 相机视角”或 UI 中的 F12 重开。

点击“在 Blender 中打开当前构图”，默认进入 **SOLID 几何相机视图**，无需先通过 GPU/降噪检查。在 Blender 三维视口按 **N → BlenderProc → 场景 / 相机预览**，选择场景与视角后点“更新候选”，通过“相机视图”与“场景总览”检查构图、物体布局及相机位置/朝向；面板同时显示相机、灯光和环境参数。

需要真实光照和材质时，点击 **“渲染预览（严格 GPU）”**。该入口验证 GPU 与降噪组合，失败时给出修改建议并保留几何视图，不回退 CPU。生成的 `.blend`、配置快照和日志保存在项目同级 `.blender-view/`；直接双击 `.blend` 不会自动注册面板，请从应用或命令行入口重新打开。场景参数在原应用修改，不做 Blender 反向同步。

两端显示的都是 **候选预览，未经像素遮挡验收**。正式生成可能因像素验收而重试，不保证最终接受帧与当前候选完全相同。

按 Ctrl+S 保存项目 YAML，再“检查配置”。在“脚本”页选择格式、数据集父目录与名称，填写：

- Conda 环境：已有的 `bop`。
- Blender 目录：包含 `blender` 和 `4.2/python/bin/python3.11` 的安装目录。
- BlenderProc CLI：对应 `bop/bin/blenderproc`。
- BOP toolkit：BOP 输出需要本地 toolkit 和可用 GPU EGL；RGB 双掩码格式不需要 toolkit。

导出到一个**尚不存在的任务目录**。任务包含 `scene.yaml`、`run_job.py`、渲染入口与运行时，关闭 UI 后仍可执行。模型和 HDRI 使用绝对路径引用，没有复制进任务包；移动资源后需要更新项目并重新导出。

### 6. 在终端预检、试渲染和正式渲染

以下路径需要替换成自己的任务目录和输出目录：

```bash
# 检查实际 Blender Python、资源与 GPU 依赖
conda run --no-capture-output -n bop python /任务目录/run_job.py --check-only

# 先渲染 1 个完整场景，使用独立输出位置
conda run --no-capture-output -n bop python /任务目录/run_job.py \
  --num-scenes 1 --output /新的试渲染父目录

# 检查小样本后，按导出的 YAML 执行完整任务
conda run --no-capture-output -n bop python /任务目录/run_job.py
```

`--output` 指**父目录**，实际结果位于 `<父目录>/<dataset_name>`。试渲染与正式渲染要使用不同的最终目录。已有结果目录即使为空也拒绝覆盖；中断后换新目录运行，目前不支持断点续跑。

可用 `--format rgb_masks` 或 `--format bop` 临时覆盖输出格式。试渲染推荐 `--num-scenes N`；旧 `--num-samples` 会折算成整场景，不能保证恰好输出指定张数，也不能与 `--num-scenes` 同用。普通采样参数可编辑导出任务的 `scene.yaml`，环境路径或应用代码更新后应重新导出。

## 仓库中的人工测试素材

用户已完成人工测试，当前仓库保存以下任务与结果，替换原先的六帧示例：

| 素材 | 内容 |
|---|---|
| [导出任务 jobs/render_job](jobs/render_job/) | `cores` 目标（`obj_id=1`，单位 m），15 个遮挡物资源（单位 mm），979 个 HDRI 路径；随机模式 **250 场景 × 4 视角 = 1000 张请求** |
| [试渲染 renders/preview/dataset](renders/preview/dataset/) | **1 场景 × 4 视角**，640×480，OPTIX、50 samples、自动降噪；`completed`，requested=4、saved=4、skipped=0、exit_code=0 |

任务使用物理尺寸遮挡策略，每帧 1 个遮挡物、物理倍率 1；项目设定无遮挡场景比例 20%、其余遮挡率 10%–50%。场景比例在小样本中会取整，本次四帧实测遮挡率约为 **20.80%、29.70%、10.54%、29.97%**。1000 张是导出任务的配置规模，**不是已完成的渲染数量**。

| RGB 构图 | 完整掩码 `mask` | 可见掩码 `mask_visib` |
|---|---|---|
| ![人工试渲染第一视角](renders/preview/dataset/rgb/000000.png) | ![目标完整轮廓](renders/preview/dataset/mask/000000_000001.png) | ![目标可见轮廓](renders/preview/dataset/mask_visib/000000_000001.png) |
| ![人工试渲染第二视角](renders/preview/dataset/rgb/000001.png) | ![第二视角完整轮廓](renders/preview/dataset/mask/000001_000001.png) | ![第二视角可见轮廓](renders/preview/dataset/mask_visib/000001_000001.png) |

其余视角及逐帧相机、摆放与遮挡率见 [manifest.json](renders/preview/dataset/run_metadata/manifest.json)，完成状态见 [status.json](renders/preview/dataset/run_metadata/status.json)。

```bash
# 在本机资源路径和运行环境有效时，检查示例任务
conda run --no-capture-output -n bop python jobs/render_job/run_job.py --check-only

# 一个完整场景的试渲染；必须使用尚不存在的最终数据集目录
conda run --no-capture-output -n bop python jobs/render_job/run_job.py \
  --num-scenes 1 --output /新的试渲染父目录

# 只读检查仓库现有四帧结果
.venv/bin/python tests/blender/check_rgb_masks.py renders/preview/dataset
```

任务包含导出时的运行时代码快照；模型、HDRI 和运行环境仍引用原机器的绝对路径，**未随仓库打包**。其他机器请在应用中替换资源与环境路径并重新导出，不要把示例任务当成开箱即用的数据包。

## 输出格式与标签

| 内容 | RGB + mask + mask_visib | BOP |
|---|---|---|
| RGB | 默认 PNG，可选 JPEG | 默认 JPEG，可选 PNG |
| 完整 / 可见掩码 | 每个目标各一张二值 PNG | 每个 GT 实例各一张二值 PNG |
| 深度、相机与 GT、模型 | 不生成 BOP 文件结构；运行元数据仍保留相机与摆放信息 | 深度、`scene_camera.json`、`scene_gt.json`、`scene_gt_info.json`、`models/` |
| 掩码文件后缀 | 稳定对象 `obj_id` | 该图 `scene_gt` 列表的零起始索引 |

两种格式的掩码均为单通道 8 位 PNG：背景 0、前景 255。`mask` 表示目标单独渲染的完整轮廓，`mask_visib` 表示其可见部分；BOP 可见掩码使用原生深度容差计算，不应直接当作采样验收的精确分割遮挡率。

例如，对象 ID 为 2 和 7 时，轻格式包含：

```text
dataset_name/
  rgb/000000.png
  mask/000000_000002.png
  mask/000000_000007.png
  mask_visib/000000_000002.png
  mask_visib/000000_000007.png
  run_metadata/
```

BOP 图像与标注位于 `train_pbr/000000/` 等场景目录，相应掩码后缀通常为 `000000`、`000001`，需通过 `scene_gt.json` 映射到对象 ID。BOP 模型坐标与位姿平移使用毫米；16 位深度 PNG 恢复为米的公式是 `像素值 × depth_scale / 1000`，其中 `scene_camera.json` 的 `depth_scale` 对应项目的 `depth_scale_mm`，默认 0.1。

运行结果先看 `run_metadata/status.json` 中的 `requested`、`saved`、`skipped` 和失败原因。`frames.jsonl` 逐帧记录元数据，`manifest.json` 在结束时汇总，`rejections.jsonl` 记录拒绝原因。BOP 后处理完成后才计入保存成功。

| 退出码 | 状态与处理 |
|---|---|
| 0 | `completed`：全部请求完成 |
| 2 | `partial`：重试耗尽等原因导致部分或全部请求未保存；查看拒绝原因 |
| 1 | `failed` 或启动失败：查看终端错误与日志 |
| 130 / 143 | SIGINT / SIGTERM 中断；等待当前原生渲染与必要收尾后退出 |

## 多尺度目标与大模型

- **按尺寸分组**：启用目标最长边相差超过 10× 时，可在“脚本”页勾选按组导出，让每组单独生成数据集。它是改善取景与深度容差的可选工作流；如果需要大小目标出现在同一场景，保持不勾选。
- **分组后自动重取景**：默认关闭。启用后，实际拆成多组时重算各组相机距离，并相应调整点光源距离和功率；内参、ID、遮挡率和深度编码保持原配置。每组的调整记录写入 `grouping.json`。
- **自动 LOD**：只简化视口显示，默认对超过 10 万面的网格生成最多 5 万面的显示网格。实际采样、导出和渲染使用完整模型。
- **图像分块**：Cycles 默认开启，分块尺寸 2048，用于减少高分辨率图像缓冲占用；全部场景几何仍需驻留显存。

分组也可在命令行执行：

```bash
# 只查看分组结果
.venv/bin/python tools/export_by_scale.py /项目路径/scene.yaml /新的任务目录 --dry-run

# 按组导出并自动重取景，画幅四周留 10% 边距
.venv/bin/python tools/export_by_scale.py /项目路径/scene.yaml /新的任务目录 \
  --reframe-groups --framing-margin 0.10
```

多组会生成带 `-size-N` 后缀的任务目录和数据集名称，逐个使用各自的 `run_job.py`。只有一组时保留原配置。

## 常见问题

| 现象 | 检查方法 |
|---|---|
| 物体极小、相机离得很远 | 核对 m/cm/mm 与显示尺寸，重新适配距离 |
| 新项目未导入遮挡物，检查不通过 | 若暂时只拍无遮挡数据，把“无遮挡样本”设为 100% |
| `out_of_frame` / `collision` | 调整目标位置范围与相机距离；布局检查使用保守包围盒 |
| `no_occluder_room` / `occluder_collision` | 核对遮挡物单位、物理倍率、数量与相机距离；物理模式不自动缩小资产 |
| `occlusion` / `invisible_target` | 查看实际遮挡率和候选构图，调整允许范围、遮挡物或目标布局；增加重试不能解决无解配置 |
| GUI 能预览，终端却报依赖或 GPU 错误 | 预览与实际渲染使用不同环境，先运行任务的 `--check-only` |
| 修改材质后视口差别很小 | 在 Blender 中查看真实光照；模型顶点色很暗时可试“统一基色” |
| Qt 无法载入 xcb 插件 | 按 [使用说明](docs/usage.md#安装与启动) 检查桌面系统库 |

## 验证与更多说明

安装开发依赖后运行单元与 Qt 测试：

```bash
.venv/bin/python -m pip install -e '.[gui,dev]' -c constraints/ui.txt
env -u PYTHONPATH -u DEBUG PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QT_API=pyside6 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -p pytestqt.plugin tests/unit tests/ui -q
```

检查真实输出时，将路径指向包含 `run_metadata/` 的数据集根目录，按格式选择一个检查器：

```bash
.venv/bin/python tests/blender/check_rgb_masks.py /RGB双掩码数据集路径
.venv/bin/python tests/blender/check_bop.py /BOP数据集路径
```

v1.1 自动验收：**775 项单元与 Qt 测试通过**；真实 Blender 4.2.1 烟测覆盖后台构建、GUI 面板注册、场景/视角往返切换、相机矩阵、对象复用和视口状态，见 [场景/机位预览验证](docs/scene-camera-preview-verification.md)。用户另已完成人工测试，上方 `renders/preview/dataset` 保存了四帧实际渲染结果；`jobs/render_job` 保存对应配置来源的完整导出任务。历史 GPU 与算法检查见 [验证记录](docs/validation.md)。

详细设置见 [使用说明](docs/usage.md)、[GPU 与表面处理](docs/gpu-rendering.md)。设计与实施计划保存在 [docs/superpowers/](docs/superpowers/)。
