"""Corrupt a valid temporary dataset and require semantic diagnostics."""
from dataclasses import replace
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import numpy as np
import pytest
from PIL import Image

from bproc_core.assets import load_mesh, write_bop_models
from bproc_core.camera import resize_camera
from bproc_core.config import Azimuth, Box, FixedCameraSpec, OcclusionSpec, Range, RegionSpec
from bproc_core.project_io import save_project
from bproc_core.scene import SceneCandidate, propose_candidate, propose_scene
from bproc_runtime.contracts import RenderedFrame, VisibilityEvidence
from bproc_runtime.output import JsonRunRecorder, atomic_json, reserve_dataset
from bproc_runtime.records import FrameRef, RunSummary
from bproc_runtime.rgb_masks import RgbMasksWriter
from tests.blender.check_rgb_masks import verify_rgb_masks
from tests.blender.check_bop import _depth_problems, _interior_mask, verify_bop
from tests.blender.verify_runtime import verify_runtime
from tests.factories import make_project


def _depth_problem(errors):
    """True when any diagnostic explains a depth/pose disagreement."""
    markers = ("inconsistent units", "deviates by more than", "inside the mask lies outside")
    return any(marker in error for error in errors for marker in markers)



def fixture_scene(tmp_path, output_format):
    config = make_project(tmp_path / "inputs", output_format=output_format)
    target = replace(config.targets[1], offset=Box(Range(0, 0), Range(.12, .12), Range(0, 0)))
    config = replace(config, scene_count=1, views_per_scene=1, camera=resize_camera(config.camera, 64, 48), targets=(config.targets[0], target),
                     regions=(RegionSpec(radius_m=Range(.6, .6), azimuth=Azimuth(0, 0, False), elevation_deg=Range(0, 0)),),
                     occlusion=OcclusionSpec(none_fraction=1))
    meshes = {target.asset.key: load_mesh(target.asset) for target in config.targets}
    candidate = propose_candidate(config, meshes, propose_scene(config, meshes, 0), 0, 0)
    assert isinstance(candidate, SceneCandidate)
    root = reserve_dataset(config.output.root, config.output.dataset_name)
    return config, meshes, candidate, root


def finish_record(config, root, candidate, ref, masks):
    recorder = JsonRunRecorder(root / "run_metadata")
    recorder.begin(config)
    recorder.record_frame(ref, candidate, VisibilityEvidence(masks[2], masks))
    recorder.finish(RunSummary("completed", 1, 1, 1, 0, {}, 0))


@pytest.fixture
def valid_rgb_masks_output(tmp_path):
    config, meshes, candidate, root = fixture_scene(tmp_path, "rgb_masks")
    masks = {}
    for obj_id, (y, x) in {2: (10, 10), 7: (28, 40)}.items():
        masks[obj_id] = np.zeros((48, 64), dtype=bool)
        masks[obj_id][y:y+8, x:x+8] = True
    writer = RgbMasksWriter(root, config)
    ref = writer.write(0, candidate, RenderedFrame(np.zeros((48, 64, 3), np.uint8), None, masks, masks))
    finish_record(config, root, candidate, ref, masks)
    return root


