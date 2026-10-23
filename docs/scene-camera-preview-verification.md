# 场景/相机预览验收记录

对应已批准规格：`superpowers/specs/2026-10-24-scene-camera-preview-design.md`。

## 本次执行结果

证据目录：`.artifacts/scene-camera-preview/run-20261024-e/`（本次新建；未覆盖历史产物）。

```bash
env -u PYTHONPATH -u DEBUG .venv/bin/python tools/verify_scene_preview.py \
  --destination .artifacts/scene-camera-preview/run-20261024-e
```

实际输出：`PASS: real Blender build + GUI/operators; evidence: /home/kepler/rm_2027/blenderproc-ui/.artifacts/scene-camera-preview/run-20261024-e`，退出码 **0**。后台构建和 GUI 子进程各自退出码 **0**，完整命令在 `commands.json`。

```bash
env -u PYTHONPATH -u DEBUG PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 \
  QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p pytestqt.plugin tests/unit tests/ui -q
```

最终实际输出：`775 passed in 52.81s`，退出码 **0**；日志 `unit-ui-snapshot-final.log`。
`git diff --check` 无输出，退出码 **0**。

## 逐项证据

| 标准 | 实现与可检查证据 |
|---|---|
| AC-1 完整生产配置 | `bproc_core/preview.py` 直接调用生产 `sample_scene/propose_candidate`，仅 UI 预览 seed 可替换，不缩减场景数、区域、固定机位。`tests/unit/test_preview_selection.py` 比较 2 场景 × 3 随机视角、多区域、2 固定机位的完整字典及无遮挡分类，重复查询一致。 |
| AC-2 应用交互与失效 | `main_window.py` 底栏真实编号控件，来源只读；`test_scene_preview_navigation.py` 用 Qt 键盘事件导航第二场景/末视角，检查总览和相机同一候选、第二固定机位 index=1、缩减与空草稿、不改 dirty。`test_preview_invalidation.py` 保留晚到/重载/mesh revision/关闭测试；`test_blender_view_request.py` 检查关闭后构建完成也不打开 Blender。 |
| AC-3 有效快照 | `state.candidate_config` 随候选保存/清空；Blender 请求使用有效配置。UI bridge 测试实际写快照并检查独立 seed=987、完整场景数、原渲染设置；真实 smoke 比较初始 `expected.json` 和会话完整候选，并验证重访。只保留一份初始精确候选，避免宿主与 Blender NumPy 在末位浮点上的差别改变交接内容。 |
| AC-4 Blender 原生功能 | `bproc_runtime/blender_preview.py` 实现原生 PropertyGroup、Panel 和更新/相机/总览/渲染操作符。`checks.json` 记录实际随机/固定编号切换；`tests/blender/check_preview.py` 检查相机矩阵/K、目标/遮挡物变换、渲染及视口隐藏、灯光、HDRI、材质、对象/网格/材质/相机/光源/world 数量不累积、VIEW_3D 状态、重复注册/注销及无关场景保留。 |
| AC-5 几何/严格渲染分离 | `BlenderBackend.prepare(..., geometry_only=True)` 不调用 BProc init（其内部会选设备/降噪并清场），只建立独立 native scene；对象组装与 `apply` 复用正式后端。真实 smoke 将 GPU、降噪和 init 函数设为失败探针，几何重建/切换通过；原生渲染操作符对 CUDA+OPTIX 返回 CANCELLED、中文建议并保持 SOLID。默认严格 `prepare_scene` 拒绝测试保留，正式渲染/导出/preflight 相关全量测试通过。 |
| AC-6 有界与诚实状态 | 最多 30 次机位尝试、1.5 秒操作间预算，布局重试中也检查取消/超时；不预建全集。unit 覆盖非法索引、取消、零期限、缺 mesh、固定次数预算；Qt/native 显示“候选预览 · 未经像素遮挡验收”。Blender 非法编号返回 CANCELLED 并回显上次成功编号。依赖缺失报错，不自动 pip 安装。 |
| AC-7 可复跑与说明 | `tools/verify_scene_preview.py --destination <全新目录>` 使用真实 Blender 4.2.1 GUI `--view-only` 链路，不模拟 bpy；检查异常传播非零。`docs/usage.md` 说明底栏、N → BlenderProc、几何/严格渲染区别、重开方式及候选限制。 |

## 关键差异与兼容性

- 新增共享有界查询和原生 Blender 会话；Qt/Blender 不另造近似采样器。
- 原 GUI“第二固定相机总是 index=0”的测试替换为完整生产候选等值断言；原 GUI“非法降噪阻止所有查看”的测试替换为显式几何可打开、配置不被修改、有效 seed 写入快照的断言。
- 原严格模式 OPTIX 拒绝测试没有删除或弱化。原有未提交 OPTIX 修复、`.pi/`、SPEC/PLAN 均保留；未提交、push、改版本/tag、安装依赖或修改 Blender 安全首选项。
- 前期真实 smoke 的失败记录仍保存在本次 `run-20261024-a/b/c/`：Blender 相对图片路径检查需使用 `bpy.path.abspath`；跨 NumPy 环境遮挡物缩放有末位差异，现通过保存初始精确候选解决。`d/e` 已通过，`e` 还检查几何视口隐藏和 world 数量。

## 操作入口与验证边界

应用底部“采样机位”选场景/视角 → “脚本”工作区“在 Blender 中打开当前构图” → Blender 三维视口 **N → BlenderProc**。默认几何；显式点击“渲染预览（严格 GPU）”才检查并启用渲染着色。

已验证真实 Blender 数据和 VIEW_3D 状态，不是人工目视验收；没有执行实际 GPU 像素渲染、整数据集渲染或最终像素遮挡验收。查询期限在现有几何操作之间检查，不能抢占单个正在执行的操作。其余验收项无已知阻塞。
