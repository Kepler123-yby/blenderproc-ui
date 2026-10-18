"""Standalone structural and numeric validation of a completed BOP dataset.

The counters follow the native ``BopWriterUtility.calc_gt_info`` rules: ``px_count_all`` is the
full silhouette, ``px_count_valid`` counts pixels that also have observed depth, and
``px_count_visib`` is the visible mask. ``visib`` is therefore not bounded by ``valid`` -- native
BOP keeps silhouette edge pixels whose depth is zero, and the legal fixtures contain them.
"""
from pathlib import Path
import json
import sys
import numpy as np
from PIL import Image
import trimesh

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.blender._checks import verify_metadata, read_json
from bproc_core.assets import load_mesh, mesh_diameter_m


def _xywh(mask: np.ndarray):
    """BOP ``[x, y, w, h]`` of a boolean mask, or None when empty.

    BlenderProc writes the bounding box as ``max - min`` (the span between the outer pixel
    centres). Measuring ``max - min + 1`` made every bbox look one pixel too narrow, so the
    replay on this dataset reported a mismatch on every frame.
    """
    rows, cols = np.nonzero(mask)
    if not rows.size:
        return None
    return [int(cols.min()), int(rows.min()), int(cols.max() - cols.min()), int(rows.max() - rows.min())]


def _touches_border(mask: np.ndarray) -> bool:
    return bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any())


def _check_gt_info(label: str, facts: dict, full: np.ndarray, visible: np.ndarray,
                   observed: np.ndarray) -> list[str]:
    """Re-derive every ``scene_gt_info`` field from the masks and the rendered depth."""
    errors = []
    all_count = int(full.sum())
    valid = int((full & observed).sum())
    visib = int(visible.sum())
    for name, expected in (("px_count_all", all_count), ("px_count_valid", valid), ("px_count_visib", visib)):
        recorded = facts.get(name)
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            errors.append(f"{label}: {name} must be an integer")
        elif recorded < 0 or recorded > all_count:
            errors.append(f"{label}: {name} {recorded} must stay within [0, {all_count}]")
        elif recorded != expected:
            errors.append(f"{label}: {name} {recorded} differs from the masks' {expected}")
    if all_count:
        fraction = facts.get("visib_fract")
        if not isinstance(fraction, (int, float)) or not np.isclose(fraction, visib / all_count, atol=1e-6):
            errors.append(f"{label}: visib_fract {fraction} differs from px_count_visib / px_count_all")
    expected_visib = _xywh(visible)
    if facts.get("bbox_visib") != expected_visib:
        errors.append(f"{label}: bbox_visib {facts.get('bbox_visib')} differs from the visible mask's {expected_visib}")
    expected_object = _xywh(full)
    # A silhouette clipped by the viewport border has no self-evident bbox; native BOP would
    # need the unclipped projection, so it is only compared when the mask is fully inside.
    if not _touches_border(full) and facts.get("bbox_obj") != expected_object:
        errors.append(f"{label}: bbox_obj {facts.get('bbox_obj')} differs from the full mask's {expected_object}")
    return errors


