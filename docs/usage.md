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

单位决定模型的实际尺寸：同一份 PLY 按 m 和按 cm 导入会差 100 倍，机位距离也跟着差 100 倍。导入后看“尺寸”一栏是否接近实物；不对就改单位重新载入，再在“采样”里点“适配距离”。

对象树和“编辑目标”只切换当前编辑对象。“设为渲染中心”单独改变相机中心。中心目标的包围盒中心位于世界原点；其他目标的包围盒中心在各自 X / Y / Z 范围内随机采样，单位是米，方向沿中心目标局部坐标轴。原始顶点和 CAD 原点保留，首版不随机旋转目标。

要停用或移除中心，先把另一个启用目标设为中心。修改模型单位会重新载入网格。“恢复建议范围”根据全部目标尺度和视野替换位置、距离设置；保留自定义范围时无需使用它。

全部启用目标需要完整入画、各有可见像素，并通过保守 AABB 不穿插检查。该检查可能拒绝实际不相交的凹模型，可调整位置范围。首版处理不透明网格、基础颜色与顶点颜色，透明表面会明确报不支持。

## 相机采样与预览

数据集先定“场景数”，再定“每场景视角数”：一个场景里的物体摆放、光照和材质抽一次后固定，同一场景的每个视角重抽机位与遮挡物。所以 4 场景 × 3 视角 = 12 张，其中前 3 张是同一个摆法的 3 个机位。

机位模式二选一：

- **固定机位**：列表里每个机位各出一张，每场景视角数自动等于机位个数。加一个机位就多一张，删掉就少一张，不需要另填张数。同一机位在每个场景里保持同一 r/φ/α，只是场景不同。
- **随机采样**：每场景在范围内抽 N 个机位，N 由你填。多个区域按 N 均分，余数按列表顺序分配。

两种模式都用半径 r、方位角 φ、仰角 α 描述机位。φ=0 沿中心局部 +X，向 +Y 为正；α=0 水平，+90° 沿 +Z，相机始终朝向中心目标包围盒中心。

- φ=170° 到 −170° 是跨界的 20° 范围。
- 两端相等表示固定角度；勾选“全方位”才表示完整一周。
- 半径均匀采样，方向对 φ 和 sin(α) 均匀采样。
- 固定机位是同一套 r/φ/α 的单值写法；随机区域填两个端点表示区间。
- “用当前视角添加”：把相机窗口里正看的那台相机钉成一个机位，不用手填角度。
- “适配距离”：按渲染中心的实际包围盒重算 r，让模型占画面高度的约 80%。导入后先点一次，可以避免毫米级模型被放在半米外。
- 距离输入框下面会写出 r 相当于物体 XY 半径的几倍（例如 `r = 4.5 × 物体 XY 半径（0.0671 m）`）。绝对米数在小模型上很难判断，倍数才是能直接对照的尺子。
- 默认距离范围按模型自身大小推导，摆放偏移只在此之上叠加；不同尺寸的模型不会共用一套写死的米数。

底部选择机位来源、视角序号和预览种子，再刷新候选。固定机位模式下视角序号对应第几个机位；随机区域换序号就换一组采样。主视口拖动仅改变观察视角。独立“相机视角”窗口使用相同 K、分辨率和画幅；关闭后可用“视图 → 相机视角”或 F12 重开。候选在后台线程计算，连续点刷新只会保留最后一次请求，界面不会因此卡住；底部第一行是当前模式的常驻说明，第二行是候选状态（“正在计算候选构图…”之后会给出候选或具体拒绝原因）。

视口只显示几何构图，不执行 Cycles 或实际像素遮挡验收。需要看真实光照、材质和遮挡时，用下一节的方式在 Blender 中打开当前构图。网格、球面线、相机图标只在主视口显示，不会进入数据集。地面网格向外渐隐到完全透明，任何视角下都看不到边界，单位尺寸的疏密随模型大小变化。

