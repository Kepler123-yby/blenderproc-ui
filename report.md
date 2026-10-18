# BlenderProc UI 修复与回归交接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 上句是 writing-plans 的交接模板；本仓库不保证安装了这些执行技能。接手者可使用自己的执行工具，按本文逐项实现、测试和记录，不要因找不到技能而停止。

**Goal:** 修复当前版本已复现的布局、预览、遮挡、验收和运行状态问题，再改善批量写入与导出响应，保持本地 UI → 独立脚本 → 终端 GPU 渲染的流程。

**Architecture:** 保留 `bproc_core / bproc_runtime / bproc_export / bproc_ui` 分层。将布局采样和遮挡验收规则集中到无桌面依赖的核心层；渲染器、UI 和验收器使用一致契约。异步任务绑定不可变配置快照，元数据采用追加记录和最终原子汇总。

**Tech Stack:** Ubuntu 22.04；UI Python 3.10、PySide6、VisPy；conda `bop` Python 3.8 启动器；BlenderProc 2.8.0、Blender 4.2.1 / 内部 Python 3.11；Cycles GPU；BOP toolkit。

**Spec:** 本文“当前事实与审查基线”“修改任务”和“验收矩阵”；当前行为参考 [docs/usage.md](docs/usage.md)、[docs/gpu-rendering.md](docs/gpu-rendering.md)。原始证据在 [.artifacts/audit-20261018/report.md](.artifacts/audit-20261018/report.md)。旧的 2026-10-16 设计仅作历史背景，不能覆盖后来已经改变的行为。

交接日期：**2026-10-18**。审查基线：`aebd537a27baf730cfea437d0d433a4035690fba`。项目：`/home/kepler/rm_2027/blenderproc-ui`。

**本次交付仅为文档。以下修复均未实施，复选框是接手者的待办，不代表已经通过。** 后续若 HEAD 已变化，先检查差异，避免重复修改。历史测试结果不能当作修改后的回归结果。

给 DeepSeek 的起步顺序：先读本文和 `git diff`，跑一次基线；优先完成任务 1–3，再按依赖逐批执行。每完成一项，补上测试结果、实际证据路径和提交编号。不要同时重做界面风格或扩展新输出格式。

## Global Constraints

- 使用 GPU 渲染，不能因 GPU 不可用而回退 CPU；保留 CPU 禁用、GPU 能力检查和离线预检。
- 仅保留 `rgb_masks` 与 `bop` 两种输出；`rgb_masks` 包含 RGB、mask、mask_visib，不新增纯 RGB、YOLO 或 COCO 转换。
- 保留原始模型顶点、CAD 原点、单位及 canonical BOP 模型对应关系；本轮不增加目标随机旋转或物理堆叠。
- 每个场景共享目标位置、材质、HDRI 和灯光；相机及专门遮挡物按视角变化，这是当前行为。
- 球坐标约定保持：φ=0 沿 +X、向 +Y 为正；α=0 水平；方位角跨 ±180° 有效；方向按 φ 和 sin(α) 均匀采样。
- 手动范围不得因重试失败而自动扩大；保持有界、确定性的尝试和真实的 partial 状态。
- 无 HDRI 时使用中性灰世界背景，这是允许的配置；不恢复旧版强制 HDRI 限制。
- 视口用于几何构图与基础颜色预览；不要求本轮实现 PBR 视口或 Blender 参数自动回写。
- 已有任务、输出目录和资源不得覆盖，包括空目录与软链接；不做断点续跑。
- 导出任务独立运行，不导入 PySide6 / VisPy；Python 3.8 启动器仅使用标准库，预检后才进入 Blender Python。
- 不向已有 `bop`、Blender Python 或原 `/home/kepler/bproc_project/dataset_rendering` 项目安装依赖；测试使用现有环境、新目录和本地资源。
- 保留 smooth / flat / auto、加权法线、GPU 降噪和标签关闭降噪的已有正确行为。

## Review Focus

1. 混合 mm/cm/m、非对称 CAD 原点、任意目标作为中心，建议布局与 BOP 仍对齐：任务 1、2、5、12。
2. 多区域、跨界角度、固定机位及非首个预览序号不会失效或悄悄改种子：任务 3、5。
3. 单目标无外部遮挡、多目标自然遮挡、数量为零的闲置遮挡资源，验收语义一致：任务 4、5。
4. 构建/导出期间切项目、A→B→A、候选更新与旧结果迟到，不混用配置或显示错误命令：任务 8、11。
5. 启动部分完成时中断、磁盘错误、BOP 后处理失败及 JSONL 尾行损坏，不留下虚假成功或可避免的 running：任务 9、10、12。

---

## 当前事实与审查基线

主要链路已可运行，当前重点是修复正确性和可用性，并非重写项目。

| 已执行检查 | 基线结果 |
|---|---|
| `tests/unit` + `tests/ui` | **268 passed，3.85 秒**；隔离外部 ROS pytest 插件 |
| 真实 cores，随机机位，无 HDRI，RGB 双掩码 | 2 场景 × 3 视角，640×480，50 samples；6/6，退出 0 |
| 双目标随机机位 BOP，HDRI、遮挡物、材质随机 | ID 2/7，混合单位，2×3，320×240，16 samples；6/6，退出 0；现有 BOP 几何检查 PASS |
| 双目标固定机位，统一红色材质，RGB 双掩码 | 2×2，320×240；4/4，退出 0 |
| 双目标直接应用当前建议范围 | 2×3；0/6，partial，退出 2；两个场景因碰撞直接跳过 |
| 独立检查上述两组 RGB 共 10 帧 | 图像尺寸、对象 ID、二值掩码、visible⊆full、非空、场景共享参数均符合预期 |

本机 RTX 3060 Ti 8GB；实际渲染使用 OptiX，BOP 日志确认 NVIDIA EGL。三组成功任务的 status 时长分别为 3.72、9.69、7.04 秒，**不包含预检和 Blender 启动**。没有重新做一轮 Blender GUI 逐按钮人工验收。

当前验收器存在误报：cores 6 帧被错误要求非零遮挡；固定机位 4 帧的 key 被当作随机区域查找。不能据此判定这些输出错误。BOP 检查的字段漏检是覆盖缺口，也不等于已证明写出数据有错。历史数据中的旧 `request_index` 记录不能直接按新 `scene_index` 校验并宣判损坏。

证据索引（`.artifacts` 被 Git 忽略，仅在当前机器保存，转移工作区时需要另行保留）：

