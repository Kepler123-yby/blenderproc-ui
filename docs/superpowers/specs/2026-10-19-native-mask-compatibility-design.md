# 原生实例掩码兼容性设计

## 目标与依据

依据用户指定的 `/home/kepler/bproc_project/dataset_rendering/`，验证现有 BlenderProc 实例分割到逐目标双掩码的原生路径，而不是新增一种“32 位整数 PNG”。基线 `c6ee5c6`。设计来自 `full-cycle` 的 architect 阶段；完整阶段输出保存在本机 `.pi/flow-workflows/4ac73551a66acf08.json`，本文件保留实施契约。

参考 `render.py:97–108` 使用 `render()`、`write_bop()`。参考转换器仅复制重命名，按帧键对齐的单目标逻辑不能直接用于多目标。**不运行参考渲染脚本**：它会删除旧输出。

## 已支持的路径与本轮变化

- 原生 `SegMapRendererUtility.py` 按实例数量选择 `uint8/uint16/uint32`；项目 `decode_instances` 直接比较整数 `idx`，OR 合并到稳定 `obj_id`，不经过浮点或缩窄转换。
- RGB 使用原生 `render_segmap` 和项目既有逐目标 writer；BOP 使用原生 `write_bop` 及 `calc_gt_masks/calc_gt_info`。保留 `calc_mask_info_coco=False`，避免自动安装依赖。
- 逐目标 `mask/mask_visib` 是单通道 **8 位、0/255 PNG**。RGB 后缀是 `obj_id`；BOP 后缀是 GT 列表索引。BOP 深度仍为单通道 16 位 PNG，恢复为米的公式是 `pixel × depth_scale_mm / 1000`。
- `uint32` 中间 ID、RGBA 四通道各 8 位的总 32 位、逐目标二值图位深是不同概念；PNG 不支持单通道 32 位整数格式。
- 本轮不改生产解码、backend、writer 或采样行为。只加强 checker 的实际 PNG/L/uint8 检查，补回归测试、只读审计及文档纠错。

## 范围与约束

只使用现有离线 GPU 环境，不安装依赖、不调用 `setup_pip`、不修改 Blender/conda/native 包。不覆盖参考资源、历史任务或数据。所有新证据写入唯一新目录。只做英文 conventional 本地提交，不 push、不改写历史。

不增加输出格式、UI 位深开关、ID 合成图或 BOP 强制转换流程；不扩展到 LOD、分块、自动重取景或生产 Haven。

## 验收契约

1. **AC-1 参考只读审计：** manifest 明列的 5 个源码和 8 张图片 SHA256 均匹配基线；图片实际为 PNG IHDR 8/0、L、uint8、640×480、0/255。禁止递归扫描参考数据树。
2. **AC-2 整数保真：** 直接解码与真实 backend 调用路径覆盖 uint8/uint16/uint32；区分 65535/65536、16777216/16777217、4294967294/4294967295。覆盖背景、遮挡物、未知目标、非连续目标 ID、合并、重复映射、顺序无关和无像素目标。
3. **AC-3 RGB 契约：** uint32 解码经实际 writer 后，文件后缀仍为稳定 obj_id；二值 PNG 编码及像素正确，visible 为 full 子集；空目标允许、空 anchor/错误维度/非 bool/错误形状/visible 越界仍拒绝。
4. **AC-4 BOP 委托：** 保留原生 writer/helpers、GT 顺序、毫米标注、深度 scale/容量/近远裁剪；不调用安装入口。
5. **AC-5 负例：** 两种 checker × 两个 mask 目录，拒绝 uint16、RGB、RGBA、非二值、错误尺寸、BMP 伪装 PNG；各 CLI 非零失败，正常数据退出 0。审计工具用临时夹具覆盖有效报告、哈希/格式/源码不符、坏元数据、拒绝覆盖已有报告。
6. **AC-6 新 GPU：** 本轮源码分别导出 RGB/BOP 新任务，目标 2/7，各完成至少 2 帧；预检/渲染退出 0，status/journal/final manifest 一致。OPTIX RTX3060Ti、CPU 禁用，BOP EGL 为 NVIDIA。
7. **AC-7 产物审计：** 重跑真实 checker；核对 PNG IHDR、逐帧命名/GT 映射、BOP 16 位深度及 scale、两个源码包和入口模板逐文件哈希。独立 reviewer 不需要重复渲染 GPU。
8. **AC-8 回归：** 全量 unit/Qt 退出 0；不删除、跳过或放宽既有采样、1e-4 固定窗口、预览、分组及中断测试。
9. **AC-9 文档：** 明确已有支持与新增校验，纠正“32 位 PNG 待做”，保存真实命令、退出码、测试数和证据路径；大 idx 只是合成边界测试，不冒充百万实例实渲染。
10. **AC-10 交付：** 只提交任务文件；`git diff --check` 通过，tracked 干净，报告/产物继续忽略；无 push。

## 风险与边界

收紧后的 checker 会拒绝原先误放行的 uint16 或伪 PNG；不迁移旧数据。RGB 精确实例分割与 BOP 深度容差可见性不要求逐像素相同。参考仅抽查 8 张，真实 GPU 仅两格式各 2 帧，不能推导任意实例数量的性能或出图率。

## 证据目录

- 最初只读 manifest：`.artifacts/review12-native-masks/reference-audit.json`。
- flow 的 checker RED/GREEN：`.artifacts/review12-native-masks/run-20261019-native-mask/logs/`。
- 补全、新 GPU 与终验：`.artifacts/review12-native-masks/completion-20261019/`。
