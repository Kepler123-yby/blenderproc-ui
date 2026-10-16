# BlenderProc Render UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 Ubuntu 本地桌面配置工具，让用户用多目标随机布局、指定中心的球坐标机位、HDRI、光照和遮挡约束，导出可独立运行并生成 RGB 双掩码或 BOP 数据的任务。

**Architecture:** Qt UI、无界面的共享核心、Blender 适配器分开，共用配置、坐标、候选布局和采样代码。导出时复制共享核心和渲染运行时，使用兼容 conda Python 3.8 的离线启动器调用实际 Blender Python 3.11。主观察视口与独立相机窗口共享场景数据，各有自己的观察状态。

**Tech Stack:** Python 3.10+；PySide6 Qt Widgets；VisPy；NumPy、PyYAML、trimesh、Pillow；pytest / pytest-qt；本机 BlenderProc 2.8.0、Blender 4.2.1；BOP toolkit / EGL 后处理仅在 BOP 模式使用。pyrender 属于 BlenderProc 默认包，即使轻格式也需预先准备，但轻格式不调用其渲染器。

**Spec:** [2026-10-16-blenderproc-render-ui-design.md](/home/kepler/rm_2027/blenderproc-ui/docs/superpowers/specs/2026-10-16-blenderproc-render-ui-design.md)。实施者先读设计，再读本计划；仓库根为 `/home/kepler/rm_2027/blenderproc-ui`。

## 实施状态（2026-10-16）

- [x] Task 1–4：配置、资源、单位、相机和可复现候选场景。
- [x] Task 5–8：真实后端、有界重试、统计、两种格式与 BOP 后处理。
- [x] Task 9：自包含任务、Python 3.8 启动器与只读离线预检。
- [x] Task 10–13：原生 Blender 风格工作区、异步资源、双视口、参数与脚本导出。
- [x] Task 14：145 项测试、真实双格式/近距离/失败/中断/GPU 视口验收及独立审查修复。

实现与证据见 [验证记录](../../validation.md)、[使用说明](../../usage.md)。下面保留原执行步骤作为规划基线；没有发生的先红后绿历史命令和逐任务提交不补勾。实施通过可用原生协作工具并行完成，没有声称使用未安装的 superpowers 执行技能。当前启动器通过预检后直接使用 BlenderProc 2.8 的初始化参数启动指定 Blender，以准确转发中断信号，避免额外 CLI 转发层改写信号。

## Global Constraints

- “在 Ubuntu 22.04 使用本地 UI，生成脚本后自行在终端运行。”
- “使用 Python 3.10+、PySide6 Qt Widgets 构建独立桌面 UI，安装在项目自己的虚拟环境中。”
- conda `bop`：Python 3.8.20、BlenderProc 2.8.0；实际 Blender 4.2.1 内部 Python 3.11。启动器仅用 Python 3.8 标准库，共享核心兼容 3.10/3.11。
- “输出格式枚举为 `rgb_masks` 和 `bop`。” 不新增纯 RGB 或 YOLO 导出。
- “生成数量指同场景图片总数，不按目标数量再乘一次”。每张图包含全部启用目标，每个模型一个实例、唯一正整数 `obj_id`。
- “列表中的‘当前编辑模型’和‘渲染中心’是两项独立状态”。相机和其他目标的位置范围采用中心目标的局部坐标。
- “保留每个目标的原始模型坐标和原点”。内部距离用米，BOP 模型和位姿平移用毫米；不能减去局部顶点的包围盒中心。
- 方位角 `[-180°, 180°]`，仰角 `[-90°, 90°]`；跨界区域与固定角度、全方位必须区分；方向按 φ 和 sin(α) 均匀采样。
- 默认图像 `640×480`，`fx=572.4114, fy=573.57043, cx=325.2611, cy=242.04899`；仅支持零 skew 的标准 pinhole K。
- “控制的遮挡率默认针对中心目标”；其他目标与专门遮挡物都计入外部遮挡，自遮挡不计入。
- 默认无遮挡 20%、其余遮挡 10%–50%、每个请求最多尝试 30 次；布局与遮挡共用总预算。
- 所有启用目标完整入画、各有可见像素、不穿插；不能满足就记录并跳过，不能越过用户范围。
- `rgb_masks`：逐目标 `mask` / `mask_visib` 为同分辨率的 8 位 PNG、0/255，后缀是六位 `obj_id`；BOP 掩码后缀是 GT 列表索引。
- BOP 深度缩放默认 `0.1`，可见性容差沿用 `delta=0.015 m`；原生 BOP 可见率不能替代采样验收遮挡率。
- “不在渲染中静默联网安装缺失依赖”。UI、导出与渲染均不修改原 `dataset_rendering` 项目或原始资源。
- “发现已有结果时拒绝覆盖”；不清空、不追加旧运行、不做断点续跑。内部向本次独占新建的数据集逐帧写入不属于用户级续跑。
- 视口为几何预览，HDRI/光影效果以终端试渲染为准；相机窗口不画辅助网格、区域或图标。

## Review Focus

1. CAD 模型原点偏离几何中心且混用 mm/cm/m：改变中心不改写模型坐标，BOP 投影仍与 RGB 对齐。归属 Task 2、3、7。
2. 中文、空格、单引号、`$()` 路径和已有/软链接输出：命令不执行路径内容，既有文件不被覆盖。归属 Task 1、7、8。
3. 多目标的空间范围互相冲突、全遮挡或机位出画：每请求最多 30 次，部分完成如实计数且非零退出。归属 Task 4、6。
4. 隔离某目标渲染 mask 时抛错：恢复所有物体可见性与相机/帧状态，后续 RGB 不丢对象。归属 Task 5。
5. 不连续 `obj_id=[2,7]`、目标列表顺序改变：轻格式按对象 ID 命名，BOP 按 GT 索引命名，不能混用实例分割临时值。归属 Task 5、7。

---

## 实施基线与交付顺序

这是一个共享配置贯穿的产品，不拆成互不兼容的 UI 和渲染项目。先做可测试的无界面核心与独立渲染任务（Task 1–9），再接桌面 UI（10–13），最后完成两种格式的真实验收（14）。Task 2/3、7/8 可在相邻接口稳定后并行，但不能跳过各自验证和提交。

- UI 首次验证锁定：Python 3.10.12、PySide6 `6.8.3`、VisPy `0.15.2`、NumPy `1.26.4`、trimesh `4.12.2`、PyYAML `6.0.3`、Pillow `11.3.0`；pytest `8.4.2`、pytest-qt `4.5.0`。
- 通用依赖声明允许 NumPy `>=1.24,<2`、trimesh `>=4.4,<5`、PyYAML `>=6,<7`、Pillow `>=9,<12`；UI 的首次组合写入约束文件。不能把 UI 的锁定版本强装进 Blender。
- 当前机器未安装这套 UI 组合，也未实际验证 EGL。本计划只安排未来验证，不将调研当作验收通过。
- 默认候选种子 `42`、场景图片数 `1000`、Cycles samples `50`、CPU 作为首次验证设备；一个点光源初始 `80–100 W`，HDRI 强度 `0.6–1.6`、Z 旋转 `0–360°`。它们是可编辑初始值。
- 遮挡物数量初始 `1–3`，尺寸建议以中心目标包围盒尺度计算；实现用投影驱动的候选摆放，不要求用户写策略。首版按不透明网格处理，保留可用顶点颜色和基础颜色；透明表面必须给出明确的不支持提示。

## 文件结构与职责

