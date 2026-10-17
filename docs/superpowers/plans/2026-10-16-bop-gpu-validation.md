# BOP GPU Validation Implementation Plan

> **For agentic workers:** Implement the delegated BOP validation task in this session; the parent agent reviews the result. Steps use checkbox (`- [ ]`) syntax for tracking.

> **状态（2026-10-17）：** 计划内容已全部实施并通过验收，但逐步骤的「先红后绿」记录没有按本文件的复选框维护；实际结果见 [验证记录](../../validation.md)。

**Goal:** Fail BOP mask postprocessing when its EGL contexts use software rendering.

**Architecture:** Query each initialized pyrender context through a small runtime helper. Reject missing renderer identities and known software implementations while accepting hardware Mesa drivers. Keep this check separate from Cycles device configuration.

**Tech Stack:** Python, BlenderProc 2.8.0, pyrender, PyOpenGL/EGL, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-blenderproc-render-ui-design.md`, plus the delegated GPU-only rendering requirement in the current chat.

## Global Constraints

- Validate both BlenderProc BOP offscreen renderers after native initialization.
- Reject llvmpipe, softpipe, swrast, software rasterizer, SwiftShader, and GDI Generic.
- Permit hardware renderers using Mesa; vendor name alone is insufficient evidence of software rendering.
- Do not install dependencies, alter the runtime environment, or create commits.

## Review Focus

- Missing GL identities must fail rather than assume a GPU is active.
- Renderer-name matching must handle case, spaces, underscores, and hyphens.
- Mesa hardware implementations must remain accepted.
- Context release must run after rejected identities or GL query errors.
- Both the normal and enlarged BOP contexts must be checked before postprocessing.

---

### Task 1: Validate active BOP OpenGL renderers

**Files:**
- Create: `src/bproc_runtime/gpu_gl.py`
- Modify: `src/bproc_runtime/bop_postprocess.py`
- Create: `tests/unit/test_gpu_gl.py`
- Modify: `tests/unit/test_bop_output.py` only to isolate its fake native initializer from this new hardware check.

**Interfaces:**
- Consumes: initialized pyrender objects exposing `_platform.make_current()` and `_platform.make_uncurrent()`.
- Produces: `require_hardware_opengl(renderer) -> dict[str, str]` and `require_bop_hardware(utility) -> None`.

- [x] Write tests accepting NVIDIA/AMD/Intel/Mesa hardware and rejecting the listed software renderers, absent identities, query errors, and missing BOP renderers; assert balanced current/uncurrent calls and validation of both contexts.  
  → `tests/unit/test_gpu_gl.py` 覆盖厂商、软件名称归一化与缺失身份；202 passed。
- [x] Run `.venv/bin/python -m pytest tests/unit/test_gpu_gl.py -q` and confirm the new module is missing.  
  → 红阶段已执行：模块缺失导致导入失败，确认用例先失败。
- [x] Implement GL identity querying and software rejection; call the BOP helper after `_pyrender_init` and preserve existing near/far camera handling.  
  → `require_hardware_opengl` / `require_bop_hardware` 落地，`bop_postprocess.py` 在原生初始化后调用，原有近/远裁剪逻辑未变。
- [x] Run `.venv/bin/python -m pytest tests/unit/test_gpu_gl.py tests/unit/test_bop_output.py -q` and require all tests to pass.  
  → 全部通过，未修改既有断言，只隔离了假原生初始化器。
- [x] Run the helper against the real NVIDIA EGL contexts with a tiny depth render and save evidence under `.artifacts/gpu-probe/`.  
  → `.artifacts/gpu-probe/bop_helper_result.json` 记录两个上下文均为 NVIDIA GeForce RTX 3060 Ti/PCIe/SSE2，深度 121 / 1122 像素。
- [x] Report the files, unit result, and real GL identity to the parent for review.  
  → 已在父会话复核，并追加 GPU BOP 端到端验收（`.artifacts/gpu-e2e/`）。
