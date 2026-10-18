"""Geometry-only proposals shared by the desktop preview and render runtime."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Mapping
from itertools import product
import numpy as np

from .assets import MeshData
from .camera import CameraPose, project_points, sample_camera
from .config import MaterialState, ProjectConfig
from .sampling import camera_for_view, scene_rng, unoccluded_scenes, view_rng


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
    scene_index: int
    view_index: int
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
    camera_kind: str = "random"
    material: MaterialState = MaterialState(.65, .5)
    layout_attempt_index: int = 0


@dataclass(frozen=True)
class SceneState:
    """What every view of one scene shares: object placement, occluders and lighting."""
    scene_index: int
    placements: tuple[Placement, ...]
    hdri_path: Path | None
    hdri_strength: float
    hdri_rotation_deg: float
    light: LightState
    wants_unoccluded: bool
    material: MaterialState = MaterialState(.65, .5)
    layout_attempt_index: int = 0


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


def nominal_radius(config: ProjectConfig) -> float:
    """Typical camera distance; occluders are placed inside that shell, not on one camera's axis."""
    radii = ([c.radius_m for c in config.fixed_cameras] if config.camera_mode == "fixed" else
             [v for region in config.regions for v in (region.radius_m.minimum, region.radius_m.maximum)])
    return float(np.mean(radii)) if radii else 1.0


def propose_scene(config: ProjectConfig, meshes: Mapping[str, MeshData], scene_index: int,
                  *, layout_attempt_index: int = 0) -> SceneState | Rejection:
    """Sample everything a scene keeps for all of its views: placement, occluders, lighting."""
    rng = scene_rng(config.seed, scene_index, layout_attempt_index)
    targets = [t for t in config.targets if t.enabled]
    anchor = next((t for t in targets if t.asset.key == config.anchor_key), None)
    if anchor is None:
        return Rejection('missing_anchor', 'Select an enabled render center')
    missing = [a.key for a in [*(t.asset for t in targets), *config.occluders] if a.key not in meshes]
    if missing:
        return Rejection('missing_mesh', ', '.join(missing))
    rotation = np.eye(3)  # PLY local axes are retained; no random object rotation in v1.
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
    wants_none = scene_index in unoccluded_scenes(config.scene_count, config.occlusion.none_fraction, config.seed)
    bounds = [placement_bounds(p, meshes) for p in placements]
    for i, bound in enumerate(bounds):
        for j in range(i):
            # Touching faces are allowed. Conservative AABB overlap rejection.
            overlap = np.minimum(bound[1], bounds[j][1]) - np.maximum(bound[0], bounds[j][0])
            if np.all(overlap > 1e-9):
                return Rejection('collision', f'{placements[j].asset_key} / {placements[i].asset_key}')
    hdri = (config.hdri.files[int(rng.integers(len(config.hdri.files)))] if config.hdri.randomize else config.hdri.files[0]) if config.hdri.files else None
    phi = rng.uniform(-np.pi, np.pi)
    alpha = np.arcsin(rng.uniform(np.sin(np.deg2rad(config.light.elevation_deg.minimum)),
                                  np.sin(np.deg2rad(config.light.elevation_deg.maximum))))
    radius = rng.uniform(config.light.radius_m.minimum, config.light.radius_m.maximum)
    light = LightState(np.array([np.cos(alpha) * np.cos(phi), np.cos(alpha) * np.sin(phi), np.sin(alpha)]) * radius,
                       float(rng.uniform(config.light.energy_w.minimum, config.light.energy_w.maximum)),
                       _temperature_color(rng.uniform(config.light.temperature_k.minimum, config.light.temperature_k.maximum)))
    material = (config.material.fixed if config.material.source == "fixed" else
                MaterialState(float(rng.uniform(config.material.roughness.minimum, config.material.roughness.maximum)),
                              float(rng.uniform(config.material.specular.minimum, config.material.specular.maximum)),
                              float(rng.uniform(config.material.metallic.minimum, config.material.metallic.maximum))))
    return SceneState(scene_index, tuple(placements), hdri,
                      float(rng.uniform(config.hdri.strength.minimum, config.hdri.strength.maximum)),
                      float(rng.uniform(config.hdri.rotation_deg.minimum, config.hdri.rotation_deg.maximum)),
                      light, wants_none, material, layout_attempt_index)


# Only a placement clash can be fixed by drawing a new layout; missing assets never can.
LAYOUT_RETRY_REASONS = frozenset({"collision"})


def sample_scene(config: ProjectConfig, meshes: Mapping[str, MeshData], scene_index: int,
                 *, on_rejection=None) -> SceneState | Rejection:
    """Draw a scene layout, retrying only collisions within its own bounded budget.

    Each retry changes the layout seed, so a range that is too tight for one draw still has a
    chance instead of dropping the whole scene. Missing meshes or an unset render centre are
    returned at once because another attempt cannot fix them.
    """
    attempts = int(config.layout_max_attempts)
    if attempts < 1:
        raise ValueError("Layout attempts must be positive")
    result = Rejection("collision", "没有可用的布局")
    for layout_attempt_index in range(attempts):
        result = propose_scene(config, meshes, scene_index, layout_attempt_index=layout_attempt_index)
        if not isinstance(result, Rejection):
            return result
        if result.reason not in LAYOUT_RETRY_REASONS:
            return result
        if on_rejection is not None:
            on_rejection(layout_attempt_index, result)
    return result