| 内容 | 路径 |
|---|---|
| 完整原始审查与汇总 | [.artifacts/audit-20261018/report.md](.artifacts/audit-20261018/report.md)、[summary.json](.artifacts/audit-20261018/summary.json) |
| 数值复现、近相机遮挡物、25% 自然遮挡 | [core/core_results.json](.artifacts/audit-20261018/core/core_results.json)、[复现源码](.artifacts/audit-20261018/core/reproduce_core.py) |
| 实际任务位置、独立图像检查 | [render/cases.json](.artifacts/audit-20261018/render/cases.json)、[render/inspection.json](.artifacts/audit-20261018/render/inspection.json) |
| UI 问题和响应测量 | [workflow/report.md](.artifacts/audit-20261018/workflow/report.md)、[observations.json](.artifacts/audit-20261018/workflow/observations.json)、[responsiveness.json](.artifacts/audit-20261018/workflow/responsiveness.json) |
| 检查器反例、metadata 性能、启动中断 | [outputs/output-audit.md](.artifacts/audit-20261018/outputs/output-audit.md)、[checker-repros.json](.artifacts/audit-20261018/outputs/checker-repros.json)、[metadata-performance.json](.artifacts/audit-20261018/outputs/metadata-performance.json)、[startup-interrupt.json](.artifacts/audit-20261018/outputs/startup-interrupt.json) |

## 文件职责与执行顺序

| 子系统 | 主要文件 | 本轮责任 |
|---|---|---|
| 资源/建议布局 | `src/bproc_core/assets.py`、`src/bproc_ui/panels/resources.py` | 为各目标生成可行范围，依据联合范围适配距离 |
| 配置/确定性采样 | `config.py`、`project_io.py`、`sampling.py`、`scene.py` | 独立布局预算、共享场景状态、逐视角遮挡物 |
| 遮挡规则 | 拟新增 `src/bproc_core/occlusion.py` | 运行时和检查器共享有效遮挡区间 |
| 渲染调度/生命周期 | `src/bproc_runtime/run.py`、`cli.py`、`records.py` | 重试、事务、中断、最终状态 |
| 元数据 | `output.py`、拟新增 `src/bproc_runtime/metadata.py` | 追加帧记录、版本化读取、最终 manifest |
| 视口 | `src/bproc_ui/preview.py`、`main_window.py`、`views/*` | 索引、观察取景、统一基础颜色 |
| 后台操作 | `state.py`、`blender_view.py`、拟新增 `export_worker.py` | 快照身份、旧结果隔离、后台导出 |
| 验收/工具 | `tests/blender/*`、`tools/render_demo.py`、`tools/gui_smoke.py` | 兼容当前场景模式，正反例均能判定 |

推荐分批：**A：任务 1–3；B：任务 4–6；C：任务 7–9；D：任务 10–11；最后任务 12 统一验收。** 每批都应可独立运行和检查。任务 3 可先于布局改造修复；任务 4 与任务 5 必须配套；任务 11 依赖任务 8 的快照身份；任务 10 需要任务 9 的最终状态保护。

以下接口均为**拟议修改**，不是已经存在的 API。如接手时采用等价方案，应同步调整所有调用者、测试及本报告接口表，不能仅更改局部名称。

## 修改任务

### 任务 1 / P1：建议范围必须允许多目标合法摆放

**问题：** 两个 0.1m 立方体当前只得到 XY ±0.0212m、Z ±0.0074m，无法分离。仅增加重试无效。

**Files:** 修改 `src/bproc_core/assets.py`、`src/bproc_ui/panels/resources.py`；测试 `tests/unit/test_assets.py`、`tests/ui/test_resources_panel.py`。

**Interfaces:** 新增 `suggest_layout(meshes: Mapping[str, MeshData], targets: Sequence[TargetSpec], anchor_key: str, camera: CameraSpec) -> tuple[dict[str, Box], Range]`。映射只含启用目标，以目标包围盒中心的位置表示偏移，单位米。生产的多目标“恢复建议范围”改用此函数；旧 `suggest_ranges()` 如保留，不能继续把同一个 Box 用于所有多目标。

- [ ] 写 `test_suggest_layout_has_disjoint_target_ranges`：两个 0.1m 立方体，锚目标位于 0，其余目标范围的极端角点也与锚目标保持正间隔；遍历 250 个场景不因这些建议范围发生目标 AABB 重叠。
- [ ] 补充不同尺寸、mm/cm、非对称原点、第二目标作锚、单目标、停用目标测试。断言建议不修改网格/ID/单位；停用目标的 authored offset 保留；无锚或启用网格缺失给出明确错误。
- [ ] 实现不同目标的独立摆放区间：以锚为原点，按稳定目标顺序安排不相交的 X 方向区间，计入每个模型的半宽、随机余量和至少最大目标尺寸 10% 的间隔；锚不随机平移。先保证合法，再考虑布局美观，不引入物理引擎。
- [ ] 依据**应用新偏移后的全部目标可达联合包围盒**计算相机距离，复用 `fit_distance()` 角点投影；保留近期联合目标距离适配的改进。更新“恢复建议范围”只应用启用目标，不自动覆盖用户手动范围。
- [ ] 运行 `bp_test tests/unit/test_assets.py tests/ui/test_resources_panel.py -q`；新用例先观察失败，修复后全部通过。记录提交建议：`fix: suggest feasible per-target placement ranges`。

**验收边界：** 建议布局保证几何可行，不保证任意角度、遮挡率都可达。布局可行率的 UI 展示可在后续追加，不阻塞本项。

### 任务 2 / P1：场景布局有独立、确定性的重试预算

**Files:** 修改 `src/bproc_core/{config,project_io,sampling,scene}.py`、`src/bproc_runtime/{run,records,output}.py`、`src/bproc_ui/{preview.py,panels/sampling.py}`；测试 `tests/unit/{test_config,test_camera_sampling,test_scene_candidates,test_run}.py`、`tests/ui/test_preview.py`。

**Interfaces:**

- `ProjectConfig.layout_max_attempts: int = 30`，正整数，旧 schema 1 YAML 缺省时自动使用默认值；独立于 `occlusion.max_attempts`，UI 名称“场景布局尝试上限”。
- `scene_rng(seed, scene_index, layout_attempt_index=0)`；`propose_scene(config, meshes, scene_index, *, layout_attempt_index=0)`。第 0 次保持原随机流；非零次数在 SeedSequence `[seed, scene_index, 0x5343454E]` 后增加次数，禁止用全局 RNG。
- 新增 `sample_scene(config, meshes, scene_index, *, on_rejection=None) -> SceneState | Rejection`；回调签名 `(layout_attempt_index: int, rejection: Rejection) -> None`，仅对可重试的布局碰撞继续抽样，缺资源/缺中心立即返回。
- `SceneState`、`SceneCandidate` 末尾增加默认 `layout_attempt_index=0`；`candidate_from_dict()` 对旧记录补默认。
- `RunRecorder.record_rejection(scene_index, view_index, attempt_index, reason, *, phase="view")`；布局记录 `phase="layout", view_index=-1`，次数用 layout_attempt_index。同步更新 fake recorders，避免模糊归入视角 0。