@pytest.fixture
def valid_bop_output(tmp_path):
    config, meshes, candidate, root = fixture_scene(tmp_path, "bop")
    write_bop_models(meshes, config.targets, root / "models")
    scene = root / "train_pbr/000000"
    for name in ("rgb", "depth", "mask", "mask_visib"):
        (scene / name).mkdir(parents=True)
    K = np.array([[config.camera.fx, 0, config.camera.cx], [0, config.camera.fy, config.camera.cy], [0, 0, 1]])
    cv_from_world = np.diag([1, -1, -1, 1]) @ np.linalg.inv(candidate.camera.world_from_camera)
    poses, infos, masks = [], [], {}
    depth = np.zeros((48, 64), dtype=np.uint16)
    for index, placement in enumerate(sorted(candidate.placements, key=lambda p: p.obj_id)):
        matrix = cv_from_world @ placement.world_from_model
        mesh = meshes[placement.asset_key]
        xyz = mesh.vertices_m @ matrix[:3, :3].T + matrix[:3, 3]
        homogeneous = xyz @ K.T
        uv = homogeneous[:, :2] / homogeneous[:, 2:]
        lo, hi = np.ceil(uv.min(axis=0)).astype(int), np.floor(uv.max(axis=0)).astype(int)
        mask = np.zeros((48, 64), dtype=bool)
        mask[lo[1]:hi[1]+1, lo[0]:hi[0]+1] = True
        masks[placement.obj_id] = mask
        depth[mask] = np.rint(xyz[:, 2].mean() * 1000 / config.output.depth_scale_mm).astype(np.uint16)
        # Native BOP may include silhouette edge pixels with no observed depth.
        first_y, first_x = np.transpose(np.nonzero(mask))[0]
        depth[first_y, first_x] = 0
        for kind in ("mask", "mask_visib"):
            Image.fromarray(mask.astype(np.uint8) * 255).save(scene / kind / f"000000_{index:06d}.png")
        poses.append({"obj_id": placement.obj_id, "cam_R_m2c": matrix[:3, :3].reshape(-1).tolist(), "cam_t_m2c": (matrix[:3, 3]*1000).tolist()})
        infos.append({"px_count_all": int(mask.sum()), "px_count_valid": int(mask.sum()-1), "px_count_visib": int(mask.sum()), "visib_fract": 1.0,
                      # BlenderProc records the span between the outer pixel centres (max - min),
                      # so a 5-pixel run from 31 to 35 is written as width 4, not 5.
                      "bbox_obj": [int(lo[0]), int(lo[1]), int(hi[0]-lo[0]), int(hi[1]-lo[1])], "bbox_visib": [int(lo[0]), int(lo[1]), int(hi[0]-lo[0]), int(hi[1]-lo[1])]})
    Image.fromarray(depth).save(scene / "depth/000000.png")
    Image.fromarray(np.zeros((48, 64, 3), dtype=np.uint8)).save(scene / "rgb/000000.png")
    atomic_json(scene / "scene_gt.json", {"0": poses})
    atomic_json(scene / "scene_camera.json", {"0": {"cam_K": K.reshape(-1).tolist(), "depth_scale": config.output.depth_scale_mm}})
    atomic_json(scene / "scene_gt_info.json", {"0": infos})
    finish_record(config, root, candidate, FrameRef(0, "train_pbr/000000/rgb/000000.png", 0, 0, (2, 7)), masks)
    return root


def edit_frames(root, function):
    """Mutate the frame records themselves; the journal is authoritative, so both copies change."""
    from bproc_runtime.metadata import encode_row, read_frame_records
    folder = Path(root) / "run_metadata"
    frames = read_frame_records(folder)
    function(frames)
    (folder / "frames.jsonl").write_text("".join(encode_row(row) + "\n" for row in frames))
    edit_json(folder / "manifest.json", lambda data: data.update(frames=frames))


def edit_json(path, function):
    data = json.loads(path.read_text())
    function(data)
    path.write_text(json.dumps(data))


def test_valid_rgb_masks_output(valid_rgb_masks_output):
    assert verify_rgb_masks(valid_rgb_masks_output) == []


@pytest.mark.parametrize("corruption", ["missing", "missing_image", "wrong_id", "nonbinary", "outside", "ratio", "position", "status", "malformed"])
def test_rgb_checker_detects_semantic_corruption(valid_rgb_masks_output, corruption):
    root = valid_rgb_masks_output
    mask = root / "mask/000000_000007.png"
    if corruption == "missing":
        mask.unlink()
    elif corruption == "missing_image":
        next((root / "rgb").glob("000000.*")).unlink()
    elif corruption == "wrong_id":
        mask.rename(mask.with_name("000000_000001.png"))
    elif corruption == "nonbinary":
        Image.fromarray(np.ones((48, 64), dtype=np.uint8)).save(mask)
    elif corruption == "outside":
        Image.fromarray(np.full((48, 64), 255, dtype=np.uint8)).save(root / "mask_visib/000000_000007.png")
    elif corruption == "ratio":
        edit_frames(root, lambda frames: frames[0].update(occlusion_ratio=.3))
    elif corruption == "position":
        edit_frames(root, lambda frames: frames[0]["candidate"]["placements"][1]["world_from_model"][1].__setitem__(3, 10))
    elif corruption == "status":
        edit_json(root / "run_metadata/status.json", lambda data: data.update(saved=10))
    else:
        (root / "run_metadata/manifest.json").write_text("[invalid")
    assert verify_rgb_masks(root)