视口不做材质着色：目标按「基色」或模型自带顶点色平涂，遮挡物保持平灰。真实的粗糙度、金属度和光照只在 Cycles 里成立，所以调材质请用下一节的「在 Blender 中查看」：场景、灯光与按场景参数建好的 Principled 材质都在，直接在 Blender 里改并渲染。把调好的数值填回「指定值」模式，导出的数据集就是同一套表面参数。

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

点光源可设置功率、距离、上半球仰角和色温范围。遮挡物在中心视线附近自动摆放，数量及相对尺寸可调，无需手写策略。遮挡物跟随**当前机位**摆放在光轴上（目标前方、比目标近，侧向偏移为自身尺寸的 10%–90%），所以重试会同时搜机位和遮挡位置；同一场景的不同视角各自有自己的遮挡物，否则宽幅区域里总有视角永远盖不住目标。

目标材质只作用于目标物体，遮挡物不参与（保持中性灰）。每个场景随机一组表面参数，同一场景的所有视角共用这一组值：粗糙度（默认 0.05–0.8）、金属度（默认 0–0）、高光（默认 0–1）。

基色有两种模式：

- **跟随模型原色**：保留顶点色或导入色，只改粗糙度/金属度/高光。注意顶点色很暗的模型（例如 `cores.ply` 的平均顶点色只有 35/255）在这个模式下几乎看不出变化——黑色表面的金属反射仍然是黑色，这也是多数"改了材质没反应"的原因。
- **统一基色**：丢掉顶点色，把目标刷成一个指定颜色（取色器选，默认 `#b3b7ba`）。这一模式在渲染和三维视口里都会生效，粗糙度与金属度的差别一目了然。

参考实现是 `bproc_project/dataset_rendering/samplers/target.py` 的 Roughness / Specular IOR Level 采样，在此基础上追加了金属度与基色模式。

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
conda run --no-capture-output -n bop python /任务目录/run_job.py --num-scenes 1 --output /新的试渲染父目录
conda run --no-capture-output -n bop python /任务目录/run_job.py
```

`--output` 是父目录，最终路径为 `<父目录>/<dataset_name>`。试渲染和完整任务必须采用不同最终目录。`--format rgb_masks|bop` 可以临时切换格式，并重新检查所需依赖。UI 仅复制命令，不执行渲染。

启用目标的最长边相差超过 10× 时，一个数据集只能有一份 BOP GT 深度容差和一份遮挡比例窗口，无法同时服务最小与最大目标。用分组入口按尺寸拆分：

```bash
# 先看分组，不写任务目录
.venv/bin/python tools/export_by_scale.py project.yaml .artifacts/render_job --dry-run