- [ ] 写测试：第 0 次碰撞、第 1 次成功，恰好两次布局调用；成功后 3 个视角共享同一组目标/灯光/HDRI/材质，候选记录 layout_attempt_index=1。
- [ ] 写测试：手动固定重叠范围最多 30 次后跳过整场景；2 场景×3 视角应 saved=0、skipped=6、退出 2，布局拒绝 60 条，未调用 GPU assess/render；每视角预算仍独立为原配置值。
- [ ] 实现共享 `sample_scene()`，运行时和预览均使用它；不要在每个视角失败后重新抽整个已接受场景。布局回调已经记录过的最终拒绝不再重复计数。Blender 查看继续使用已选候选快照，无需再次随机抽样。
- [ ] 更新 `tests/unit/test_run.py` 的 stub_scene/monkeypatch 入口及 fake recorder：采样从 propose_scene 换到 sample_scene 后，测试必须拦截实际调用点，不能仍 mock 一个已不再使用的函数。
- [ ] 检查 YAML round-trip、旧候选反序列化、超过 64 位 seed、重复运行同 seed 的结构化结果一致；非零布局次数确实产生不同样本。
- [ ] 运行上述四个 unit 文件及 `tests/ui/test_preview.py`；先红后绿并更新旧预算断言。提交建议：`fix: retry scene layouts with a separate deterministic budget`。

### 任务 3 / P1：修复第二个及后续随机预览视角

**Files:** 修改 `src/bproc_ui/preview.py`、`src/bproc_ui/main_window.py`；测试 `tests/ui/test_preview.py`、`tests/ui/test_main_window.py`。

**Interfaces:** 保留 `PreviewController.refresh(region_key=None, sample_index=None, preview_seed=None)`。预览仍是“选定区域 + 场景 0 + 预览种子”的独立构图；不是完整多区域任务的全局帧序号。

- [ ] 扩展 `test_preview_uses_production_proposal_without_editing_persistent_seed`，参数覆盖索引 0、1、3，配置每场景 4 视角。断言均得到候选、view_index 正确，persistent seed 和 dirty 不变。
- [ ] 随机派生配置保留原 `views_per_scene`，仅收窄到选定区域；预览输入上限改为 `max(1, config.views_per_scene)`，越界输入钳制；不能再以 `total_shots` 作为上限。
- [ ] 固定模式仍按下拉框选中的单机位构建派生配置，view_index=0；序号显示 1 并禁用。模式切换、删除选中区域、空草稿时无异常。
- [ ] 测试多区域切换、170°→−170° 范围、第二个固定机位；候选与**相同派生配置**的核心采样结果比较，不要求与完整多区域任务偶然相同。状态说明保留“未经过像素遮挡验收”。
- [ ] 运行 `bp_test tests/ui/test_preview.py tests/ui/test_main_window.py -q`，新增用例先失败再通过。提交建议：`fix: preserve valid view indices in composition preview`。

### 任务 4 / P2：统一有效遮挡要求，支持多目标自然遮挡

**Files:** 新增 `src/bproc_core/occlusion.py`；修改 `src/bproc_runtime/run.py`、`src/bproc_core/config.py`、`tests/blender/_checks.py`；测试 `tests/unit/test_run.py`、新增 `tests/unit/test_occlusion_policy.py`、`tests/unit/test_output_checkers.py`。

**Interfaces:** `effective_occlusion_range(config: ProjectConfig, wants_unoccluded: bool) -> Range`；返回验收闭区间。运行时和检查器共同调用，不重复写 `not config.occluders` 分支。

- [ ] 写策略表测试：wants_unoccluded=True → `[0,0]`；只有一个启用目标且无有效专门遮挡物 → `[0,0]`；其他情况 → 配置 ratio。有效专门遮挡物定义为资源列表非空且 count 最大值 > 0；多个启用目标本身就是外部遮挡来源。
- [ ] 写 `test_natural_occlusion_is_independent_of_unused_occluder_assets`：使用相同 25% 像素遮挡、要求 `[.1,.5]`，无专门遮挡物与导入闲置 count=(0,0) 资源必须都接受。单目标无资源仍接受无遮挡，区间端点必须接受。
- [ ] 用共享规则替换 runtime 与 checker 的验收。保留完整基准非空、所有启用目标均有可见像素、后端 ID/尺寸/dtype 校验；其他目标完全不可见仍须拒绝。
- [ ] 测试 none_fraction 指定的场景仍要求零遮挡；不满足时按视角预算耗尽并 partial，不能因有其他目标就忽略实际像素。配置 warning 应准确说明何时退化为无遮挡。
- [ ] 运行 `bp_test tests/unit/test_run.py tests/unit/test_occlusion_policy.py tests/unit/test_output_checkers.py -q`；更新旧 `test_missing_occluders_accepts_every_target_visibility` 的错误多目标假设。提交建议：`fix: share effective occlusion rules across rendering and validation`。

### 任务 5 / P2：验收器适配 fixed/random，并补齐反例

**Files:** 修改 `tests/blender/_checks.py`、`tests/blender/check_bop.py`，必要时 `check_rgb_masks.py`；扩展 `tests/unit/test_output_checkers.py`。消费任务 4 的遮挡接口。

- [ ] 写正例：无 HDRI/无专门遮挡物单目标、多个目标自然遮挡、固定 key 为 left/right、固定和随机列表中存在同名 key。以 `candidate.camera_kind` 区分命名空间；旧记录缺 kind 时采用明确兼容规则。
- [ ] fixed 对照 `fixed_cameras` 的 r/φ/α 单值，random 对照 regions 的范围；保留跨界角度、全圆、极点相机矩阵一致性检查。把固定角度改错 10°、随机半径移出范围的临时数据必须 FAIL。
- [ ] 增加 BOP 反例：负 valid count、计数超 all、错误 bbox 宽高、错误 visible bbox、篡改 full count 并同步修改 fraction、RGB 相对路径穿越或指向另一张图。每个反例由原本合法的临时数据单独修改一项，要求返回具体诊断。
- [ ] 按已安装 `BopWriterUtility.py` 的 `calc_gt_info` 规则核对：`valid = count(full_mask & (depth>0))`；visib 等于可见掩码像素数；分别 `0<=valid<=all`、`0<=visib<=all`。**不能要求 visib<=valid**，原生 BOP 允许可见边缘深度为 0，现有合法 fixture 就覆盖此情况。
- [ ] bbox 使用 BOP xywh 与包含末像素的规则；visib bbox 可由掩码精确计算。bbox_obj 和 all 涉及大视口截断部分：无截断 fixture 可与 full mask 严格比较，有截断时不可简单要求负起点非法或 all 等于图内 mask；需要原生参考结果或明确报告该项未完全覆盖，不伪称已验证。
- [ ] relative_rgb 必须是安全相对路径，解析后仍在 dataset 内，并与该 BOP scene/image 的真实 RGB 文件匹配。保留对象 ID 与 GT 索引区分、毫米位姿/K/深度验证。旧 request_index 格式若不支持，明确提示版本不兼容，不归为新数据损坏。
- [ ] 运行 `bp_test tests/unit/test_output_checkers.py -q`；再对本报告三组已生成的合法数据运行检查器，全部正例通过，全部篡改反例失败。提交建议：`test: validate current camera modes and BOP metadata semantics`。

