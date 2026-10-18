# report-pi.md 独立复核

复核对象：[report-pi.md](report-pi.md)。代码基线：`95c8b6285a5521b099da5ffd79e4d252b8d3ef9f`；交付所称业务验收版本为 `4f75430`，后续两次提交只涉及文档。

**结论：交付的主要进展属实，原先四个具体反例已有修复；完整验收仍未通过。发现四个可复现的代码/检查器缺口，以及若干证据范围描述错误，需关闭后再冻结版本。**

本次未修改业务代码、原始资源或既有数据集，未新跑 GPU 渲染。只进行了代码核对、unit/Qt 回归、已有产物检查，以及临时夹具上的故障注入。新增证据：[summary.json](.artifacts/pi-review-95c8b62-esjtugqi/summary.json)。

## 已独立确认的结果

| 检查 | 结果与范围 |
|---|---|
| 当前 unit/Qt | **403 passed，12.87 秒** |
| 原 N1 CAD 原点反例 | 中心为 0 与 .11m 的同尺寸模型已有修复和测试；原点平移不变性测试通过 |
| 原 N2 尺度间隙反例 | 已改为相对间隙和沿视线投影，已有缩放及角点测试通过 |
| 原 N3 最终 manifest 失败反例 | 真实 JsonRunRecorder 的返回值/落盘失败状态一致性测试通过 |
| 原 N4 同帧号内容篡改 | final=True 的整行内容比较、未知版本检查已有修复；另有绕过条件，见 R2 |
| 已有 RGB 产物 | real-pilot、layout、near-500mm、far-5m、millimetre，当前检查器均 PASS |
| 已有真实 BOP 产物 | `.artifacts/next-20261023/real-out/real-bop`，当前 BOP 检查器 PASS |
| 240 帧试生产 | 60 scene × 4 view；737 次拒绝（396 occlusion、341 invisible_target），共 977 次尝试，接受率约 24.6% |
| 运行耗时/显存 | status 区间约 424 秒，含启动的记录约 427 秒，约 1.8 秒/保存帧；显存日志峰值 2940 MiB |
| 独立导出运行代码 | 关键任务的 bproc_core / bproc_runtime 与当前源码逐字节一致，未发现旧 runtime 冒充新修复 |
| 大模型后台导出 | 留有 996,000 面、18,924,245 字节、2.79 秒、390 次心跳记录及实际导出目录 |

“检查器 PASS”只表示通过当前已实现的检查。R3 的反例说明其中仍有漏检，不能据此声称所有像素均经严格验证；也不能反过来据此断言已生成的真实 BOP 数据错误。

## 冻结前应修复的发现

### R1 / P2：取景包围盒只计算位置范围的中点

位置：`src/bproc_core/assets.py:289–298`；调用者包含 `suggest_layout()` 与 `src/bproc_ui/panels/sampling.py`。

`content_bounds()` 声明覆盖全部可采样位置，但对非中心目标只取 offset 各轴中点。CAD 原点语义的修复是正确的；遗漏区间宽度是另一处边界问题。

**独立复现：** 两个边长 .1m 的立方体，a 为中心，b 的 X 中心范围为 `[.2,2]`，Y/Z 固定 0。函数返回联合 X 上界 **1.15m**，应为 **2.05m**。用生产 `sample_scene()` 抽取 250 个场景，有 **133 个**实际目标边界超出报告的取景包围盒。此结果证明覆盖计算错误，不等于已渲染 133 张错误图片。

**修复与验收：**

- [ ] 非中心目标下界使用 `offset.minimum + centered_bounds[0]`，上界使用 `offset.maximum + centered_bounds[1]`；中心目标仍固定在 0，原始 CAD 顶点不变。
- [ ] 在 `tests/unit/test_layout_suggestions.py` / `test_assets.py` 补 X/Y/Z 区间端点、宽范围、任意中心和 CAD 平移测试。
- [ ] 断言所有合法区间端点的放置包围盒都包含在 content_bounds 内，并保持原点平移不变性；重新检查自动适配距离，不能通过只检查中点消除测试失败。

### R2 / P2：非布尔 final 或运行中标记可绕过 manifest 校验

位置：`src/bproc_runtime/metadata.py:52–62`。