def occluder_placements(config: ProjectConfig, meshes: Mapping[str, MeshData], pose: CameraPose,
                        anchor_key: str, rng) -> list[Placement] | Rejection:
    """Occluders for one camera: on its line of sight, offset sideways so the cover is partial.

    A world-space occluder sampled anywhere on the sphere almost never crosses the target's line
    of sight (the camera band is a few percent of it), which left ``occlusion_ratio`` at 0 on
    almost every frame. Placing them along this camera's ray, a fraction of the way to the target
    and pushed sideways by a fraction of the target size, keeps the ratio inside the configured
    window and lets a retry adjust it.
    """
    anchor = meshes[anchor_key]
    center_size = float(np.max(np.ptp(anchor.bounds_m, axis=0)))
    nominal = nominal_radius(config)
    ray = np.asarray(pose.position_m, dtype=float)
    length = float(np.linalg.norm(ray))
    if length <= 0:
        return Rejection('invalid_pivot', anchor_key)
    direction = ray / length
    side = np.cross(direction, np.array([0., 0., 1.]))
    if np.linalg.norm(side) < 1e-6:
        side = np.array([1., 0., 0.])
    side /= np.linalg.norm(side)
    up = np.cross(side, direction)
    placements = []
    for _ in range(int(rng.integers(config.occlusion.count[0], config.occlusion.count[1] + 1))):
        asset = config.occluders[int(rng.integers(len(config.occluders)))]
        mesh = meshes[asset.key]
        max_side = float(np.max(np.ptp(mesh.bounds_m, axis=0)))
        if max_side <= 0:
            return Rejection('invalid_mesh', asset.key)
        size = center_size * rng.uniform(config.occlusion.scale.minimum, config.occlusion.scale.maximum)
        angle = rng.uniform(0., 2 * np.pi)
        offset = size * rng.uniform(.1, .9)
        center = (direction * nominal * rng.uniform(.3, .55)
                  + (np.cos(angle) * side + np.sin(angle) * up) * offset)
        scale = size / max_side
        matrix = np.eye(4)
        matrix[:3, :3] *= scale
        matrix[:3, 3] = center - matrix[:3, :3] @ mesh.bounds_m.mean(axis=0)
        placements.append(Placement(asset.key, None, None, matrix, 'occluder'))
    return placements


def propose_candidate(config: ProjectConfig, meshes: Mapping[str, MeshData], scene: SceneState,
                      view_index: int, attempt_index: int) -> SceneCandidate | Rejection:
    """Sample one camera for an existing scene; a retry only moves the camera."""
    rng = view_rng(config.seed, scene.scene_index, view_index, attempt_index)
    kind, region_key = camera_for_view(config.camera_mode, [c.key for c in config.fixed_cameras],
                                       [r.key for r in config.regions], config.views_per_scene, view_index)
    if kind == "fixed":
        region = next(c for c in config.fixed_cameras if c.key == region_key).region()
    else:
        region = next(r for r in config.regions if r.key == region_key)
    pose = sample_camera(region, np.zeros(3), np.eye(3), config.camera, rng)
    for placement in scene.placements:
        bound = placement_bounds(placement, meshes)
        if np.all(pose.position_m >= bound[0]) and np.all(pose.position_m <= bound[1]):
            return Rejection('camera_inside', placement.asset_key)
        if placement.role != 'target':
            continue
        pts = transform_points(bounds_corners(meshes[placement.asset_key].bounds_m), placement.world_from_model)
        uv, valid = project_points(pts, pose, config.camera)
        if (not valid.all() or np.any(uv[:, 0] < -.5) or np.any(uv[:, 0] > config.camera.width - .5)
                or np.any(uv[:, 1] < -.5) or np.any(uv[:, 1] > config.camera.height - .5)):
            return Rejection('out_of_frame', placement.asset_key)
    placements = list(scene.placements)
    if not scene.wants_unoccluded and config.occluders:
        occluders = occluder_placements(config, meshes, pose, config.anchor_key, rng)
        if isinstance(occluders, Rejection):
            return occluders
        placements.extend(occluders)
    return SceneCandidate(scene.scene_index, view_index, attempt_index, region_key, config.anchor_key, pose,
                          tuple(placements), scene.hdri_path, scene.hdri_strength, scene.hdri_rotation_deg,
                          scene.light, scene.wants_unoccluded, kind, material=scene.material,
                          layout_attempt_index=scene.layout_attempt_index)


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


def candidate_from_dict(payload: Mapping) -> SceneCandidate:
    """Rebuild a candidate serialised by :func:`candidate_to_dict`."""
    camera = payload["camera"]
    light = payload["light"]
    placements = tuple(
        Placement(item["asset_key"], item["target_key"], item["obj_id"],
                  np.asarray(item["world_from_model"], dtype=float), item["role"])
        for item in payload["placements"])
    hdri = payload["hdri_path"]
    return SceneCandidate(
        int(payload.get("scene_index", payload.get("request_index", 0))), int(payload.get("view_index", 0)),
        int(payload["attempt_index"]), payload["region_key"],
        payload["anchor_key"],
        CameraPose(np.asarray(camera["world_from_camera"], dtype=float),
                   np.asarray(camera["position_m"], dtype=float), float(camera["radius_m"]),
                   float(camera["azimuth_deg"]), float(camera["elevation_deg"]),
                   float(camera["clip_near_m"]), float(camera["clip_far_m"])),
        placements, Path(hdri) if hdri else None, float(payload["hdri_strength"]),
        float(payload["hdri_rotation_deg"]),
        LightState(np.asarray(light["position_m"], dtype=float), float(light["energy_w"]),
                   tuple(float(c) for c in light["color_rgb"])),
        bool(payload["wants_unoccluded"]), str(payload.get("camera_kind", "random")),
        MaterialState(float((payload.get("material") or {}).get("roughness", .65)),
                      float((payload.get("material") or {}).get("specular", .5)),
                      float((payload.get("material") or {}).get("metallic", 0.))),
        int(payload.get("layout_attempt_index", 0)))