### 任务 6 / P2：遮挡物按当前相机距离摆放

**Files:** 修改 `src/bproc_core/scene.py` 的 `occluder_placements()`；扩展 `tests/unit/test_scene_candidates.py`。

**Interfaces:** 保留现有候选接口，使用传入的当前 pose；不再用所有机位的 `nominal_radius(config)` 决定遮挡物位置。无法放入合法空间时返回现有 `Rejection`，仍消耗该视角的有界预算。

- [ ] 增加同任务固定 r=.5m / 5m 用例，各生成 30 次尝试；断言成功候选的遮挡物全部角点在当前相机前方，深度大于 near、小于目标前表面的深度；旧实现的近机位用例应先失败。
- [ ] 在当前相机到锚中心的线段内采样位置，计入遮挡物实际变换尺寸与安全余量，侧向抖动沿当前相机 right/up；保留每视角、每次尝试重新采样行为。
- [ ] 加入保守 AABB 穿插检查（这是本项新增约束）：目标/遮挡物之间、专门遮挡物之间不穿插；无容纳空间返回可解释的拒绝，不能挪到相机后方或无限循环。
- [ ] 覆盖随机 r=.5–5m、α=±90°、大遮挡物、小尺度、多个遮挡物、同 seed 重现。保留已有三条逐视角遮挡物测试，并将几何断言改为当前 pose 的距离。
- [ ] 运行 `bp_test tests/unit/test_scene_candidates.py -q`；最终 GPU 近/远机位矩阵验证不再因“全部在相机后”而必然耗尽。提交建议：`fix: place occluders within the active camera segment`。

### 任务 7 / P2：主视口联合取景与相机窗口基础颜色一致

**Files:** 修改 `src/bproc_ui/views/scene_view.py`、`camera_view.py`、`src/bproc_ui/main_window.py`；新增 `tests/ui/test_scene_view.py`，扩展 `test_camera_window.py`、`test_main_window.py`。

**Interfaces:** 在 scene_view 新增 `placed_content_bounds(candidate, meshes) -> np.ndarray | None`，返回所有已放置目标和遮挡物的世界联合边界，复用 `core.scene.placement_bounds()`；新增 `target_preview_rgba(config) -> tuple[float,float,float,float] | None`。`CameraPreview.set_scene(candidate, meshes, target_rgba=None)` 增加可选色参数，主窗口传同一颜色定义。

- [ ] 写联合取景测试：两个 5cm 立方体中心 x=0/1m，期望联合 X 宽 1.05m、中心 x=.5m。覆盖旋转、非对称原点、缺 mesh、空场景；不把采样球、相机图标、辅助网格纳入边界。
- [ ] `fit_scene()` 用联合中心和半对角线更新**观察相机**，不修改 config、seed、candidate.camera 或科学内参。保留无有效几何的 fallback，保留用户观察旋转，不能每次刷新无条件重置交互视角。
- [ ] 写颜色测试：solid #ff0000 时两视口目标均红色，original 时保留原顶点色；遮挡物不被目标色覆盖；两个视口使用独立 visual 节点。更新 FakeCamera/FakeScene 的签名。
- [ ] 运行 `bp_test tests/ui/test_scene_view.py tests/ui/test_camera_window.py tests/ui/test_main_window.py tests/unit/test_view_projection.py -q`；真实桌面再检查 Home、F12 和改色即时重绘。提交建议：`fix: align viewport framing and target base colors`。

### 任务 8 / P2：Blender 查看请求绑定快照，拒绝旧结果串项目

**Files:** 修改 `src/bproc_ui/state.py`、`blender_view.py`；必要时修改 `src/bproc_export/blender_view.py`；扩展 `tests/ui/test_project_state.py`、`test_blender_viewer.py`、`tests/unit/test_blender_view.py`。

**Interfaces:**

- `ProjectState.context_revision: int` 在 set_project、有效 replace_config、项目路径改变时递增；单纯选择编辑对象、只刷新预览候选不递增。load 在发出项目变化前设置新 path，save-as 路径改变也须使旧上下文失效。该值不写科学 YAML。
- 新增冻结 `ViewRequest`，携带 request_id、context_revision、config、candidate、workspace、script；每个请求使用自己的子目录。
- 完成回调接受请求与结果，只有仍匹配当前请求/上下文，且 `state.candidate is request.candidate`，才调用 `open_gui(request.config, blend, request.script, request.workspace)`；日志、HDRI 提示也用请求快照。候选身份单独检查，避免只刷新预览就使无关的脚本导出过期。

- [ ] 注入 FakePool，暂停 A 的 prepare，切到 B 后完成 A；断言没有用 B 配置打开 A.blend，旧请求提示已过期、busy 清除，B 的新请求可正常执行。
- [ ] 覆盖 A→B→A、同项目候选刷新、另存路径变化、迟到失败回调；不得只比较 `config==`，旧失败不能清掉新任务。现有已打开的用户 Blender 窗口不应被误杀。
- [ ] 实现请求身份与快照检查；后台继续只做构建，Qt 控件只在主线程更新。正常请求仍能打开，错误仍显示具体日志路径。
- [ ] 用 Event 暂停后台构建，QTimer 心跳在释放前继续增加；运行 `bp_test tests/ui/test_project_state.py tests/ui/test_blender_viewer.py tests/unit/test_blender_view.py -q`。
- [ ] 桌面复验“打开 Blender → 切项目 → 再打开”与无 HDRI 提示。提交建议：`fix: retain Blender viewer request context through completion`。

**未证实项：** 先前怀疑“构建失败却复用旧 scene.blend”尚无独立实证，不作为已确认缺陷；如接手者验证，应单列复现和结论。

### 任务 9 / P2：部分初始化阶段也必须写出最终状态

**Files:** 修改 `src/bproc_runtime/{cli,run,output,records}.py`；扩展 `tests/unit/test_run.py`，新增 `tests/unit/test_runtime_lifecycle.py`。

**Interfaces:** `RunRecorder.finish(summary)` 必须在 begin 尚未完成时安全调用；JsonRunRecorder 的 `_config/_world/_started` 未设置齐也不得因此再次崩溃。已独占创建的数据集由一个明确生命周期 owner 负责最终状态；run_project 接管后 CLI 不重复 finish。

