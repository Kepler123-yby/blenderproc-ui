from types import SimpleNamespace

import pytest

from bproc_runtime.contracts import RenderError
from bproc_runtime.render_quality import (
    apply_object_shading, assert_gpu_only, configure_gpu,
    configure_rgb_quality, label_pass_without_denoising,
)


def device(kind, name=None):
    return SimpleNamespace(type=kind, name=name or kind, use=True)


def runtime(devices, available=None, fallback=False):
    preferences = SimpleNamespace(devices=devices, compute_device_type="NONE")
    preferences.get_devices_for_type = lambda kind: list(devices if available is None else available)
    preferences.has_optixdenoiser_gpu_devices = lambda: preferences.compute_device_type == "OPTIX"
    scene = SimpleNamespace(cycles=SimpleNamespace(device="CPU", use_denoising=True,
                                                   denoising_use_gpu=False))
    bpy = SimpleNamespace(context=SimpleNamespace(scene=scene,
        view_layer=SimpleNamespace(cycles=SimpleNamespace(use_denoising=True)),
        preferences=SimpleNamespace(addons={"cycles": SimpleNamespace(preferences=preferences)})))
    calls = []
    def select(**kwargs):
        calls.append(("devices", kwargs))
        scene.cycles.device = "CPU" if fallback else "GPU"
        for entry in devices:
            entry.use = True  # Mirror BProc's permissive mixed-device behavior.
    def denoise(value):
        calls.append(("denoiser", value))
        scene.cycles.use_denoising = value is not None
    api = SimpleNamespace(renderer=SimpleNamespace(set_render_devices=select,
        set_denoiser=denoise, set_noise_threshold=lambda value: calls.append(("noise", value))))
    return api, bpy, calls


def render_settings(**kwargs):
    values = dict(device="OPTIX", denoiser="auto", noise_threshold=.01,
                  shading_mode="auto", smooth_angle_deg=30., weighted_normals=False)
    return SimpleNamespace(**dict(values, **kwargs))


def test_cpu_is_disabled_even_when_blenderproc_enables_it():
    cpu, optix, cuda = device("CPU"), device("OPTIX", "RTX test"), device("CUDA")
    api, bpy, calls = runtime([cpu, optix, cuda], [cpu, optix])
    assert configure_gpu(api, bpy, "OPTIX") == ("RTX test",)
    assert [entry.use for entry in [cpu, optix, cuda]] == [False, True, False]
    assert bpy.context.scene.cycles.device == "GPU"
    assert calls[0] == ("devices", {"use_only_cpu": False, "desired_gpu_device_type": "OPTIX"})


@pytest.mark.parametrize("available", [[], [device("CPU")]])
def test_native_cpu_fallback_is_an_error(available):
    cpu = device("CPU")
    api, bpy, _ = runtime([cpu], available, fallback=True)
    with pytest.raises(RenderError, match="CPU fallback is disabled"):
        configure_gpu(api, bpy, "OPTIX")
    assert not cpu.use
    assert bpy.context.scene.cycles.device == "GPU"


def test_direct_cpu_request_is_rejected_before_selection():
    api, bpy, calls = runtime([device("CPU")])
    with pytest.raises(RenderError, match="GPU rendering requires"):
        configure_gpu(api, bpy, "CPU")
    assert not calls


def test_missing_driver_does_not_leave_cpu_enabled():
    cpu = device("CPU")
    api, bpy, _ = runtime([cpu])
    api.renderer.set_render_devices = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("driver missing"))
    with pytest.raises(RenderError, match="driver missing"):
        configure_gpu(api, bpy, "CUDA")
    assert not cpu.use


@pytest.mark.parametrize("backend", ["CUDA", "HIP", "ONEAPI", "METAL"])
def test_requested_gpu_backend_is_used_exclusively(backend):
    cpu, gpu = device("CPU"), device(backend)
    api, bpy, _ = runtime([cpu, gpu])
    configure_gpu(api, bpy, backend)
    assert_gpu_only(bpy, backend)
    assert not cpu.use and gpu.use


def test_guard_catches_cpu_reenabled_between_render_passes():
    cpu, gpu = device("CPU"), device("OPTIX")
    api, bpy, _ = runtime([cpu, gpu])
    configure_gpu(api, bpy, "OPTIX")
    cpu.use = True
    with pytest.raises(RenderError, match="exclusively"):
        assert_gpu_only(bpy, "OPTIX")


@pytest.mark.parametrize("backend, expected", [("OPTIX", "OPTIX"), ("CUDA", None),
                                               ("HIP", None), ("ONEAPI", None), ("METAL", None)])
def test_auto_denoising_remains_on_gpu(backend, expected):
    api, bpy, calls = runtime([])
    bpy.context.preferences.addons["cycles"].preferences.compute_device_type = backend
    assert configure_rgb_quality(api, bpy, render_settings(device=backend)) == expected
    assert calls == [("denoiser", expected), ("noise", .01)]
    assert bpy.context.scene.cycles.denoising_use_gpu == (expected == "OPTIX")


def test_other_gpu_backends_cannot_request_optix_denoiser():
    api, bpy, _ = runtime([])
    with pytest.raises(RenderError, match="requires the OPTIX"):
        configure_rgb_quality(api, bpy, render_settings(device="HIP", denoiser="OPTIX"))


def test_explicit_optix_rejects_unavailable_gpu_denoiser():
    api, bpy, calls = runtime([])
    bpy.context.preferences.addons["cycles"].preferences.compute_device_type = "CUDA"
    with pytest.raises(RenderError, match="requires the OPTIX render backend"):
        configure_rgb_quality(api, bpy, render_settings(device="CUDA", denoiser="OPTIX"))
    assert not calls


def test_cuda_auto_never_enables_a_denoiser_even_if_capability_probe_is_true():
    api, bpy, calls = runtime([])
    bpy.context.preferences.addons["cycles"].preferences.has_optixdenoiser_gpu_devices = lambda: True
    assert configure_rgb_quality(api, bpy, render_settings(device="CUDA")) is None
    assert calls[0] == ("denoiser", None)


@pytest.mark.parametrize("mode", ["flat", "smooth", "auto"])
def test_shading_and_optional_normals_do_not_add_geometry_modifiers(mode):
    calls = []
    obj = SimpleNamespace(set_shading_mode=lambda *args, **kwargs: calls.append((args, kwargs)),
                          add_modifier=lambda *args, **kwargs: calls.append((args, kwargs)))
    apply_object_shading(obj, render_settings(shading_mode=mode, smooth_angle_deg=42., weighted_normals=True))
    assert calls == [((mode.upper(),), {"angle_value": 42.}),
                     (("WEIGHTED_NORMAL",), {"keep_sharp": True, "weight": 50, "mode": "FACE_AREA_WITH_ANGLE"})]


def test_label_pass_restores_fresh_scene_handles_after_failure():
    _, bpy, _ = runtime([])
    with pytest.raises(RuntimeError, match="label failure"):
        with label_pass_without_denoising(bpy):
            assert not bpy.context.scene.cycles.use_denoising
            assert not bpy.context.view_layer.cycles.use_denoising
            # Simulate render_segmap undo replacing the original RNA owners.
            bpy.context.scene.cycles = SimpleNamespace(use_denoising=False)
            bpy.context.view_layer.cycles = SimpleNamespace(use_denoising=False)
            raise RuntimeError("label failure")
    assert bpy.context.scene.cycles.use_denoising
    assert bpy.context.view_layer.cycles.use_denoising
