# 遮挡物导入与物理策略实施计划（第十六轮）

日期：2026-10-20
设计：`docs/superpowers/specs/2026-10-20-occluder-import-rendering-design.md`
基线：`2d84815`

所有命令先 `cd /home/kepler/rm_2027/blenderproc-ui`；UI 测试用
`env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p pytestqt.plugin <path> -q`。

## 步骤 0（已完成）：修改前复现

- 工具：`tools/verify_occluder_import_rendering.py`（`run --phase before|after`、只读 `verify`）。
- 真实运行写入 `.artifacts/review16-occluder-reference/run-20261020-141959/before`，退出 0。
- RED 证据是上一步的错误行为本身（`lifecycle.json` + traceback + 渲染图），不是人为改坏代码。

## 步骤 1：UI 导入/单位/选择

改动：`src/bproc_ui/panels/resources.py`

- 新增 `occluder_import_unit`（mm/cm/m，默认 mm）与遮挡物尺寸标签 `occluder_size_label`。
- `add_occluders(paths, unit=None)`：`unit or occluder_import_unit`；`resolve()` 后与“已有 + 本批已见”去重。
- `_remove_occluders` 后同步列表；`_sync_occluder_unit` 用 `next(..., None)`，不抛 `StopIteration`。
- `_set_occluder_unit` 只对选中行应用；混合时置 `-1` + placeholder。
- `sync()` 保存/恢复 current 与 selected keys；填充尺寸反馈。
- 文案改为“新增遮挡物使用独立单位（默认 mm，请按源文件确认）”。

RED（先写、先跑、保留失败日志）：
`tests/ui/test_occluder_import_lifecycle.py`，真实 `ResourcesPanel` + `ProjectState` + `FakeLoader`。
覆盖：目标 m / 遮挡 mm 独立；mm bounds = 原值 × .001 且只一次；重复批次、同批重复、软链接别名去重；
多选混合单位显示；单位修改后选择保持；移除/清空；项目保存重载；目标单位不被遮挡物操作改变；延迟旧结果失效。

GREEN 命令：见 AC-2/AC-3。

## 步骤 2：配置策略与兼容

改动：`src/bproc_core/config.py`、`src/bproc_core/occlusion.py`

- `OcclusionSpec.strategy/physical_scale`；`new_project()` 默认 `physical_view`、`[1,1]`、倍率 `[1,1]`。
- `validate_project` 校验策略/倍率/数量与唯一资源数。
- `effective_occlusion_range` 物理策略不降级 none。

测试：扩展 `tests/unit/test_config.py`（旧 YAML → aimed_view；新项目字段；物理校验失败；旧 scale 不变）。
GREEN：`pytest tests/unit/test_config.py -q`。

提交边界（英文 conventional）：
`feat(config): add physical occluder strategy with independent scale`

## 步骤 3：物理采样与完整变换

改动：`src/bproc_core/scene.py`

- `_offset_span`/`_aimed_offset` 接受标量或 3×3 线性矩阵（标量保持旧公式与 RNG 顺序）。
- `occluder_placements` 按 `strategy` 分支；`physical_view`：无放回选择、均匀 SO3、固定物理倍率、
  旋转后深度/AABB/碰撞、一次组成矩阵。
- 不改 `aimed_view` 分支的调用顺序与随机消费。

测试：`tests/unit/test_occluder_physical.py`
- 固定 seed 可重现；`RᵀR=I`、`det=1`（≤1e-10）；奇异值 = 配置倍率；非中心原点/非立方资产。
- 最终矩阵重算深度、旋转后 AABB、碰撞、`_offset_span`、silhouette 一致；倍率 1 近远 view 无距离缩放。
- 数量：key 唯一、数量等于采样值；`[5,8]` 在 15 资源下合法，8 资源不足时校验失败；无库请求遮挡被拒。
GREEN：AC-5/AC-6。

提交：`feat(scene): place physical occluders with uniform SO3 rotation`

## 步骤 4：后端 pool 与异常清理

改动：`src/bproc_runtime/blender_backend.py`

- `prepare`：`physical_view` 每资产 1 个 pool 实例。
- `apply`：候选校验、先隐藏后显示、异常隐藏全部并清空 candidate。
测试：`tests/unit/test_occluder_backend_lifecycle.py`（fake objects/pools，不启动 Blender）
覆盖 none→多→少→none、重复 apply、非法 placement、apply 异常、隔离恢复、矩阵只应用一次、mask 不含遮挡物。
GREEN：AC-7。

提交：`fix(runtime): size occluder pools per strategy and clear on failure`

## 步骤 5：采样面板

改动：`src/bproc_ui/panels/sampling.py`

- 策略下拉 + 物理倍率；切换不覆盖另一策略数值；启用态随策略切换。
- 文案说明两种尺寸语义与 scene/view 生命周期。
测试：扩展 `tests/ui/test_sampling_export.py`；策略经 UI → `ProjectState` → 保存 → 导出 `scene.yaml` 往返。
GREEN：AC-4。

提交：`feat(ui): expose occluder strategy and physical scale`

## 步骤 6：真实 GPU 与 UI 烟测

命令（真实桌面）：

```
env -u PYTHONPATH -u QT_QPA_PLATFORM DISPLAY=:1 QT_API=pyside6 .venv/bin/python \
  tools/verify_occluder_import_rendering.py run \
  --destination .artifacts/review16-occluder-reference/run-20261020-141959/after --phase after
```

产出：5 个数据集（rgb/bop × aimed/physical，另加 physical 多遮挡），每格式 ≥4 帧；真实 checker + audit；
`ui/aimed` 与 `ui/physical` 双 GL 截图；`safety` 逐 placement 深度/AABB/奇异值。
只读复核：`... verify --destination <RUN>/after`。

## 步骤 7：回归、文档、提交

- 全量：AC-12 命令。
- `docs/usage.md`：单位、倍率、旋转、数量、scene/view、旧配置兼容、验证范围。
- `docs/validation.md`：本轮复现 → 修复前后视觉/数据、真实数量与限制。
- `report.md` 第十六轮 + `report-handoff.md`（忽略文件，不入 Git）。
- 本地英文 conventional 提交（分 5 个逻辑提交），`git diff --check`、tracked 干净，不 push/amend/rebase。