- [ ] 复现 begin 已写 running、save_project 后抛 KeyboardInterrupt：要求退出 130、status=interrupted；SIGTERM 要求 143。分别在 project/objects/初始 manifest 写入前后注入，不能残留 running。
- [ ] 覆盖 reserve_dataset 成功后、进入 run_project 前的 backend/writer 构造异常和中断。该窗口也要最终 failed/interrupted；已有别人的目录冲突不得被修改。
- [ ] 实现整个已保留输出生命周期的 try/finally；run_project 在 begin 返回前就承担清理义务。close/finalize 调用次数明确为最多一次，部分构造时只清理已经获得的对象。
- [ ] 保留原生 bpy 调用期间延迟信号、提交 RGB/manifest/计数事务、BOP 后处理完成才计 saved 的现有测试。finish 本身失败时仍恢复信号处理器并保留原始故障诊断；磁盘不可写要明确报错，不能承诺一定写出 status。
- [ ] 运行 `bp_test tests/unit/test_run.py tests/unit/test_runtime_lifecycle.py -q`；任务 12 再做独立子进程的真实 SIGINT/SIGTERM。提交建议：`fix: finalize run status during partial initialization`。

**边界：** SIGKILL/断电无法保证 Python 清理；不把这种情况纳入“绝不留下 running”的承诺，也不新增自动续跑。

### 任务 10 / 性能：帧 metadata 改为线性追加

**问题：** 400 条最终约 1.13MB，却累计写入约 225.94MB、耗时 10 秒。每帧全量重写导致平方增长。

**Files:** 修改 `src/bproc_runtime/output.py`，新增 `src/bproc_runtime/metadata.py`；更新 `tests/blender/_checks.py`、`check_failures.py`；新增 `tests/unit/test_metadata_journal.py`，扩展 `test_world_metadata.py`、`test_output_checkers.py`。

**Interfaces:** 保留 recorder 公共方法；新增 `read_frame_records(metadata_dir: Path, *, allow_incomplete_tail: bool = False) -> list[dict]`。新 manifest 使用 `schema_version: 2`，帧 journal 为 `frames.jsonl`；每行有 `schema_version: 2` 和原 frame row 字段。旧无版本 manifest 可只读兼容，不原地迁移历史数据。

- [ ] 写测试：100/200/400 个固定长度记录，每帧只追加一行，累计序列化/写入字节呈线性；用 I/O spy 比较，400 条不应约为 100 条的 16 倍。不要用严格墙钟阈值做 CI 断言。
- [ ] record_frame 先生成完整 JSON 行，再追加、flush、fsync，成功后才计入已提交列表。begin 仅写空初始 manifest；finish 原子生成最终完整 manifest，禁止逐帧写全量快照。所有 journal/schema 字段允许被旧测试适配，但不得改变原 frame ID、位姿、seed 含义。
- [ ] reader 优先读取新 journal，最终 manifest 与 journal 同时存在时核对帧数/索引一致；拒绝中间坏行、重复帧、未知版本。仅显式 recovery 模式容许忽略末尾不完整行并给出 warning；正常 completed 数据有坏尾行仍 FAIL。
- [ ] 更新运行中的监控和 `check_failures`：轮询已 fsync 的 journal，而不是依赖每帧刷新的 manifest。正常/partial/interrupted/failed 均尝试写最终 manifest；BOP journal 的 written 不等于后处理成功的 saved。
- [ ] 测试中断后最后一条提交记录可读、坏尾行、坏中间行、旧 manifest、begin 半完成、finalize 失败；运行 `bp_test tests/unit/test_metadata_journal.py tests/unit/test_world_metadata.py tests/unit/test_run.py tests/unit/test_output_checkers.py -q`。
- [ ] 在新目录重跑 100/200/400 条测量，记录耗时与累计写入字节，和原证据比较。提交建议：`perf: append frame metadata and finalize manifests atomically`。

### 任务 11 / 性能：导出按钮走后台任务，保留同步核心 API

**Files:** 新增 `src/bproc_ui/export_worker.py`；修改 `src/bproc_ui/panels/export.py`、`src/bproc_ui/main_window.py`；必要时更新 `tools/gui_smoke.py`；扩展 `tests/ui/test_sampling_export.py`、`tests/ui/test_main_window.py`，保留 `tests/unit/test_export.py`。

**Interfaces:** 保留 `export_job(config, destination) -> ExportResult` 以及已有同步 `ExportPanel.export_to(path)` 供非交互调用；按钮改用新增 `ExportPanel.start_export(path) -> bool`。后台 QRunnable 捕获 config、绝对 destination、任务 8 的 revision、request_id；完成信号传 request 与 ExportResult，失败信号携带 request 与诊断。

- [ ] 用 Event 阻塞假的 export_function，点击后在 release 前 QTimer 心跳继续增加、其他编辑操作仍响应；重复点击不启动第二任务，按钮显示忙碌且完成/失败后恢复。不要在主线程 sleep 或仅凭一次 processEvents 声称异步。
- [ ] 按钮保存为 `self.export_button`；任务只执行打包和检查，完成后主线程更新 command、preview_command、last_result 及 exported 信号。同步路径与异步路径共用结果展示函数。
- [ ] 切项目或配置变更时，清空已显示的 A 命令，或明确标记为 A 的过期快照：覆盖 ExportPanel.command/preview_command/last_result 和 MainWindow.command_preview。仅过滤迟到回调不够；磁盘上 A 的任务保留。只刷新候选不使导出失效，复用任务 8 的 config/path revision。
- [ ] 增加 A 已导出→切 B 的测试，两个命令区均不能看似 B 的当前命令；B 完成新导出后 A 的迟到成功/失败不得覆盖或清掉 B。
- [ ] 保留网格重新校验、非法点云拒绝、路径引用/引用转义、独占目录、launch.json 最后发布与失败标记。后台化不需要新增通用队列或可中断文件事务；本轮不强制实现取消按钮。
- [ ] 运行 `bp_test tests/ui/test_sampling_export.py tests/ui/test_main_window.py tests/unit/test_export.py -q`；真实约 50 万面文件测量导出期间心跳，保存新证据。若 gui_smoke 仍走同步 export_to 可保留，但它不能证明交互按钮异步，须由上述测试和桌面操作补齐。
- [ ] 提交建议：`perf: export job snapshots outside the Qt event thread`。

### 任务 12 / 交付门槛：修复验收工具、完整回归与 GPU 小样本

**Files:** 修改 `tests/blender/check_failures.py`、`tools/render_demo.py`、`src/bproc_export/{commands,bundle}.py`、`src/bproc_export/templates/run_job.py`、`src/bproc_runtime/{preflight,cli}.py`、`src/bproc_ui/panels/export.py`；测试 `tests/unit/{test_launcher,test_config,test_export}.py`、`tests/ui/test_sampling_export.py`，新增 `tests/unit/test_render_demo.py`；更新 `docs/{usage,validation}.md`。

