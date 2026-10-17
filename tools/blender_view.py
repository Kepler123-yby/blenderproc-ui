"""Rebuild the exact render scene inside Blender and hand it to the GUI.

Two passes, both running under Blender's own Python:

* build:  ``blender --background --python tools/blender_view.py -- ... --save-blend scene.blend``
  rebuilds objects, materials, shading, camera intrinsics, pose, light and world
  through the same ``BlenderBackend`` the renderer uses, then saves the file.
* view:   ``blender scene.blend --python tools/blender_view.py -- ... --view-only``
  only switches the 3D viewport to the camera with rendered shading, because
  opening a file skips the splash screen that would cover the scene.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _parse(argv):
    argv = list(argv)
    while argv and not argv[0].startswith("--"):
        argv.pop(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--save-blend", type=Path)
    parser.add_argument("--view-only", action="store_true")
    parser.add_argument("--scene", type=int, default=0)
    parser.add_argument("--view", type=int, default=0)
    return parser.parse_args(argv)


def _bpy():
    import bpy  # provided by Blender itself

    return bpy


def _set_rendered_viewport() -> str:
    bpy = _bpy()
    touched = 0
    try:
        for area in bpy.context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.shading.type = "RENDERED"
                    space.region_3d.view_perspective = "CAMERA"
                    touched += 1
    except Exception as error:  # viewport display is a convenience, never fatal
        return f"viewport left at default: {error}"
    return f"viewport: rendered camera view in {touched} area(s)"


def _load_candidate(config, meshes, scene_index, view_index):
    from bproc_core.scene import Rejection, propose_candidate, propose_scene

    scene = propose_scene(config, meshes, scene_index)
    if isinstance(scene, Rejection):
        raise SystemExit(f"场景无法摆放：{scene.reason}")
    for attempt in range(60):
        candidate = propose_candidate(config, meshes, scene, view_index, attempt)
        if not isinstance(candidate, Rejection):
            return candidate
    raise SystemExit("没有可用候选机位；先在 UI 里调整距离或位置范围")


def _build(arguments) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    # BlenderProc must be imported before numpy: it rebuilds sys.path so numpy comes
    # from Blender, not from the conda 3.8 site-packages the exporter points to.
    import blenderproc  # noqa: F401

    from bproc_core.assets import load_mesh
    from bproc_core.project_io import load_project
    from bproc_core.scene import candidate_from_dict
    from bproc_runtime.blender_backend import BlenderBackend
    from bproc_runtime.render_quality import configure_rgb_quality

    if arguments.config is None:
        raise SystemExit("--config 必填")
    config = load_project(arguments.config)
    targets = [t for t in config.targets if t.enabled]
    assets = [t.asset for t in targets] + list(config.occluders)
    meshes = {asset.key: load_mesh(asset) for asset in assets}
    candidate = (candidate_from_dict(json.loads(arguments.candidate.read_text(encoding="utf-8")))
                 if arguments.candidate else _load_candidate(config, meshes, arguments.scene, arguments.view))
    backend = BlenderBackend()
    backend.prepare(config, meshes)
    backend.apply(candidate)
    configure_rgb_quality(backend.api, backend.bpy, config.render)
    scene = backend.bpy.context.scene
    if hasattr(scene, "cycles"):
        scene.cycles.preview_samples = min(32, max(8, config.render.samples))
    bpy = _bpy()
    if arguments.save_blend is not None:
        destination = arguments.save_blend.expanduser().absolute()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # ``render.use_file_extension`` off keeps the path identical to the request,
        # so the launcher can open exactly what was written.
        bpy.ops.wm.save_as_mainfile(filepath=str(destination))
    camera = candidate.camera
    return {
        "scene": str(arguments.save_blend or ""),
        "camera_m": [round(float(v), 5) for v in camera.position_m],
        "azimuth_deg": round(camera.azimuth_deg, 2),
        "elevation_deg": round(camera.elevation_deg, 2),
        "radius_m": round(camera.radius_m, 4),
        "targets": len(targets),
        "occluders": len(candidate.placements) - len(targets),
        "samples": config.render.samples,
        "device": config.render.device,
        "viewport": _set_rendered_viewport(),
    }


def main(argv=None) -> int:
    arguments = _parse(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv)
    if arguments.view_only:
        print(_set_rendered_viewport(), flush=True)
        return 0
    print(json.dumps(_build(arguments), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    # An exception escaping a ``--python`` script closes the GUI, so report it and keep
    # the window open on whatever did get built.
    try:
        status = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        status = 1
    if _bpy().app.background:
        raise SystemExit(status)