| 路径 | 职责 |
|---|---|
| `pyproject.toml`, `constraints/ui.txt` | 包、CLI、依赖组与首次可复现安装组合 |
| `src/bproc_core/config.py`, `project_io.py` | 数据类、校验、UTF-8 YAML 和路径解析 |
| `src/bproc_core/assets.py` | 网格读取、单位、颜色、局部几何和 BOP 规范模型 |
| `src/bproc_core/camera.py`, `sampling.py` | 相机坐标/投影、球坐标、区域分配与确定性随机源 |
| `src/bproc_core/scene.py` | 候选场景记录、非中心布局、光照/HDRI 与几何拒绝 |
| `src/bproc_core/masks.py` | 临时实例 ID 解码、掩码和遮挡统计 |
| `src/bproc_runtime/contracts.py`, `blender_backend.py` | 后端接口、Blender 场景状态与单帧渲染 |
| `src/bproc_runtime/run.py`, `records.py` | 有限重试、运行状态、manifest 与中断记录 |
| `src/bproc_runtime/output.py`, `rgb_masks.py` | 独占输出目录、图片与双掩码写出 |
| `src/bproc_runtime/bop_output.py`, `bop_postprocess.py` | BOP 2.8.0 适配、模型、GT 索引、显式后处理 |
| `src/bproc_runtime/cli.py` | Blender 内参数解析与调度，不导入 Qt |
| `src/bproc_export/bundle.py`, `commands.py` | 自包含复制与 shell 安全命令 |
| `src/bproc_export/templates/run_job.py`, `render_scene.py` | 3.8 离线启动器与首行导入 bproc 的实际入口 |
| 导出任务的 `launch.json` | 标准库启动器读取的版本化环境定位与完成标志，由 exporter 生成，不含用户密钥 |
| `src/bproc_ui/app.py`, `state.py`, `theme.py`, `main_window.py` | Qt 生命周期、项目状态、Blender 风格工作区 |
| `src/bproc_ui/views/scene_view.py`, `camera_view.py`, `projection.py` | 两个 VisPy 场景、独立相机窗口、K 投影适配 |
| `src/bproc_ui/panels/resources.py`, `sampling.py`, `export.py` | 资源/中心/位置、采样、导出三个属性面板 |
| `tests/unit/`, `tests/ui/`, `tests/blender/` | 纯函数、Qt 状态与真实 Blender 验证 |
| `tests/factories.py`, `tools/create_demo.py`, `tools/gui_smoke.py` | 可控双目标 fixture、最小 HDRI 和桌面视觉验收 |
| `docs/usage.md`, `docs/validation.md`, `README.md` | 安装、操作、终端步骤与真实验收证据 |

每个目录创建最小 `__init__.py`，包名不得使用 `blenderproc`。`bproc_core` 不导入 Qt、VisPy、bpy 或 BlenderProc；普通 Python 能导入 `bproc_runtime`，实际后端只在初始化时载入 Blender API。

## 共享数据契约

下列名称在任务之间保持一致；数据类集中在所属模块，不能让各任务另造近似版本。

| 模块/类型 | 固定字段或含义 |
|---|---|
| `config.Range` | `minimum: float, maximum: float`；有限数；允许相等 |
| `config.Azimuth` | `start_deg: float, end_deg: float, full_circle: bool` |
| `config.Box` | `x: Range, y: Range, z: Range`，米制中心偏移 |
| `config.AssetSpec` | `key: str, path: Path, unit: Literal['m','cm','mm']` |
| `config.TargetSpec` | `asset: AssetSpec, name: str, obj_id: int, enabled: bool, offset: Box` |
| `config.RegionSpec` | `key: str, name: str, radius_m: Range, azimuth: Azimuth, elevation_deg: Range` |
| `config.CameraSpec` | `width, height: int, fx, fy, cx, cy: float, auto_scale: bool`；K 底行为 `[0,0,1]`、skew=0 |
| `config.ProjectConfig` | `schema_version=1, targets, anchor_key, regions, camera, occluders, hdri, light, occlusion, num_samples, seed, render, output, runtime` |
| `config.HdriSpec/LightSpec/OcclusionSpec` | 分别包含文件列表/随机开关/强度/旋转；功率/位置范围/冷暖色；`none_fraction=.2, ratio=Range(.1,.5), max_attempts=30, count=(1,3), scale` |
| `config.RenderSpec/OutputSpec/RuntimeSpec` | samples/device；`format, root, dataset_name, rgb_encoding=('default'|'png'|'jpeg'), depth_scale_mm=.1`；conda_env/Blender 路径/BlenderProc 可执行文件/toolkit 路径 |
| `assets.MeshData` | `key, vertices_m[N,3], faces[M,3], vertex_rgba[N,4]或None, bounds_m[2,3]`；数组不带 Qt/Blender 对象 |
| `camera.CameraPose` | `world_from_camera[4,4], position_m[3], radius_m, azimuth_deg, elevation_deg, clip_near_m, clip_far_m`；列向量、相机为 OpenGL 轴 |
| `scene.Placement` | `asset_key, target_key或None, obj_id或None, world_from_model[4,4], role=('target'|'occluder')` |
| `scene.SceneCandidate` | `request_index, attempt_index, region_key, anchor_key, camera, placements, hdri_path, hdri_strength, hdri_rotation_deg, light, wants_unoccluded` |
| `scene.Rejection` | `reason: str, detail: str`；稳定原因码用于统计 |
| `contracts.VisibilityEvidence` | `anchor_full: bool[H,W], visible: dict[int,bool[H,W]]`；键是 obj_id |
| `contracts.RenderedFrame` | `rgb: uint8[H,W,3], depth_m: float[H,W]或None, full_masks: dict[int,bool[H,W]]或None, visible_masks: dict[int,bool[H,W]]` |
| `records.FrameRef` | `frame_index:int, relative_rgb:str, bop_scene_id:int或None, bop_image_id:int或None, gt_obj_ids:tuple[int,...]` |
| `records.RunSummary` | `status, requested, written_frames, saved, skipped, failures_by_reason, exit_code`；saved 指格式所需文件全部完成，不能把只有 RGB/pose 的 BOP 算成功 |

所有数值输入禁止 NaN/Infinity，整数禁止布尔值冒充；相机外参/单位规范只能在边界转换一次。`OutputSpec.root` 和 CLI `--output` 都指输出父目录，最终目录是 `root/dataset_name`；UI 同时显示最终路径。测试辅助 `tests.factories.make_project(tmp_path, ids=(2,7), output_format='rgb_masks') -> ProjectConfig` 和 `make_mesh(...) -> MeshData` 在首次使用的任务创建。测试代码中简写的 fixture/helper 由所属任务在该测试文件或 factories 定义，必须调用真实生产接口，不在 fake 中重写被测业务逻辑。

### Task 1: 可保存、校验和重新载入的项目配置

**Files:** Create `pyproject.toml`, `constraints/ui.txt`, `src/bproc_core/{__init__,config,project_io}.py`, `tests/{conftest,factories}.py`, `tests/unit/test_project.py`; Modify `.gitignore`, `README.md`。

**Interfaces:** Produces `new_project(base_dir: Path) -> ProjectConfig`、`validate_project(config: ProjectConfig, *, check_files: bool=True) -> tuple[ValidationIssue,...]`、`load_project(path: Path) -> ProjectConfig`、`save_project(config: ProjectConfig, path: Path) -> None`。`ValidationIssue(field,message,severity)`；解析错误为 `ConfigError`。