**Interfaces:** `build_command(job_dir: Path, conda_env: str, *, num_samples: int | None = None, num_scenes: int | None = None, output: Path | None = None) -> str`；双计数参数同时非空时抛 ValueError。各 CLI 使用互斥参数组。`preflight.apply_overrides(config, args)` 优先处理合法 num_scenes 为 `replace(config, scene_count=N)`，旧构造的 args 缺该属性时用 `getattr(args, "num_scenes", None)` 兼容；仍拒绝双参数输入。

- [ ] `check_failures.py` 的 `replace(..., num_samples=...)` 全部改为明确的 scene_count/views_per_scene 或固定机位数，所有 requested/skipped 从 total_shots 推导；布局预算断言按任务 2，轮询方式按任务 10。当前脚本仅 `--help` 能启动，**尚不能直接拿来验收**。
- [ ] 保留旧 `--num-samples` 兼容行为，但新增准确的 `--num-scenes N`，两者互斥；N 为非负整数。启动器 → preflight → runtime CLI → build_command 全链路透传。少量试渲染改成 `--num-scenes 1`，UI 文案显示实际 `config.views` 张；不再声称一定 4 张。
- [ ] `tools/render_demo.py` 对 warning 仅提示、只拦 error；两种计数选项都在导出前应用到快照，导出和 --run 语义一致，运行时不再重复覆盖。旧 --num-samples 的帮助说明按视角数取整，并展示实际张数。测试 3 视角时旧 1/4/5 → 3/3/6 张，新 1 场景 → 3 张；两个计数选项同时出现明确报错。
- [ ] 执行下文全量单元/Qt、检查器正反例、真实桌面及 GPU 矩阵。每个用例写入新目录，保存 config、HEAD、环境、命令、日志、status、检查器输出和人工观察。
- [ ] 更新验证文档，只记录本次实际执行结果；旧 `docs/validation.md` 的 273 passed 是历史记录，当前审查为 268 passed，不将历史计数复制为新结论。记录修复提交及剩余限制。
- [ ] 提交建议：`test: align acceptance tools with scene-based GPU jobs`。达到下文完成标准后交付；不以单元测试全绿替代 GPU 标签验证。

## 详细测试步骤

### 1. 接手、环境与基线

先读当前工作区差异，不重置别人的修改。测试时关闭会自动更新同一输出目录的旧任务；不要删除已有数据集来让冲突测试通过。

```bash
cd /home/kepler/rm_2027/blenderproc-ui
git status --short
git rev-parse HEAD

# 在当前终端定义一次；只加载项目需要的 Qt pytest 插件。
bp_test() {
  env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    QT_API=pyside6 QT_QPA_PLATFORM=offscreen \
    /home/kepler/rm_2027/blenderproc-ui/.venv/bin/python \
    -m pytest -p pytestqt.plugin "$@"
}
bp_test tests/unit tests/ui -q
```

基线预期 268 passed；如果 HEAD 不同，以当前实跑为准并记录差异。新缺陷测试按任务先观察失败，再实现修复；不能为保留旧断言而恢复已确认错误的行为。

| 环境 | 本机路径/版本 |
|---|---|
| UI Python | `/home/kepler/rm_2027/blenderproc-ui/.venv/bin/python`，3.10.12 |
| PySide6 / VisPy / NumPy | 6.8.3 / 0.15.2 / 1.26.4 |
| conda 启动 Python | `/home/kepler/miniconda3/envs/bop/bin/python`，3.8.20 |
| BlenderProc 可执行文件 | `/home/kepler/miniconda3/envs/bop/bin/blenderproc`，2.8.0 |
| Blender | `/home/kepler/blender/blender-4.2.1-linux-x64/blender`，4.2.1 |
| Blender 内部 Python | `/home/kepler/blender/blender-4.2.1-linux-x64/4.2/python/bin/python3.11` |
| Blender 扩展依赖目录 | `/home/kepler/blender/blender-4.2.1-linux-x64/custom-python-packages/lib/python3.11/site-packages` |
| BOP toolkit 源码 | `/home/kepler/blenderproc/23AutoEngineer/Linemod-Render/bop_toolkit` |

GPU 访问在受限沙箱可能失败，既有审查通过有 GPU 访问权限的执行环境完成。遇到 driver/EGL 权限错误，应使用正确权限重新测试并记录，不改成 CPU 来“通过”。不为 ROS 插件导入错误安装 ROS 依赖。

### 2. 在新目录导出当前代码，不能复跑旧 runtime 冒充修复验证

原任务包含当时的 runtime 拷贝。修复源代码后必须**重新 export_job**。下列命令使用现有导出接口与任务 1 拟新增的 suggest_layout；整段在当前未修复基线不能直接执行，至少先完成任务 1、4。所有路径均指向新目录，不修改原测试证据。

```bash
cd /home/kepler/rm_2027/blenderproc-ui
mkdir -p .artifacts
export BPROC_HANDOFF_RUN="$(mktemp -d "$PWD/.artifacts/handoff-validation-XXXXXX")"
env -u PYTHONPATH .venv/bin/python - <<'PY'
from dataclasses import replace
from pathlib import Path
import json
import os

from bproc_core.assets import load_mesh, suggest_layout
from bproc_core.config import Range
from bproc_core.project_io import load_project
from bproc_export.bundle import export_job

repo = Path('/home/kepler/rm_2027/blenderproc-ui')
audit = repo / '.artifacts/audit-20261018/render'
root = Path(os.environ['BPROC_HANDOFF_RUN'])
rows = []
for name in ('real-cores-rgb', 'multi-random-bop', 'multi-fixed-rgb', 'suggested-layout'):
    source = 'multi-random-bop' if name == 'suggested-layout' else name
    config = load_project(audit / f'{source}-job' / 'scene.yaml')
    if name == 'suggested-layout':
        meshes = {t.asset.key: load_mesh(t.asset) for t in config.targets if t.enabled}
        boxes, radius = suggest_layout(meshes, config.targets, config.anchor_key, config.camera)
        config = replace(config,
            targets=tuple(replace(t, offset=boxes[t.asset.key]) if t.enabled else t
                          for t in config.targets),
            regions=tuple(replace(r, radius_m=radius) for r in config.regions),
            occluders=(), hdri=replace(config.hdri, files=()),
            occlusion=replace(config.occlusion, none_fraction=0, ratio=Range(0, 1)),
            output=replace(config.output, format='rgb_masks'))
    config = replace(config, output=replace(config.output, root=root / 'results', dataset_name=name))
    result = export_job(config, root / 'jobs' / name)
    rows.append(dict(name=name, job=str(result.directory),
                     dataset=str(config.output.root / name), requested=config.total_shots))
(root / 'cases.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
print(root)
print(json.dumps(rows, ensure_ascii=False, indent=2))
PY
```

