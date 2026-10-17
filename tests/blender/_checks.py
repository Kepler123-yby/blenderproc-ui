"""Shared output metadata checks, independent of any writer implementation."""
from pathlib import Path
import json
import numpy as np

from bproc_core.assets import load_mesh
from bproc_core.project_io import load_project


def read_json(path, diagnostics):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        diagnostics.append(f"{path}: {type(exc).__name__}: {exc}")
        return None


def verify_metadata(root: Path, *, require_complete=True):
    """Return diagnostics and parsed records; malformed files are diagnostics."""
    errors = []
    root = Path(root)
    folder = root / "run_metadata"
    status = read_json(folder / "status.json", errors)
    manifest = read_json(folder / "manifest.json", errors)
    objects = read_json(folder / "objects.json", errors)
    try:
        config = load_project(folder / "project.yaml")
    except Exception as exc:
        errors.append(f"project.yaml: {type(exc).__name__}: {exc}")
        config = None
    if not isinstance(status, dict) or not isinstance(manifest, dict) or not isinstance(objects, list) or config is None:
        return errors or ["Metadata documents have invalid types"], config, status, []
    try:
        for name in ("requested", "written_frames", "saved", "skipped", "exit_code"):
            if isinstance(status.get(name), bool) or not isinstance(status.get(name), int):
                raise ValueError(f"status.{name} must be an integer")
        requested, written, saved = (status[k] for k in ("requested", "written_frames", "saved"))
        if not 0 <= saved <= written <= requested or status["skipped"] != requested - saved:
            errors.append("Run counters are inconsistent (0 <= saved <= written <= requested; skipped=requested-saved)")
        if requested != config.total_shots:
            errors.append("Requested frame count differs from project snapshot")
        name, code = status.get("status"), status["exit_code"]
        if name == "completed" and (saved != requested or written != saved or code != 0):
            errors.append("Completed status does not describe a complete successful run")
        elif name == "partial" and (saved >= requested or code != 2):
            errors.append("Partial run must have skipped samples and exit code 2")
        elif name == "interrupted" and code not in (130, 143):
            errors.append("Interrupted run must report SIGINT/SIGTERM exit code")
        elif name == "failed" and code == 0:
            errors.append("Failed run must have a nonzero exit code")
        elif name not in ("completed", "partial", "failed", "interrupted"):
            errors.append(f"Unknown or unfinished run status: {name}")
        if require_complete and name != "completed":
            errors.append(f"Dataset is not complete: {name}")
        failures = status.get("failures_by_reason")
        if not isinstance(failures, dict) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in failures.values()):
            errors.append("Invalid failure counters")
        records = manifest.get("frames")
        if not isinstance(records, list):
            raise ValueError("manifest.frames must be a list")
        if len(records) != written:
            errors.append("Manifest frame count differs from written_frames")
        if manifest.get("seed") != config.seed:
            errors.append("Manifest seed differs from project snapshot")
        targets = {t.obj_id: t for t in config.targets if t.enabled}
        if sorted(row.get("obj_id") for row in objects) != sorted(targets):
            errors.append("Object map does not contain exactly every enabled target ID")
        meshes = {}
        for obj_id, target in targets.items():
            try:
                meshes[obj_id] = load_mesh(target.asset)
            except Exception as exc:
                errors.append(f"Cannot validate canonical source model {obj_id}: {exc}")
        seen_views = set()
        view_counts = {}
        regions = {r.key: r for r in config.regions}
        for index, row in enumerate(records):
            try:
                if row.get("frame_index") != index:
                    errors.append(f"Frame {index}: manifest indices must be consecutive from zero")
                if sorted(row["gt_obj_ids"]) != sorted(targets):
                    errors.append(f"Frame {index}: target ID list differs from project")
                candidate = row["candidate"]
                scene_index, view_index, attempt = (candidate["scene_index"], candidate["view_index"],
                                                   candidate["attempt_index"])
                if isinstance(scene_index, bool) or not isinstance(scene_index, int) or not 0 <= scene_index < config.scene_count:
                    errors.append(f"Frame {index}: invalid scene index")
                if isinstance(view_index, bool) or not isinstance(view_index, int) or not 0 <= view_index < config.views:
                    errors.append(f"Frame {index}: invalid view index")
                if (scene_index, view_index) in seen_views:
                    errors.append(f"Frame {index}: duplicate scene/view pair")
                seen_views.add((scene_index, view_index))
                view_counts[scene_index] = view_counts.get(scene_index, 0) + 1
                if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 <= attempt < config.occlusion.max_attempts:
                    errors.append(f"Frame {index}: attempt index exceeds shared retry budget")
                if candidate["anchor_key"] != config.anchor_key:
                    errors.append(f"Frame {index}: render center differs from project")
                placements = {p["obj_id"]: p for p in candidate["placements"] if p["role"] == "target"}
                if set(placements) != set(targets):
                    errors.append(f"Frame {index}: placement target IDs differ from project")
                anchor_id = next(i for i, target in targets.items() if target.asset.key == config.anchor_key)
                anchor_matrix = np.asarray(placements[anchor_id]["world_from_model"], dtype=float)
                centers = {}
                for obj_id, placement in placements.items():
                    matrix = np.asarray(placement["world_from_model"], dtype=float)
                    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
                        raise ValueError(f"invalid target transform for object {obj_id}")
                    if not np.allclose(matrix[3], [0, 0, 0, 1]) or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6):
                        errors.append(f"Frame {index}: target transform must preserve canonical model scale")
                    if obj_id in meshes:
                        local_center = meshes[obj_id].bounds_m.mean(axis=0)
                        centers[obj_id] = matrix[:3, :3] @ local_center + matrix[:3, 3]
                if anchor_id in centers:
                    anchor_center = centers[anchor_id]
                    if not np.allclose(anchor_center, 0, atol=1e-7):
                        errors.append(f"Frame {index}: center target bounding-box center is not at world origin")
                    for obj_id, center in centers.items():
                        if obj_id == anchor_id:
                            continue
                        offset = anchor_matrix[:3, :3].T @ (center - anchor_center)
                        box = targets[obj_id].offset
                        if any(value < bounds.minimum - 1e-7 or value > bounds.maximum + 1e-7 for value, bounds in zip(offset, (box.x, box.y, box.z))):
                            errors.append(f"Frame {index}: object {obj_id} lies outside its configured local position box")
                camera = candidate["camera"]
                matrix = np.asarray(camera["world_from_camera"], dtype=float)
                position = np.asarray(camera["position_m"], dtype=float)
                if matrix.shape != (4, 4) or position.shape != (3,) or not np.isfinite(matrix).all() or not np.isfinite(position).all():
                    raise ValueError("invalid camera transform")
                if not np.allclose(matrix[:3, 3], position) or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6):
                    errors.append(f"Frame {index}: inconsistent camera position/rotation")
                region = regions[candidate["region_key"]]
                radius, phi, elevation = (float(camera[k]) for k in ("radius_m", "azimuth_deg", "elevation_deg"))
                if not np.isfinite([radius, phi, elevation]).all():
                    raise ValueError("nonfinite camera spherical coordinates")
                if not region.radius_m.minimum - 1e-7 <= radius <= region.radius_m.maximum + 1e-7 or not region.elevation_deg.minimum - 1e-7 <= elevation <= region.elevation_deg.maximum + 1e-7:
                    errors.append(f"Frame {index}: camera outside spherical sampling ranges")
                if not region.azimuth.full_circle:
                    sweep = (region.azimuth.end_deg - region.azimuth.start_deg) % 360
                    offset = (phi - region.azimuth.start_deg) % 360
                    if min(abs(offset), abs(offset - 360)) > 1e-7 and offset > sweep + 1e-7:
                        errors.append(f"Frame {index}: camera outside azimuth range")
                if not np.isclose(np.linalg.norm(position), radius, atol=1e-7):
                    errors.append(f"Frame {index}: recorded camera radius differs from actual position")
                if candidate["hdri_path"] is not None and candidate["hdri_path"] not in [str(p) for p in config.hdri.files]:
                    errors.append(f"Frame {index}: HDRI is outside configured resource list")
                ratio = float(row["occlusion_ratio"])
                if not np.isfinite(ratio) or not 0 <= ratio <= 1:
                    errors.append(f"Frame {index}: invalid measured center occlusion")
                elif candidate["wants_unoccluded"] and ratio != 0 or not candidate["wants_unoccluded"] and not config.occlusion.ratio.minimum <= ratio <= config.occlusion.ratio.maximum:
                    errors.append(f"Frame {index}: measured center occlusion does not satisfy requested category")
            except Exception as exc:
                errors.append(f"Frame {index}: malformed manifest record: {type(exc).__name__}: {exc}")
        return errors, config, status, records
    except Exception as exc:
        errors.append(f"Malformed run metadata: {type(exc).__name__}: {exc}")
        return errors, config, status, []
