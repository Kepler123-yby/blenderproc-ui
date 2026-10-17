# GPU 与表面处理

渲染默认使用 **OptiX GPU**，不提供 CPU 选项。程序在场景初始化和每个渲染 pass 前检查实际 Cycles 设备：关闭所有 CPU 和其他后端设备，要求当前后端至少有一张启用 GPU。没有兼容 GPU 或驱动时明确失败，不能悄悄改用 CPU。

本机 NVIDIA GeForce RTX 3060 Ti（8 GiB，驱动 580.178.04）已完成 OptiX 与 CUDA 实际渲染探测。其他 GPU 后端可配置，但需要对应硬件和驱动；没有在本机测试 AMD、Intel 或 Apple 设备。

## 界面设置

在“脚本”工作区调整：

| 选项 | 行为 |
|---|---|
| GPU 后端 | 默认 OPTIX；还支持 CUDA、HIP、ONEAPI、METAL |
| 平直着色 | 每个面独立法线，保留多面体外观 |
| 平滑着色 | 面之间插值法线，适合曲面网格 |
| 按角度平滑 | 默认，角度为 30°；平滑小夹角连接并保留明显硬边 |
| 加权法线 | 可选，结合面面积与夹角改善平面/倒角高光，并保留锐边；仅平滑区有效 |
| 自动 GPU 降噪 | OPTIX 后端且硬件支持时启用 OptiX；其他情况关闭 |
| OptiX 降噪 | 仅允许 OPTIX 后端，GPU 能力不足时报错 |
| 噪声阈值 | 默认 0.01；0 关闭自适应采样；采样数仍为上限 |

表面处理应用于目标与遮挡模型。不加入细分、倒角或顶点平滑等会改变几何的操作，保持 PLY 原点、轮廓、BOP 模型及位姿一致。视口显示几何构图，最终法线与光照以 Blender 结果为准。

Blender 4.2 的 CUDA 渲染搭配 OptiX 降噪即使设置 `denoising_use_gpu=True`，仍可能使用 CPU 降噪。本机调试日志实际复现了这一行为，所以程序禁止这个组合；需要 GPU 降噪时选择 OPTIX。

## 保存与兼容

项目及导出任务的 `scene.yaml` 保存全部设置：

```yaml
render:
  samples: 50
  device: OPTIX
  shading_mode: auto
  smooth_angle_deg: 30.0
  weighted_normals: false
  denoiser: auto
  noise_threshold: 0.01
```

旧项目的 `device: CPU` 在载入时迁移为 OPTIX；只读载入不会改写源 YAML。新字段缺失时使用上述默认值。旧任务自带旧版运行时，因此需从新版 UI **重新导出到新目录**。原有结果保持不变。

## 掩码、深度与 BOP

RGB 使用原生 Cycles GPU 降噪；实例分割渲染临时关闭降噪，执行后恢复。深度不经过颜色降噪。

### BOP EGL 校验

BOP 的完整与可见掩码由独立的 EGL/OpenGL 后处理生成，与 Cycles 设备是两套上下文。原生 `BopWriterUtility._pyrender_init` 初始化 `renderer`（掩码尺寸）和 `renderer_large`（放大可见性检测）两个上下文后，`bproc_runtime/gpu_gl.py` 依次 `make_current`、读取 `GL_VENDOR` / `GL_RENDERER` / `GL_VERSION`、`make_uncurrent`，两个上下文都必须通过。

- 拒绝：`llvmpipe`、`softpipe`、`swrast`、`lavapipe`、`swiftshader`、`software rasterizer`、`software renderer`、`GDI Generic`。匹配前把大小写、空格、下划线和连字符归一化。
- 放行：硬件 Mesa 实现照常通过，厂商名本身不作为软件渲染的证据。
- 读不到厂商或渲染器字符串、上下文未初始化、缺少任一渲染器，都直接失败，不假定 GPU 可用。

终端日志会打印两行 `BOP EGL renderer: …` 和 `BOP EGL renderer_large: …`，出问题时先看这两行的厂商与渲染器。

GPU 渲染仍需要 CPU 执行 Python、场景准备与文件读写，这些不是 CPU 路径追踪或 CPU 降噪。终端日志可能提到加载 CPU 指令内核，实际设备以启用的 Cycles GPU 和路径追踪设备为准。

验证记录、实际导出任务和截图位于本机 `.artifacts/gpu-*`；正式验收结果见 [验证记录](validation.md)。
