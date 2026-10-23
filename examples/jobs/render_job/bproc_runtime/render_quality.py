"""Cycles GPU selection and normal-only surface treatment.

BlenderProc's device helper permits CPU fallback and mixed CPU/GPU use. The
application deliberately applies a stricter policy after that helper returns.
"""
from __future__ import annotations

from contextlib import contextmanager

from .contracts import RenderError


GPU_BACKENDS = frozenset({"OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"})


def configure_gpu(api, bpy, device_type: str) -> tuple[str, ...]:
    """Select the requested GPU backend, or fail without rendering on the CPU."""
    if device_type not in GPU_BACKENDS:
        raise RenderError(f"GPU rendering requires OPTIX, CUDA, HIP, ONEAPI or METAL; got {device_type!r}")
    try:
        api.renderer.set_render_devices(use_only_cpu=False, desired_gpu_device_type=device_type)
        preferences = bpy.context.preferences.addons["cycles"].preferences
        # BlenderProc may have changed this to NONE after its own fallback.
        preferences.compute_device_type = device_type
        available = list(preferences.get_devices_for_type(device_type))
        selected = [device for device in available if device.type == device_type]
        # get_devices_for_type can contain the CPU. It also leaves other backend
        # entries in preferences.devices, so explicitly disable every other entry.
        for device in list(preferences.devices) + available:
            device.use = device.type == device_type and any(device == gpu for gpu in selected)
        bpy.context.scene.cycles.device = "GPU"
        if not selected:
            raise RenderError(f"No {device_type} GPU is available; CPU fallback is disabled")
        assert_gpu_only(bpy, device_type)
        names = tuple(device.name for device in selected)
        print(f"GPU rendering: {device_type} — {', '.join(names)}; CPU devices disabled", flush=True)
        return names
    except RenderError:
        raise
    except Exception as exc:
        # Never allow a failed selection to leave an enabled CPU from BProc's
        # default initialization or the user's saved Blender preferences.
        try:
            for device in bpy.context.preferences.addons["cycles"].preferences.devices:
                device.use = False
            bpy.context.scene.cycles.device = "GPU"
        except Exception:
            pass
        raise RenderError(f"Cannot initialize {device_type} GPU; CPU fallback is disabled: {exc}") from exc


def assert_gpu_only(bpy, device_type: str) -> None:
    preferences = bpy.context.preferences.addons["cycles"].preferences
    enabled = [device for device in preferences.devices if device.use]
    if (bpy.context.scene.cycles.device != "GPU" or preferences.compute_device_type != device_type
            or not any(device.type == device_type for device in enabled)
            or any(device.type != device_type for device in enabled)):
        raise RenderError(f"Cycles is not configured exclusively for {device_type} GPU rendering")


def configure_rgb_quality(api, bpy, render) -> str | None:
    """Use native Cycles denoising: Combined RGB only, never compositor depth."""
    preferences = bpy.context.preferences.addons["cycles"].preferences
    probe = getattr(preferences, "has_optixdenoiser_gpu_devices", None)
    has_optix_gpu = bool(probe()) if callable(probe) else False
    denoiser = render.denoiser
    if denoiser == "auto":
        denoiser = "OPTIX" if render.device == "OPTIX" and has_optix_gpu else "none"
    if denoiser == "OPTIX" and render.device != "OPTIX":
        # Blender 4.2 silently uses a CPU denoiser with a CUDA render device,
        # even when denoising_use_gpu=True. Require the verified OptiX path.
        raise RenderError("OPTIX denoising requires the OPTIX render backend; CPU denoising fallback is disabled")
    if denoiser == "OPTIX" and not has_optix_gpu:
        raise RenderError("No enabled OptiX GPU denoiser is available; select the OPTIX render backend "
                          "or disable denoising. CPU denoising fallback is disabled")
    if denoiser not in {"none", "OPTIX"}:
        raise RenderError(f"Unsupported GPU denoiser: {denoiser!r}")
    api.renderer.set_denoiser(None if denoiser == "none" else denoiser)
    if denoiser == "OPTIX" and hasattr(bpy.context.scene.cycles, "denoising_use_gpu"):
        bpy.context.scene.cycles.denoising_use_gpu = True
    api.renderer.set_noise_threshold(render.noise_threshold)
    return None if denoiser == "none" else denoiser


def apply_object_shading(obj, render) -> None:
    """Change normals only; retain canonical vertices, faces and silhouettes."""
    if render.shading_mode not in {"flat", "smooth", "auto"}:
        raise RenderError(f"Unsupported surface shading: {render.shading_mode!r}")
    obj.set_shading_mode(render.shading_mode.upper(), angle_value=render.smooth_angle_deg)
    if render.weighted_normals:
        obj.add_modifier("WEIGHTED_NORMAL", keep_sharp=True, weight=50,
                         mode="FACE_AREA_WITH_ANGLE")


@contextmanager
def label_pass_without_denoising(bpy):
    """Keep label colors exact, including on failure or a Blender undo cycle."""
    def settings():
        context = bpy.context
        owners = [getattr(context.scene, "cycles", None),
                  getattr(getattr(context, "view_layer", None), "cycles", None)]
        return [owner for owner in owners if owner is not None and hasattr(owner, "use_denoising")]
    before = [owner.use_denoising for owner in settings()]
    try:
        for owner in settings():
            owner.use_denoising = False
        yield
    finally:
        # Re-fetch RNA handles: render_segmap's UndoAfterExecution invalidates
        # the original scene/view-layer pointers.
        for owner, value in zip(settings(), before):
            owner.use_denoising = value
