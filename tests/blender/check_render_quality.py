import blenderproc as bproc
"""Actual GPU rendering and geometry invariance; execute through BlenderProc."""
from argparse import ArgumentParser
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from bproc_runtime.render_quality import (apply_object_shading, assert_gpu_only, configure_gpu,
                                         configure_rgb_quality, label_pass_without_denoising)
from blenderproc.python.utility.SetupUtility import SetupUtility


def deny_install(*args, **kwargs):
    raise RuntimeError("GPU quality fixture may not install dependencies")


SetupUtility.setup_pip = staticmethod(deny_install)
parser = ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--device", choices=["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"], default="OPTIX")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
(args.output / "labels").mkdir()

bproc.init()
gpu_names = configure_gpu(bproc, bpy, args.device)
quality = SimpleNamespace(device=args.device, denoiser="auto", noise_threshold=0.,
                          shading_mode="auto", smooth_angle_deg=37., weighted_normals=False)
resolved_denoiser = configure_rgb_quality(bproc, bpy, quality)
bproc.renderer.set_max_amount_of_samples(8)
bproc.renderer.set_output_format("PNG", 8)
bproc.renderer.enable_depth_output(activate_antialiasing=False)
bproc.camera.set_intrinsics_from_K_matrix(np.array([[65., 0., 31.5], [0., 65., 23.5], [0., 0., 1.]]),
                                         64, 48, clip_start=.001, clip_end=100.)
location = np.array([.5, -.6, .4])
rotation = bproc.camera.rotation_from_forward_vec(-location)
world_from_camera = bproc.math.build_transformation_mat(location, rotation)
bproc.camera.add_camera_pose(world_from_camera, frame=0)

geometries = []
objects = []
for mode in ("flat", "smooth", "auto"):
    obj = bproc.object.create_primitive("CUBE")
    obj.set_scale([.1, .1, .1])
    obj.set_cp("ui_target_id", 7)
    mesh = obj.get_mesh()
    vertices = np.array([v.co[:] for v in mesh.vertices])
    faces = [tuple(p.vertices) for p in mesh.polygons]
    quality.shading_mode = mode
    quality.weighted_normals = mode == "auto"
    apply_object_shading(obj, quality)
    bpy.context.view_layer.update()
    evaluated = obj.blender_obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
    try:
        assert np.array_equal(vertices, np.array([v.co[:] for v in evaluated.vertices])), mode
        assert faces == [tuple(p.vertices) for p in evaluated.polygons], mode
    finally:
        obj.blender_obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
    smooth_faces = [face.use_smooth for face in mesh.polygons]
    assert all(value == (mode != "flat") for value in smooth_faces)
    if mode == "auto":
        modifier = next(mod for mod in obj.blender_obj.modifiers if mod.type == "NODES")
        assert abs(modifier["Input_1"] - np.deg2rad(37.)) < 1e-6
        assert any(mod.type == "WEIGHTED_NORMAL" and mod.keep_sharp for mod in obj.blender_obj.modifiers)
    geometries.append(dict(mode=mode, weighted_normals=mode == "auto", vertices=len(vertices),
                           faces=len(faces), geometry_unchanged=True,
                           modifiers=[mod.type for mod in obj.blender_obj.modifiers]))
    obj.hide(mode != "auto")
    objects.append(obj)

light = bproc.types.Light()
light.set_type("POINT")
light.set_location([.2, -.3, .6])
light.set_energy(80.)
bpy.context.scene.world.use_nodes = True
bpy.context.scene.world.node_tree.nodes.get("Background").inputs["Strength"].default_value = .4

# Native Cycles denoising only changes Combined RGB. An un-antialiased depth
# pass must remain bit-for-bit identical when denoising is toggled.
quality.denoiser = "none"
configure_rgb_quality(bproc, bpy, quality)
plain = bproc.renderer.render(output_dir=str(args.output / "render"), load_keys={"colors", "depth"}, verbose=True)
quality.denoiser = "auto"
resolved_denoiser = configure_rgb_quality(bproc, bpy, quality)
assert_gpu_only(bpy, args.device)
processed = bproc.renderer.render(output_dir=str(args.output / "render"), load_keys={"colors", "depth"}, verbose=True)
depth_equal = np.array_equal(plain["depth"][0], processed["depth"][0], equal_nan=True)
assert depth_equal, "Native RGB denoising changed the depth pass"

with label_pass_without_denoising(bpy):
    assert not bpy.context.scene.cycles.use_denoising
    assert not bpy.context.view_layer.cycles.use_denoising
    labels = bproc.renderer.render_segmap(output_dir=str(args.output / "labels"),
        map_by=["instance", "cp_ui_target_id"], default_values={"cp_ui_target_id": 0}, use_alpha_channel=False)
assert bpy.context.scene.cycles.use_denoising == (resolved_denoiser is not None)
segmap = labels["instance_segmaps"][0]
assert len(np.unique(segmap)) == 2 and np.count_nonzero(segmap), "Expected exact object/background labels"
assert_gpu_only(bpy, args.device)

preferences = bpy.context.preferences.addons["cycles"].preferences
result = dict(device=args.device, gpu_names=gpu_names, scene_device=bpy.context.scene.cycles.device,
    enabled_devices=[dict(name=d.name, type=d.type) for d in preferences.devices if d.use],
    cpu_disabled=not any(d.type == "CPU" and d.use for d in preferences.devices),
    resolved_denoiser=resolved_denoiser, denoising_use_gpu=bpy.context.scene.cycles.denoising_use_gpu,
    adaptive_sampling_enabled=bpy.context.scene.cycles.use_adaptive_sampling,
    requested_noise_threshold=quality.noise_threshold,
    rgb_channels_changed_by_denoising=int(np.count_nonzero(plain["colors"][0] != processed["colors"][0])),
    shading=geometries, depth_unchanged_by_denoising=depth_equal,
    labels_denoising_disabled=True, label_values=np.unique(segmap).tolist())
(args.output / "quality.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print("GPU quality verification PASS:", args.output / "quality.json", flush=True)
