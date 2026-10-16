"""Geometry-only proposals shared by the desktop preview and render runtime."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Mapping
from itertools import product
import numpy as np

from .assets import MeshData
from .camera import CameraPose, project_points, sample_camera
from .config import ProjectConfig
from .sampling import region_for_request, request_rng, unoccluded_requests


@dataclass(frozen=True)
class Placement:
    asset_key: str
    target_key: str | None
    obj_id: int | None
    world_from_model: np.ndarray
    role: str


@dataclass(frozen=True)
class LightState:
    position_m: np.ndarray
    energy_w: float
    color_rgb: tuple[float, float, float]


@dataclass(frozen=True)
class SceneCandidate:
    request_index: int
    attempt_index: int
    region_key: str
    anchor_key: str
    camera: CameraPose
    placements: tuple[Placement, ...]
    hdri_path: Path | None
    hdri_strength: float
    hdri_rotation_deg: float
    light: LightState
    wants_unoccluded: bool


@dataclass(frozen=True)
class Rejection:
    reason: str
    detail: str


def bounds_corners(bounds: np.ndarray) -> np.ndarray:
    return np.array(list(product(*zip(bounds[0], bounds[1]))), dtype=float)


def transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return np.asarray(points) @ matrix[:3, :3].T + matrix[:3, 3]


def placement_bounds(placement: Placement, meshes: Mapping[str, MeshData]) -> np.ndarray:
    pts = transform_points(bounds_corners(meshes[placement.asset_key].bounds_m), placement.world_from_model)
    return np.array([pts.min(axis=0), pts.max(axis=0)])


def _temperature_color(kelvin: float) -> tuple[float, float, float]:
    t = np.clip(kelvin, 1000, 40000) / 100.
    r = 255 if t <= 66 else 329.698727446 * (t - 60) ** -.1332047592
    g = 99.4708025861 * np.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * (t - 60) ** -.0755148492
    b = 255 if t >= 66 else (0 if t <= 19 else 138.5177312231 * np.log(t - 10) - 305.0447927307)
    return tuple(float(x) for x in np.clip([r, g, b], 0., 255.) / 255.)


def propose_candidate(config: ProjectConfig, meshes: Mapping[str, MeshData], request_index: int,
                      attempt_index: int) -> SceneCandidate | Rejection:
    rng = request_rng(config.seed, request_index, attempt_index)
    targets = [t for t in config.targets if t.enabled]
    anchor = next((t for t in targets if t.asset.key == config.anchor_key), None)
    if anchor is None:
        return Rejection('missing_anchor', 'Select an enabled render center')
    missing = [a.key for a in [*(t.asset for t in targets), *config.occluders] if a.key not in meshes]
    if missing:
        return Rejection('missing_mesh', ', '.join(missing))
    region_key = region_for_request(config.num_samples, [r.key for r in config.regions], request_index)
    region = next(r for r in config.regions if r.key == region_key)
    rotation = np.eye(3)  # PLY local axes are retained; no random object rotation in v1.
    pose = sample_camera(region, np.zeros(3), rotation, config.camera, rng)
    placements = []
    for target in targets:
        mesh = meshes[target.asset.key]
        if target.asset.key == config.anchor_key:
            center = np.zeros(3)
        else:
            center = rotation @ np.array([rng.uniform(v.minimum, v.maximum)
                                           for v in (target.offset.x, target.offset.y, target.offset.z)])
        matrix = np.eye(4)
        matrix[:3, :3] = rotation
        matrix[:3, 3] = center - rotation @ mesh.bounds_m.mean(axis=0)
        placements.append(Placement(target.asset.key, target.asset.key, target.obj_id, matrix, 'target'))
    wants_none = request_index in unoccluded_requests(config.num_samples, config.occlusion.none_fraction, config.seed)
    if not wants_none and config.occluders:
        center_size = float(np.max(np.ptp(meshes[anchor.asset.key].bounds_m, axis=0)))
        for _ in range(int(rng.integers(config.occlusion.count[0], config.occlusion.count[1] + 1))):
            asset = config.occluders[int(rng.integers(len(config.occluders)))]
            mesh = meshes[asset.key]
            max_side = float(np.max(np.ptp(mesh.bounds_m, axis=0)))
            if max_side <= 0:
                return Rejection('invalid_mesh', asset.key)
            # Sample in the camera image plane, so users need no placement strategy.
            fraction = float(rng.uniform(.25, .65))
            scale = center_size * rng.uniform(config.occlusion.scale.minimum, config.occlusion.scale.maximum) / max_side
            jitter = rng.uniform(-.35, .35, 2) * center_size
            center = pose.position_m * fraction + pose.world_from_camera[:3, :2] @ jitter
            matrix = np.eye(4)
            matrix[:3, :3] *= scale
            matrix[:3, 3] = center - matrix[:3, :3] @ mesh.bounds_m.mean(axis=0)
            placements.append(Placement(asset.key, None, None, matrix, 'occluder'))
    bounds = [placement_bounds(p, meshes) for p in placements]
    for i, bound in enumerate(bounds):
        if np.all(pose.position_m >= bound[0]) and np.all(pose.position_m <= bound[1]):
            return Rejection('camera_inside', placements[i].asset_key)
        for j in range(i):
            # Touching faces are allowed. Conservative AABB overlap rejection.
            overlap = np.minimum(bound[1], bounds[j][1]) - np.maximum(bound[0], bounds[j][0])
            if np.all(overlap > 1e-9):
                return Rejection('collision', f'{placements[j].asset_key} / {placements[i].asset_key}')
        if placements[i].role == 'target':
            pts = transform_points(bounds_corners(meshes[placements[i].asset_key].bounds_m), placements[i].world_from_model)
            uv, valid = project_points(pts, pose, config.camera)
            if (not valid.all() or np.any(uv[:, 0] < -.5) or np.any(uv[:, 0] > config.camera.width - .5)
                    or np.any(uv[:, 1] < -.5) or np.any(uv[:, 1] > config.camera.height - .5)):
                return Rejection('out_of_frame', placements[i].asset_key)
    hdri = (config.hdri.files[int(rng.integers(len(config.hdri.files)))] if config.hdri.randomize else config.hdri.files[0]) if config.hdri.files else None
    phi = rng.uniform(-np.pi, np.pi)
    alpha = np.arcsin(rng.uniform(np.sin(np.deg2rad(config.light.elevation_deg.minimum)),
                                  np.sin(np.deg2rad(config.light.elevation_deg.maximum))))
    radius = rng.uniform(config.light.radius_m.minimum, config.light.radius_m.maximum)
    light = LightState(np.array([np.cos(alpha) * np.cos(phi), np.cos(alpha) * np.sin(phi), np.sin(alpha)]) * radius,
                       float(rng.uniform(config.light.energy_w.minimum, config.light.energy_w.maximum)),
                       _temperature_color(rng.uniform(config.light.temperature_k.minimum, config.light.temperature_k.maximum)))
    return SceneCandidate(request_index, attempt_index, region_key, config.anchor_key, pose, tuple(placements),
                          hdri, float(rng.uniform(config.hdri.strength.minimum, config.hdri.strength.maximum)),
                          float(rng.uniform(config.hdri.rotation_deg.minimum, config.hdri.rotation_deg.maximum)), light, wants_none)


def candidate_to_dict(candidate: SceneCandidate) -> dict:
    def convert(value):
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value):
            return {f.name: convert(getattr(value, f.name)) for f in fields(value)}
        if isinstance(value, (tuple, list)):
            return [convert(v) for v in value]
        if isinstance(value, np.generic):
            return value.item()
        return value
    return convert(candidate)
