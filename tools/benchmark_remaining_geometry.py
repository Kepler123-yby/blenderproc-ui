#!/usr/bin/env python3
"""Measure the real geometry boundaries: viewport LOD and streaming silhouettes.

`--case lod` builds a >= 1,000,000-face fixture, runs the production LOD controller in its
background thread, keeps a 10 ms main-thread heartbeat, and captures the two real VisPy
viewports on the desktop. `--case silhouette` builds a > 2,000,000-face hollow fixture and
measures the bounded coverage path. Nothing here mocks the simplification or the rasteriser.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np


def sphere(rings: int, steps: int) -> tuple[np.ndarray, np.ndarray]:
    u = np.linspace(0, 2 * np.pi, steps, endpoint=False)
    v = np.linspace(-np.pi / 2, np.pi / 2, rings)
    grid_u, grid_v = np.meshgrid(u, v)
    points = np.column_stack([np.cos(grid_v).ravel() * np.cos(grid_u).ravel(),
                              np.cos(grid_v).ravel() * np.sin(grid_u).ravel(),
                              np.sin(grid_v).ravel()])
    index = np.arange(rings * steps).reshape(rings, steps)
    a, b = index[:-1, :], np.roll(index[:-1, :], -1, axis=1)
    c, d = np.roll(index[1:, :], -1, axis=1), index[1:, :]
    faces = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3),
                            np.stack([a, c, d], -1).reshape(-1, 3)]).astype(np.int64)
    return points.astype(np.float64), faces


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _frame(canvas) -> np.ndarray:
    image = canvas.render()
    image = np.asarray(image)
    if image.ndim == 3 and image.shape[2] == 4:
        image = image[..., :3]
    return image


def _non_background(image: np.ndarray, background=(0x39, 0x39, 0x39)) -> float:
    delta = np.abs(image.astype(np.int16) - np.asarray(background, dtype=np.int16)).max(axis=2)
    return float(np.count_nonzero(delta > 8)) / float(delta.size)


def lod_case(output: Path) -> int:
    from PySide6.QtCore import QEventLoop, QTimer
    from bproc_core.assets import MeshData
    from bproc_core.camera import CameraPose
    from bproc_core.config import new_project
    from bproc_core.scene import LightState, Placement, SceneCandidate
    from bproc_ui.app import create_app
    from bproc_ui.main_window import MainWindow
    from bproc_ui.state import ProjectState
    from vispy.io import write_png

    output.mkdir(parents=True, exist_ok=False)
    points, faces = sphere(501, 1000)
    assert len(faces) >= 1_000_000, len(faces)
    fixture = output / "lod-fixture.npz"
    np.savez_compressed(fixture, vertices=points, faces=faces)
    fixture_hash = _sha256(fixture)
    mesh = MeshData("fixture", points, faces, None,
                    np.array([points.min(axis=0), points.max(axis=0)]))
    pose = CameraPose(np.array([[1., 0, 0, 0], [0, 1., 0, 0], [0, 0, 1., 2.], [0, 0, 0, 1.]]),
                      np.array([0., 0., 2.]), 2., -90., 0., 0.002, 100.)
    candidate = SceneCandidate(0, 0, 0, "region-1", "fixture", pose,
                               (Placement("fixture", "fixture", 1, np.eye(4), "target"),),
                               None, 1., 0., LightState(np.array([1., 1., 2.]), 100., (1., 1., 1.)),
                               False)
    app = create_app([])
    project = new_project(output / "renders")
    state = ProjectState(project)
    state.set_meshes({})
    window = MainWindow(state, restore_settings=False)
    # No project targets here: the fixture is injected directly, so the preview's empty-project
    # path must not write a null candidate over the benchmark scene.
    window.preview.close()
    window.resize(1280, 860)
    window.show()
    window.camera_preview.resize(520, 440)
    window.camera_preview.show_preview()
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()

    beats: list[float] = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(time.perf_counter()))
    started = time.perf_counter()
    timer.start()
    state.set_meshes({"fixture": mesh})
    deadline = started + 120.0
    while time.perf_counter() < deadline:
        app.processEvents()
        display = window._display_meshes.get("fixture")
        if display is not None and not window._pending:
            break
        time.sleep(0.002)
    elapsed = time.perf_counter() - started
    timer.stop()
    gaps = np.diff(beats) if len(beats) > 1 else np.array([0.0])
    display = window._display_meshes.get("fixture")
    if display is None:
        raise SystemExit("LOD did not finish")
    if state.config.viewport.lod_mode != "auto":
        raise SystemExit("benchmark must run in auto mode")

    state.set_candidate(candidate)
    window.camera_preview.resize(520, 440)
    window.camera_preview.show_preview()
    app.processEvents()
    window.camera_preview.letterbox._place()
    app.processEvents()
    time.sleep(0.5)
    app.processEvents()
    print("camera canvas size", window.camera_preview.canvas.native.size(), flush=True)
    auto_status = window.lod_status.text()
    auto_frame_scene = _frame(window.scene_view.canvas)
    auto_frame_camera = _frame(window.camera_preview.canvas)
    write_png(str(output / "scene-view-auto.png"), auto_frame_scene)
    write_png(str(output / "camera-view-auto.png"), auto_frame_camera)
    window.grab().save(str(output / "window-auto.png"))

    window.lod_mode.setCurrentIndex(window.lod_mode.findData("full"))
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()
    full_status = window.lod_status.text()
    full_display = window._display_meshes["fixture"]
    full_frame = _frame(window.scene_view.canvas)
    write_png(str(output / "scene-view-full.png"), full_frame)
    window.grab().save(str(output / "window-full.png"))
    window.lod_mode.setCurrentIndex(window.lod_mode.findData("auto"))
    app.processEvents()

    result = {
        "case": "lod",
        "fixture": {"path": str(fixture), "sha256": fixture_hash,
                    "input_faces": int(len(faces)), "input_vertices": int(len(points)),
                    "input_bytes": int(points.nbytes + faces.nbytes)},
        "simplify": display.as_dict(),
        "wall_seconds": elapsed,
        "heartbeat": {"interval_ms": 10, "beats": len(beats),
                      "max_gap_ms": float(gaps.max() * 1000.0)},
        "rss_mib": peak_rss_mib(),
        "status": {"auto": auto_status, "full": full_status},
        "full": {"faces": int(len(full_display.faces)), "simplified": bool(full_display.simplified)},
        "screenshots": {
            "scene_auto": {"path": str(output / "scene-view-auto.png"),
                           "non_background": _non_background(auto_frame_scene)},
            "camera_auto": {"path": str(output / "camera-view-auto.png"),
                            "non_background": _non_background(auto_frame_camera)},
            "scene_full": {"path": str(output / "scene-view-full.png"),
                           "non_background": _non_background(full_frame)},
            "window_auto": str(output / "window-auto.png"),
            "window_full": str(output / "window-full.png"),
        },
        "display": {"width": int(window.width()), "height": int(window.height())},
        "command": " ".join(sys.argv),
    }
    _write_json(output / "lod.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    window._closing = True
    window.camera_preview.hide()
    window.close()
    app.quit()
    ok = (result["fixture"]["input_faces"] >= 1_000_000
          and 0 < display.display_faces <= 50_000
          and elapsed <= 60.0
          and result["heartbeat"]["max_gap_ms"] <= 250.0
          and result["screenshots"]["scene_auto"]["non_background"] > 0.01
          and result["screenshots"]["camera_auto"]["non_background"] > 0.01
          and "视口 LOD" in auto_status and "原始网格" in full_status)
    return 0 if ok else 2


def hollow_fixture(radial=1002, angular=1000, inner=.10, outer=.30):
    rings = np.linspace(inner, outer, radial)
    angles = np.linspace(0, 2 * np.pi, angular, endpoint=False)
    radius, theta = np.meshgrid(rings, angles, indexing="ij")
    points = np.column_stack([(radius * np.cos(theta)).ravel(),
                              (radius * np.sin(theta)).ravel(),
                              np.zeros(radial * angular)])
    index = np.arange(radial * angular).reshape(radial, angular)
    a, b = index[:-1, :], np.roll(index[:-1, :], -1, axis=1)
    c, d = np.roll(index[1:, :], -1, axis=1), index[1:, :]
    faces = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3),
                            np.stack([a, c, d], -1).reshape(-1, 3)]).astype(np.int64)
    return points, faces


def silhouette_case(output: Path) -> int:
    import tracemalloc
    from dataclasses import replace
    from PIL import Image
    from bproc_core.assets import MeshData
    from bproc_core.camera import CameraPose
    from bproc_core import silhouette as sil
    from bproc_core import scene as scene_module
    from bproc_core.config import (CameraSpec, OcclusionSpec, OutputSpec, ProjectConfig, Range,
                                   TargetSpec, AssetSpec, Box)
    from bproc_core.scene import Placement, occluder_placements
    from tests.factories import make_box

    output.mkdir(parents=True, exist_ok=False)
    points, faces = hollow_fixture()
    assert len(faces) > 2_000_000
    fixture = output / "hollow-fixture.npz"
    np.savez_compressed(fixture, vertices=points, faces=faces)
    mesh = MeshData("hollow", points, faces, None,
                    np.array([points.min(axis=0), points.max(axis=0)]))
    camera = CameraSpec()
    pose = CameraPose(np.array([[1., 0, 0, 0], [0, 1., 0, 0], [0, 0, 1., 1.], [0, 0, 0, 1.]]),
                      np.array([0., 0., 1.]), 1., 0., 90., 1e-3, 100.)
    grid = scene_module._silhouette_grid([(mesh, np.eye(4))], pose, camera)

    rss_before = peak_rss_mib()
    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    mask, diagnostic = sil.stream_coverage(mesh, np.eye(4), pose, camera, grid)
    cold_seconds = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = peak_rss_mib()
    if mask is None:
        raise SystemExit(diagnostic.fallback_reason)
    started = time.perf_counter()
    cached_mask, cached_diagnostic = sil.stream_coverage(mesh, np.eye(4), pose, camera, grid)
    cached_seconds = time.perf_counter() - started
    assert cached_mask is mask and cached_diagnostic.cached

    def cell(point):
        from bproc_core.camera import project_points
        pixels, valid = project_points(np.atleast_2d(point), pose, camera)
        assert valid.all()
        mapped = grid.map(pixels)[0]
        return int(round(mapped[1])), int(round(mapped[0]))

    row, column = cell((0., 0., 0.))
    hole_empty = not bool(mask[row - 4:row + 5, column - 4:column + 5].any())
    row, column = cell((.2, 0., 0.))
    solid_covered = bool(mask[row - 3:row + 4, column - 3:column + 4].all())
    Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(output / "hole-mask-256.png")

    # 30 CPU proposals scored against an independent 512x512 raster of the same geometry.
    occluder = make_box("occ", (-.02, -.02, -.02), (.02, .02, .02))
    target = TargetSpec(AssetSpec("hollow", output / "hollow.ply", "m"), "hollow", 2, True)
    config = ProjectConfig(
        targets=(target,), anchor_key="hollow", camera_mode="fixed",
        fixed_cameras=(), regions=(), scene_count=1, views_per_scene=1,
        camera=camera,
        occluders=(AssetSpec("occ", output / "occ.ply", "m"),),
        occlusion=OcclusionSpec(ratio=Range(0.10, 0.60), max_attempts=30, count=(1, 1),
                                scale=Range(0.30, 0.60)),
        output=OutputSpec(root=output / "renders", dataset_name="silhouette"))
    config = replace(config, fixed_cameras=(), regions=())
    meshes = {"hollow": mesh, "occ": occluder}
    placements = (Placement("hollow", "hollow", 2, np.eye(4), "target"),)
    low_grid = grid
    low_uv = np.array([low_grid.left_px, low_grid.top_px])
    high_uv = low_uv + (low_grid.size - 1) / low_grid.pixels_per_px
    high_scale = (512 - 5) / float(np.max(high_uv - low_uv))
    high_pose_grid = sil.CoverageGrid(float(low_uv[0] - 2 / high_scale),
                                      float(low_uv[1] - 2 / high_scale), high_scale, 512)
    high_target, _ = sil.stream_coverage(mesh, np.eye(4), pose, camera, high_pose_grid)
    proposals = []
    rejected = 0
    rng = np.random.default_rng(7)
    target_mask_512 = high_target
    for index in range(30):
        for _attempt in range(20):
            result = occluder_placements(config, meshes, pose, "hollow", rng, targets=placements)
            if not isinstance(result, list) or not result:
                rejected += 1
                continue
            occluder_placement = result[0]
            occ_mask, _ = sil.stream_coverage(occluder, occluder_placement.world_from_model,
                                              pose, camera, high_pose_grid)
            share = (float(np.count_nonzero(target_mask_512 & occ_mask))
                     / float(np.count_nonzero(target_mask_512))) if occ_mask is not None else 0.0
            proposals.append({"index": index, "coverage_512": share,
                              "inside_declared_window": 0.10 - 1e-12 <= share <= 0.60 + 1e-12,
                              "occluder_offset_m": occluder_placement.world_from_model[:3, 3].tolist()})
            break
        else:
            proposals.append({"index": index, "coverage_512": None,
                              "inside_declared_window": False, "error": "no placement"})
    passed = sum(1 for row in proposals if row["inside_declared_window"])
    Image.fromarray((target_mask_512.astype(np.uint8) * 255), mode="L").save(
        output / "hole-mask-512.png")
    result = {
        "case": "silhouette",
        "fixture": {"vertices": int(len(points)), "faces": int(len(faces)),
                    "canonical_bytes": int(points.nbytes + faces.nbytes),
                    "path": str(fixture)},
        "cold": {"seconds": cold_seconds, "diagnostic": diagnostic.as_dict(),
                 "working_peak_mib": peak / (1024 * 1024)},
        "cached": {"seconds": cached_seconds},
        "rss": {"before_mib": rss_before, "after_mib": rss_after,
                "extra_mib": max(0.0, rss_after - rss_before)},
        "hole": {"centre_empty": hole_empty, "solid_covered": solid_covered,
                 "mask_256": str(output / "hole-mask-256.png"),
                 "mask_512": str(output / "hole-mask-512.png")},
        "proposals": proposals,
        "proposal_summary": {"requested": 30, "measured": len(proposals),
                             "inside_window": passed, "sampler_rejections": rejected,
                             "window": [0.10, 0.60], "high_res_grid": 512},
        "command": " ".join(sys.argv),
    }
    _write_json(output / "silhouette.json", result)
    print(json.dumps({k: result[k] for k in ("fixture", "cold", "cached", "rss", "hole",
                                             "proposal_summary")}, ensure_ascii=False, indent=2))
    ok = (len(faces) > 2_000_000 and cold_seconds <= 120.0
          and peak <= sil.STREAM_WORK_BUDGET_BYTES
          and max(0.0, rss_after - rss_before) <= 256.0
          and hole_empty and solid_covered and passed == 30)
    return 0 if ok else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["lod", "silhouette"], required=True)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    args = parser.parse_args(argv)
    output = args.output.expanduser().absolute()
    if output.exists():
        parser.error(f"output already exists: {output}")
    if args.case == "lod":
        if os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
            parser.error("LOD benchmark needs a real desktop OpenGL context (DISPLAY=:1)")
        return lod_case(output)
    return silhouette_case(output)


if __name__ == "__main__":
    raise SystemExit(main())