- [ ] **Step 1:** 建最小可安装包和 core/dev 依赖组，忽略 `.venv/`, `.artifacts/`, `__pycache__/`；执行 `python3 -m venv .venv` 与 `.venv/bin/python -m pip install -e '.[dev]' -c constraints/ui.txt`。只安装到新 UI 环境，不改 `bop`。
- [ ] **Step 2:** 写配置红测试，工厂提供 IDs 2/7、独立 key 和标准默认相机；加入缺失资源、重复 key/obj_id、禁用中心、空区域、未知 schema、零/负距离、skew 和 YAML 重复键用例。
```python
def test_defaults_and_roundtrip(tmp_path):
    p = make_project(tmp_path); save_project(p, tmp_path / '中文 项目.yaml')
    q = load_project(tmp_path / '中文 项目.yaml')
    assert q == p and q.camera.width == 640 and q.camera.fx == 572.4114
    assert q.output.format == 'rgb_masks' and q.occlusion.max_attempts == 30
def test_invalid_nonfinite_and_reference(tmp_path):
    p = replace(make_project(tmp_path), anchor_key='missing')
    assert 'anchor_key' in {e.field for e in validate_project(p, check_files=False)}
    p = replace(p, camera=replace(p.camera,fx=float('nan')))
    assert 'camera.fx' in {e.field for e in validate_project(p, check_files=False)}
```
- [ ] **Step 3:** Run `.venv/bin/python -m pytest tests/unit/test_project.py -q`；应因配置接口缺失失败，不把依赖安装失败当成红测试。
- [ ] **Step 4:** 在 `config.py` 实现上述数据类、默认值与校验。`new_project` 是允许暂缺资源的编辑草稿，导出/运行校验仍拒绝缺项；无倾斜 K、至少一个启用目标/有效中心/区域/环境。
- [ ] **Step 5:** 在 `project_io.py` 用严格键检查的 safe YAML 读写；相对资源路径相对项目文件解析，导出快照绝对化。load/save 允许资源暂缺的编辑草稿，完整 validate_project 负责导出/运行就绪校验，不能因此禁止新项目保存。写入临时文件后原子替换项目配置，所有文本 UTF-8。
- [ ] **Step 6:** 重跑该测试文件，应全部 PASS；确认含单引号与 `$()` 的路径保持字面值。
- [ ] **Step 7:** `git add pyproject.toml constraints src/bproc_core tests .gitignore README.md`；`git commit -m "feat: add validated project configuration"`。

### Task 2: 保持原点的网格与单位处理

**Files:** Create `src/bproc_core/assets.py`, `tests/unit/test_assets.py`; extend `tests/factories.py`。

**Interfaces:** Consumes `AssetSpec/TargetSpec`。Produces `load_mesh(asset: AssetSpec) -> MeshData`、`mesh_diameter_m(mesh: MeshData) -> float`、`write_bop_models(meshes: Mapping[str,MeshData], targets: Sequence[TargetSpec], models_dir: Path) -> None`、`suggest_ranges(meshes: Mapping[str,MeshData], targets: Sequence[TargetSpec], camera: CameraSpec) -> tuple[Box,Range]`；无效网格抛 `AssetError`。

- [ ] **Step 1:** 写混合单位与偏置原点红测试，另测无面点云、空网格、非有限顶点、平面网格、单位错误和不连续对象 ID。
```python
def test_offset_mm_mesh_keeps_its_origin(tmp_path):
    asset = write_cube_fixture(tmp_path, unit='mm', center=(100,-50,25), size=100)
    m = load_mesh(asset)
    np.testing.assert_allclose(m.bounds_m.mean(axis=0), [.1,-.05,.025])
    assert mesh_diameter_m(m) == pytest.approx(np.sqrt(3)*.1)
    write_bop_models({m.key:m}, [target_for(asset, obj_id=7)], tmp_path/'models')
    assert exported_vertices(tmp_path/'models/obj_000007.ply').mean(axis=0) == pytest.approx([100,-50,25])
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_assets.py -q`，预期缺少资产接口而 FAIL。
- [ ] **Step 3:** 用 `trimesh.load(..., process=False)` 读取面网格，不自动居中/合并顶点；仅在读取时应用单位因子。PLY 目标与 PLY/OBJ 遮挡物形成三角面数组，保留可用颜色；不支持的透明表面明确报错。
- [ ] **Step 4:** 实现毫米模型与 `models_info.json`；直径在凸包顶点上分块求最大距离，平面/退化凸包回退原顶点，避免分配整个 NxN 矩阵。初始位置/距离建议考虑全部目标包围盒与 FOV，不覆盖用户已编辑值。
- [ ] **Step 5:** 重跑资产文件，应 PASS；输出顶点不得减去中心，原资源字节不变。
- [ ] **Step 6:** `git add src/bproc_core/assets.py tests`；`git commit -m "feat: add unit-aware mesh resources"`。

### Task 3: 球坐标、区域分配与校准相机数学

**Files:** Create `src/bproc_core/{camera,sampling}.py`, `tests/unit/test_camera_sampling.py`。

**Interfaces:** Produces `allocate_regions(total: int, keys: Sequence[str]) -> dict[str,int]`、`request_rng(seed: int, request_index: int, attempt_index: int) -> np.random.Generator`、`sample_camera(region: RegionSpec, center_m: np.ndarray, anchor_rotation: np.ndarray, camera: CameraSpec, rng) -> CameraPose`、`project_points(points_m, pose: CameraPose, camera: CameraSpec) -> tuple[np.ndarray,np.ndarray]`、`projection_matrix(camera: CameraSpec, near: float, far: float) -> np.ndarray`。

- [ ] **Step 1:** 写固定值、170°→−170°、全方位、极点、非对称 K、分辨率缩放、球面均匀分布和坐标旋转红测试。
```python
def test_fixed_camera_and_pixel_axes():
    p = sample_fixed_camera(r=1, phi=0, alpha=0)
    uv, valid = project_points(np.array([[0,0,0],[0,.05,0],[0,0,.05]]), p, default_camera())
    assert valid.all(); np.testing.assert_allclose(uv[0], [325.2611,242.04899])
    assert uv[1,0] > uv[0,0] and uv[2,1] < uv[0,1]
def test_region_budget_and_solid_angle():
    assert allocate_regions(5, ['a','b','c']) == {'a':2,'b':2,'c':1}
    assert abs(sample_sin_elevation_mean(n=5000, seed=42, lo=0, hi=90)-.5) < .02
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_camera_sampling.py -q`；预期新接口缺失而 FAIL。
- [ ] **Step 3:** 实现 SeedSequence `[seed,request_index,attempt_index]`，不操作全局随机源；固定角度不能误当整周。r 均匀、φ 沿正向跨界均匀、sin(α) 均匀。
- [ ] **Step 4:** 实现列向量 look-at：GL 相机 +X 右、+Y 上、−Z 前；极点使用中心模型局部 +Y 作为备用上方向。OpenCV 转换只在投影/BOP 边界做 `[X,-Y,-Z]`。
- [ ] **Step 5:** 钉住像素中心约定：图像边界 `[-.5,W-.5]×[-.5,H-.5]`，P 的前两行分别为 `[2fx/W,0,1-2(cx+.5)/W,0]`、`[0,2fy/H,2(cy+.5)/H-1,0]`；近远平面用标准 GL 形式。新增断言 P 主点项，禁止仅靠 fov。
- [ ] **Step 6:** 重跑该测试文件，应 PASS；将相同默认区域固定随机结果保存为少量 golden 数值，不保存大数组。
- [ ] **Step 7:** `git add src/bproc_core tests/unit/test_camera_sampling.py`；`git commit -m "feat: add calibrated spherical camera sampling"`。

### Task 4: 可复现的多目标候选场景

**Files:** Create `src/bproc_core/scene.py`, `tests/unit/test_scene_candidates.py`。

**Interfaces:** Consumes 前述配置、MeshData、CameraPose。Produces `propose_candidate(config: ProjectConfig, meshes: Mapping[str,MeshData], request_index: int, attempt_index: int) -> SceneCandidate | Rejection`、`candidate_to_dict(candidate: SceneCandidate) -> dict` 和共享的 `Placement/SceneCandidate/Rejection` 数据类；序列化将 Path/数组转为字符串/list，供测试和 manifest 共用。

