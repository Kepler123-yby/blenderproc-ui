"""Rebuild the exact render scene inside Blender and hand it to the GUI.

Two passes, both running under Blender's own Python:

* build:  ``blender --background --python tools/blender_view.py -- ... --save-blend scene.blend``
  rebuilds objects, materials, shading, camera intrinsics, pose, light and world
  through the same ``BlenderBackend`` the renderer uses, then saves the file.
* view:   ``blender scene.blend --python tools/blender_view.py -- ... --view-only``
  restores the on-demand geometry session and native BlenderProc sidebar. Rendering
  is enabled only by its explicit strict-GPU operator; no auto-run preference is needed.
"""
from __future__ import annotations

import argparse
import json
import os
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
    parser.add_argument("--geometry-only", action="store_true")
    parser.add_argument("--blenderproc-package", type=Path)
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
    from bproc_core.preview import candidate_for_preview
    from bproc_core.scene import Rejection

    candidate = candidate_for_preview(config, meshes, scene_index, view_index)
    if isinstance(candidate, Rejection):
        raise ValueError(f"没有可用候选：{candidate.reason}: {candidate.detail}")
    return candidate


def _bootstrap(arguments):
    """Use the installed runtime only; never allow BProc bootstrap to install packages."""
    import subprocess

    sys.path.insert(0, str(ROOT / "src"))
    if arguments.blenderproc_package is not None:
        sys.path.insert(0, str(arguments.blenderproc_package.resolve().parent))
    os.environ["INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT"] = "1"
    original_popen, original_argv = subprocess.Popen, sys.argv

    def installed_only(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and ("ensurepip" in command or
                ("pip" in command and any(word in command for word in ("install", "uninstall")))):
            raise RuntimeError("BlenderProc 依赖缺失；预览不会安装依赖：" + str(command))
        return original_popen(command, *args, **kwargs)

    scratch = Path(original_argv[original_argv.index("--") + 2])
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen = installed_only
        # Use the same scratch convention for GUI and background, without creating BProc's
        # relative examples/debugging/temp or letting it rewrite the caller's argv.
        sys.argv = ["blender", "--background", "--", str(__file__), str(scratch)]
        import blenderproc  # noqa: F401 — MUST precede numpy
    finally:
        subprocess.Popen, sys.argv = original_popen, original_argv


def _build(arguments) -> dict:
    _bootstrap(arguments)

    from bproc_core.assets import load_mesh
    from bproc_core.project_io import load_project
    from bproc_core.scene import candidate_from_dict, candidate_to_dict
    from bproc_runtime.blender_backend import BlenderBackend
    from bproc_runtime.blender_preview import SESSION_KEY, start_session

    if arguments.config is None:
        raise SystemExit("--config 必填")
    config = load_project(arguments.config)
    targets = [t for t in config.targets if t.enabled]
    assets = [t.asset for t in targets] + list(config.occluders)
    meshes = {asset.key: load_mesh(asset) for asset in assets}
    candidate = (candidate_from_dict(json.loads(arguments.candidate.read_text(encoding="utf-8")))
                 if arguments.candidate else _load_candidate(config, meshes, arguments.scene, arguments.view))
    if arguments.geometry_only:
        backend = start_session(arguments.config, candidate=candidate).backend
    else:
        backend = BlenderBackend()
        backend.prepare(config, meshes)
        backend.apply(candidate)
    backend.bpy.context.scene[SESSION_KEY] = json.dumps({"config": str(arguments.config.resolve())})
    backend.bpy.context.scene["bproc_preview_candidate"] = json.dumps(candidate_to_dict(candidate))
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
        "viewport": "SOLID camera" if arguments.geometry_only else _set_rendered_viewport(),
    }


def main(argv=None) -> int:
    arguments = _parse(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv)
    if arguments.view_only:
        # GUI starts without the build environment. Add only the installed BProc package;
        # its bootstrap removes the conda site directory before numpy is imported.
        if arguments.blenderproc_package is None:
            raise RuntimeError("缺少 --blenderproc-package；请用应用或 tools/open_blender.py 打开")
        _bootstrap(arguments)
        from bproc_runtime.blender_preview import restore_session
        restore_session()
        print("BlenderProc 侧栏已注册 · 候选预览 · 未经像素遮挡验收", flush=True)
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
