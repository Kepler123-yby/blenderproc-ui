#!/usr/bin/env python3
"""Real Blender 4.2 geometry + GUI/operator smoke; writes only into a NEW destination."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bproc_core.assets import load_mesh
from bproc_core.config import AssetSpec, FixedCameraSpec, Range, RegionSpec, RuntimeSpec
from bproc_core.preview import candidate_for_preview
from bproc_core.project_io import save_project
from bproc_core.scene import Rejection, candidate_to_dict
from bproc_export.blender_view import prepare_scene, gui_command, gui_environment
from bproc_export.bundle import locate_runtime
from tests.factories import make_project, make_mesh, write_mesh


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args(argv)
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        config = make_project(destination / "fixture")
        asset = AssetSpec("blocker", write_mesh(destination / "fixture/blocker.ply",
                                               make_mesh("blocker", size=.02)), "m")
        config = replace(config, seed=71, scene_count=2, views_per_scene=3,
                         regions=(RegionSpec(key="east"), RegionSpec(key="west")),
                         fixed_cameras=(FixedCameraSpec(key="one"),
                                        FixedCameraSpec(key="two", azimuth_deg=90)),
                         occluders=(asset,),
                         occlusion=replace(config.occlusion, count=(1, 1), none_fraction=.5,
                                           scale=Range(.3, .5)),
                         render=replace(config.render, device="CUDA", denoiser="OPTIX"),
                         runtime=RuntimeSpec(
                             blender_dir=Path("/home/kepler/blender/blender-4.2.1-linux-x64"),
                             blenderproc_executable=Path("/home/kepler/miniconda3/envs/bop/bin/blenderproc")))
        meshes = {a.key: load_mesh(a) for a in [*(t.asset for t in config.targets), asset]}
        candidate = candidate_for_preview(config, meshes, 0, 0)
        if isinstance(candidate, Rejection):
            raise RuntimeError(str(candidate))
        save_project(replace(config, camera_mode="fixed"), destination / "fixed.yaml")
        (destination / "expected.json").write_text(json.dumps(candidate_to_dict(candidate)), encoding="utf-8")
        script = ROOT / "tools/blender_view.py"
        def runner(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            records.append({"phase": "build", "command": command, "exit_code": result.returncode})
            return result
        blend = prepare_scene(config, candidate, destination / "snapshot", script,
                              runner=runner, geometry_only=True)
        locator = locate_runtime(config)
        command = gui_command(locator, script, blend)
        # Execute the real --view-only startup first, then inspect that very GUI session.
        separator = command.index("--")
        command[separator:separator] = ["--python-exit-code", "1", "--python",
                                       str(ROOT / "tests/blender/check_preview.py")]
        environment = gui_environment()
        environment["BPROC_PREVIEW_EVIDENCE"] = str(destination)
        environment.setdefault("DISPLAY", ":1")
        with (destination / "gui.log").open("w") as log:
            result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=120)
        records.append({"phase": "gui", "command": command, "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError(f"Blender GUI smoke failed: exit {result.returncode}; see gui.log")
        result_path = destination / "checks.json"
        result_payload = json.loads(result_path.read_text(encoding="utf-8"))
        if result_payload.get("passed") is not True:
            raise RuntimeError("Blender did not report completed checks")
        print(f"PASS: real Blender build + GUI/operators; evidence: {destination}")
        return 0
    except Exception as error:
        print(f"FAIL: {error}; evidence: {destination}", file=sys.stderr)
        return 1
    finally:
        (destination / "commands.json").write_text(json.dumps(records, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