- [ ] **Step 1:** 写中心切换、偏移盒、碰撞/出画拒绝、固定距离、HDRI/光源可复现和无遮挡请求分配红测试。
```python
def test_center_moves_objects_not_local_vertices(project, meshes):
    c = propose_valid_candidate(project, meshes, request=0, attempt=0)
    np.testing.assert_allclose(world_center(c, project.anchor_key, meshes), [0,0,0])
    assert all(offset_in_own_box(p, c, project, meshes) for p in non_anchor_placements(c))
    assert candidate_to_dict(c) == candidate_to_dict(propose_valid_candidate(project, meshes, request=0, attempt=0))
def test_impossible_boxes_return_reason(project, meshes):
    assert propose_candidate(colliding_fixed_boxes(project), meshes, 0, 0).reason == 'collision'
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_scene_candidates.py -q`，预期 FAIL。
- [ ] **Step 3:** 中心平移设为 `-R @ local_bbox_center`；其他模型按中心局部 Box 采样其包围盒中心，再反算对象平移。先采机位，随后摆目标和遮挡物，拒绝 AABB 穿插、相机位于物体包围盒内部及完整投影出画。
- [ ] **Step 4:** 默认用保守 AABB 拒绝，首版不加 FCL。遮挡物沿中心目标视线附近采样、按目标投影尺度给尺寸建议；数量/缩放服从配置。未穿插但是否真实遮挡交给 Task 5/6 的像素验收。
- [ ] **Step 5:** 每个请求的区域和无遮挡类别预先确定；无遮挡数量按 `floor(N*f+.5)`、种子打乱，attempt 不能改变类别。所有对象/环境/点光源状态写入候选，预览刷新只改变显式 preview seed。
- [ ] **Step 6:** 重跑候选场景测试，应 PASS；不同 attempt 可变化，同 request/attempt 精确复现；两个默认目标可产生有效候选。
- [ ] **Step 7:** `git add src/bproc_core/scene.py tests/unit/test_scene_candidates.py`；`git commit -m "feat: generate reproducible multi-target scenes"`。

### Task 5: Blender 单帧后端与可恢复的逐目标分割

**Files:** Create `src/bproc_core/masks.py`, `src/bproc_runtime/{__init__,contracts,blender_backend}.py`, `tests/unit/test_backend_masks.py`, `tools/create_demo.py`。

**Interfaces:** Produces `contracts.Backend` Protocol：`prepare(config, meshes) -> None`、`apply(candidate: SceneCandidate) -> None`、`assess(anchor_obj_id: int) -> VisibilityEvidence`、`render(evidence: VisibilityEvidence) -> RenderedFrame`、`close() -> None`，以及 `BlenderBackend(api=None)`；api=None 时才加载真实 Blender API。`contracts.RenderError` 是后端异常；core.masks 提供 `decode_instances(segmap, attributes, obj_ids) -> dict[int,np.ndarray]`、`occlusion_ratio(evidence, anchor_obj_id) -> float`。

- [ ] **Step 1:** 写实例值不同于对象 ID、完全不可见对象、隔离过程异常后恢复、单帧状态和 RGB 后再恢复全部对象的红测试；用 fake MeshObject/renderer 测状态，不要求普通 pytest 导入 bpy。
```python
def test_decode_ids_are_not_segmentation_values():
    masks = decode_instances(np.array([[0,3,8]]), [{'idx':3,'ui_target_id':2},{'idx':8,'ui_target_id':7}], [2,7])
    assert masks[2].tolist() == [[False,True,False]]
    assert masks[7].tolist() == [[False,False,True]]
def test_isolation_restores_on_failure(backend_with_fake_api):
    backend, api = backend_with_fake_api
    before = api.hidden_states(); api.fail_on_segmentation_call = 2
    with pytest.raises(RenderError): backend.assess(2)
    assert api.hidden_states() == before
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_backend_masks.py -q`；预期 FAIL。
- [ ] **Step 3:** 在后端构造实际网格：将 Task 2 的米制局部顶点/三角面通过 `bpy.data.meshes` 和 `bproc.object.create_from_blender_mesh` 建模，scale=1，保留原点；PLY 顶点颜色建颜色属性和基础 shader。设置 `category_id=obj_id` 和 `ui_target_id=obj_id`，专门遮挡物不列为目标。
- [ ] **Step 4:** 每次 apply 固定 `frame_start=0, frame_end=1, frame_set(0)`，`add_camera_pose(...,frame=0)`；明确设置 K、所有对象、点光源、HDRI/旋转/强度。采用当前状态逐张渲染，不积累动画关键帧。
- [ ] **Step 5:** 调用 `render_segmap(map_by=['instance','cp_ui_target_id'], default_values={'cp_ui_target_id':0}, use_alpha_channel=False)`，从 `instance_segmaps[0]` 和 `instance_attribute_maps[0]` 解码。完整场景取得所有 visible，隔离中心取得 baseline；隔离使用 MeshObject 的 `hide/is_hidden`、try/finally，不跨 Undo 持有原生 bpy 对象引用。
- [ ] **Step 6:** `render` 在接受候选后才做 RGB；轻格式补齐其他目标的 full masks，复用已有中心 baseline；BOP 返回米制 depth。分割与颜色每次必须长度为 1，临时目录互不冲突。
- [ ] **Step 7:** 在 `tools/create_demo.py` 实现 `create_demo(root: Path, blender: Path) -> Path`：生成两个不对称且原点有偏移的 ASCII PLY、遮挡物、demo YAML；用独立 Blender 的 `bpy.data.images` 生成极小 HDRI，不联网下载，也不把源码目录的空 asserts 当成已有资源。
- [ ] **Step 8:** 重跑后端掩码单测应 PASS；`python -m compileall src/bproc_runtime tools/create_demo.py` 应成功。真实 Blender 验证统一在 Task 9/14，不把 fake 后端通过记作真实渲染通过。
- [ ] **Step 9:** `git add src/bproc_core/masks.py src/bproc_runtime tests/unit/test_backend_masks.py tools/create_demo.py`；`git commit -m "feat: add isolated Blender mask backend"`。

### Task 6: 有界采样、统计与中断语义

**Files:** Create `src/bproc_runtime/{run,records}.py`, `tests/unit/test_run.py`。

**Interfaces:** Consumes Backend、候选函数。Produces `run.run_project(config: ProjectConfig, meshes, backend: Backend, writer: FrameWriter, recorder: RunRecorder) -> RunSummary`；`run.FrameWriter` Protocol 的 `write(index,candidate,frame) -> FrameRef`、`finalize() -> None`；`records.RunRecorder` Protocol 的 `begin(config)`, `record_frame(ref,candidate,evidence)`, `record_rejection(request_index,attempt_index,reason)`, `finish(summary)`，均返回 None。FrameRef/RunSummary 定义在 records.py；candidate_to_dict 消费 Task 4 的接口。

- [ ] **Step 1:** 用 fake Backend/Writer 写预算和状态红测试，包括未知目标 ID、零 baseline、其他目标全遮挡、写出错误、BOP finalize 抛错及 KeyboardInterrupt；后处理失败必须 failed、saved=0、written_frames 保留数量。按请求 ID 和尝试数核验，不只测总计数。
```python
def test_retry_budget_is_shared(config, always_reject_backend, recorder):
    config = replace(config, num_samples=2)
    result = run_with_fakes(config, always_reject_backend, recorder)
    assert result.saved == 0 and result.skipped == 2 and result.exit_code == 2
    assert recorder.candidate_attempts == 60
def test_partial_is_not_success(config, one_success_backend):
    r = run_with_fakes(replace(config,num_samples=2), one_success_backend)
    assert (r.status,r.saved,r.skipped,r.exit_code) == ('partial',1,1,2)
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_run.py -q`，预期 FAIL。
- [ ] **Step 3:** 每请求最多 max_attempts 次，几何拒绝和像素拒绝共用循环；anchor baseline>0，所有目标 visible>0；无遮挡要求中心额外遮挡为 0，否则闭区间 `ratio.minimum <= value <= ratio.maximum`。候选被接受后才 render/write，write 返回后累加 written_frames；轻格式可立即累加 saved，BOP 只有 finalize 成功才把这批 written_frames 计为 saved，后处理失败时不虚报成功。
- [ ] **Step 4:** 实现 recorder 的 JSON 友好记录：帧号/区域/中心/所有位姿/相机/HDRI/光源/遮挡/seed；requested/written_frames/saved/skipped 与原因统计分开，最终 skipped=requested-saved，包含未尝试部分。运行状态 `running/completed/partial/failed/interrupted`；退出码分别 0/2/1，SIGINT=130、SIGTERM=143，尽力 finalize 已接受帧并关闭 backend。
- [ ] **Step 5:** 未捕获后端/写出异常视为 fatal，不继续用可能污染的场景；summary 记录未完成，`finally` 释放后端。SIGKILL 留 running 标记，不伪造 completed；不提供自动恢复。
- [ ] **Step 6:** 重跑运行单测，应 PASS；增加已保存帧被 manifest 唯一引用、失败写出不计成功的断言。
- [ ] **Step 7:** `git add src/bproc_runtime tests/unit/test_run.py`；`git commit -m "feat: bound sampling retries and record run results"`。