说明：前三组严格沿用原配置；第四组是**修复后的建议布局专用验证配置**，允许自然遮挡 `[0,1]`，避免把布局修复与特定遮挡窗口的可达性混在一起。原 0/6 案例快照保留，不篡改历史状态。如果 `.artifacts` 证据或用户 cores 文件已不在，记录资源缺失，使用新生成演示资源替代功能测试；不能声称重跑了原模型。

演示资源缺失时可在另一个新目录生成：

```bash
env -u PYTHONPATH .venv/bin/python tools/create_demo.py \
  "$BPROC_HANDOFF_RUN/demo-inputs" \
  --blender /home/kepler/blender/blender-4.2.1-linux-x64/blender
```

该工具会短暂启动 Blender 生成本地 HDRI，不联网；生成的 demo 默认 2×2，可通过 dataclasses.replace 明确改为所需场景/视角数，再导出。

### 3. 离线预检、实际 GPU 渲染、检查产物

下例先跑 cores。其他用例按 `cases.json` 的绝对路径逐一替换；每条命令单独保存退出码和日志。`--check-only` 返回 0 只证明预检通过，不证明已执行 GPU 渲染。

```bash
cd /home/kepler/rm_2027/blenderproc-ui
env -u PYTHONPATH .venv/bin/python tests/blender/verify_runtime.py \
  "$BPROC_HANDOFF_RUN/jobs/real-cores-rgb"

cd /tmp
env -u PYTHONPATH /home/kepler/miniconda3/envs/bop/bin/python \
  "$BPROC_HANDOFF_RUN/jobs/real-cores-rgb/run_job.py" --check-only
env -u PYTHONPATH /home/kepler/miniconda3/envs/bop/bin/python \
  "$BPROC_HANDOFF_RUN/jobs/real-cores-rgb/run_job.py" \
  > "$BPROC_HANDOFF_RUN/real-cores-rgb.log" 2>&1

cd /home/kepler/rm_2027/blenderproc-ui
env -u PYTHONPATH .venv/bin/python tests/blender/check_rgb_masks.py \
  "$BPROC_HANDOFF_RUN/results/real-cores-rgb"
env -u PYTHONPATH .venv/bin/python tests/blender/check_bop.py \
  "$BPROC_HANDOFF_RUN/results/multi-random-bop"
```

最后一条只在已渲染 BOP 用例后执行。不要给完整任务附加旧 `--num-samples 4`：它会按整场景取整，当前 3 视角时实际为 3 张。预检后若修改 YAML，最终检查必须依据真正运行的 project.yaml。

检查顺序：退出码 → status 计数 → manifest/journal → 每帧文件 → 独立几何/标签检查 → 人工看图。日志需确认所选 GPU、CPU 禁用、BOP NVIDIA EGL，无软件光栅器；保留 RGB 样图及至少一组 full/visible mask。

### 4. 验收矩阵

| 用例 | 设置 | 修改后验收标准 |
|---|---|---|
| 原 cores RGB | 原快照 2×3，无 HDRI，50 samples | 6/6，退出 0，修复后 checker PASS，不能再误报非零遮挡 |
| 原随机 BOP | 原快照 2×3，IDs 2/7，混合单位、HDRI、遮挡物 | 6/6，退出 0；K、位姿、canonical mm 模型、深度、GT 索引、双 mask、gt_info 均通过 |
| 原固定 RGB | 原快照 2×2，left/right，统一红色 | 4/4，退出 0；fixed checker PASS；主/相机/实际 RGB 的基础颜色设定一致 |
| 新建议布局 | 2×3，无专门遮挡物，ratio=[0,1] | 目标布局不再全部 collision；固定 seed 的此夹具应 6/6；若仍拒绝，定位具体几何/可见性原因，不自动放宽约束 |
| 手工不可能布局 | 两目标固定相交，2×3，layout=30 | 有界结束，0/6、skipped=6、partial、退出 2；布局拒绝准确归类 |
| 自然遮挡规则 | 同一 25% 掩码证据，有/无闲置遮挡资源 | 单元测试都接受；再构造真实相交投影视角做至少一张 GPU 观察，所有目标仍可见 |
| 近/远遮挡物 | 小目标，固定 r=.5/5m，各一视角 | 几何角点均在当前相机前；选定可达的遮挡窗口后两种距离各保存至少 1 帧，确认不是相机后方导致拒绝 |
| 表面与降噪 | flat / smooth / auto，各含有面网格；OPTIX auto / none | RGB 效果符合设置，顶点/位姿不变；标签二值、深度不经颜色降噪；保留 CUDA+显式 OptiX 不兼容拒绝测试 |
| 无效输入/冲突 | 点云、丢失模型、无效 GPU、已有目录/软链接 | 明确失败；不联网补装、不回退 CPU、不修改既有目录 |
| 信号与后处理 | 新 RGB/BOP 长一点的小任务，SIGINT/SIGTERM | 状态 interrupted、退出 130/143，已提交帧可检查；BOP 后处理失败不得计为 saved |
| 元数据性能 | 相同结构 100/200/400 条 | 写入字节近线性，无逐帧全量 manifest；中断后 journal 可读 |
| UI 后台流程 | 约 50 万面模型，导出/Blender 构建期间切项目 | 心跳不中断、按钮状态恢复、旧快照不冒充新项目 |

注意：Cycles GPU 浮点结果不要求跨硬件逐字节一致；同 seed 的候选参数、场景共享属性与索引应确定。原 BOP delta=0.015m 的 visibility 与运行时精确分割遮挡率不是同一指标，不能互相替代。

### 5. 真实桌面检查

Qt offscreen 只能证明不创建 GL 上下文的测试，不能证明 VisPy 真实画面正确。使用已有桌面会话启动：

```bash
cd /home/kepler/rm_2027/blenderproc-ui
env -u PYTHONPATH -u QT_QPA_PLATFORM .venv/bin/python tools/gui_smoke.py \
  --project "$BPROC_HANDOFF_RUN/jobs/multi-fixed-rgb/scene.yaml" \
  --output "$BPROC_HANDOFF_RUN/gui-fixed" --timeout 60
```

再以随机多视角项目运行一遍。保存 gui-manifest 与截图，人工补齐：

1. 随机序号 1、2、最后一个均出现构图；固定机位切 left/right，序号不误导。
2. 两目标拉开到 1m，按 Home 能看到所有目标；观察旋转/平移不改科学相机。
3. 切换原色/统一红色，两视口同步；关闭相机窗再 F12，原 GL 上下文继续使用。
4. 调球坐标范围、切中心、保存/重开 YAML，参数精度与单位不丢失。
5. 点击导出期间仍能操作；完成命令对应点击时快照；切项目后不展示过期命令。
6. Blender 构建期间切项目，旧结果丢弃；正常构建能打开正确构图，无 HDRI 提示正确。