# 导出：普通项目仍是一个任务；混尺度项目生成 render_job-size-1、render_job-size-2 …
.venv/bin/python tools/export_by_scale.py project.yaml .artifacts/render_job
```

导出面板也有“目标尺寸相差 >10× 时按组导出”选项，勾选后按钮会直接生成这些分组任务；命令行入口与 UI 使用同一套分组规则。每组只保留该尺度段的目标，数据集名同步加 `-size-N` 后缀，任务之间互不覆盖。相机、灯光、深度编码和绝对米制参数保持原配置，不会自动缩放；如果某组不含原渲染中心，该组最大目标会成为新中心，其余目标偏移按新中心的名义偏移平移，保持相对布局。需要更近的取景时，导出后分别编辑各组 `scene.yaml` 的机位范围。

两个计数选项互斥，都从启动器透传到预检与渲染：

- `--num-scenes N` 只渲染前 N 个场景，每个场景的视角数不变；试渲染默认用它（`--num-scenes 1`）。
- `--num-samples N` 是旧参数，只保留兼容：按张数截断，向上取整到整场景。3 视角项目里 1/4/5 张实际是 3/3/6 张，所以界面不再承诺"4 张"。

导出面板的"少量试渲染"命令固定为 `--num-scenes 1`，实际张数等于该项目的每场景视角数。两参数同时出现会被启动器、预检和渲染 CLI 一致拒绝。

启动器兼容 conda Python 3.8，用实际 Blender Python 3.11 预检真实依赖版本、导入位置与安装缓存。通过后直接启动指定 Blender，沿用 BlenderProc 2.8 的初始化参数，同时准确转发中断信号。渲染入口首行仍为 `import blenderproc as bproc`。

缺项会停止，不联网安装或更改缓存。需要人工准备时，在 `bop` 环境使用 `blenderproc pip install --custom-blender-path /Blender目录 ...`，确认依赖版本与本机 BlenderProc `DefaultConfig.default_pip_packages` 一致，再运行 `--check-only`。BOP 额外需要本地 toolkit 和可用 EGL；轻格式不需要 toolkit。

## 输出与运行状态

`RGB + 双掩码` 默认 RGB PNG，可选 JPEG。每帧每个目标都有同分辨率、单通道 8 位二值 PNG（0 / 255）：

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

`BOP` 默认 RGB JPEG，另有 16 位深度 PNG、相机、位姿、canonical models、双掩码和 `scene_gt_info.json`。模型坐标与位姿平移用毫米；深度恢复公式为 `PNG数值 × depth_scale_mm / 1000` 米（默认 scale 为 0.1 mm）。掩码后缀是 **GT 列表索引**，例如对象 ID 2 和 7 对应索引 0 和 1。双掩码同样是单通道 8 位、0/255 PNG。BOP 后处理的可见性容差先按每个目标计算 `min(0.015, max(2 × depth_scale_mm / 1000, 0.05 × 最长边米数))`，再取启用目标中的最小值；不是固定 15 mm，也不等同于采样验收的精确分割遮挡率。本版不生成 COCO / YOLO。

### 实例 ID 与掩码位深

| 数据 | 含义 |
|---|---|
| 原生实例分割数组 `uint8/uint16/uint32` | BlenderProc 根据实例数选择中间整数类型；临时 `idx` 经属性映射到稳定 `obj_id`，不截断或转成 float32 |
| `mask` / `mask_visib` | 每个目标各自的单通道 8 位二值 PNG，0 为背景、255 为前景；不是实例 ID 合成图 |
| RGBA 的“32 位” | 四通道各 8 位的总位数，不代表单通道 uint32 掩码 |
| BOP depth | 单通道 16 位深度 PNG，按上述 scale 恢复距离 |

现有 RGB 路径使用原生 `render_segmap`；BOP 路径使用原生 `write_bop` 和 `calc_gt_masks/calc_gt_info`，无需新增“32 位 PNG”选项。PNG 不支持单通道 32 位整数样本。2026-10-19 对照 `bproc_project/dataset_rendering/render.py`、原生实现和有限真实产物完成兼容验证；生产渲染路径未改，本轮增加整数边界回归、只读审计和检查器格式校验，参见 [验证记录](validation.md#原生实例掩码兼容性2026-10-19)。

`run_metadata/status.json` 记录 requested、written_frames、saved、skipped 和失败原因。每帧提交时先向 `run_metadata/frames.jsonl` 追加并落盘一行（相机、位姿、环境、光源、遮挡率），整轮结束时再一次性写 `manifest.json`；因此写入量随帧数线性增长，中断后 journal 仍可逐行读取，末尾未写完的一行默认报错、只允许显式按"可恢复"方式忽略。BOP 只有后处理完成才计入 saved。退出码：完成 0、部分完成 2、失败 1、SIGINT 130、SIGTERM 143。

已有任务或输出目录（包括空目录、软链接、断链）均拒绝覆盖。中断保留已有结果供检查，下次换新目录；不做断点续跑。

## 视口 LOD、图像分块与分组重取景（2026-10-19）

这些设置只改变视口显示、图像缓冲或可选的按组导出；采样、碰撞、稳定 ID、模型、掩码和深度语义不变。

- **视口 LOD**：三维视口标题栏的“自动 LOD / 原始网格”。`auto` 在网格超过 100,000 面时于后台生成不超过 50,000 面的显示网格；结果未就绪时只画包围盒代理，不会先上传全量几何。切换为“原始网格”会直接绘制全分辨率数组，百万面模型可能明显卡顿。视口下方的状态行显示输入面数、绘制面数及生成/缓存/失败状态。`project.viewport.lod_mode/lod_threshold_faces/lod_target_faces` 可写入 YAML；删除整块即回到默认。
- **Cycles 图像分块**：脚本面板 Cycles 区域的“图像分块”和“分块尺寸”（8–8192）。它设置 Blender 4.2 原生 `scene.cycles.use_auto_tile/tile_size`，只降低高分辨率图像缓冲；整个场景和几何仍完整驻留，**不是**超显存几何分块。默认开启、尺寸 2048，与本机 Blender 默认一致。`tools/render_demo.py --tile-size 512` 或 `--no-tiling` 可覆盖 YAML。
- **分组后自动重取景**：脚本面板“目标尺寸相差 >10× 时按组导出”下方的同名开关与边距，或 `tools/export_by_scale.py ... --reframe-groups --framing-margin 0.10`。默认关闭；只有实际分成多个尺寸组时才重算每组相机距离，并按 `s=R_group/R_reference` 缩放点光源距离与能量。内参、像素尺寸、稳定 ID、模型、遮挡窗口和 `depth_scale_mm` 不自动改变。普通单组项目勾选后仍按原配置导出。每组任务写入 `grouping.json`，记录来源项目哈希、包络、相机/灯光前后值、钳位和未改动字段。
- **BOP 深度容量**：重取景后若某组完整深度包络超过 `65535 × depth_scale_mm / 1000` 米，导出在创建任何任务目录前拒绝并指出组与所需容量；不会自动改 `depth_scale_mm`。
- **超限模型的真实轮廓**：超过旧 2M 面/1M 交叉限制的目标与遮挡物改用分块三角形并集覆盖（真实孔洞、凹形与重叠组件并集），并在视口标题行显示方法/面数/网格；无法处理时给出稳定原因，凸包回退标记为估计。不同输出格式与阈值不变。

## 真实资源批量验收（2026-10-19）

`tools/validate_production_resources.py` 是离线入口，不下载资源、不运行参考 `render.py`、不扫描历史数据集：

```bash
.venv/bin/python tools/validate_production_resources.py run \
  --manifest /abs/manifest.json --destination /abs/new-evidence [--num-scenes 1] [--case ID]