### Task 7: 安全写出 RGB 与逐目标双掩码

**Files:** Create `src/bproc_runtime/{output,rgb_masks}.py`, `tests/unit/test_rgb_masks.py`。

**Interfaces:** Produces `output.reserve_dataset(root: Path, dataset_name: str) -> Path`、`rgb_masks.RgbMasksWriter(dataset_dir: Path, config: ProjectConfig)` 实现 FrameWriter（`root=dataset_dir`）、`output.JsonRunRecorder(metadata_dir: Path)` 实现 RunRecorder。`OutputConflict` 表示目录冲突。

- [ ] **Step 1:** 写 PNG 二值/对象 ID/尺寸、写中断及路径保护红测试；也测空旧目录、软链接、断链和已有标记文件均保持不变。
```python
def test_per_object_masks_and_ids(writer, frame, candidate):
    writer.write(0,candidate,frame)
    assert image_names(writer.root/'mask') == ['000000_000002.png','000000_000007.png']
    assert set(np.unique(read_png(writer.root/'mask/000000_000007.png'))) <= {0,255}
    assert np.all(read_mask(writer.root/'mask_visib/000000_000007.png') <= read_mask(writer.root/'mask/000000_000007.png'))
def test_existing_output_is_never_cleared(tmp_path):
    root=tmp_path/'data'; root.mkdir(); (root/'keep').write_text('user')
    with pytest.raises(OutputConflict): reserve_dataset(tmp_path,'data')
    assert (root/'keep').read_text() == 'user'
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_rgb_masks.py -q`；预期 FAIL。
- [ ] **Step 3:** 使用 `mkdir(exist_ok=False)` 原子预留最终 dataset，包含空目录/软链接/断链一律拒绝；dataset_name 必须是单个安全路径组件。立即写 running 状态，run_metadata 与三个图片目录分开。
- [ ] **Step 4:** PNG 默认、JPEG 仅作用于 RGB；mask 按 bool→uint8×255 写无损 PNG。拒绝不等尺寸、缺失/额外目标、visible 非 full 子集；文件名后缀用 obj_id，绝不使用临时 segmap 值。
- [ ] **Step 5:** 每帧先写本任务 staging，成功才发布图片并提交 manifest；写出失败时停止任务并写 failed，不能增加 saved。metadata 用同目录临时文件加 os.replace；只清理本次新建 staging，绝不递归清空用户目录。
- [ ] **Step 6:** 重跑写出单测应 PASS；检查三个图片目录没有 JSON/深度/位姿，run_metadata 有对象映射和项目快照。
- [ ] **Step 7:** `git add src/bproc_runtime tests/unit/test_rgb_masks.py`；`git commit -m "feat: export RGB and per-target masks safely"`。

### Task 8: 多目标 BOP 2.8.0 适配与显式标注后处理

**Files:** Create `src/bproc_runtime/{bop_output,bop_postprocess}.py`, `tests/unit/test_bop_output.py`, `tests/blender/check_bop.py`。

**Interfaces:** Produces `BopWriter(dataset_dir, config, meshes, backend)` 实现 FrameWriter；`finalize_bop(dataset_dir: Path, config: ProjectConfig, meshes) -> None`；`initialize_bop_worker(width,height,trimesh_objects,near_m,far_m) -> None`；bop_output 内的 `bop_translation(t_m: Sequence[float]) -> list[float]`、`depth_capacity_m(scale_mm: float) -> float`。只在此适配层依赖 BlenderProc 2.8.0 的私有 BOP helpers。

- [ ] **Step 1:** 写调用契约、单位、GT 顺序、chunk 及深度范围红测试。fake 只测试本项目传参与映射，真实标准结构在 Task 14 校验。
```python
def test_bop_uses_frame_zero_and_sorted_targets(bop_adapter_spy, frames):
    writer, api = bop_adapter_spy
    refs = [writer.write(i,candidate_for(i),frame) for i,frame in enumerate(frames)]
    assert api.calls_all_have(frame_start=0,frame_end=1,color_count=1,depth_count=1)
    assert all(ref.gt_obj_ids == (2,7) for ref in refs)
def test_mm_pose_and_depth_capacity():
    assert bop_translation([.1,-.2,.3]) == pytest.approx([100,-200,300])
    assert depth_capacity_m(.1) == pytest.approx(6.5535)
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_bop_output.py -q`；预期 FAIL。
- [ ] **Step 3:** 先用 Task 2 写全部 canonical models；target_objects 按 obj_id 排序。每次只写帧 0、colors/depths 长度 1，`annotation_unit='mm', depth_scale=.1, frames_per_chunk=1000, append_to_existing_output=True, calc_mask_info_coco=False, ignore_dist_thres=float('inf')`，避免原生距离过滤静默丢目标。编码默认 JPEG，用户显式选 PNG 时传 PNG。首次也只追加到本次独占新目录。
- [ ] **Step 4:** 深度输入是米，非有限背景设无效 0；超出 16-bit depth_scale 可表示范围的有效几何深度报错，不能饱和后继续。FrameRef 记录 writer 实际 chunk/image 号，不能按对象 ID 猜 GT 索引。
- [ ] **Step 5:** finalize 显式调用 `_BopWriterUtility.calc_gt_masks(pool, chunk_dirs, starting_frame_id=0, annotation_scale=1000., delta=.015)` / `calc_gt_info` 的相同参数，不调用会 setup_pip 的完整 writer 路径；toolkit 从已校验的本机路径导入。Linux EGL、一个专用 worker 为首版默认，pyrender 只在 worker 导入；COCO 不生成。
- [ ] **Step 6:** `_pyrender_init` 后在专用 worker 内封装 `pyrender.IntrinsicsCamera`，用该任务真实 near/far，避免原 helper 的硬编码 znear=.1 截掉近距离小模型；不污染主进程。用实际最小正深度的四分之一给安全 near，下限 1e-6。测试 helper 传 .1 时仍采用校准 near；未知 API 形状明确报不兼容。
- [ ] **Step 7:** 只有 masks/info 后处理完成才能标 completed；失败保留 failed 状态。重跑 BOP 单测，应 PASS；`tests/blender/check_bop.py` 实现结构、GT/模型单位、掩码索引与深度恢复检查供 Task 14 使用。
- [ ] **Step 8:** `git add src/bproc_runtime tests/unit/test_bop_output.py tests/blender/check_bop.py`；`git commit -m "feat: add complete multi-target BOP output"`。

### Task 9: 离线预检与可脱离仓库运行的任务导出

**Files:** Create `src/bproc_export/{__init__,bundle,commands}.py`, `src/bproc_export/templates/{run_job,render_scene}.py`, `src/bproc_runtime/{preflight,cli}.py`, `tests/unit/test_export.py`, `tests/unit/test_launcher.py`。

**Interfaces:** Produces `export_job(config: ProjectConfig, destination: Path) -> ExportResult`（`directory, command, preview_command`）；`build_command(job_dir: Path, conda_env: str, *, num_samples: int|None=None, output: Path|None=None) -> str`；`cli.main(argv: Sequence[str]|None=None) -> int`。启动模板独立 `main(argv=None)`，不得导入共享核心后才检查 Python 3.8。