def test_valid_bop_including_visible_pixels_with_missing_depth(valid_bop_output):
    assert verify_bop(valid_bop_output) == []


@pytest.mark.parametrize("corruption", ["translation_units", "model_origin", "wrong_gt_index", "depth_scale", "empty_info"])
def test_bop_checker_detects_units_origin_and_native_index_corruption(valid_bop_output, corruption):
    root = valid_bop_output
    scene = root / "train_pbr/000000"
    if corruption == "translation_units":
        edit_json(scene / "scene_gt.json", lambda data: data["0"][0].update(cam_t_m2c=(np.asarray(data["0"][0]["cam_t_m2c"])/1000).tolist()))
    elif corruption == "model_origin":
        path = root / "models/obj_000002.ply"
        text = path.read_text()
        before, vertices = text.split("end_header\n", 1)
        rows = vertices.splitlines()
        for i in range(8):
            values = rows[i].split()
            values[0] = str(float(values[0]) - 20)
            rows[i] = " ".join(values)
        path.write_text(before + "end_header\n" + "\n".join(rows) + "\n")
    elif corruption == "wrong_gt_index":
        path = scene / "mask/000000_000001.png"
        path.rename(path.with_name("000000_000007.png"))
    elif corruption == "depth_scale":
        edit_json(scene / "scene_camera.json", lambda data: data["0"].update(depth_scale=100))
    else:
        atomic_json(scene / "scene_gt_info.json", {})
    assert verify_bop(root)


@pytest.mark.parametrize("function", [verify_rgb_masks, verify_bop, verify_runtime])
def test_missing_dataset_is_returned_as_diagnostics(tmp_path, function):
    diagnostics = function(tmp_path / "missing")
    assert isinstance(diagnostics, list) and diagnostics


