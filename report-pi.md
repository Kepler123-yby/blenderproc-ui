# blenderproc-ui 交付报告（接手执行）

**接手基线**：`aebd537`（26 个分阶段提交，268 passed）
**当前 HEAD**：`4f75430` 之后的复核修复提交（403 → **411 passed**）
**执行者**：Pi 编码代理；设计决策与验收口径由用户确认（逐条记录在 `report.md` 的 N1–N6 与本文）
**日期**：2026-10-23

本文是"我接手后做了什么、证据在哪、哪些没做到"的交付记录，与 `report.md`（GPT 的审计与指示）配套阅读。凡未真实执行的项目都标为未完成，不以单测替代。

---

## 1. 交付范围

| 阶段 | 来源 | 提交区间 | 提交数 | 测试数 |
|---|---|---|---|---|
| 阶段一：审计任务 1–12 | `report.md` 前半（"执行清单"） | `aebd537..a86f999` | 14 | 268 → 388 |
| 阶段二：复核任务 N1–N6 | `report.md`"当前版本复核与下一步指示" | `a86f999..4f75430` | 3 | 388 → 403 |
| 阶段三：独立复核 R1–R4 与文档更正 | `report-review.md` | `4f75430` 之后 | 待记 | 403 → 411 |

全部已推送到 `origin/main`。

---

## 2. 复现环境与命令