有 journal 时，manifest 的版本和整行一致性只在 `manifest.get('final') is True` 才检查。运行中的合法空快照需要豁免最终内容比较，但不能一并豁免文档版本和字段类型。

**独立复现：** 存在合法 journal，manifest 写 `schema_version=999`、`frames='invalid'`，并分别将 final 设为 False、字符串 `'true'`、None，三种情况 reader 均接受。`report-pi.md` 所称“校验 schema 版本与字段类型”覆盖不完整。

**修复与验收：**

- [ ] 有/无 journal 均先验证 manifest 版本、frames 类型和 final 类型，再区分运行中与最终快照。
- [ ] 正常 schema 2 的 `final=False, frames=[]` 在运行中仍可读取 journal；未知版本、非布尔 final、缺少新格式必要字段必须报 MetadataError。
- [ ] 完整产物检查增加 `status=completed` 必须对应合法最终快照的断言，不能把 completed + final=False 当作运行中豁免。
- [ ] 在 `tests/unit/test_metadata_journal.py`、`test_output_checkers.py` 增加上述反例，保留旧无版本 manifest 的明确定义兼容路径。

### R3 / P2：所谓 1% 边缘容差允许内部严重深度错误通过

位置：`tests/blender/check_bop.py:265–279`；引入于 `4f75430`。

当前例外不检查像素是否位于轮廓边缘，也不限制深度偏差幅度；below/above 各有一份预算，`max(1,...)` 还使小样本的最低容差超过 1%。它检查的是模型深度范围，不能简单描述成“掩码边缘有 1% 像素越界”。

使用项目现有 `valid_bop_output` 合法夹具，各复制一份后独立污染，当前 `verify_bop()` 都返回空错误列表：

| 反例 | 污染内容 | 当前结果 |
|---|---|---|
| 内部极值 | 25 个可见像素、24 个正深度中，内部一点由约 .6m 改成 6.5535m | 仍 PASS |
| 双向配额 | 一个内部像素为 6.5535m，另一个为 .0001m；合计 2/24≈8.33% 越界 | 仍 PASS |
| 唯一观测值 | 只保留 1 个正深度并改错，同时将合法的 px_count_valid 更新为 1 | 唯一深度错误仍 PASS |

原始真实 real-bop 六帧按既有模型界限±1mm 重算，未出现这类越界。这些反例证明检查器漏检，**没有证明真实渲染深度错误**。

证据：[depth/results.json](.artifacts/pi-review-95c8b62-esjtugqi/depth/results.json)。复现脚本：[reproduce_depth.py](.artifacts/pi-review-95c8b62-esjtugqi/reproduce_depth.py)；脚本会创建 depth 子目录，应复制到新目录后重跑，避免覆盖本次证据。

**修复与验收：**

- [ ] 将单位/数量级健全性检查与有依据的轮廓误差容许分开；内部像素的巨大偏差不能通过。
- [ ] 如保留边缘例外，明确边界像素位置与物理偏差上限，双向越界合并计数；小样本不能把全部观测值豁免。
- [ ] 增加内部极值、双向累计、小掩码唯一观测值错误的反例；保留原生合法零深度边缘及真实 BOP 正例。
- [ ] 把“这是原生语义修正”的判断限于有原生参考结果支持的情况，不能用未限定的百分比替代参考验证。

### R4 / P2：journal 提交失败后 BOP saved 计数仍可虚高

位置：`src/bproc_runtime/run.py:115–119,139–141`。

`writer.write()` 成功后先递增 written，随后 `record_frame()` 可能失败。BOP finalize 成功后又无条件 `saved=written`，使没有可追踪帧元数据的图像被计为已完整保存。该问题早于本轮改动，属于冻结前补充发现。

**独立故障注入：** fake backend/writer、真实 JsonRunRecorder，仅让 append_frame 抛 OSError。结果 `failed/1, requested=1, written=1, saved=1, skipped=0`，但 journal 与最终 manifest **均为 0 行**。没有虚报 completed，saved 计数与可追踪帧记录仍不一致。

**修复与验收：**

- [ ] 区分图像已写入、帧元数据已提交、BOP 标签已完成的计数；saved 只能包含同时具备可追踪记录与完整标签的帧。
- [ ] 在 `tests/unit/test_runtime_lifecycle.py` / `test_run.py` 加 journal append/write/fsync 失败用例，检查返回值、磁盘 status、journal/manifest 及实际文件；不得只检查退出码非零。
- [ ] 不删除既有已写图片来掩盖故障；失败运行可保留未提交文件，但需准确标记，不能计为完整可用帧。

