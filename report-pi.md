# blenderproc-ui 交付报告（接手执行）

**接手基线**：`aebd537`（26 个分阶段提交，268 passed）
**当前 HEAD**：`4f75430`（在基线之上新增 17 个提交，403 passed）
**执行者**：Pi 编码代理；设计决策与验收口径由用户确认（逐条记录在 `report.md` 的 N1–N6 与本文）
**日期**：2026-10-23

本文是"我接手后做了什么、证据在哪、哪些没做到"的交付记录，与 `report.md`（GPT 的审计与指示）配套阅读。凡未真实执行的项目都标为未完成，不以单测替代。

---

## 1. 交付范围

| 阶段 | 来源 | 提交区间 | 提交数 | 测试数 |
|---|---|---|---|---|
| 阶段一：审计任务 1–12 | `report.md` 前半（"执行清单"） | `aebd537..a86f999` | 14 | 268 → 388 |
| 阶段二：复核任务 N1–N6 | `report.md`"当前版本复核与下一步指示" | `a86f999..4f75430` | 3 | 388 → 403 |

全部已推送到 `origin/main`。

---

## 2. 复现环境与命令

| 项 | 值 |
|---|---|
| 单元测试 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p pytestqt.plugin -q` → **403 passed, 14 s** |
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
| 近/远/毫米机位 | `n5_occlusion.py`、`occlusion-summary.json` | 6/6、6/6、4/4，`verify_bop` PASS，遮挡比与角点深度逐帧留存 |
| 信号链路 | `signal_evidence.sh`、`signal-evidence.jsonl` | RGB/BOP × SIGINT/SIGTERM → `exit 130/143`、`status=interrupted`、`requested 240`、`saved 2`、`skipped 238`、journal/manifest 各 2 行、引用文件 0 缺失 |
| BOP 真实资源 | `real-bop.yaml`、`real-bop.log`、`real-bop.verify.log` | 6/6 保存，`verify_bop` PASS |
| 大模型导出 | `n4_big_export.py`、`heavy-export.json` | 996,000 面 / 18.9 MB：2.79 s、390 次心跳；另测 327,680 面 1.04 s、1,310,720 面 3.63 s |
| 失败矩阵（真实） | `bop-broken.log`、`bop-postfail.log` | 预检缺 `bop_toolkit_lib` → 可读诊断 + 非零退出；三次人为注入均未制造自然后处理失败 |
| 最终回归 | 见 §2 命令 | 403 passed；五组产物检查器原始输出另存 |

### 4.2 N6 试生产统计（`pilot-stats.json`）

| 指标 | 值 |
|---|---|
| 帧数 / 场景 / 视图 | 240 / 60 / 4（RGB + 双掩码 + 深度 + 位姿） |
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

## 6. 未完成 / 风险

1. **BOP 后处理失败的真实链路（未完成）**。三次注入都没有制造自然失败：坏掉的 `bop_toolkit_lib` 副本被 `run_job.py` 重设的 `PYTHONPATH` 绕过；破坏深度 PNG 不影响 BlenderProc 的 GT 计算；渲染中途删除 `models/*.ply` 仍 `completed`。目前只有单测 `test_bop_postprocess_failure_is_not_saved` 覆盖"不虚计 saved"。**不以单测声称通过。**
2. **接受率 24.6% 偏低**。240 帧需要 977 次尝试（每帧约 4 次）。需判断这是"遮挡窗口 + 目标全可见"联合约束的必然结果，还是采样策略需要调整；调整前不宜扩大规模。
3. **train/val 未切分**。60 个 scene 具备切分条件，但尚未实际划分。
4. **检查器容差已调整**。`check_bop.py` 允许可见掩码 1% 的像素越界（轮廓边缘），并有"整体偏移必须报错"的反例测试；这是修正误报，不是放宽标准，但需在下游确认可接受。
5. **HDRI 可选**已按用户要求实现（未选时中性灰 0.05 世界背景，`world_metadata` 记录 `neutral`），与早期"必须有环境图"的校验不同。

---

## 7. 结论与下一步

- 审计任务 1–12 与复核任务 N1–N4 全部落实，各有提交、单测与真实产物证据；N5 除"BOP 后处理失败"外齐备；N6 240 帧试生产完成。
- **不宜直接冻结版本**。下一轮顺序：
  1. 用可控方式让 GT 子进程真崩（例如给后处理一个必然失败的 toolkit 或注入异常），补齐失败链路证据；
  2. 分析接受率，决定是放宽窗口还是改进采样（先看拒绝分布，不改验收口径）；
  3. 按 scene 切分 train/val，输出分布报告；
  4. 再扩到 2000+ 帧并冻结。
- 冻结前的门槛与 `report.md` 一致：N1–N4 回归通过、N5 专项证据完整、小批量标签语义与采样分布经人工确认。
