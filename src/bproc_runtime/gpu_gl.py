"""Verify the OpenGL devices used by BOP's independent EGL renderers."""
from __future__ import annotations

import re


_SOFTWARE_RENDERERS = (
    "llvmpipe", "softpipe", "swrast", "lavapipe", "swiftshader",
    "software rasterizer", "software renderer", "gdi generic",
)


def require_hardware_opengl(renderer) -> dict[str, str]:
    """Read a pyrender context's identity and reject software rasterization."""
    platform = getattr(renderer, "_platform", None)
    if platform is None:
        raise RuntimeError("BOP GPU verification requires an initialized OpenGL context")
    platform.make_current()
    try:
        from OpenGL import GL

        identity = {}
        for key, token in (("vendor", GL.GL_VENDOR), ("renderer", GL.GL_RENDERER), ("version", GL.GL_VERSION)):
            value = GL.glGetString(token)
            identity[key] = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        if not identity["vendor"].strip() or not identity["renderer"].strip():
            raise RuntimeError("BOP GPU verification could not read the OpenGL vendor/renderer identity")
        description = re.sub(r"[\s_-]+", " ", f"{identity['vendor']} {identity['renderer']}".casefold())
        if any(name in description for name in _SOFTWARE_RENDERERS):
            raise RuntimeError(
                f"BOP requires a GPU, but EGL selected a software renderer: "
                f"{identity['vendor']} / {identity['renderer']}")
        return identity
    finally:
        platform.make_uncurrent()


def require_bop_hardware(utility) -> None:
    """Check both contexts created by BlenderProc 2.8's native initializer."""
    namespace = getattr(getattr(utility, "_pyrender_init", None), "__globals__", {})
    names = ("renderer", "renderer_large")
    if any(namespace.get(name) is None for name in names):
        raise RuntimeError("BOP GPU verification requires both native renderers to be initialized")
    identities = [require_hardware_opengl(namespace[name]) for name in names]
    for name, identity in zip(names, identities):
        print(f"BOP EGL {name}: {identity['vendor']} / {identity['renderer']} / {identity['version']}", flush=True)
