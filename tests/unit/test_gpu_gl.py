from types import SimpleNamespace
import sys

import pytest

from bproc_runtime import gpu_gl


def fake_context(monkeypatch, *, vendor=b"NVIDIA Corporation", renderer=b"NVIDIA GeForce RTX 3060 Ti", query_error=None):
    calls = []
    values = {1: vendor, 2: renderer, 3: b"4.1.0 NVIDIA 580.178.04"}

    def query(token):
        if query_error is not None:
            raise query_error
        return values[token]

    gl = SimpleNamespace(GL_VENDOR=1, GL_RENDERER=2, GL_VERSION=3, glGetString=query)
    monkeypatch.setitem(sys.modules, "OpenGL", SimpleNamespace(GL=gl))
    platform = SimpleNamespace(make_current=lambda: calls.append("current"),
                               make_uncurrent=lambda: calls.append("uncurrent"))
    return SimpleNamespace(_platform=platform), calls


@pytest.mark.parametrize("vendor, renderer", [
    (b"NVIDIA Corporation", b"NVIDIA GeForce RTX 3060 Ti/PCIe/SSE2"),
    (b"Mesa", b"AMD Radeon RX 6800 (radeonsi, LLVM 15.0.7)"),
    (b"Intel", b"Mesa Intel(R) UHD Graphics 770 (ADL-S GT1)"),
])
def test_hardware_opengl_accepts_real_devices_including_mesa(monkeypatch, vendor, renderer):
    context, calls = fake_context(monkeypatch, vendor=vendor, renderer=renderer)
    identity = gpu_gl.require_hardware_opengl(context)
    assert identity["vendor"] == vendor.decode()
    assert identity["renderer"] == renderer.decode()
    assert calls == ["current", "uncurrent"]


@pytest.mark.parametrize("renderer", [
    b"llvmpipe (LLVM 15.0.7, 256 bits)", b"softpipe", b"swrast",
    b"Software Rasterizer", b"software-rasterizer", b"software_rasterizer",
    b"Google SwiftShader", b"GDI Generic", b"lavapipe",
])
def test_software_opengl_is_rejected_and_context_released(monkeypatch, renderer):
    context, calls = fake_context(monkeypatch, renderer=renderer)
    with pytest.raises(RuntimeError, match="software renderer"):
        gpu_gl.require_hardware_opengl(context)
    assert calls == ["current", "uncurrent"]


@pytest.mark.parametrize("vendor, renderer", [(None, b"NVIDIA"), (b"NVIDIA", None), (b"", b"NVIDIA")])
def test_missing_opengl_identity_fails_closed(monkeypatch, vendor, renderer):
    context, calls = fake_context(monkeypatch, vendor=vendor, renderer=renderer)
    with pytest.raises(RuntimeError, match="identity"):
        gpu_gl.require_hardware_opengl(context)
    assert calls == ["current", "uncurrent"]


def test_opengl_query_error_releases_context(monkeypatch):
    context, calls = fake_context(monkeypatch, query_error=RuntimeError("driver query failed"))
    with pytest.raises(RuntimeError, match="driver query failed"):
        gpu_gl.require_hardware_opengl(context)
    assert calls == ["current", "uncurrent"]


def test_missing_platform_is_not_assumed_to_be_hardware():
    with pytest.raises(RuntimeError, match="context"):
        gpu_gl.require_hardware_opengl(SimpleNamespace())


def test_bop_checks_both_initialized_contexts(monkeypatch, capsys):
    normal, enlarged = object(), object()
    utility = SimpleNamespace(_pyrender_init=SimpleNamespace(__globals__={
        "renderer": normal, "renderer_large": enlarged}))
    checked = []

    def verify(renderer):
        checked.append(renderer)
        return {"vendor": "NVIDIA Corporation", "renderer": "RTX 3060 Ti", "version": "4.1"}

    monkeypatch.setattr(gpu_gl, "require_hardware_opengl", verify)
    gpu_gl.require_bop_hardware(utility)
    assert checked == [normal, enlarged]
    assert "RTX 3060 Ti" in capsys.readouterr().out


def test_bop_missing_native_renderer_fails_closed():
    utility = SimpleNamespace(_pyrender_init=SimpleNamespace(__globals__={}))
    with pytest.raises(RuntimeError, match="initialized"):
        gpu_gl.require_bop_hardware(utility)
