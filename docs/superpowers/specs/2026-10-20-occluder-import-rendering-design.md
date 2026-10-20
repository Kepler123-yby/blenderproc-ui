# 遮挡物导入、单位与渲染状态修复（第十六轮）设计

日期：2026-10-20
基线：`2d84815`（tracked 干净）
证据根：`/home/kepler/rm_2027/blenderproc-ui/.artifacts/review16-occluder-reference/run-20261020-141959`

## 1. 目标与范围

用户反馈：从 UI 导入遮挡物后“遮挡渲染状态不对”。本设计以真实 `ResourcesPanel/ProjectState/AssetLoader`
导入链为入口，修复可复现的状态缺陷，并按参考（`bproc_project/dataset_rendering/samplers/obstacle.py`）
新增显式的**物理尺寸策略**；旧配置的表观尺寸算法、随机数顺序与既有测试契约保持不变。

不做：重写渲染器/材质/mask 管线；新增 scene-stable 遮挡策略；把近似高度当遮挡真值；执行参考
`render.py`（其中 `shutil.rmtree()` 会删除 `bop_data`）。

## 2. 修改前真实复现（生产代码未改）

命令（真实桌面 GL，非 offscreen）：

```
env -u PYTHONPATH -u QT_QPA_PLATFORM DISPLAY=:1 QT_API=pyside6 \
  .venv/bin/python tools/verify_occluder_import_rendering.py run \
  --destination .artifacts/review16-occluder-reference/run-20261020-141959/before --phase before
```

记录：`<RUN>/before/ui/lifecycle.json`、`<RUN>/before/ui/preview.json`、`<RUN>/before/ui/aimed/*.png`、
`<RUN>/before/logs/*`、`<RUN>/before/results/*`。

观察到的确定缺陷（全部来自真实控件操作，不是静态推断）：

| # | 操作 | 实测 | 代码位置 |
|---|------|------|----------|
| 1 | 目标导入单位 m，再导入 BOP 遮挡物 | 遮挡物控件不存在，默认沿用 `import_unit=m`；`obj_000001` canonical bounds 变为 ±37.9 m（应为 ±0.0379 m） | `src/bproc_ui/panels/resources.py:259`、`:102` |
| 2 | 同批 7 个路径（含同文件重复与软链接别名） | 生成 7 个资源，`obj_000001` 出现 3 个不同 key；去重集合未随批次更新 | `src/bproc_ui/panels/resources.py:257-261` |
| 3 | 选中 2 行后把单位改成 cm | `sync()` 重建列表，选中集变空；`_sync_occluder_unit` 在清空后抛 `StopIteration` traceback | `src/bproc_ui/panels/resources.py:279-284`、`:416` |
| 4 | 混合单位多选（cm + mm） | 单位控件静默显示 `mm`（index 2），没有混合提示 | `src/bproc_ui/panels/resources.py:279-292` |
| 5 | 目录导入 15 个文件 | 已有重复资源再入库，得到 17 个资源 / 15 条唯一路径 | `src/bproc_ui/panels/resources.py:257` |
| 6 | 预览 | 遮挡物为标量轴对齐缩放、无旋转；画面中像从目标顶盖伸出的尖片，与目标轮廓粘连 | `src/bproc_core/scene.py:1033-1127` |
| 7 | 渲染 | 修复前 aimed_view 可用：RGB 4 帧 0/0/0.187/0.303，BOP 4 帧 0/0/0.181/0.378，checker/audit 退出 0 | `<RUN>/before/logs/cores-*-report.json` |

**没有收到的用户原截图不能声称复现。** 上表只声明本轮真实操作能观察到的问题；旧
`.artifacts/review15-cores-final/release-check` 的 50 帧通过只说明其固定配置可用，不覆盖这里的多资源导入链。

## 3. 单元/缓存现状（已核对，不改动）

- `src/bproc_core/assets.py:96-120` 只在加载时按 `m=1 / cm=.01 / mm=.001` 转换一次；实测 mm bounds =
  原数值 × `.001`（`obj_000001` → 0.0759 m），没有二次缩放。
- `src/bproc_ui/state.py:66-98` 在资源定义变化时删除对应 canonical mesh；`resources.py:342-375` 的
  generation 检查已能拒绝旧异步结果。因此“换单位后看到旧尺寸”不是缓存缺失，而是第 1、3 条 UI 状态缺陷。

## 4. 修复决策

### 4.1 导入/单位（UI）

- 新增独立 `occluder_import_unit` 下拉（默认 `mm`，文案“默认 mm，请按源文件确认”）；`add_target`
  仍只读 `import_unit`。已导入资源的单位只由“选中资源单位”编辑器修改。
- 资源身份 = `Path(path).expanduser().resolve()`；去重集合同时包含已有资源与本批已见路径（含软链接别名）。
- `sync()` 重建列表前保存 current key 与 selected keys，重建后恢复；找不到 key 时安全跳过。
- 混合单位：选中资源单位不一致时把单位控件置为 `-1` 并显示“混合单位（请选择）”；用户显式选单位后才批量应用。
- 新增遮挡物尺寸反馈：当前行 canonical bounds（m）与顶点/面数，明确“已按单位换算一次”。

### 4.2 策略字段与兼容

```python
@dataclass(frozen=True)
class OcclusionSpec:
    ...
    scale: Range = Range(0.25, 0.8)                  # 旧表观尺寸，语义不变
    strategy: Literal["aimed_view", "physical_view"] = "aimed_view"
    physical_scale: Range = Range(1.0, 1.0)          # 物理倍率（线性，非表观）
```

