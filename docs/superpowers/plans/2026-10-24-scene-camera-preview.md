# Scene and Camera Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 本任务执行方式已由用户指定为 **flow build-verify**，以此替代上面通用执行选项。用户已经批准本计划对应的范围；实现、独立验收在同一个 preset 内完成，不再重新设计范围。本次不做 git 提交。

**Goal:** 在应用与 Blender 内按完整项目的场景/视角编号浏览同一套候选，分离几何预览与严格渲染初始化。

**Architecture:** 在 core 复用生产采样的有界查询；Qt 现有异步预览增加场景选择并携带有效配置。Blender 使用一个按需更新的预览会话与侧栏，复用现有场景组装，不预建全部场景。保持严格渲染路径默认行为。

**Tech Stack:** Python 3.10+、PySide6/VisPy、Blender 4.2.1 / BlenderProc 2.8（现有离线安装）、pytest/pytest-qt。

**Spec:** `docs/superpowers/specs/2026-10-24-scene-camera-preview-design.md`

## Global Constraints

- 场景科学参数仍在原应用编辑；不实现从 Blender 反向同步。
- UI 从 1 计数，采样器从 0 计数；固定模式使用 `config.views` 而不是 `views_per_scene`。
- 不缩减配置中的场景数、区域或固定机位清单；候选保留未做像素验收的提示。
- 几何预览不初始化 GPU/降噪，正式渲染及显式渲染预览保持 GPU-only，不加入 CPU 回退。
- 保留已有未提交的 OPTIX 修复；不安装依赖、不修改系统/Blender 安全首选项、不提交或推送、不改版本或标签。

## Review Focus

- 多个随机区域或第二个固定机位：不能因筛选预览来源而重编号/改变 RNG。Task 1/2 对比生产候选。
- 数量缩小、模式切换和异步晚到：显示的编号必须属于当前候选，不允许旧结果覆盖。Task 2 的 Qt 测试覆盖。
- 预览种子不同于项目 seed：首次打开正确但切换再返回不能变为另一场景。Task 2/3 测试有效配置交接。
- GUI 的 Python 初始化不同于后台构建：面板不能仅在后台 smoke 里可用。Task 3/4 测试 `--view-only` 注册链路、路径依赖和真实 bpy 操作符。
- 几何查看允许不兼容降噪，但显式渲染不能偷偷启用 CPU；大量切换不能堆积网格。Task 3/4 用配置冲突、调用探针和 Blender 数据块数量回归覆盖。

---

### Task 1: 共享、完整配置的候选查询

**Files:**
- Create: `src/bproc_core/preview.py`
- Test: `tests/unit/test_preview_selection.py`
- Read: `src/bproc_core/scene.py`, `src/bproc_core/sampling.py`

**Interfaces:**
- Consumes: `ProjectConfig`, mesh mapping, `sample_scene(config, meshes, scene_index)`, `propose_candidate(config, meshes, scene, view_index, attempt)`。
- Produces: `candidate_for_preview(config, meshes, scene_index, view_index, *, max_attempts=30, deadline_s=1.5, cancelled=None)` 返回 `SceneCandidate | Rejection`；cancelled 是可选零参布尔函数。索引非法返回明确信息，不偷偷 wrap。复用生产配置，完整候选含原编号、attempt。

- [ ] 写测试：2×3、多区域、固定机位、相同编号重复、部分无遮挡场景分配，完整字典等于生产查询；非法索引、取消、次数上限、失败有原因。
- [ ] 运行新文件确认未实现时失败。
- [ ] 实现共享查询，不引入缓存全场景或新的采样规则；时限在现有操作间检查，不声称能抢占正在执行的几何函数。
- [ ] 运行 `env -u PYTHONPATH -u DEBUG PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/unit/test_preview_selection.py -q`。

### Task 2: 应用内场景/视角导航与有效快照

**Files:**
- Modify: `src/bproc_ui/preview.py`, `src/bproc_ui/main_window.py`, `src/bproc_ui/state.py`, `src/bproc_ui/blender_view.py`
- Modify if needed: `src/bproc_ui/views/scene_view.py`, `src/bproc_ui/views/camera_view.py`, `src/bproc_ui/panels/export.py`
- Test: `tests/ui/test_preview.py`, `tests/ui/test_preview_invalidation.py`, `tests/ui/test_main_window.py`, `tests/ui/test_blender_view_request.py`, `tests/ui/test_blender_viewer.py`, `tests/ui/test_project_state.py`

**Interfaces:**
- Consumes: Task 1 的查询；现有 Qt 请求 token/context/mesh revision 隔离。
- Produces: `PreviewController.scene_index` 与扩展 `refresh(region_key=None, sample_index=None, preview_seed=None, scene_index=None)`；编号实际对应完整配置，不再区域缩减。`ProjectState.candidate_config` 保存生成候选时有效配置；`set_candidate(candidate, *, config=None)` 在缺省情况下使用当前项目配置，清空候选同时清空候选配置。
- `BlenderViewer` 请求使用候选有效配置（包括预览种子）和当前候选的精确快照。

- [ ] 写 Qt 测试选择第二场景、末视角、第二固定机位，检查两视图及摘要一致；预览选择不修改持久配置/dirty。
- [ ] 写测试数量减少/模式切换/旧请求晚到/项目重新载入，保持现有失效规则；独立种子的传递与清理。
- [ ] 扩展现有底栏：场景编号、视角编号/总数、实际机位来源、种子、刷新。复用现有相机窗口与总览/相机标记。来源选择若保留，只导航到该来源实际分配的视角，不修改配置或随机流；无分配视角须提示。
- [ ] `_Search` 使用共享查询；更新现有单场景/缩减模式测试为新的生产一致性断言，保留旧测试验证的取消/异步安全含义。
- [ ] 运行相关 Qt 测试（全量命令见 Task 4）。