- [ ] **Step 1:** 写隔离导出、含中文/引号/`$()` 的命令、拒绝重叠目录、缺离线缓存不启动 Blender、轻格式不要求 toolkit、格式覆盖生效的红测试。
```python
def test_bundle_is_standalone(tmp_path, project):
    job=export_job(project,tmp_path/'中文 $() 任务')
    assert shlex.split(job.command)[-1] == str(job.directory/'scene.yaml')
    assert first_effective_line(job.directory/'render_scene.py') == 'import blenderproc as bproc'
    assert not copied_packages(job.directory) & {'bproc_ui','PySide6','vispy'}
def test_missing_offline_cache_stops_before_import(fake_installation):
    result = run_launcher_probe(fake_installation, cache_exists=False)
    assert result.exit_code != 0 and result.blenderproc_start_count == 0
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_export.py tests/unit/test_launcher.py -q`；预期 FAIL。
- [ ] **Step 3:** 显式复制 `bproc_core/`、`bproc_runtime/` 和两个模板到新 job；YAML 资源绝对化。对最终 export/dataset 路径检查输入资源和彼此重叠；原子 mkdir 独占新 job，在内部 staging 写文件，最后发布 `launch.json` 的 ready 标志。已有目录一律拒绝，半成品不能启动，不实现替换旧 job。
- [ ] **Step 4:** exporter 生成 `launch.json`：`schema_version=1, ready=true, blender_dir, blender_python, custom_packages, blenderproc_executable, blenderproc_package_dir`，所有路径已解析。3.8 stdlib 启动器据此定位实际 Blender Python，不自行解析 YAML；用 argv 调用 runtime/preflight.py，该探针先加已知自定义包路径，再读 YAML/覆盖参数、校验模块位置和 DefaultConfig 默认包/cache 的精确匹配，全程不 import bproc。DefaultConfig 包列表通过 AST 读取当前版本静态列表，不执行该文件。缺缓存/不一致就退出，给人工准备步骤；cache 不能代替真实模块检查。配置中的运行时定位字段与 launch.json 不一致时拒绝并要求重新导出，普通资源/采样参数仍可直接改 YAML。
- [ ] **Step 5:** 检查通过才执行 BlenderProc；子进程设 `PIP_NO_INDEX=1`, `PIP_DISABLE_PIP_VERSION_CHECK=1`。实际 render 入口首行固定 bproc import，随后设置 runtime 相对 __file__ 的路径；首 import 后将后续 `SetupUtility.setup_pip` 替为明确拒绝安装的 guard。BOP 使用 Task 8 显式后处理避开安装路径，未通过版本/能力检查不启动任务。
- [ ] **Step 6:** CLI 支持 `--config`, `--format rgb_masks|bop`, `--num-samples`, `--output`；覆盖在完整校验前应用。启动器透传相同参数；`--check-only` 只预检、不创建数据集。README 模板推荐 `conda run --no-capture-output -n bop python <job>/run_job.py --config <job>/scene.yaml`，用 shlex.join 生成，subprocess 禁用 shell=True。
- [ ] **Step 7:** 重跑导出/启动器单测应 PASS；再用 `bop/bin/python` 对模板执行 py_compile，证明 3.8 可解析。实际导出复制到临时目录、cwd=/tmp、清除 PYTHONPATH，执行 `run_job.py --check-only`；当前安装若不满足，必须明确失败并记录待人工准备项，不能偷偷修环境。
- [ ] **Step 8:** `git add src/bproc_export src/bproc_runtime tests/unit/test_export.py tests/unit/test_launcher.py`；`git commit -m "feat: export standalone offline-checked render jobs"`。

### Task 10: Qt 项目状态与可读写项目的桌面工作区

**Files:** Create `src/bproc_ui/{__init__,__main__,app,state,theme,main_window}.py`, `tests/ui/test_project_state.py`; Modify `pyproject.toml`, `README.md`。

**Interfaces:** Produces `ProjectState(config: ProjectConfig)`，属性 `config, edited_target_key, meshes, candidate, dirty`；方法 `set_project(config)`, `select_target(key)`, `set_anchor(key)`, `set_meshes(meshes)`, `set_candidate(candidate)`, `load(path)`, `save(path)`；信号 `project_changed`, `selection_changed`, `meshes_changed`, `candidate_changed`, `dirty_changed`。`create_app(argv) -> QApplication`、`app.main(argv: Sequence[str]|None=None) -> int`、`MainWindow(state: ProjectState)`；CLI `blenderproc-ui [--project PATH]` 指向 `bproc_ui.app:main`。

- [ ] **Step 1:** 在现有 .venv 安装 `pip install -e '.[gui,dev]' -c constraints/ui.txt`，显式使用 `QT_API=pyside6`；若缺 xcb/EGL 系统库，先报告 ldd 缺项和安装说明，不污染其他 Qt 环境。
- [ ] **Step 2:** 写 pytest-qt 状态测试，Canvas 由 fake 工厂注入，不要求 offscreen 平台建立 OpenGL context。
```python
def test_edit_selection_does_not_change_camera_center(state):
    original=state.config.anchor_key
    state.select_target('target-7')
    assert state.config.anchor_key == original and not state.dirty
    state.set_anchor('target-7')
    assert state.config.anchor_key == 'target-7' and state.dirty
def test_project_load_restores_ids_and_units(state, tmp_path):
    state.save(tmp_path/'项目.yaml'); state.load(tmp_path/'项目.yaml')
    assert [t.obj_id for t in state.config.targets] == [2,7]
```
- [ ] **Step 3:** Run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui/test_project_state.py -q`；预期接口缺失 FAIL。
- [ ] **Step 4:** 状态使用不可变配置替换，编辑选择/观察机位不标 dirty，科学参数改变才标 dirty；禁止把中心设为缺失/禁用目标，停用/删除中心前让用户选替代中心。
- [ ] **Step 5:** 创建紧凑灰色 MainWindow：顶部工作区、可调 splitter、对象树、属性区、底部采样/命令区；实现新建/打开/保存/另存、未保存提示和状态栏。theme 接近已确认草图的灰色、橙色选中、蓝色激活，不复制无功能 Blender 建模菜单。
- [ ] **Step 6:** 先给三个面板和两个视口保留明确挂载点，不加入伪渲染按钮。重跑状态/UI 文件应 PASS；实际桌面 `blenderproc-ui` 能打开、保存草稿和关闭。
- [ ] **Step 7:** `git add src/bproc_ui tests/ui pyproject.toml README.md`；`git commit -m "feat: add native Blender-style project workspace"`。

### Task 11: 真实网格视口和独立校准相机窗口

**Files:** Create `src/bproc_ui/views/{__init__,scene_view,camera_view,projection}.py`, `src/bproc_ui/preview.py`, `tests/unit/test_view_projection.py`, `tests/ui/test_camera_window.py`, `tools/gui_smoke.py`。

**Interfaces:** Produces `SceneView.set_scene(candidate, meshes)`, `set_regions(config)`, `set_candidates(candidates)`；`CameraPreview.set_scene(candidate, meshes)`, `set_camera(camera: CameraSpec, pose: CameraPose)`, `show_preview()`；`PreviewController(state).refresh(region_key, sample_index, preview_seed)`；`letterbox_rect(window_width,window_height,image_width,image_height) -> tuple[int,int,int,int]`。

- [ ] **Step 1:** 写纯投影和 Qt 生命周期红测试，涵盖高 DPI 的留边比例、非对称主点、不同 fx/fy、主视口拖动不改采样、窗口关闭/重开复用实例。测试 helper `preview_pixels` 必须实际调用 VisPy MatrixTransform.map 再换到像素坐标，不能转调 core.project_points 让两侧变成同一个实现。
```python
def test_vispy_matrix_matches_core_projection(camera, pose, points):
    assert preview_pixels(points,pose,camera) == pytest.approx(project_points(points,pose,camera)[0])
    assert letterbox_rect(800,800,640,480) == (0,100,800,600)