## 交付文档与专项证据需要校正

以下是证据范围问题，不能直接当作产品功能失败。

1. **近/远/毫米专项实际为 RGB 双掩码。** 三份 YAML 均为 `format=rgb_masks`，`verify_occlusion.py` 调用 verify_rgb_masks。`report-pi.md:72` 的 `verify_bop PASS` 应更正。真实 BOP 六帧另有独立产物，不代表毫米 BOP 已通过。
2. **近远用例验证的是等比例缩放。** 当前为 .1m 模型@.5m、1m 模型@5m、1mm 模型@5mm。需另补“同一物体、同一配置中 r=.5m/5m 双机位或宽范围采样”的用例。
3. **信号脚本的缺文件检查为空检查。** `.artifacts/next-20261023/signal_evidence.sh:42` 查 rgb_path 等不存在的字段；实际用 relative_rgb，双 mask 按对象 ID 或 GT 索引推导。脚本还先删除同名输出，导致 SIGINT 产物被后续 SIGTERM 运行覆盖。四份日志/终态快照存在，当前保留的两组 TERM 文件实际存在；不能据空 missing 数组声称四组文件都逐项验收。
4. **240 帧试生产没有深度图。** 其 format=rgb_masks，目录共 720 张 RGB/full-mask/visible-mask PNG，位姿在 candidate 元数据中。`report-pi.md:83` 的“RGB+双掩码+深度+位姿”应改为实际输出。
5. **失败日志索引不准确。** bop-broken.log / bop-postfail.log 最终是 completed，退出 0；缺 toolkit 的预检诊断在 real-bop-preflight.log。真实 GT 后处理子进程失败的验收仍未完成，文档对“未完成”的披露是正确的。
6. **原始检查器日志不齐。** *.verify.log 只有 real-pilot 和 real-bop 两份，尺度专项结果在聚合 JSON。本次已经独立复查相应 RGB/BOP 产物通过，下次应按用例分别保存原始输出。
7. **版本/日期/API 名应与源码对齐。** 当前 HEAD 是 95c8b62；业务代码验收版本可另记 4f75430。报告中的 generate_views、reserve_interval/slice_size/required_visible、not_started 等描述与当前实现名称/状态不符。日期与实际 status 时间应按原始日志核对，不能把实现者的验收容差选择写成已由用户确认。

## 下一轮执行顺序与通过门槛

1. **先关闭 R1–R4。** 每项先加入明确反例，再修复；跑对应局部测试，最后全量 unit/Qt。同步修订 report-pi 的覆盖表述，不将“已编码”与“端到端通过”合并标记。
2. **补 GT 子进程真实失败链路。** 在全新导出的测试任务副本中，对实际后处理 worker 初始化/工作函数注入确定异常或受控退出；不要再依靠改外部 PYTHONPATH、删未被使用的文件来猜测。保存父子进程日志、PID、退出码、failed 状态及 written/committed/saved。验证无无限等待；故障只注入测试任务，不改系统环境或正式源码默认路径。
3. **补同物体近远机位与毫米 BOP。** 后者同时核对模型单位、深度精度和原生 visibility 容差；BOP 默认 .015m 容差与运行时精确像素遮挡率不是同一指标，不能因 RGB 遮挡样本通过就宣称 BOP 语义相同。
4. **重留四种信号的独立产物。** 每种 format×signal 使用不同的新任务/输出目录；修正引用字段与文件推导方式，逐帧检查 RGB/depth/GT/full/visible mask，不覆盖 SIGINT 证据。
5. **完成数据分布与 scene 划分。** 当前 24.6% 接受率本身不是功能失败，约 1.8 秒/保存帧也已量化；先按 scene/view/角度拆分拒绝原因，判断是否需要优化提案分布。train/val 按 scene 划分，抽查 RGB 与标签后，再决定是否扩大到 2000 帧。保持用户设置的遮挡验收范围。

可以保留当前 240 帧作为开发试生产结果；本轮复核没有宣布它损坏。冻结门槛是四项反例关闭、上述专项证据完整、检查器能拒绝新增坏数据且继续接受真实正确数据。