GUI smoke 当前使用同步 export_to；这不妨碍作为截图工具，但后台按钮必须另行验证。Blender 内修改不自动回写 UI，按当前产品边界手动回填即可。

### 6. 中断、坏数据和故障测试

修好任务 12 的旧字段后，才运行：

```bash
cd /home/kepler/rm_2027/blenderproc-ui
env -u PYTHONPATH .venv/bin/python tests/blender/check_failures.py \
  --project "$BPROC_HANDOFF_RUN/demo-inputs/demo.yaml" \
  --destination "$BPROC_HANDOFF_RUN/failure-cases"
```

这里的 demo-inputs 来自前面的可选生成步骤；未生成则改为当前存在的 `.artifacts/demo/demo.yaml`。检查该工具每种输出分别创建新目录，信号仅发送给测试自己创建的 PID；不能使用泛化进程名杀掉用户的 Blender。

begin 中间故障由单元测试或独立子进程精确注入，避免靠“碰巧几秒后 Ctrl+C”命中窗口；原生渲染期间中断可能等当前 pass 结束，应设置合理超时并检查退出状态。磁盘异常用临时 recorder/writer 注入；非法 BOP 字段只改临时夹具，不篡改真实数据集证据。

## 暂不并入本轮修复的改进建议

- **尺度可见性：** 导入/改单位/自动适配后显示实际三轴尺寸、旧/新相机距离；同一 cores 的近期快照出现 m/cm/mm，不能据此推定其真实单位。现有导入自动适配是明确实现，不作为未经授权改参数的缺陷回滚。
- **训练分布：** 原 cores 6 帧中心 x 仅约 325–325.5px、可见面积约 1.8%–3.0%，背景中性灰。之后可加构图偏移、目标占比、背景与灯光分布统计，但先定义需求，再添加随机策略；不自动改变用户原任务。
- **数据划分：** 同场景多视角应按 scene 分训练/验证集，避免共享摆放和光照泄漏；本轮不增加 YOLO 转换或数据管理模块。
- **大型模型预览：** 若实际操作仍慢，可考虑仅视口使用简化网格；渲染与 BOP 始终使用原模型。需独立测量后决定，不纳入当前正确性修复。
- **查看体验：** 显示实际抽样材质值/HDRI、构建阶段和 Blender 窗口状态；取消构建、渲染队列、完整三维背景、PBR 视口属于后续范围。

## 完成标准与下一位执行者记录

只有同时满足以下条件才能把本轮标记完成：

- [x] 任务 1–12 对应修复及正反例测试已落实；未做部分见下表"未完成/限制"列。
- [x] 当前全量 unit/Qt 通过（388 passed）；新增缺陷用例有先失败后通过的记录（布局 0/6 → 6/6、`_xywh` 24 处误报 → PASS）。
- [x] 三组原配置与新建议布局均重新导出当前 runtime，在本机 GPU 真实运行；RGB/BOP 检查器通过。
- [x] partial、SIGTERM、既有目录保护有独立证据；近远遮挡窗口产物与 BOP 后处理失败只有单元测试，未单独造 GPU 夹具（见下表）。
- [x] 真实桌面验证通过（`gui_smoke.py`，12 张截图 + 异步导出任务）。
- [x] metadata 写入随帧数线性（journal 追加 + 结束一次 manifest，单测比较 100/200/400 帧累计字节）、交互导出按钮全程不阻塞事件循环（真实 QThreadPool + 心跳计时）。
- [x] 更新 usage/validation，记录 commit、环境、输出路径、退出码、测试数量和剩余问题。

执行记录建议追加到下表；一个任务一行，证据放新验证目录：

| 任务 | 实际提交/版本 | 测试与退出码 | 新证据路径 | 未完成/限制 |
|---|---|---|---|---|
| 1 布局策略 | `110ab08` | `test_layout_suggestions.py` 7 例，全量 275 passed | — | 仍用 AABB 判交，凹模型可能被保守拒绝 |
| 2 布局重试 | `1d2ea1d` | `test_layout_retries.py` 8 例（含种子 5 首次碰撞、第 2 次成功） | — | 只对 collision 重试，遮挡拒绝仍按视角计数 |
| 3 遮挡策略 | `1180434` | `test_occlusion_policy.py` 与更新后的 run/preview 用例 | — | 25% 掩码证据是合成掩码，真实 GPU 只做了自然遮挡观察 |
| 4 遮挡物沿机位 | `b678994` | 段内放置 13 例，含"段太短可解释拒绝" | `results/multi-random-bop`（遮挡拒绝 15 次后 6/6） | 近/远遮挡窗口未单独造夹具 |
| 5 检查器 | `e325adc` | `check_rgb_masks`/`check_bop` 双向用例 | 见下条 | BOP 原生零深度边缘按原语义记录，不当作缺陷 |
| 6 检查器误报 | `e325adc` + 本轮 `_xywh` 修正 | 修复前每帧 1px 误报 24 处，修复后 PASS | `.artifacts/handoff-20261022/multi-random-bop` | — |
| 7 视口 | `5c43643` | `test_scene_view.py`，`gui_smoke.py` 真实桌面截图 | `.artifacts/handoff-20261022/gui-real/` | 相机窗口按候选同步；Blender 内改动不回写 |
| 8 视图身份 | `785c8d4` | `test_views.py`、`test_blender_view.py` | — | — |
| 9 运行时生命周期 | 本轮（随任务 12 一起提交） | `test_runtime_lifecycle.py` 10 例 | `results/interrupted-4`（143、3 帧可读） | begin 中途故障用临时 recorder 注入，未造真实磁盘故障 |
| 10 元数据 journal | 本轮 | `test_metadata_journal.py` 9 例，schema 2 | `results/interrupted-4/run_metadata` | 旧版 manifest 仍可读，不迁移 |
| 11 异步导出 | `ea5dba3` | `test_export_worker.py` 8 例（含 487k 面模型、1.5 s 导出内 95 次心跳） | `gui-real/gui-async-job` | 导出不可取消（无"取消"按钮） |
| 12 计数语义 | 本轮 | `test_launcher.py`/`test_export.py`/`test_render_demo.py` 新增 12 例 | 四个 job 全部用 `--num-scenes` 预检通过 | `--num-samples` 仅兼容，不推荐 |

本报告已对照当前源码、原始审查证据和测试接口进行自查：布局/预览/遮挡/检查器/UI/生命周期/性能均有任务和验收归属；拟新增接口与现有接口已区分；BOP 原生零深度边缘与截断语义单独注明。

**执行结果（2026-10-22）**：任务 1–12 全部落实，全量 388 passed，GPU 真实渲染与真实桌面验证见 `docs/validation.md` 的"本轮修复的验收"。报告中"暂不并入本轮"的建议仍未实施，保持原样；`jobs/` 下早期导出的旧运行时目录是历史产物，不是当前源码。
