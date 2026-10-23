"""Executed after tools/blender_view.py --view-only in a REAL Blender GUI process."""
import json
import os
from pathlib import Path
import sys
import traceback

import bpy
import numpy as np

from bproc_core.preview import candidate_for_preview
from bproc_core.scene import Rejection, candidate_to_dict
from bproc_runtime import blender_preview as preview
from bproc_runtime import blender_backend

DESTINATION = Path(os.environ["BPROC_PREVIEW_EVIDENCE"])


def check_live(session, expected):
    candidate = session.candidate
    assert candidate_to_dict(candidate) == candidate_to_dict(expected)
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    np.testing.assert_allclose(np.array(scene.camera.matrix_world), expected.camera.world_from_camera, atol=1e-6)
    c = session.config.camera
    K = [[c.fx, 0, c.cx], [0, c.fy, c.cy], [0, 0, 1]]
    np.testing.assert_allclose(session.backend.api.camera.get_intrinsics_as_K_matrix(), K, atol=1e-4)
    used = {}
    shown = set()
    for placement in expected.placements:
        if placement.role == "target":
            obj = session.backend.targets[placement.obj_id]
        else:
            index = used.get(placement.asset_key, 0)
            obj = session.backend._pools[placement.asset_key][index]
            used[placement.asset_key] = index + 1
        shown.add(obj.get_name())
        assert not obj.is_hidden()
        np.testing.assert_allclose(obj.get_local2world_mat(), placement.world_from_model, atol=1e-6)
    for obj in session.backend.objects:
        assert obj.is_hidden() == (obj.get_name() not in shown)
        assert obj.blender_obj.hide_viewport == (obj.get_name() not in shown)
    np.testing.assert_allclose(session.backend.light.get_location(), expected.light.position_m, atol=1e-6)
    np.testing.assert_allclose(session.backend.light.blender_obj.data.energy, expected.light.energy_w, rtol=1e-6)
    np.testing.assert_allclose(session.backend.light.blender_obj.data.color, expected.light.color_rgb, atol=1e-6)
    nodes = scene.world.node_tree.nodes
    background = next(n for n in nodes if n.type == "BACKGROUND")
    mapping = next(n for n in nodes if n.type == "MAPPING")
    np.testing.assert_allclose(background.inputs["Strength"].default_value, expected.hdri_strength, rtol=1e-6)
    np.testing.assert_allclose(mapping.inputs["Rotation"].default_value[2],
                               np.deg2rad(expected.hdri_rotation_deg), atol=1e-6)
    env = next(n for n in nodes if n.type == "TEX_ENVIRONMENT")
    assert Path(bpy.path.abspath(env.image.filepath)).resolve() == expected.hdri_path.resolve()
    for obj in session.backend.targets.values():
        shader = obj.get_materials()[0].blender_obj.node_tree.nodes.get("Principled BSDF")
        np.testing.assert_allclose(shader.inputs["Roughness"].default_value, expected.material.roughness, atol=1e-6)
    props = scene.bproc_preview
    assert (props.scene_number, props.view_number) == (candidate.scene_index + 1, candidate.view_index + 1)
    assert preview.NOTICE in props.status


def counts():
    return {name: len(getattr(bpy.data, name)) for name in ("objects", "meshes", "materials", "lights", "cameras", "worlds")}