- 缺字段旧 YAML / 直接构造 `OcclusionSpec()` → `aimed_view`，`scale` 语义与 RNG 消费顺序不变。
- `new_project()` 显式 `physical_view`、`count=(1,1)`、`physical_scale=Range(1,1)`；已有项目文件不迁移。
- `validate_project`：
  - `strategy` 必须为两个值之一；`physical_scale` 区间正数、下限 ≤ 上限。
  - 物理策略且 `none_fraction < 1` 时：必须有遮挡物、`count[0] >= 1`、`count[1] <= 唯一路径资源数`；
    否则报错（数量不足是错误，不截断、不填 none）。
- `occlusion.effective_occlusion_range`：`physical_view` 且非 none 帧直接返回 `occlusion.ratio`，不再走
  “单目标无专用遮挡物 → 零遮挡窗口”的旧降级。

### 4.3 采样（`scene.py`）

- `occluder_placements` 按 `strategy` 分支；`aimed_view` 代码路径、随机数消费顺序与 `Rejection` 语义原样保留。
- `physical_view`：
  - 数量 `k = rng.integers(count[0], count[1]+1)`；从遮挡物资源中 `rng.choice(..., size=k, replace=False)`，
    key 不重复；数量不足交给校验/运行前拒绝。
  - 线性部分 `s * R`：`s = rng.uniform(physical_scale.min, max)`，`R` 由 `rng.normal(size=4)` 归一化四元数
    构造（均匀 SO3，确定性，可复现）。
  - 深度：仍取 `[near+clearance, target_front-clearance]`，用**旋转后**盒体沿视线半宽
    `|Rᵀ·view|·(extent/2)·s` 检查；放不下返回 `Rejection('no_occluder_room')`，不缩物体、不动机位/K。
  - 平移：`t = center - (sR)·bbox_center`，一次组成 `M = [sR | t]`；AABB、碰撞、`_offset_span`、`_aimed_offset`、
    轮廓估计、预览、后端都消费同一个 `M`。
  - `_offset_span` / `_aimed_offset` 接受标量**或** 3×3 线性矩阵：标量走旧公式（`scale*extent/2`），矩阵走
    support function `|Mᵀ·direction|·(extent/2)`；`matrix_at` 用同一线性矩阵做平移补偿。
  - 无放回、无缩小、无“失败转 none”；碰撞重试仍受 `OCCLUDER_DRAWS` 限制，超限 `Rejection`。
- 两种策略都保持逐 view 布局；目标布局/光照/材质仍按 scene 共享。

### 4.4 后端（`blender_backend.py`）

- pool 容量：`aimed_view` 保留 `count[1]`（旧重复实例契约）；`physical_view` 每资产 1 个（无放回）。
- `apply()`：
  - 先校验 placement（目标 obj_id、遮挡物 key、pool 索引、物理策略下 key 不重复）；非法即隐藏全部受管理网格
    并抛 `ValueError`。
  - 开始先隐藏全部对象，再按 placement 显示；矩阵只 `set_local2world_mat` 一次。
  - 任何异常：隐藏全部受管理网格、清空 `candidate`、重新抛出（不留下半显示状态）。
- `_isolate` 的 `finally` 恢复进入前状态（现有行为保留，补测试）。

### 4.5 UI 采样面板

- 新增“遮挡策略”下拉（按视角表观尺寸 / 按物理尺寸）与“物理倍率”`RangeEditor`。
- 两套尺寸编辑器都保留在配置里；切换策略只启用适用编辑器，不改写另一策略的值（保存/重载/导出都保留两字段）。
- 文案明确：aimed_view 的 `scale` 是相对中心尺寸的表观比例；physical_view 的倍率作用于 canonical 米制尺寸，
  每 view 重新采样遮挡物旋转/位置；数量不足/空间不足均为明确拒绝。

### 4.6 导出与查看链

`bundle.py` 已保存完整 `project_to_dict`，`blender_view.py` 读取同一 `scene.yaml`/candidate；本轮只加回归测试，
不另建导出算法。

## 5. 验收对应

- AC-1：`<RUN>/before/`（上文命令与文件）。
- AC-2/3：`tests/ui/test_occluder_import_lifecycle.py` + 既有 state/preview/LOD 测试。
- AC-4/5/6：`tests/unit/test_occluder_physical.py` + 既有 config/export/scene 测试；`<RUN>/after` 的 15 资源 `[5,8]` 记录。
- AC-7：`tests/unit/test_occluder_backend_lifecycle.py` + 既有 backend/runtime 测试。
- AC-8/9/10/11：`tools/verify_occluder_import_rendering.py run/verify`，真实 DISPLAY=:1、OPTIX、Haven、15 BOP 库。
- AC-12：全量 `tests/unit tests/ui`。
- AC-13：文档、提交、`git diff --check`、arbiter 独立重验。

## 6. 风险

1. 旧候选随机序列：`aimed_view` 分支不插入任何新的 RNG 调用；用修改前候选快照锁定。
2. 固定物理尺寸放不下：明确 `no_occluder_room` / `occluder_collision` 拒绝，运行状态为 partial，不静默成功。
3. 旋转只进渲染不进瞄准：由 `test_occluder_physical.py` 用最终矩阵重算深度/AABB/offset/silhouette 锁定。