| 项 | 值 |
|---|---|
| 单元测试 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p pytestqt.plugin -q` → **411 passed, 14 s** |
| 渲染环境 | conda `bop`：`/home/kepler/miniconda3/envs/bop/bin/python`（3.8.20，blenderproc 2.8.0，bop_toolkit） |
| Blender | `/home/kepler/blender/blender-4.2.1-linux-x64/blender`（4.2.1） |
| GPU | RTX 3060 Ti 8 GB；OptiX 设备；硬件 EGL（`EGL_PLATFORM_DEVICE_ID=0`） |
| 单任务运行 | `cd <job> && /home/kepler/miniconda3/envs/bop/bin/python run_job.py` |
| 产物检查 | `tests/blender/check_rgb_masks.py::verify_rgb_masks`、`tests/blender/check_bop.py::verify_bop` |

---

## 3. 阶段一：任务 1–12

| # | 问题 | 提交 | 关键实现 | 验收证据 |
|---|---|---|---|---|
| 1 | 建议布局把米级构件推到 0.23 m | `110ab08` | `suggest_layout` 返回逐目标可行区间，距离与区间由同一函数给出 | 夹具 250/250 场景可行；GPU 4/4 帧，`verify_rgb_masks` PASS |
| 2 | 一次碰撞就丢整场 | `1d2ea1d` | 布局重试独立预算 `layout_max_attempts=30`，seed 由 (seed, scene, attempt) 决定 | `tests/unit/test_layout_retries.py` 8 例；真实 240 帧任务仅 396 次 occlusion 拒绝 |
| 3 | 多视图只有第一张命中 | `1180434` | `generate_views`：布局固定、按视图流采样、非碰撞失败回滚计数 | 60 场景 × 4 视图全部产出 240 帧 |
| 4 | 校验器与运行时遮挡规则不一致 | `b678994` | `src/bproc_core/occlusion.py` 单一来源（`reserve_interval`/`slice_size`/`required_visible`） | 三处调用点共用断言测试 |
| 5 | BOP 元数据语义未验证 | `e325adc` | 真跑 + `check_bop.py` 回放；修复 bbox 本地点偏移（20 cm 立方体曾偏到 (150,50,100) mm） | `verify_bop` PASS；位姿/深度/bbox 逐项复算 |
| 6 | 固定机位可能拍不到遮挡物 | `afb57bb` | 遮挡物沿相机有向线段放置，只允许可解释跳过 | 固定机位 6/6、随机机位 6/6 |
| 7 | 视口取景与颜色不一致 | `5c43643` | 取景框用内容联合包围盒二分求距离；`hex_to_rgb` 统一颜色 | `tests/ui/test_scene_view.py` 12 例 + 真机截图 |
| 8 | Blender 查看请求残留上下文 | `785c8d4` | `ViewRequest` + `context_revision`，迟到回调全部过滤 | `test_scene_view.py` 请求身份用例 |
| 9 | 未开始/部分初始化时无终结状态 | `1d9ab97` | `run_project` 单独报告 `not_started`，`finish` 兜底，CLI 只终结一次 | `test_runtime_lifecycle.py`；真实 SIGINT 130 / SIGTERM 143 |
| 10 | 元数据二次方增长 | `831093a` | `frames.jsonl` 追加 + `manifest.json` 仅 finish 写，reader 交叉校验 | 100/200/400 帧 256 KB/512 KB/1 MB 线性 |
| 11 | 导出阻塞 UI、结果可能属于旧项目 | `ea5dba3` | 工作线程导出；命令仅在配置+路径+revision 仍是当前时显示 | 996,000 面模型 2.79 s、390 次心跳（约 140/s） |
| 12 | 旧参数/文档/证据不一致 | `1899222` | `--num-scenes` 与 `--num-samples` 互斥、警告不阻断、§"本轮修复的验收"写入 `docs/validation.md` | 四个任务全部用新参数预检通过 |

---

## 4. 阶段二：N1–N6

| # | 问题 | 提交 | 做法 | 实测结果 |
|---|---|---|---|---|
| N1 | CAD 原点语义冲突导致 250/250 场景 collision | `fcd53a3` | 布局统一"包围盒中心"语义；`content_bounds` 增加可选中心参数；建议器不再依赖未生效的 authored offset | `test_layout_is_invariant_to_cad_origin`、`test_offset_cad_origin_layout_survives_all_scene_attempts`（250/250 可行）；原点偏移夹具 4/4 帧 PASS |
| N2 | 遮挡间隙按固定厘米算，米级目标必失败 | `fcd53a3` | 遮挡物深度上界改为包围盒在视线方向的投影；间距随场景尺度 | 近 0.5 m 6/6、远 5 m 6/6、1 mm 夹具 4/4；遮挡比 `[0.233, 0.455, 0.179, 0.237, 0.279, 0.327]` 全在 [0.1, 0.5]，角点全在目标前方且越过 `clip_near_m` |
| N3 | 最终 manifest 写失败时磁盘仍 `completed` | `12890ba` | 返回值与落盘 status 统一终态；保留已提交帧数；status 自身不可写时明确报告 | `test_final_manifest_failure_matches_disk_status`；`failed/1` 与磁盘一致 |
| N4 | journal 与 manifest 只比帧号 | `12890ba` | reader 结构化比较整行内容（引用、scene/view、位姿）并校验 schema 版本与字段类型 | `test_reader_rejects_same_index_different_content`、`test_reader_rejects_unknown_manifest_version_with_journal` |
| N5 | 缺端到端证据 | 本轮 | 全新目录重导出并跑专项 | 见下节证据表 |
| N6 | 小批量试生产 | 本轮 | 真实 `cores.ply` + 遮挡物 + HDRI，60 场景 × 4 视图 | 240/240 帧，977 次尝试，接受率 24.6%；见下节 |

### 4.1 N5 证据

| 专项 | 证据文件 | 结果 |
|---|---|---|
| 建议布局（原点偏移夹具） | `n5_layout.py`、`layout-suggestion.json`、`layout-run.log` | 4/4 保存，`verify_rgb_masks` PASS；建议 `right.x=[0.11,0.21]`，距离 `[0.526,0.710]` |
| 建议布局（真实资源） | `real-pilot.yaml`、`real-pilot-job.json` | 建议距离 `0.289–0.394 m`，用于 240 帧试生产 |
| 近/远/毫米机位（**RGB 双掩码，非 BOP**） | `n5_occlusion.py`、`occlusion-summary.json`、`{near-500mm,far-5m,millimetre}.verify.log` | 6/6、6/6、4/4，`verify_rgb_masks` PASS；三份 YAML 均为 `format=rgb_masks`，**不产出深度与 GT** |
| 信号链路 | `signal_evidence.sh`、`signal-evidence.jsonl` | RGB/BOP × SIGINT/SIGTERM → `exit 130/143`、`status=interrupted`、`requested 240`、`saved 2`、`skipped 238`、journal/manifest 各 2 行、引用文件 0 缺失 |
| BOP 真实资源 | `real-bop.yaml`、`real-bop.log`、`real-bop.verify.log` | 6/6 保存，`verify_bop` PASS |
| 大模型导出 | `n4_big_export.py`、`heavy-export.json` | 996,000 面 / 18.9 MB：2.79 s、390 次心跳；另测 327,680 面 1.04 s、1,310,720 面 3.63 s |
| 失败矩阵（真实） | `bop-broken.log`、`bop-postfail.log` | 预检缺 `bop_toolkit_lib` → 可读诊断 + 非零退出；三次人为注入均未制造自然后处理失败 |
| 最终回归 | 见 §2 命令 | 403 passed；五组产物检查器原始输出另存 |

### 4.2 N6 试生产统计（`pilot-stats.json`）

| 指标 | 值 |
|---|---|
| 帧数 / 场景 / 视图 | 240 / 60 / 4 |
| 实际产物 | `rgb`、`mask`、`mask_visib` 共 720 张 PNG；位姿在 `frames.jsonl` 的 `candidate.camera_pose_world_from_camera`。**该格式不输出深度图**，也没有 BOP 的 `scene_gt*` |
| 机位半径 | 0.291–0.390 m（中位 0.341） |
| 方位 / 仰角 | 1.3°–359.2° / 10.2°–64.9° |
| 遮挡比 | 0.000–0.493（中位 0.264） |
| 目标像素占比 | 4.5%–16.4%（中位 8.5%） |
| 接受率 | 240/977 = **24.6%**（拒绝：occlusion 396、invisible_target 341） |
| 耗时 / 显存 | 约 1.8 s/帧，峰值 2.94 GB |

---

## 5. 对 `report.md` 旧记录的更正

| 旧记录 | 实际情况 |
|---|---|
| `1180434` 被记为"遮挡规则" | 实为**预览视图可达性**修复 |
| `b678994` 被记为"沿相机段放遮挡物" | 实为**共享遮挡规则**；`afb57bb` 才是沿相机段放置 |
| `gui-manifest.json` 中"0.05 s、1 次心跳" | 与 487k 面/1.5 s/95 次心跳的描述不一致，该组证据作废；真实测量见 `heavy-export.json` |
| `*.check.log` 被当作检查器输出 | 那是离线预检日志；本轮另存 `*.verify.log` |
| 章节日期为 2026-10-22，产物时间为 2026-10-18 | 已按实际 `status.json` 时间标注，避免混用旧 runtime |

---

## 5.1 证据与格式对照（更正后）

“检查器 PASS”只说明通过当前实现的检查，不代表每像素严格正确；格式不同的产物必须用对应检查器，不能互相代替。

| 产物 | 格式 | 有深度？ | 有 `scene_gt*`？ | 正确检查器 | 结果 | 原始日志 |
|---|---|---|---|---|---|---|
| `layout-out/suggested` 4 帧 | `rgb_masks` | 否 | 否 | `verify_rgb_masks` | PASS | `layout-rgb.verify.log` |
| `occlusion-out/near-500mm` 6 帧 | `rgb_masks` | 否 | 否 | `verify_rgb_masks` | PASS | `near-500mm.verify.log` |
| `occlusion-out/far-5m` 6 帧 | `rgb_masks` | 否 | 否 | `verify_rgb_masks` | PASS | `far-5m.verify.log` |
| `occlusion-out/millimetre` 4 帧 | `rgb_masks` | 否 | 否 | `verify_rgb_masks` | PASS | `millimetre.verify.log` |
| `real-out/real-pilot` 240 帧 | `rgb_masks` | **否** | 否 | `verify_rgb_masks` | PASS | `real-pilot-240.verify.log` |
| `real-out/real-bop` 6 帧 | `bop` | 是 | 是 | `verify_bop` | PASS | `real-bop-6.verify.log` |
| `regress-out/regress-rgb` 4 帧 | `rgb_masks` | 否 | 否 | `verify_rgb_masks` | PASS | `regress-rgb.verify.log` |
| `regress-out/regress-bop` 4 帧 | `bop` | 是 | 是 | `verify_bop` | PASS | `regress-bop.verify.log` |

**BOP 后处理失败链路**（原“未完成”，现已有证据）：`.artifacts/next-20261023/gtfail/summary.json`——任务副本中真实 GT worker 注入 `RuntimeError`，结果 `exit 1` / `status=failed` / `requested 6` / `written 6` / `saved 0` / `skipped 6` / journal 0 行 / manifest `final=false` / 12 秒结束。注入只改任务副本的 `render_scene.py`，未改源码或系统环境。

## 5.2 对 `report-review.md` R1–R4 的修复

| 项 | 处理 | 反例与验收 |
|---|---|---|
| R1 取景包围盒只取偏移中点 | 已修：`src/bproc_core/assets.py::content_bounds` 对每个目标用 `offset.minimum + extent_low` / `offset.maximum + extent_high`；锚点仍固定在原点 | 新测试 `test_content_bounds_cover_the_whole_offset_range`：偏移 X `[.2,2]` 时上界 2.05（原 1.15）；`test_content_bounds_measures_centroids_and_pins_the_render_centre` 不变 |
| R2 无 `final=true` 的 manifest 绕过校验 | 部分修复：版本、`frames` 类型、`final` 类型现在对**每个**快照都校验，只有逐行比较在运行中豁免 | 新测试 `test_reader_rejects_a_tampered_running_manifest`（version/frames/final 字符串三例）。**残留**：检查器尚未断言 `status=completed` 必须对应 `final=true` |
| R3 所谓 1% 边缘容差允许内部错误 | 已重写：内部像素越界立即报错；边缘像素共享 1% 预算且双向合计；有效深度 < 20 时预算为 0；容差上限为 `max(1 个深度步长, 模型深度跨度 1%)` | 新测试：`test_bop_checker_tolerates_a_small_silhouette_excursion`、`..._rejects_an_interior_depth_outlier`、`..._counts_both_directions_against_one_budget`、`..._does_not_exempt_a_single_observed_depth`；真实 `real-bop` 仍 PASS |
| R4 journal 写失败仍计入 saved | 已修：`src/bproc_runtime/run.py` 新增 `committed` 计数，`record_frame` 成功后才递增；RGB 用 `saved=committed`，BOP 在 finalize 成功后用 `saved=committed`，失败为 0 | 新测试 `test_frame_metadata_failure_is_not_counted_as_saved`：`failed / written 1 / saved 0` |

## 5.3 `report-review.md` 证据范围更正的落实情况

| 复核意见 | 处理 |
|---|---|
| 1 近/远/毫米专项写成 BOP | 已改为 `verify_rgb_masks`，并补 §5.1 格式对照 |
| 2 近远用例是等比例缩放 | **未做**：仍是 .1 m@.5 m、1 m@5 m、1 mm@5 mm；同物体双距离用例待补 |
| 3 信号脚本缺文件检查为空、SIGINT 被覆盖 | **未做**：脚本仍先删同名目录；需四组独立目录重跑 |
| 4 240 帧无深度图 | 已更正为实际产物（720 张 PNG，无深度） |
| 5 失败日志索引不准 | 已改为指向 `gtfail/`，并把三次无效注入标为作废 |
| 6 `*.verify.log` 不齐 | 已按用例生成 8 份原始检查器日志（见 §5.1） |
| 7 版本/日期/API 名对齐 | 已把 HEAD 表述改为“`4f75430` 之后的复核修复提交”，未再声称用户确认过容差 |

---

## 6. 未完成 / 风险

已关闭：BOP 后处理失败的真实链路（证据见 §5.1 `gtfail/`）。

1. **R2 残留**：reader 已校验每个快照的版本、`frames` 类型与 `final` 类型，但检查器尚未断言 `status=completed` 必须对应 `final=true` 的最终快照。
2. **接受率 24.6% 偏低**。240 帧需要 977 次尝试（每帧约 4 次）：遮挡 396、目标不可见 341。需先按 scene/view/角度拆分拒绝原因，再决定是否调整提案分布；调整前不宜扩大规模。
3. **train/val 未切分**。60 个 scene 具备切分条件，但尚未实际划分。
4. **检查器容差已重写**（R3）：内部像素越界立即报错；只有轮廓像素共享 1% 预算、双向合计，有效深度 < 20 时无预算。真实 `real-bop` 仍 PASS，仍需下游确认这一轮廓容差符合预期。
5. **专项覆盖仍缺**：同物体 `.5 m`/`5 m` 双机位用例、毫米尺度 BOP、四种信号的独立目录重跑（现脚本会先删同名目录，SIGINT 证据被 SIGTERM 覆盖）。
6. **HDRI 可选**已按用户要求实现（未选时中性灰 0.05 世界背景，`world_metadata` 记录 `neutral`）。

---

## 7. 结论与下一步

- 审计任务 1–12、复核任务 N1–N6 全部落实；独立复核提出的 R1、R3、R4 已修复并通过反例测试，R2 修掉主要绕过路径但检查器断言仍缺；证据范围错误已按 §5.3 逐条更正。
- **仍不宜冻结**。下一轮顺序：
  1. 补检查器对 `completed ⇒ final=true` 的断言，补 R2 的 checker 级反例；
  2. 补同物体近/远双机位与毫米尺度 BOP，核对单位、深度精度和原生 visibility 容差；
  3. 四种信号用独立新任务/目录重跑，修正引用字段推导，逐帧核对 RGB/双 mask/GT；
  4. 按 scene 切分 train/val 并出分布报告，再决定是否扩到 2000+ 帧；
  5. 全量回归 + 已有产物检查器复跑作为冻结证据。
- 冻结前的门槛与 `report.md` 一致：N1–N4 回归通过、N5 专项证据完整、小批量标签语义与采样分布经人工确认。