def run():
    assert bpy.app.version[:2] == (4, 2)
    assert not bpy.app.background, "must exercise the GUI --view-only startup"
    assert bpy.types.BPROC_PT_preview.is_registered
    session = preview.current_session()
    initial = session.candidate
    assert candidate_to_dict(initial) == json.loads((DESTINATION / "expected.json").read_text())
    preview.register()
    preview.register()
    assert preview.current_session() is session
    user_scene = bpy.data.scenes.new("Unrelated smoke scene")
    user_mesh = bpy.data.meshes.new("Unrelated smoke mesh")
    user_object = bpy.data.objects.new("Unrelated smoke object", user_mesh)
    user_scene.collection.objects.link(user_object)
    user_identity = user_object.as_pointer()
    safety = bpy.context.preferences.filepaths.use_scripts_auto_execute

    def forbidden(*args, **kwargs):
        raise AssertionError("geometry called a GPU/denoiser initializer")

    # Strong call probes, not just a config that happens to work on this machine.
    blender_backend.configure_gpu = forbidden
    blender_backend.configure_rgb_quality = forbidden
    session.backend.api.init = forbidden
    session.backend.api.renderer.set_render_devices = forbidden
    session.backend.api.renderer.set_denoiser = forbidden
    session = preview.start_session(DESTINATION / "snapshot/scene.yaml", candidate=initial)
    baseline = counts()
    checks = []
    for s, v in [(0, 0), (0, 2), (1, 1), (1, 2), (0, 0)]:
        props = bpy.context.scene.bproc_preview
        props.scene_number, props.view_number = s + 1, v + 1
        assert bpy.ops.bproc.preview_update() == {'FINISHED'}
        expected = initial if (s, v) == (0, 0) else candidate_for_preview(session.config, session.meshes, s, v)
        assert not isinstance(expected, Rejection)
        check_live(session, expected)
        assert counts() == baseline
        checks.append({"scene": s, "view": v, "candidate": candidate_to_dict(expected)})
    returned = candidate_to_dict(session.candidate)
    initial_payload = candidate_to_dict(initial)
    assert returned == initial_payload, {k: (initial_payload[k], returned[k]) for k in returned
                                        if returned[k] != initial_payload[k]}
    props = bpy.context.scene.bproc_preview
    props.scene_number = 999
    assert bpy.ops.bproc.preview_update() == {'CANCELLED'}
    assert props.scene_number == 1 and session.candidate.scene_index == 0
    assert "更新失败" in props.status
    spaces = [a.spaces.active for a in bpy.context.screen.areas if a.type == 'VIEW_3D']
    assert spaces
    assert bpy.ops.bproc.preview_overview() == {'FINISHED'}
    assert all(s.region_3d.view_perspective == 'PERSP' and s.overlay.show_extras for s in spaces)
    assert bpy.ops.bproc.preview_camera() == {'FINISHED'}
    assert all(s.region_3d.view_perspective == 'CAMERA' for s in spaces)
    assert bpy.ops.bproc.preview_render() == {'CANCELLED'}
    assert "自动 GPU" in props.status and "CPU" in props.status
    assert all(s.shading.type == 'SOLID' for s in spaces)
    assert candidate_to_dict(session.candidate) == candidate_to_dict(initial)

    from bproc_core.project_io import load_project
    fixed = load_project(DESTINATION / "fixed.yaml")
    fixed_initial = candidate_for_preview(fixed, session.meshes, 0, 0)
    session = preview.start_session(DESTINATION / "fixed.yaml", candidate=fixed_initial)
    baseline = counts()
    for s, v in [(0, 0), (0, 1), (1, 1), (0, 0)]:
        props = bpy.context.scene.bproc_preview
        props.scene_number, props.view_number = s + 1, v + 1
        assert bpy.ops.bproc.preview_update() == {'FINISHED'}
        expected = candidate_for_preview(session.config, session.meshes, s, v)
        assert expected.camera_kind == "fixed"
        check_live(session, expected)
        assert counts() == baseline
        checks.append({"scene": s, "view": v, "candidate": candidate_to_dict(expected)})
    preview.unregister()
    preview.unregister()
    assert not hasattr(bpy.types.Scene, "bproc_preview")
    assert not any(preview.SESSION_KEY in scene for scene in bpy.data.scenes)
    assert bpy.data.objects.get("Unrelated smoke object").as_pointer() == user_identity
    assert bpy.context.preferences.filepaths.use_scripts_auto_execute == safety
    preview.register()
    preview.unregister()
    payload = {"passed": True, "blender": bpy.app.version_string, "gui_startup": True,
               "checks": checks, "no_gpu_or_denoiser_calls": True,
               "native_view3d": True, "invalid_render_rejected": True,
               "unrelated_scene_preserved": True, "registration_cleanup": True}
    (DESTINATION / "checks.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: camera/K, placements, lights, world, material, round trips, object reuse, VIEW_3D, GPU rejection", flush=True)


try:
    run()
except BaseException:
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)  # Blender must not turn a Python assertion failure into a successful process.
else:
    sys.stdout.flush()
    bpy.ops.wm.quit_blender()