def test_runtime_checker_rejects_incomplete_export_and_wrong_first_import(tmp_path):
    source = Path(__file__).resolve().parents[2] / "src"
    config = make_project(tmp_path / "inputs")
    root = tmp_path / "job"
    root.mkdir()
    for package in ("bproc_core", "bproc_runtime"):
        shutil.copytree(source / package, root / package, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("run_job.py", "render_scene.py"):
        shutil.copyfile(source / "bproc_export/templates" / name, root / name)
    save_project(config, root / "scene.yaml")
    (root / "README.txt").write_text("fixture")
    launch = {"schema_version": 1, "ready": True, "blender_dir": str(config.runtime.blender_dir), "blender_python": str(tmp_path / "python"),
              "custom_packages": str(tmp_path / "custom"), "blenderproc_executable": str(config.runtime.blenderproc_executable), "blenderproc_package_dir": str(tmp_path / "blenderproc")}
    atomic_json(root / "launch.json", launch)
    assert verify_runtime(root, execute_probe=False) == []
    atomic_json(root / "launch.json", {**launch, "ready": False})
    assert verify_runtime(root, execute_probe=False)
    atomic_json(root / "launch.json", launch)
    (root / "render_scene.py").write_text("import sys\nimport blenderproc as bproc\n")
    assert verify_runtime(root, execute_probe=False)


def natural_occlusion_output(tmp_path):
    """A valid rgb_masks dataset where target 7 really covers a quarter of target 2."""
    config = make_project(tmp_path / "inputs", output_format="rgb_masks")
    config = replace(config, scene_count=1, views_per_scene=1, occluders=(),
                     camera=resize_camera(config.camera, 64, 48),
                     regions=(RegionSpec(radius_m=Range(.6, .6), azimuth=Azimuth(0, 0, False),
                                         elevation_deg=Range(0, 0)),),
                     occlusion=OcclusionSpec(none_fraction=0, ratio=Range(.1, .5)))
    meshes = {target.asset.key: load_mesh(target.asset) for target in config.targets}
    candidate = propose_candidate(config, meshes, propose_scene(config, meshes, 0), 0, 0)
    assert isinstance(candidate, SceneCandidate) and not candidate.wants_unoccluded

    full = {2: np.zeros((48, 64), bool), 7: np.zeros((48, 64), bool)}
    full[2][10:18, 10:18] = True            # anchor silhouette, 8x8 = 64 px
    full[7][10:14, 10:14] = True            # the occluding quarter, 4x4 = 16 px
    visible = {obj_id: mask.copy() for obj_id, mask in full.items()}
    visible[2][10:14, 10:14] = False        # 16 of 64 anchor pixels are covered
    evidence = VisibilityEvidence(full[2], visible)

    root = reserve_dataset(config.output.root, config.output.dataset_name)
    writer = RgbMasksWriter(root, config)
    frame = RenderedFrame(np.zeros((48, 64, 3), np.uint8), None, full, visible)
    ref = writer.write(0, candidate, frame)
    recorder = JsonRunRecorder(root / "run_metadata")
    recorder.begin(config)
    recorder.record_frame(ref, candidate, evidence)
    recorder.finish(RunSummary("completed", 1, 1, 1, 0, {}, 0))
    return root, config


def test_checker_accepts_natural_occlusion_without_occluder_assets(tmp_path):
    root, _ = natural_occlusion_output(tmp_path)
    assert json.loads((root / "run_metadata/manifest.json").read_text())["frames"][0]["occlusion_ratio"] == .25
    assert verify_rgb_masks(root) == []


def test_checker_rejects_natural_occlusion_in_the_unoccluded_category(tmp_path):
    root, _ = natural_occlusion_output(tmp_path)
    def relabel(frames):
        frame = frames[0]
        frame.update(wants_unoccluded=True)
        frame["candidate"] = {**frame["candidate"], "wants_unoccluded": True}

    edit_frames(root, relabel)
    errors = verify_rgb_masks(root)
    assert any("measured center occlusion" in error for error in errors), errors


def write_rgb_masks(root, config, candidate, masks):
    writer = RgbMasksWriter(root, config)
    ref = writer.write(0, candidate, RenderedFrame(np.zeros((48, 64, 3), np.uint8), None, masks, masks))
    finish_record(config, root, candidate, ref, masks)


def fixed_camera_output(tmp_path, camera, regions):
    config = make_project(tmp_path / "inputs", output_format="rgb_masks")
    config = replace(config, scene_count=1, views_per_scene=1, camera=resize_camera(config.camera, 64, 48),
                     camera_mode="fixed", fixed_cameras=(camera,), regions=regions,
                     occlusion=OcclusionSpec(none_fraction=1))
    meshes = {target.asset.key: load_mesh(target.asset) for target in config.targets}
    candidate = propose_candidate(config, meshes, propose_scene(config, meshes, 0), 0, 0)
    assert isinstance(candidate, SceneCandidate) and candidate.camera_kind == "fixed"
    root = reserve_dataset(config.output.root, config.output.dataset_name)
    masks = {}
    for obj_id, (y, x) in {2: (10, 10), 7: (28, 40)}.items():
        masks[obj_id] = np.zeros((48, 64), dtype=bool)
        masks[obj_id][y:y+8, x:x+8] = True
    write_rgb_masks(root, config, candidate, masks)
    return root, candidate


FIXED = FixedCameraSpec(key="left", name="左侧", radius_m=.6, azimuth_deg=0., elevation_deg=0.)
SHARED_NAME_REGION = (RegionSpec(key="left", radius_m=Range(.9, .9), azimuth=Azimuth(0, 0, False),
                                 elevation_deg=Range(0, 0)),)


def test_fixed_camera_records_are_checked_against_the_fixed_namespace(tmp_path):
    root, candidate = fixed_camera_output(tmp_path, FIXED, SHARED_NAME_REGION)
    assert candidate.region_key == "left"
    # The region and the pinned camera share the name "left" and disagree on radius: only a
    # namespace-aware checker can accept this dataset.
    assert verify_rgb_masks(root) == []


@pytest.mark.parametrize("corruption", ["angle", "kind", "missing_camera"])
def test_fixed_camera_corruption_is_reported(tmp_path, corruption):
    root, _ = fixed_camera_output(tmp_path, FIXED, SHARED_NAME_REGION)

    def mutate(frames):
        candidate = frames[0]["candidate"]
        if corruption == "angle":
            candidate["camera"]["azimuth_deg"] = (candidate["camera"]["azimuth_deg"] + 10) % 360
        elif corruption == "kind":
            candidate["camera_kind"] = "random"
        else:
            candidate["region_key"] = "camera-404"

    edit_frames(root, mutate)
    errors = verify_rgb_masks(root)
    expected = {"angle": "differs from the fixed camera", "kind": "differs from project mode",
                "missing_camera": "is not in fixed_cameras"}[corruption]
    assert any(expected in error for error in errors), errors


def test_records_without_a_camera_kind_fall_back_to_random(valid_rgb_masks_output):
    root = valid_rgb_masks_output

    def strip_kind(frames):
        frames[0]["candidate"].pop("camera_kind", None)

    edit_frames(root, strip_kind)
    assert verify_rgb_masks(root) == [], "a record without camera_kind must validate as a random record"

    def unknown_region(frames):
        frames[0]["candidate"]["region_key"] = "camera-404"

    edit_frames(root, unknown_region)
    errors = verify_rgb_masks(root)
    assert any("is not in regions" in error for error in errors), errors


BOP_INFO_CORRUPTIONS = {
    "negative_valid": (lambda info: info[0].update(px_count_valid=-1), "px_count_valid -1 must stay within"),
    "valid_over_all": (lambda info: info[0].update(px_count_valid=info[0]["px_count_all"] + 5), "must stay within"),
    "visib_over_all": (lambda info: info[0].update(px_count_visib=info[0]["px_count_all"] + 5), "must stay within"),
    "bbox_width": (lambda info: info[0].update(bbox_obj=[info[0]["bbox_obj"][0], info[0]["bbox_obj"][1], info[0]["bbox_obj"][2] + 3, info[0]["bbox_obj"][3]]), "bbox_obj"),
    "bbox_visib": (lambda info: info[0].update(bbox_visib=[info[0]["bbox_visib"][0] + 4, info[0]["bbox_visib"][1], info[0]["bbox_visib"][2], info[0]["bbox_visib"][3]]), "bbox_visib"),
    "full_count": (lambda info: info[0].update(px_count_all=info[0]["px_count_all"] + 50, visib_fract=info[0]["px_count_visib"] / (info[0]["px_count_all"] + 50)), "px_count_all"),
    "visible_count": (lambda info: info[0].update(px_count_visib=0, visib_fract=0.0), "px_count_visib 0 differs"),
}


@pytest.mark.parametrize("corruption", sorted(BOP_INFO_CORRUPTIONS))
def test_bop_checker_recomputes_native_gt_info_counters(valid_bop_output, corruption):
    mutate, needle = BOP_INFO_CORRUPTIONS[corruption]
    edit_json(valid_bop_output / "train_pbr/000000/scene_gt_info.json", lambda data: mutate(data["0"]))
    errors = verify_bop(valid_bop_output)
    assert any(needle in error for error in errors), errors


@pytest.mark.parametrize("relative, expected", [
    ("../../../../etc/passwd", "must be a relative path inside the dataset"),
    ("train_pbr/000000/depth/000000.png", "does not name this scene/image's RGB"),
    ("train_pbr/000000/rgb/999999.png", "does not exist"),
])
def test_bop_checker_rejects_unsafe_or_wrong_rgb_references(valid_bop_output, relative, expected):
    edit_frames(valid_bop_output, lambda frames: frames[0].update(relative_rgb=relative))
    errors = verify_bop(valid_bop_output)
    assert any(expected in error for error in errors), errors


def test_bop_checker_reports_the_legacy_reference_format_as_incompatible(valid_bop_output):
    def legacy(frames):
        frame = frames[0]
        frame.pop("bop_scene_id")
        frame.pop("bop_image_id")
        frame["request_index"] = 0

    edit_frames(valid_bop_output, legacy)
    errors = verify_bop(valid_bop_output)
    assert any("旧 request_index" in error and "版本" in error for error in errors), errors


def test_candidate_records_using_request_index_are_reported_as_incompatible(valid_rgb_masks_output):
    def legacy_candidate(frames):
        candidate = frames[0]["candidate"]
        candidate["request_index"] = candidate.pop("scene_index")
        candidate.pop("view_index")

    edit_frames(valid_rgb_masks_output, legacy_candidate)
    errors = verify_rgb_masks(valid_rgb_masks_output)
    assert errors == ["旧 request_index 数据集格式不受支持（版本不兼容），请用当前版本重新导出"], errors


def _edit_depth(root, mutate):
    for path in sorted((root / "train_pbr/000000/depth").glob("*.png")):
        image = np.asarray(Image.open(path)).copy()
        mutate(image)
        Image.fromarray(image).save(path)


def _visible_mask(root):
    return np.asarray(Image.open(root / "train_pbr/000000/mask_visib/000000_000000.png")) > 0


def test_bop_checker_tolerates_a_small_silhouette_excursion(valid_bop_output):
    """An outline pixel may sample one depth step past the model bound, no further."""

    def nudge(image):
        mask = _visible_mask(valid_bop_output)
        y, x = np.argwhere(mask & (image > 0))[0]
        image[y, x] = image[y, x] + 1

    _edit_depth(valid_bop_output, nudge)
    assert verify_bop(valid_bop_output) == []


def test_bop_checker_rejects_an_interior_depth_outlier(valid_bop_output):
    """The silhouette allowance must not cover pixels inside the mask."""

    def corrupt(image):
        mask = _visible_mask(valid_bop_output)
        pixels = np.argwhere(mask & (image > 0))
        y, x = pixels[len(pixels) // 2]
        image[y, x] = 6553

    _edit_depth(valid_bop_output, corrupt)
    errors = verify_bop(valid_bop_output)
    assert _depth_problem(errors), errors


def test_bop_checker_counts_both_directions_against_one_budget(valid_bop_output):
    """Two outline pixels, one far and one near, exceed the shared one-percent budget."""

    def corrupt(image):
        mask = _visible_mask(valid_bop_output)
        pixels = np.argwhere(mask & (image > 0))
        (y, x), (y2, x2) = pixels[0], pixels[1]
        image[y, x] = 6553
        image[y2, x2] = 1

    _edit_depth(valid_bop_output, corrupt)
    errors = verify_bop(valid_bop_output)
    assert _depth_problem(errors), errors


def test_bop_checker_does_not_exempt_a_single_observed_depth(valid_bop_output):
    """A mask with one observed pixel gets no tolerance at all."""

    scene = valid_bop_output / "train_pbr/000000"
    mask = _visible_mask(valid_bop_output)
    y, x = np.argwhere(mask)[len(np.argwhere(mask)) // 2]

    def trim(image):
        single = np.zeros_like(image)
        single[y, x] = image[y, x] + 600
        image[:] = single

    _edit_depth(valid_bop_output, trim)
    edit_json(scene / "scene_gt_info.json", lambda data: data["0"][0].update(px_count_valid=1))
    errors = verify_bop(valid_bop_output)
    assert _depth_problem(errors), errors


def test_bop_checker_rejects_a_shifted_depth_map(valid_bop_output):
    def shift(image):
        positive = image > 0
        image[positive] = np.clip(image[positive].astype(np.int64) + 500, 1, 65535)

    _edit_depth(valid_bop_output, shift)
    errors = verify_bop(valid_bop_output)
    assert _depth_problem(errors), errors


def test_depth_rule_rejects_one_large_outline_error_on_a_big_mask():
    """A pixel past the rasteriser amplitude is never covered by the one-percent budget."""
    mask = np.ones((120, 160), bool)
    interior = _interior_mask(mask)
    depth = np.full((120, 160), .4)
    depth[0, 0] = 6.5  # outside the model band and past the amplitude limit
    problems = _depth_problems(depth, mask, interior, .35, .45, .01)
    assert "amplitude" in problems


def test_depth_rule_spends_one_budget_for_many_mild_outline_errors():
    """Outline excursions inside the amplitude limit still add up against one percent."""
    mask = np.zeros((10, 10), bool)
    mask[2:8, 2:8] = True          # 36 observed pixels -> allowance of 1
    interior = _interior_mask(mask)
    depth = np.full((10, 10), .4)
    depth[2, 2] = .345
    depth[2, 7] = .345
    problems = _depth_problems(depth, mask, interior, .35, .45, .01)
    assert "budget" in problems


def test_bop_checker_cli_entry_point_reports_pass_and_failure(valid_bop_output):
    """The script entry point must work without importing the module first."""
    repo = Path(__file__).resolve().parents[2]
    script = repo / "tests" / "blender" / "check_bop.py"
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}

    def run(root):
        return subprocess.run([sys.executable, str(script), str(root)], capture_output=True,
                              text=True, env=env)

    good = run(valid_bop_output)
    assert good.returncode == 0, good.stdout + good.stderr
    assert "BOP: PASS" in good.stdout
    assert "NameError" not in good.stderr

    def shift(image):
        positive = image > 0
        image[positive] = np.clip(image[positive].astype(np.int64) + 500, 1, 65535)

    _edit_depth(valid_bop_output, shift)
    bad = run(valid_bop_output)
    assert bad.returncode == 1
    assert "NameError" not in bad.stderr
    assert "PASS" not in bad.stdout


def test_checkers_refuse_a_completed_run_without_a_final_manifest(valid_rgb_masks_output):
    """A completed dataset must ship a schema 2 manifest with final=true and matching rows."""
    manifest = valid_rgb_masks_output / "run_metadata" / "manifest.json"
    original = json.loads(manifest.read_text())

    def rewrite(mutate):
        data = json.loads(json.dumps(original))
        mutate(data)
        manifest.write_text(json.dumps(data))

    cases = {
        "final=False": lambda data: data.update(final=False),
        "final=False with no frames": lambda data: data.update(final=False, frames=[]),
        "missing final": lambda data: data.pop("final", None),
        "wrong first row": lambda data: data["frames"][0].update(scene_index=99),
    }
    for name, mutate in cases.items():
        rewrite(mutate)
        diagnostics = verify_rgb_masks(valid_rgb_masks_output)
        assert diagnostics, name
        assert any("final" in error or "不一致" in error or "最终快照" in error or "scene_index" in error
                   for error in diagnostics), (name, diagnostics)

    manifest.write_text(json.dumps(original))
    assert verify_rgb_masks(valid_rgb_masks_output) == []


def test_a_completed_legacy_manifest_without_a_journal_is_accepted(valid_rgb_masks_output):
    """Version 0/1 manifests predate the journal: they are a format, not corrupt data."""
    folder = valid_rgb_masks_output / "run_metadata"
    (folder / "frames.jsonl").unlink()
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest.pop("schema_version", None)
    manifest.pop("final", None)
    (folder / "manifest.json").write_text(json.dumps(manifest))

    assert verify_rgb_masks(valid_rgb_masks_output) == []


def test_an_unknown_manifest_version_is_rejected_even_without_a_journal(valid_rgb_masks_output):
    """Accepting legacy versions must not turn into accepting anything without a journal."""
    folder = valid_rgb_masks_output / "run_metadata"
    (folder / "frames.jsonl").unlink()
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["schema_version"] = 3
    (folder / "manifest.json").write_text(json.dumps(manifest))

    diagnostics = verify_rgb_masks(valid_rgb_masks_output)
    assert any("版本" in error for error in diagnostics), diagnostics
