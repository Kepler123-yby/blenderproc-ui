"""Real, offline launch acceptance for close geometry and interrupted runs.

This opt-in integration check creates new jobs/results; it never removes old
results. Run with a prepared local BlenderProc installation and a demo YAML.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import sys
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bproc_core.config import AssetSpec, Azimuth, Box, Range
from bproc_core.project_io import load_project
sys.path.insert(0, "src")
from bproc_runtime.metadata import read_frame_records
from bproc_export.bundle import export_job


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(job, python, log_path, *, interrupt=None, after_frames=1):
    argv = [str(python), str(job.directory / "run_job.py")]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(argv, cwd="/tmp", stdout=log, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        if interrupt is not None:
            config = load_project(job.directory / "scene.yaml")
            metadata_dir = config.output.root / config.output.dataset_name / "run_metadata"
            deadline = time.monotonic() + 90
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    committed = len(read_frame_records(metadata_dir, allow_incomplete_tail=True))
                except (OSError, ValueError):
                    committed = 0
                if committed >= after_frames:
                    process.send_signal(interrupt)
                    break
                time.sleep(.05)
            else:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                raise AssertionError("No committed frame became available for interruption within 90s")
        try:
            code = process.wait(timeout=30 if interrupt is not None else 90)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    return code


def check_failures(project: Path, destination: Path, python: Path) -> dict:
    destination = destination.absolute()
    destination.mkdir(parents=True, exist_ok=False)
    original = load_project(project)
    summary = {}

    # Shrink the original CAD coordinates as well as the object dimensions;
    # canonical displaced model origins remain displaced by the same factor.
    source = original.targets[0].asset.path
    import trimesh
    mesh = trimesh.load(source, process=False)
    mesh.vertices *= .05
    tiny_path = destination / "tiny.ply"
    mesh.export(tiny_path)
    target = replace(original.targets[0], asset=AssetSpec("tiny", tiny_path, original.targets[0].asset.unit))
    region = replace(original.regions[0], radius_m=Range(.035,.035),
                     azimuth=Azimuth(0,0,False), elevation_deg=Range(15,15))
    camera = replace(original.camera,width=128,height=96,fx=114.48228,fy=114.714086,cx=65.05222,cy=48.409798)
    tiny = replace(original, targets=(target,), anchor_key="tiny", regions=(region,),camera=camera,
                   occluders=(),scene_count=1,views_per_scene=1,render=replace(original.render,samples=2),
                   occlusion=replace(original.occlusion,none_fraction=1,count=(0,0)),
                   output=replace(original.output,format="bop",root=destination/"results",dataset_name="close-bop"))
    job = export_job(tiny,destination/"close-job")
    code = _run(job,python,destination/"close.log")
    dataset = tiny.output.root / tiny.output.dataset_name
    state = json.loads((dataset/"run_metadata/status.json").read_text())
    assert code == 0 and state["saved"] == 1 and state["status"] == "completed", (code,state)
    from check_bop import check_bop
    check_bop(dataset,expected_ids=(target.obj_id,))
    from PIL import Image
    import numpy as np
    depth = np.asarray(Image.open(dataset/"train_pbr/000000/depth/000000.png")) * .0001
    assert 0 < depth.max() < .1
    summary["close_bop"] = {"exit_code":code,"saved":state["saved"],"max_depth_m":float(depth.max())}

    targets = list(original.targets)
    targets[1] = replace(targets[1],offset=Box(Range(0,0),Range(0,0),Range(0,0)))
    impossible = replace(original,targets=tuple(targets),scene_count=1,views_per_scene=1,
        output=replace(original.output,root=destination/"results",dataset_name="impossible"))
    job = export_job(impossible,destination/"impossible-job")
    code = _run(job,python,destination/"impossible.log")
    dataset = impossible.output.root / impossible.output.dataset_name
    state = json.loads((dataset/"run_metadata/status.json").read_text())
    assert code != 0 and state["status"] == "partial" and state["saved"] == 0 and state["skipped"] == 1
    assert state["failures_by_reason"] == {"collision":30}, state
    before = tree_hash(dataset)
    repeated_code = _run(job,python,destination/"existing-output.log")
    assert repeated_code != 0 and before == tree_hash(dataset)
    summary["bounded_impossible"] = {"exit_code":code,**state}
    summary["existing_output"] = {"exit_code":repeated_code,"unchanged_sha256":before}

    # Requests stay plentiful after one frame so the signal targets a live run.
    for sig, name in ((signal.SIGINT,"sigint"),(signal.SIGTERM,"sigterm")):
        config = replace(original,scene_count=250,views_per_scene=4,occluders=(),
            render=replace(original.render,samples=8),
            occlusion=replace(original.occlusion,none_fraction=1,count=(0,0)),
            output=replace(original.output,root=destination/"results",dataset_name=name))
        job = export_job(config,destination/(name+"-job"))
        code = _run(job,python,destination/(name+".log"),interrupt=sig)
        dataset = config.output.root / config.output.dataset_name
        state = json.loads((dataset/"run_metadata/status.json").read_text())
        assert state["status"] == "interrupted" and code == 128 + sig, (code,state)
        assert 0 < state["saved"] < 1000 and state["skipped"] == 1000-state["saved"]
        assert state["written_frames"] == state["saved"]
        summary[name] = {"process_exit_code":code,**state}
    (destination/"acceptance.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    return summary


def check_smoke(project: Path, destination: Path, python: Path) -> dict:
    """Render four multi-target images in each format with current exported code."""
    from check_bop import verify_bop
    from check_rgb_masks import verify_rgb_masks
    original = load_project(project)
    summary = {}
    for output_format in ("rgb_masks", "bop"):
        config = replace(original,scene_count=1,views_per_scene=4,
            output=replace(original.output,format=output_format,root=destination/"results",
                           dataset_name="final-"+output_format))
        job = export_job(config,destination/("final-"+output_format+"-job"))
        code = _run(job,python,destination/("final-"+output_format+".log"))
        dataset = config.output.root / config.output.dataset_name
        state = json.loads((dataset/"run_metadata/status.json").read_text())
        assert code == 0 and state["status"] == "completed" and state["saved"] == 4, (code,state)
        errors = (verify_bop(dataset) if output_format == "bop" else verify_rgb_masks(dataset))
        assert not errors, errors
        summary[output_format] = {"exit_code":code,**state}
    target = destination/"smoke.json"
    target.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    return summary


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",type=Path,required=True)
    parser.add_argument("--destination",type=Path,required=True)
    parser.add_argument("--python",type=Path,default=Path.home()/"miniconda3/envs/bop/bin/python")
    parser.add_argument("--smoke",action="store_true",help="Also render four multi-target frames in both formats")
    args=parser.parse_args()
    print(json.dumps(check_failures(args.project,args.destination,args.python),ensure_ascii=False,indent=2))
    if args.smoke:
        print(json.dumps(check_smoke(args.project,args.destination,args.python),ensure_ascii=False,indent=2))