.venv/bin/python tools/validate_production_resources.py verify --destination /abs/new-evidence
```

manifest schema 1 的 `resources` 显式列出路径、role（model/occluder/hdri）、SHA256、来源和 `production|fixture` 分类；`cases` 列出唯一 id、项目 YAML、HDRI 列表、场景数、seed、是否分组/重取景。相对路径相对 manifest 文件解析；项目引用的每个实际资源都必须覆盖。缺文件、哈希不符、重复 case、非法计数、目标目录已存在（含空目录/软链接/断链）都会在启动任何任务前失败。

每个 (case, HDRI) 派生一个确定性任务，强制 `hdri.randomize=false` 并保持项目内启用目标完整；依次执行新导出、`--check-only`、GPU 渲染、仓库真实 `check_bop.py` 与原生掩码审计。`summary.json` 保存 requested/written/saved/skipped、提案接受率、出图率、错误、耗时、GPU、资源来源、源码快照和每步命令/退出码。SIGINT/SIGTERM 转发给当前任务并等待其收尾，未启动的 case 记为 `not_started`，退出 130/143。失败或 partial 时入口非零，但证据保留；重跑必须换新目录。`fixture` 资源生成的 summary 永远不会得到 `production_verified: true`，`verify` 也会返回非零。`verify` 只读复核，只重跑真实 checker/哈希/快照，不重新导出或渲染。