def _check_images(root: Path, expected_ids, errors: list[str]) -> dict:
    root = Path(root)
    model_info = read_json(root / "models/models_info.json", errors)
    if set(model_info) != {str(i) for i in expected_ids}:
        errors.append(f"models_info must contain exactly the enabled targets {sorted(expected_ids)}")
    models = {}
    for obj_id in expected_ids:
        if not (root / "models" / f"obj_{obj_id:06d}.ply").is_file():
            errors.append(f"Object {obj_id}: canonical model file obj_{obj_id:06d}.ply is missing")
            continue
        try:
            if model_info[str(obj_id)]["diameter"] <= 0:
                errors.append(f"Object {obj_id}: models_info diameter must be positive")
            models[obj_id] = np.asarray(trimesh.load(root / "models" / f"obj_{obj_id:06d}.ply",
                                                    process=False).vertices)
        except Exception as exc:
            errors.append(f"Object {obj_id}: cannot read canonical model: {type(exc).__name__}: {exc}")
    frames = 0
    for chunk in sorted((root / "train_pbr").glob("[0-9]*")):
        try:
            gt = json.loads((chunk / "scene_gt.json").read_text())
            camera = json.loads((chunk / "scene_camera.json").read_text())
            info = json.loads((chunk / "scene_gt_info.json").read_text())
        except Exception as exc:
            errors.append(f"BOP scene {chunk.name}: unreadable metadata: {type(exc).__name__}: {exc}")
            continue
        if set(gt) != set(camera) or set(gt) != set(info):
            errors.append(f"BOP scene {chunk.name}: scene_gt, scene_camera and scene_gt_info disagree on images")
        for key, poses in gt.items():
            label = f"BOP scene {chunk.name} image {key}"
            try:
                im_id = int(key)
                if [p["obj_id"] for p in poses] != list(expected_ids):
                    errors.append(f"{label}: ground-truth objects {[p['obj_id'] for p in poses]} differ from {list(expected_ids)}")
                if len(info.get(key, ())) != len(poses):
                    errors.append(f"{label}: scene_gt_info has {len(info.get(key, ()))} rows for {len(poses)} poses")
                depth = np.asarray(Image.open(chunk / "depth" / f"{im_id:06d}.png"))
                if depth.ndim != 2:
                    errors.append(f"{label}: depth must be a single channel")
                if not np.any(depth > 0):
                    errors.append(f"{label}: depth contains no observed pixels")
                recovered_m = depth.astype(float) * camera[key]["depth_scale"] / 1000
                if recovered_m.max() >= 100:
                    errors.append(f"{label}: depth exceeds 100 m after applying depth_scale")
                rgb_paths = [*(chunk / "rgb").glob(f"{im_id:06d}.*")]
                if len(rgb_paths) != 1:
                    errors.append(f"{label}: expected exactly one RGB file, found {len(rgb_paths)}")
                elif np.asarray(Image.open(rgb_paths[0])).shape[:2] != depth.shape:
                    errors.append(f"{label}: RGB size differs from depth")
                for index, pose in enumerate(poses):
                    entry = f"{label} object {pose['obj_id']}"
                    rotation = np.array(pose["cam_R_m2c"]).reshape(3, 3)
                    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5):
                        errors.append(f"{entry}: cam_R_m2c is not a rotation matrix")
                    if not pose["cam_t_m2c"][2] > 0:
                        errors.append(f"{entry}: object origin must be in front of the camera")
                    full = np.asarray(Image.open(chunk / "mask" / f"{im_id:06d}_{index:06d}.png"))
                    visible = np.asarray(Image.open(chunk / "mask_visib" / f"{im_id:06d}_{index:06d}.png"))
                    if full.shape != visible.shape or full.shape != depth.shape:
                        errors.append(f"{entry}: mask, mask_visib and depth differ in size")
                        continue
                    if not np.any(full) or not np.any(visible):
                        errors.append(f"{entry}: masks must contain pixels")
                    if not set(np.unique(full)) <= {0, 255} or not set(np.unique(visible)) <= {0, 255}:
                        errors.append(f"{entry}: masks must be binary 0/255 images")
                    if not np.all(visible <= full):
                        errors.append(f"{entry}: visible pixels must be a subset of the full silhouette")
                    full_bool, visible_bool = full > 0, visible > 0
                    if index < len(info.get(key, ())):
                        errors.extend(_check_gt_info(f"{entry} gt_info", info[key][index], full_bool,
                                                     visible_bool, depth > 0))
                    # Canonical model coordinates and pose translations must use the same
                    # millimetre convention, including displaced CAD origins.
                    if pose["obj_id"] not in models:
                        continue
                    xyz = models[pose["obj_id"]] @ rotation.T + pose["cam_t_m2c"]
                    if not np.all(xyz[:, 2] > 0):
                        errors.append(f"{entry}: canonical model vertices must lie in front of the camera")
                    K = np.array(camera[key]["cam_K"]).reshape(3, 3)
                    homogeneous = xyz @ K.T
                    uv = homogeneous[:, :2] / homogeneous[:, 2, None]
                    rows, cols = np.nonzero(full_bool)
                    if rows.size and not np.allclose([cols.min(), rows.min(), cols.max(), rows.max()],
                                                     [uv[:, 0].min(), uv[:, 1].min(), uv[:, 0].max(), uv[:, 1].max()],
                                                     atol=2.5):
                        errors.append(f"{entry}: projected canonical model does not match the full mask")
                frames += 1
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
    return {"frames": frames, "object_ids": list(expected_ids)}


