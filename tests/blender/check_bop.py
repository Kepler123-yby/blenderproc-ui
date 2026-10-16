"""Standalone structural and numeric validation of a completed BOP dataset."""
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


def _check_images_and_projection(root: Path, expected_ids=(2, 7)) -> dict:
    root = Path(root)
    model_info = json.loads((root / "models/models_info.json").read_text())
    assert set(model_info) == {str(i) for i in expected_ids}
    models = {}
    for obj_id in expected_ids:
        assert (root / "models" / f"obj_{obj_id:06d}.ply").is_file()
        assert model_info[str(obj_id)]["diameter"] > 0
        models[obj_id] = np.asarray(trimesh.load(root / "models" / f"obj_{obj_id:06d}.ply", process=False).vertices)
    frames = 0
    for chunk in sorted((root / "train_pbr").glob("[0-9]*")):
        gt = json.loads((chunk / "scene_gt.json").read_text())
        camera = json.loads((chunk / "scene_camera.json").read_text())
        info = json.loads((chunk / "scene_gt_info.json").read_text())
        assert set(gt) == set(camera) == set(info)
        for key, poses in gt.items():
            im_id = int(key)
            assert [p["obj_id"] for p in poses] == list(expected_ids)
            assert len(info[key]) == len(poses)
            depth = np.array(Image.open(chunk / "depth" / f"{im_id:06d}.png"))
            assert depth.ndim == 2 and np.any(depth > 0)
            recovered_m = depth.astype(float) * camera[key]["depth_scale"] / 1000
            assert recovered_m.max() < 100
            rgb_paths = [* (chunk / "rgb").glob(f"{im_id:06d}.*")]
            assert len(rgb_paths) == 1
            assert np.asarray(Image.open(rgb_paths[0])).shape[:2] == depth.shape
            for index, pose in enumerate(poses):
                rotation = np.array(pose["cam_R_m2c"]).reshape(3,3)
                np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), atol=1e-5)
                assert pose["cam_t_m2c"][2] > 0
                full = np.asarray(Image.open(chunk / "mask" / f"{im_id:06d}_{index:06d}.png"))
                visible = np.asarray(Image.open(chunk / "mask_visib" / f"{im_id:06d}_{index:06d}.png"))
                assert full.shape == visible.shape == depth.shape
                assert np.any(full) and np.any(visible)
                assert set(np.unique(full)) <= {0,255} and set(np.unique(visible)) <= {0,255}
                assert np.all(visible <= full)
                # Canonical model coordinates and pose translations must use the
                # same millimeter convention, including displaced CAD origins.
                xyz = models[pose["obj_id"]] @ rotation.T + pose["cam_t_m2c"]
                assert np.all(xyz[:,2] > 0)
                K = np.array(camera[key]["cam_K"]).reshape(3,3)
                homogeneous = xyz @ K.T
                uv = homogeneous[:,:2] / homogeneous[:,2,None]
                yy,xx = np.nonzero(full)
                np.testing.assert_allclose([xx.min(),yy.min(),xx.max(),yy.max()],
                    [uv[:,0].min(),uv[:,1].min(),uv[:,0].max(),uv[:,1].max()], atol=2.5)
            frames += 1
    return {"frames": frames, "object_ids": list(expected_ids)}


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
            stats = _check_images_and_projection(root, expected_ids)
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
                        if not measured.size:
                            errors.append(f"BOP {pair} object {obj_id}: no positive observed depth in visible mask")
                        elif measured.min() < points_cv_mm[:, 2].min() / 1000 - margin_m or measured.max() > points_cv_mm[:, 2].max() / 1000 + margin_m:
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