def test_camera_window_reopens_without_replacing_context(qtbot, preview):
    canvas=preview.canvas; preview.close(); preview.show_preview()
    assert preview.isVisible() and preview.canvas is canvas
```
- [ ] **Step 2:** Run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/unit/test_view_projection.py tests/ui/test_camera_window.py -q`，预期 FAIL。
- [ ] **Step 3:** `vispy.app.use_app('pyside6')`；SceneCanvas.native 加 Qt 布局。主视图用 TurntableCamera，Mesh/Line/Markers 显示真实网格、橙色选择、坐标轴、球面区域和机位；主/相机视图各建自己的 Visual，复用只读数组，不能共享有父节点的 Visual。
- [ ] **Step 4:** 相机视图使用 VisPy 0.15.2 的 PerspectiveCamera 投影钩子和 MatrixTransform，传入 Task 3 列向量矩阵的转置；world_from_camera 也仅在 VisPy 边界转置。严格固定 K 与宽高比，不用 fov 替代主点。Qt 高 DPI 换算仅作用于视口矩形，不缩放相机科学参数。
- [ ] **Step 5:** CameraPreview 用非删除式浮动 QDialog，close 只 hide，菜单可重开；只绘目标/遮挡物。标题显示中心、区域/机位和“候选机位构图预览”；选择其他编辑模型不改变中心。
- [ ] **Step 6:** PreviewController 调用同一个 propose_candidate，以选中区域、sample_index 和显式 preview_seed 构造最多 30 次的几何候选；不伪称通过遮挡验收，不修改持久配置 seed。无有效候选时清楚显示原因，保留可编辑界面。
- [ ] **Step 7:** 重跑相关测试应 PASS；用 `tools/gui_smoke.py --project <demo.yaml> --output .artifacts/gui` 在真实桌面检查网格、留边、球区和独立窗口，截图记录。投影集成 oracle 用 bpy 的 `calc_matrix_camera` 和实际不对称标记图，禁止用丢失主点项的 bproc.get_projection_matrix 代替。
- [ ] **Step 8:** `git add src/bproc_ui tests/unit/test_view_projection.py tests/ui/test_camera_window.py tools/gui_smoke.py`；`git commit -m "feat: add mesh and calibrated camera previews"`。

### Task 12: 多模型、渲染中心、随机位置与 Haven 资源面板

**Files:** Create `src/bproc_ui/panels/{__init__,resources}.py`, `src/bproc_ui/asset_loader.py`, `tests/ui/test_resources_panel.py`; Modify `main_window.py`。

**Interfaces:** `ResourcesPanel(state: ProjectState)`；`AssetLoader.request(asset: AssetSpec, generation: int) -> None`，信号 `loaded(key,generation,MeshData)`, `failed(key,generation,message)`。消费者只把与当前资源路径/单位/代次一致的结果放入 state.meshes。

- [ ] **Step 1:** 写多选导入、编辑/中心独立、异步旧结果丢弃、移除中心保护、HDRI 扫描和批量位置范围应用的红测试。
```python
def test_stale_mesh_result_cannot_restore_removed_target(panel, loader):
    panel.add_target('a.ply'); old=loader.last_generation
    panel.remove_target('a'); loader.emit_loaded('a',old,mesh_a())
    assert 'a' not in panel.state.meshes
def test_bulk_offsets_keep_distinct_ids(panel):
    panel.apply_offsets_to_selected(box_fixture())
    assert [t.obj_id for t in panel.state.config.targets] == [2,7]
```
- [ ] **Step 2:** Run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui/test_resources_panel.py -q`；预期 FAIL。
- [ ] **Step 3:** QFileDialog 批量选 PLY；列表分别提供 enabled、单位、obj_id 和中心选择。新条目分配未占用正整数 ID，改列表顺序不重编 ID；任何移除只改变配置，不删除源文件。
- [ ] **Step 4:** 中心显示固定原点，非中心显示局部 X/Y/Z 偏移范围；范围应用到其他选中模型时复制值，不共享可变引用。切中心保留编辑数值，提供显式“恢复建议范围”。
- [ ] **Step 5:** 解析网格放 QRunnable/QThreadPool，Qt/VisPy 上传留在 GUI 线程；失效 generation 不落状态。错误显示具体文件、单位和原因；支持单文件/目录遮挡物和 `.hdr/.exr` 递归列表、固定/随机、环境强度与 Z 旋转。
- [ ] **Step 6:** 连接对象树、PreviewController 与相机窗口；缺失资源清理对应旧缓存和预览引用。重跑资源 UI 文件应 PASS，手动导入两个不同尺度模型可看清并切换中心。
- [ ] **Step 7:** `git add src/bproc_ui tests/ui/test_resources_panel.py`；`git commit -m "feat: configure multi-target layouts and Haven environments"`。

### Task 13: 采样参数、输出格式与可复制命令的完整 UI 流程

**Files:** Create `src/bproc_ui/panels/{sampling,export}.py`, `tests/ui/test_sampling_export.py`; Modify `main_window.py`, `state.py`, `README.md`。

**Interfaces:** `SamplingPanel(state)`、`ExportPanel(state)`；后者只调用 `validate_project/export_job/build_command`，不启动 Blender 或子进程。当前 region key 与 sample index 属于会话选择，科学设置保存在 ProjectConfig。

- [ ] **Step 1:** 写固定角度/跨界编辑、区域增删复制、总数量、内参缩放和模式切换后实际导出快照的红测试；不写主题颜色/控件排列断言。
```python
def test_output_switch_exports_selected_mode(panel, tmp_path):
    panel.choose_format('rgb_masks'); job=panel.export_to(tmp_path/'job')
    assert load_project(job.directory/'scene.yaml').output.format == 'rgb_masks'
    argv=shlex.split(job.command)
    assert '--format' not in argv or argv[argv.index('--format')+1] == 'rgb_masks'
def test_resolution_scales_intrinsics_once(panel):
    panel.resize_image(1280,960)
    assert panel.state.config.camera.fx == pytest.approx(1144.8228)
    assert panel.state.config.camera.cx == pytest.approx(650.5222)
```
- [ ] **Step 2:** Run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui/test_sampling_export.py -q`，预期 FAIL。
- [ ] **Step 3:** 实现 r/φ/α、全方位开关、区域名称/复制/删除、中心选择、场景图片总数、seed、光源/HDRI/遮挡与高级相机 K。允许上下限相等和 φ 跨界，不自动排序破坏含义；展示区域分配、重叠/零分配提示。
- [ ] **Step 4:** 默认按比例缩放 fx/fy/cx/cy，手动模式不缩放；格式下拉只含“RGB + 双掩码”和“BOP”，文件说明分别展示帧号_obj_id 与标准 GT 索引，不向用户暴露中间 segmap 值。
- [ ] **Step 5:** 导出前集中展示阻塞校验与具体字段，不能用未完成表单导出；新目录导出后给完整命令及独立小样本输出目录命令。复制到剪贴板是唯一动作，不自动在终端执行；选择已有目录明确提示换目录。
- [ ] **Step 6:** 连接区域/机位选择到 PreviewController 和相机窗口；主观察拖动不回写相机范围。保存/重载恢复全部科学配置，QSettings 仅保存窗口几何和分隔布局。
- [ ] **Step 7:** 重跑该测试文件应 PASS；手动在 1024×768 完成“选两目标→设中心→改区域→看相机→选格式→保存→导出”，面板滚动可达全部控件，没有无效 Render 动作。
- [ ] **Step 8:** `git add src/bproc_ui tests/ui/test_sampling_export.py README.md`；`git commit -m "feat: complete sampling and render-job export workflow"`。

### Task 14: 两种格式的真实端到端验收与使用说明

**Files:** Create `tests/blender/{verify_runtime,check_rgb_masks,check_projection}.py`, `tests/unit/test_output_checkers.py`, `docs/{usage,validation}.md`; Modify `tests/blender/check_bop.py`, `tools/create_demo.py`, `README.md`, `.gitignore`。