def _check_relative_rgb(root: Path, row: dict, scene: Path, image_id: int, pair, errors: list[str]) -> None:
    relative = row.get("relative_rgb")
    if relative is None:
        return
    path = Path(relative)
    resolved = (root / path).resolve()
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"BOP {pair}: relative_rgb {relative!r} must be a relative path inside the dataset")
    elif root.resolve() not in resolved.parents:
        errors.append(f"BOP {pair}: relative_rgb {relative!r} escapes the dataset")
    elif not resolved.is_file():
        errors.append(f"BOP {pair}: relative_rgb {relative!r} does not exist")
    elif resolved.parent != (scene / "rgb").resolve() or resolved.stem != f"{image_id:06d}":
        errors.append(f"BOP {pair}: relative_rgb {relative!r} does not name this scene/image's RGB")


def verify_bop(root: Path) -> list[str]:
    """Validate native BOP structure, geometry, units and run provenance."""
    root = Path(root)
    errors, config, status, records = verify_metadata(root)
    if config is None:
        return errors
    try:
        if config.output.format != "bop":
            errors.append("Project output format is not bop")
        targets = {t.obj_id: t for t in config.targets if t.enabled}
        expected_ids = tuple(sorted(targets))
        try:
            stats = _check_images(root, expected_ids, errors)
            if stats["frames"] != status.get("saved"):
                errors.append("BOP RGB/pose frame count differs from saved counter")
        except Exception as exc:
            errors.append(f"BOP images/projection: {type(exc).__name__}: {exc or 'structure, mask or canonical projection mismatch'}")
        model_info = read_json(root / "models/models_info.json", errors)
        models = {}
        for obj_id, target in targets.items():
            try:
                canonical = load_mesh(target.asset)
                exported = trimesh.load(root / "models" / f"obj_{obj_id:06d}.ply", process=False)
                vertices = np.asarray(exported.vertices)
                models[obj_id] = vertices
                if vertices.shape != canonical.vertices_m.shape or not np.allclose(vertices, canonical.vertices_m * 1000, atol=1e-5, rtol=1e-7):
                    errors.append(f"Object {obj_id}: exported millimeter vertices changed canonical origin/scale")
                if not np.array_equal(exported.faces, canonical.faces):
                    errors.append(f"Object {obj_id}: exported canonical triangle topology differs")
                item = model_info[str(obj_id)]
                for i, axis in enumerate("xyz"):
                    if not np.isclose(item[f"min_{axis}"], canonical.bounds_m[0, i] * 1000, atol=1e-5) or not np.isclose(item[f"size_{axis}"], np.ptp(canonical.bounds_m[:, i]) * 1000, atol=1e-5):
                        errors.append(f"Object {obj_id}: models_info bounds differ from canonical millimeter model")
                if not np.isclose(item["diameter"], mesh_diameter_m(canonical) * 1000, atol=1e-5):
                    errors.append(f"Object {obj_id}: models_info diameter differs from canonical model")
            except Exception as exc:
                errors.append(f"Object {obj_id}: cannot validate canonical BOP model: {exc}")
        gt_frame_count = 0
        referenced_frames = set()
        for row in records:
            try:
                if "bop_scene_id" not in row or "bop_image_id" not in row:
                    if "request_index" in row:
                        errors.append(f"BOP frame {row.get('frame_index', '?')}: 旧 request_index 数据集格式不受支持，请用当前版本重新导出")
                    else:
                        errors.append(f"BOP frame {row.get('frame_index', '?')}: missing bop_scene_id / bop_image_id")
                    continue
                scene_id, image_id = row["bop_scene_id"], row["bop_image_id"]
                if isinstance(scene_id, bool) or not isinstance(scene_id, int) or isinstance(image_id, bool) or not isinstance(image_id, int):
                    raise ValueError("BOP scene/image references must be integers")
                pair = (scene_id, image_id)
                if pair in referenced_frames:
                    errors.append(f"Duplicate BOP image reference: {pair}")
                referenced_frames.add(pair)
                scene = root / "train_pbr" / f"{scene_id:06d}"
                key = str(image_id)
                gt = json.loads((scene / "scene_gt.json").read_text(encoding="utf-8"))[key]
                camera = json.loads((scene / "scene_camera.json").read_text(encoding="utf-8"))[key]
                info = json.loads((scene / "scene_gt_info.json").read_text(encoding="utf-8"))[key]
                _check_relative_rgb(root, row, scene, image_id, pair, errors)
                expected_K = [config.camera.fx, 0, config.camera.cx, 0, config.camera.fy, config.camera.cy, 0, 0, 1]
                if not np.allclose(camera["cam_K"], expected_K, atol=1e-4):
                    errors.append(f"BOP {pair}: camera K differs from project")
                if not np.isclose(camera["depth_scale"], config.output.depth_scale_mm):
                    errors.append(f"BOP {pair}: depth_scale differs from configured millimeters per PNG step")
                candidate = row["candidate"]
                world_from_camera = np.asarray(candidate["camera"]["world_from_camera"], dtype=float)
                cv_from_world = np.diag([1, -1, -1, 1]) @ np.linalg.inv(world_from_camera)
                placements = {p["obj_id"]: p for p in candidate["placements"] if p["role"] == "target"}
                with Image.open(scene / "depth" / f"{image_id:06d}.png") as image:
                    depth_m = np.asarray(image).astype(float) * camera["depth_scale"] / 1000
                for index, pose in enumerate(gt):
                    obj_id = pose["obj_id"]
                    expected = cv_from_world @ np.asarray(placements[obj_id]["world_from_model"])
                    rotation = np.asarray(pose["cam_R_m2c"]).reshape(3, 3)
                    translation_mm = np.asarray(pose["cam_t_m2c"])
                    if not np.allclose(rotation, expected[:3, :3], atol=1e-5) or not np.allclose(translation_mm, expected[:3, 3] * 1000, atol=1e-3, rtol=1e-6):
                        errors.append(f"BOP {pair} object {obj_id}: pose does not match canonical candidate in millimeters")
                    visible = np.asarray(Image.open(scene / "mask_visib" / f"{image_id:06d}_{index:06d}.png")) > 0
                    facts = info[index]
                    if facts["px_count_visib"] != int(visible.sum()):
                        errors.append(f"BOP {pair} object {obj_id}: gt_info visible count differs from mask")
                    total = facts["px_count_all"]
                    if total <= 0 or not np.isclose(facts["visib_fract"], facts["px_count_visib"] / total):
                        errors.append(f"BOP {pair} object {obj_id}: inconsistent native visible fraction")
                    if obj_id in models and visible.any():
                        points_cv_mm = models[obj_id] @ rotation.T + translation_mm
                        # Native BOP visibility includes some pixels with missing
                        # observed depth; zero means unavailable, never zero meters.
                        measured = depth_m[visible & (depth_m > 0)]
                        margin_m = max(.001, config.output.depth_scale_mm / 1000 * 2)
                        lowest = points_cv_mm[:, 2].min() / 1000 - margin_m
                        highest = points_cv_mm[:, 2].max() / 1000 + margin_m
                        if not measured.size:
                            errors.append(f"BOP {pair} object {obj_id}: no positive observed depth in visible mask")
                        else:
                            # The mask comes from the rasterised silhouette, so pixels on its outer
                            # edge may sample a fraction beyond the analytic model bound. One percent
                            # of the visible mask is allowed to differ; a unit error moves them all.
                            allowed = max(1, int(measured.size * .01))
                            below = int((measured < lowest).sum())
                            above = int((measured > highest).sum())
                            if below > allowed or above > allowed:
                                errors.append(f"BOP {pair} object {obj_id}: PNG depth and model pose use inconsistent units")
                gt_frame_count += 1
            except Exception as exc:
                errors.append(f"BOP frame {row.get('frame_index', '?')}: {type(exc).__name__}: {exc}")
        if gt_frame_count != len(records):
            errors.append("Not every manifest BOP frame could be validated")
    except Exception as exc:
        errors.append(f"Malformed BOP dataset: {type(exc).__name__}: {exc}")
    return errors


def check_bop(root: Path, expected_ids=None) -> dict:
    """Backwards-compatible raising wrapper used by the initial smoke script."""
    diagnostics = verify_bop(root)
    if diagnostics:
        raise AssertionError("\n".join(diagnostics))
    _, config, status, _ = verify_metadata(root)
    ids = sorted(t.obj_id for t in config.targets if t.enabled)
    if expected_ids is not None and ids != list(expected_ids):
        raise AssertionError(f"Expected target IDs {expected_ids}; found {ids}")
    return {"frames": status["saved"], "object_ids": ids}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    diagnostics = verify_bop(parser.parse_args().dataset)
    print("\n".join(diagnostics) if diagnostics else "BOP: PASS")
    raise SystemExit(1 if diagnostics else 0)