### Task 3: Blender 几何会话与原生预览面板

**Files:**
- Create: `src/bproc_runtime/blender_preview.py`
- Modify: `src/bproc_runtime/blender_backend.py`, `src/bproc_export/blender_view.py`, `tools/blender_view.py`, `tools/open_blender.py`
- Modify only as needed: `src/bproc_runtime/render_quality.py`（保留严格规则）, `src/bproc_core/config.py`（复用已存在提示）
- Test: `tests/unit/test_blender_view.py`, `tests/unit/test_open_blender.py`, 新建 `tests/unit/test_blender_preview.py` 及必要 backend tests。

**Interfaces:**
- Consumes: 完整配置 + 初始 candidate.json；Task 1 查询；现有 `BlenderBackend.apply(candidate)`。
- Produces: `BlenderBackend.prepare(config, meshes, *, geometry_only=False)` 或同等独立几何入口，缺省严格行为不变；`prepare_scene(..., *, geometry_only=False)` 显式支持几何快照，GUI 查看走几何模式，现有严格调用仍拒绝冲突配置。
- Blender module exports `register()` / `unregister()` 与明确会话初始化函数，VIEW_3D 侧栏「BlenderProc」面板。操作符：更新所选候选、相机视图、场景总览、开启渲染预览。会话只管理本次导入的对象；尽量使用同一个 backend.apply，不能重新写一个近似场景生成器。

- [ ] 写 tests：严格模式仍在构建前拒绝 CUDA+OPTIX，显式几何模式不调用 GPU/降噪；候选/有效种子往返不变，CLI 可显式选择几何查看。
- [ ] 把后端几何初始化与 renderer 配置分离，保持默认渲染行为，几何查看不得通过修改用户 config 为其他 device/denoiser 来伪装兼容。
- [ ] `.blend` 保存可定位的项目快照/候选元数据，GUI 初始化加载此会话；BlenderProc 先于 numpy 导入，保证本机 GUI 路径能找到源码/依赖。session backend 生命周期以真实 Blender API 为准，可按必要性采用重建一次再复用，禁止每个编号无限追加对象。
- [ ] 面板显示 1-based 编号及完整边界、候选提示、种子、相机内外参摘要、灯光/环境/遮挡物信息。切换失败保持上次成功编号/内容并报错，禁止显示请求编号假装成功。
- [ ] 相机视图/总览使用真正 VIEW_3D 状态；总览可观察当前相机朝向。渲染按钮先运行既有 `render_problems` / GPU/降噪检查，失败保持 SOLID 并中文提示。模式/注册/数据加载必须可在真实 Blender 测试。
- [ ] 运行针对性 unit/UI tests，保留 prior OPTIX 修复的严格模式断言。任何与新几何模式冲突的旧 UI 预期只能换成严格入口测试并补新几何允许测试，不可直接删掉。

### Task 4: 真实 Blender 验证与使用说明

**Files:**
- Create: `tests/blender/check_preview.py`, `tools/verify_scene_preview.py`（一个 host launcher + 一个 Blender 检查脚本，若现有工具能复用则无需另建）
- Modify: `docs/usage.md`, 必要时 `README.md`
- Local evidence: `.artifacts/scene-camera-preview/`（不得覆盖其他历史产物）

**Interfaces:**
- `tools/verify_scene_preview.py --destination <new-directory>` 用本机 RuntimeSpec/locate_runtime 启动 Blender 检查，保存日志/结果，断言失败必须传播为非零退出。
- Blender 检查从少量合成网格的测试项目及同一构建/GUI 注册代码启动，不要求安装环境或读用户生产模型。

- [ ] 真实 Blender 验证两个场景、多个随机视角、两个固定机位、切换后返回，比较候选、camera matrix/K、目标/遮挡物/光源变换及对象数量；运行注册/操作符而不是只测纯函数。
- [ ] 在有 VIEW_3D context 的真实 Blender 检查相机/总览切换（可通过屏幕空间数据验证，不把后台成功称为肉眼验证）；测试几何 mode 的冲突降噪及严格渲染拒绝。无可用 GPU也应能验证几何；真实 GPU 渲染不是本次必需 AC。
- [ ] 执行真实检查：`env -u PYTHONPATH -u DEBUG .venv/bin/python tools/verify_scene_preview.py --destination .artifacts/scene-camera-preview/<unique-run>`。记录真实退出码、执行环境和证据路径。
- [ ] 更新 usage：应用底栏导航、Blender N 侧栏入口、如何打开/重新打开、几何与渲染预览、参数仅原应用修改、候选非最终验收帧。
- [ ] 全量回归：`env -u PYTHONPATH -u DEBUG PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p pytestqt.plugin tests/unit tests/ui -q`。
- [ ] `git diff --check`；报告每条 AC 的文件与实际命令/退出码。独立 arbiter 必须自行复跑必要命令；任何环境阻塞报告 REVISE/未完成，不能把 mock 等同真实 Blender 证据。

## 自检

已批准范围由 Task 1（采样一致）、Task 2（应用预览/交接）、Task 3（Blender 交互/几何与渲染分离）、Task 4（真实验证/使用说明）覆盖。五项 Review Focus 均落在上述明确测试中。不改变生产采样行为；实现者可按真实 API 调整内部结构及必要测试文件，但不可缩减 AC，也不可把后台成功宣称为人工可视验证。