**Interfaces:** `verify_rgb_masks(root: Path) -> list[str]`、`verify_bop(root: Path) -> list[str]`、`verify_projection(artifact_dir: Path) -> list[str]`、`verify_runtime(job_dir: Path) -> list[str]` 返回诊断列表，空列表表示成功；脚本 CLI 成功 exit 0、失败非零。`tools/create_demo.py` 接口保持 Task 5 不变。

- [ ] **Step 1:** 写检查器的红测试：正常小 fixture 返回空诊断；删除 mask、换错对象 ID 后缀、把毫米平移写成米后应返回非空诊断，不只检测文件存在。
```python
def test_verifier_detects_missing_target_mask(valid_rgb_masks_output):
    root=valid_rgb_masks_output
    assert verify_rgb_masks(root) == []
    (root/'mask/000000_000007.png').unlink()
    assert verify_rgb_masks(root)
```
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/test_output_checkers.py -q`，新检查接口未实现时应 FAIL。
- [ ] **Step 3:** 实现四个只读检查器和 CLI；单位检查需从模型+GT 投影验证，不以文件数量代替几何正确性。故意损坏的 fixture 只放 pytest tmp_path。
- [ ] **Step 4:** Run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/unit tests/ui -q`，应全部 PASS。仅因本任务新改动/失败才重复先前专项测试。
- [ ] **Step 5:** 用本机 Blender 生成 `.artifacts/demo`，IDs 固定为 `[2,7]`、一个毫米偏置原点目标和一个米制目标、一个遮挡物与最小 HDRI；导出两个新任务，其中至少一个目录名包含中文、空格、单引号和字面 `$()`。采样图 `64×48`、samples=8、4 张、固定 seed，保证至少一张无额外遮挡和一张有实际遮挡。
- [ ] **Step 6:** 在 `/tmp` 工作目录、移除 PYTHONPATH，以 `conda run --no-capture-output -n bop python <job>/run_job.py --check-only` 验证实际运行时。缺依赖时记录准确缺项和人工准备命令，不能安装后不记录，也不能跳过实际运行却声称验收成功。
- [ ] **Step 7:** 实际运行 `rgb_masks`，`check_rgb_masks.py` 检查 4 张 RGB、每图每个 ID 的两类掩码、0/255、子集、同分辨率、记录中实际中心遮挡率、位置范围与可见目标数。确认没有 BOP toolkit 也可走该模式，不输出 depth/pose/models。
- [ ] **Step 8:** 实际运行 BOP，`check_bop.py` 检查全部对象 GT、相机、深度/scale、canonical models、gt_info 和按 GT 索引命名的掩码；从毫米模型+位姿重投影至图像验证对齐。另用近于 .1 m 的小模型任务确认后处理不会被固定 near plane 截空。
- [ ] **Step 9:** `check_projection.py` 用 off-center cx/cy、fx≠fy 和不对称标记，比较核心/VisPy 与 `bpy.calc_matrix_camera` 及实际标记图，像素中心误差 ≤1 pixel；不以对称 FOV 矩阵作为 oracle。
- [ ] **Step 10:** 执行不可能布局与已有输出两个真实失败用例，验证 30 次上限、partial/非零状态、旧文件哈希不变；中断一次小任务，summary 必须未完成且后续拒绝覆盖。
- [ ] **Step 11:** 在真实 Ubuntu 桌面运行 GUI smoke，记录主视口、相机窗口、多目标中心切换、两种导出格式的截图、版本、命令、用时、实际成功/失败数。文档明确 GUI 不做 Cycles、原点/单位、中心局部坐标和两种掩码命名差异。
- [ ] **Step 12:** 完成 `docs/usage.md`（独立 UI venv 安装、系统库缺项诊断、资源/单位/中心、试渲染/批量命令、离线依赖准备）及 `docs/validation.md`（真实证据与剩余限制）。没有真实验证的项必须标记未验证，不能写“全通过”。
- [ ] **Step 13:** `git diff --check`；确认未修改原项目/conda/用户资源；`git add tests/blender tests/unit/test_output_checkers.py tools/create_demo.py docs README.md .gitignore`；`git commit -m "test: validate complete multi-target rendering workflow"`。
- [ ] **Step 14:** 做全分支独立审查；存在必须修复问题时完成修复和相关复验后交付。

## 需求覆盖表与实施顺序

| 设计部分 | 对应任务 / 证据 |
|---|---|
| Ubuntu 独立 UI 环境、原 bop 不污染 | 1、9、10、14 |
| 多目标/单位/原点/位置/渲染中心 | 1、2、4、10、12、14 |
| 球坐标边界、均匀分布、相机 K | 3、4、11、13、14 |
| Haven、点光源逐图状态 | 4、5、12、13、14 |
| 真实遮挡率、所有目标可见、有限重试 | 5、6、14 |
| RGB + mask + mask_visib | 5、7、9、13、14 |
| BOP 完整结构、GT 索引、毫米深度 | 2、5、8、9、14 |
| Blender 风格、多区域和独立相机窗口 | 10、11、12、13、14 |
| 项目保存恢复、关闭 UI 后独立任务 | 1、9、10、13、14 |
| 文件保护、离线预检、中断可辨认 | 6、7、9、14 |

串行依赖为 `1 → 2/3 → 4 → 5 → 6 → 7/8 → 9 → 10 → 11 → 12 → 13 → 14`。每个 Task 的测试与提交完成后才交接；跨包接口改变要先同步本计划和调用任务，不能默默改名。

## 规划自检记录

本节由计划作者自己核对，不能以分派一个计划 reviewer 代替。

- [x] 对照设计第 1–12 节逐项确认覆盖表无遗漏。
- [x] 扫描每一步：有可核查动作和明确接口，没有 TBD、泛化“处理边界”或不必要的完整实现体。
- [x] 对照 Interfaces、数据字段和测试名称，修正生产接口遗漏、数组直接相等比较和只测试 fake 自身逻辑的问题。
- [x] Review Focus 五项都有所属任务具体断言；补充真实含特殊字符路径、不同对象 ID 和近距离 BOP 验证。
- [x] 计划约为设计两倍篇幅，以接口、决定、测试和命令为主，没有展开生产函数实现体。

自检关闭的具体缺口：新增 launch.json 使标准库 3.8 启动器不必解析 YAML；明确草稿保存与渲染就绪校验分工；定义 FrameWriter/RunRecorder 所属模块和 candidate_to_dict；输出路径统一为 root/dataset_name；native BOP 帧 0、安装副作用和 near-plane 限制均安排适配及真实检查。此记录只表示计划自检完成，产品任务与测试仍未执行。

## 执行交接与资料

- 原计划在实施前完成；当前 14 个任务的产品内容已实现，执行偏差与真实证据见开头实施状态。
- 推荐逐任务子代理实施、逐任务新 reviewer 审核，最后全分支审查：本项目跨 Qt/GL、Python 3.8/3.10/3.11 和 BOP 坐标边界，及早审查比最终才发现错标更有价值。
- 用户随后明确授权“开始按方案进行”，本会话使用原生子代理并行实现并审查，无额外审批环节。
- 当前可用技能目录未发现 `superpowers:subagent-driven-development` / `superpowers:executing-plans`；执行前先查找所选技能。若仍缺失，明确报告来源与限制，不能声称已读取；不为本次写计划安装执行技能。
- Qt/VisPy 依据：[Qt for Python](https://doc.qt.io/qtforpython-6/gettingstarted.html)、[VisPy backend](https://vispy.org/installation.html)、[Qt 嵌入](https://vispy.org/gallery/scene/realtime_data/ex01_embedded_vispy.html)、[矩阵约定](https://vispy.org/api/vispy.visuals.transforms.linear.html)。
- 实际 BlenderProc 源码依据：本机 `CameraUtility.py`, `SegMapRendererUtility.py`, `BopWriterUtility.py`, `SetupUtility.py`；适配器针对已核查的 2.8.0，不以最新网络文档替代本机实际行为。
