"""Geometry-only proposals shared by the desktop preview and render runtime."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Mapping, Sequence
from itertools import product
import numpy as np
from PIL import Image, ImageDraw

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


# Clearance is a fraction of the scene, never a fixed centimetre: a 1 mm fixture with a fixed
# 1 cm gap had no room left between the near plane and the target in any of 30 draws.
OCCLUDER_CLEARANCE_FRACTION = (0.1, 0.01)      # of the target size, and of the camera distance
OCCLUDER_DRAWS = 24

# The aiming estimate needs the projected *shape*, not its bounding box: a hollow or U-shaped
# part has a convex hull several times its real silhouette. Up to this many triangles the
# silhouette is rasterised exactly on a small screen-space grid; heavier parts fall back to the
# hull of a bounded vertex sample, which keeps a million-face preview responsive.
SILHOUETTE_MASK_MAX_FACES = 6000
SILHOUETTE_MASK_MAX_VERTICES = 20000
SILHOUETTE_POINT_LIMIT = 4096
SILHOUETTE_GRID_SIZE = 256


def _world_aabb(bounds_m: np.ndarray, world_from_model: np.ndarray):
    """World axis-aligned bounds of a transformed box; exact for the scale+translation placements."""
    world = transform_points(bounds_corners(bounds_m), world_from_model)
    return world.min(axis=0), world.max(axis=0)


def _boxes_overlap(a, b) -> bool:
    """Conservative AABB intersection test used to keep occluders out of every solid."""
    return bool(np.all(a[0] < b[1]) and np.all(b[0] < a[1]))


def _convex_hull(points: np.ndarray) -> np.ndarray:
    """2D convex hull (monotone chain); enough for a silhouette envelope."""
    ordered = np.unique(np.round(points, 6), axis=0)
    if len(ordered) < 3:
        return ordered
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in ordered[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return np.array(lower[:-1] + upper[:-1])


def _silhouette(points_m, world_from_model, pose: CameraPose, camera):
    """Screen-space convex hull of the supplied points, or None when one is behind the camera."""
    uv, valid = project_points(transform_points(points_m, world_from_model), pose, camera)
    if not valid.all():
        return None
    return _convex_hull(uv)


def _silhouette_points(mesh: MeshData) -> np.ndarray:
    """Vertices worth projecting for the hull fallback, capped for million-face parts.

    ``bounds_corners`` is always included: a strided sample may otherwise miss one extreme of a
    dense mesh, and the hull would shrink below the real outline.
    """
    vertices = mesh.vertices_m
    if len(vertices) <= SILHOUETTE_POINT_LIMIT:
        return vertices
    stride = int(np.ceil(len(vertices) / SILHOUETTE_POINT_LIMIT))
    return np.vstack([vertices[::stride], bounds_corners(mesh.bounds_m)])


@dataclass(frozen=True)
class _SilhouetteGrid:
    """A square screen-space grid that target and occluder masks share."""
    left_px: float
    top_px: float
    pixels_per_px: float
    size: int = SILHOUETTE_GRID_SIZE

    def map(self, uv) -> np.ndarray:
        return (np.asarray(uv, dtype=float) - [self.left_px, self.top_px]) * self.pixels_per_px


def _silhouette_grid(entries, pose: CameraPose, camera) -> _SilhouetteGrid | None:
    """Grid covering every mesh that will be compared as a rasterised mask."""
    boxes = []
    for mesh, matrix in entries:
        uv, valid = project_points(transform_points(mesh.vertices_m, matrix), pose, camera)
        if not valid.all():
            return None
        if len(uv):
            boxes.append((uv.min(axis=0), uv.max(axis=0)))
    if not boxes:
        return None
    low = np.min([box[0] for box in boxes], axis=0)
    high = np.max([box[1] for box in boxes], axis=0)
    span = float(np.max(high - low))
    if not np.isfinite(span) or span <= 0:
        return None
    margin = 2.
    scale = (SILHOUETTE_GRID_SIZE - 1 - 2 * margin) / span
    return _SilhouetteGrid(float(low[0]) - margin / scale, float(low[1]) - margin / scale,
                           scale)


def _rasterize_mask(mesh: MeshData, world_from_model: np.ndarray, pose: CameraPose, camera,
                    grid: _SilhouetteGrid, *, triangles: bool) -> np.ndarray:
    """Fill the projected mesh (or its bounded hull) into the shared grid."""
    points = mesh.vertices_m if triangles else _silhouette_points(mesh)
    uv, valid = project_points(transform_points(points, world_from_model), pose, camera)
    pixel = grid.map(uv)
    image = Image.new("1", (grid.size, grid.size), 0)
    draw = ImageDraw.Draw(image)
    if triangles:
        for triangle in mesh.faces:
            if valid[triangle].all():
                draw.polygon([tuple(pixel[index]) for index in triangle], fill=1)
    elif valid.any():
        hull = _convex_hull(pixel[valid])
        if len(hull) >= 3:
            draw.polygon([tuple(point) for point in hull], fill=1)
    return np.asarray(image, dtype=bool)


def _mask_covered_share(target_mask: np.ndarray, occluder_mask: np.ndarray) -> float:
    """Pixel share of the target silhouette hidden by the occluder."""
    total = int(np.count_nonzero(target_mask))
    if total == 0:
        return 0.0
    return float(np.count_nonzero(target_mask & occluder_mask)) / total


@dataclass(frozen=True)
class _TargetShapes:
    """Targets as either exact masks or convex-hull fallbacks on one shared grid."""
    hulls: tuple[np.ndarray, ...]
    grid: _SilhouetteGrid | None
    masks: tuple[np.ndarray, ...]


def _target_shapes(entries, pose: CameraPose, camera) -> _TargetShapes:
    hulls, masked = [], []
    for mesh, matrix in entries:
        # Tiny fixtures describe a hull with one triangle; rasterising that triangle would
        # under-state the shape, so only a real surface takes the mask path.
        if (8 <= len(mesh.faces) <= SILHOUETTE_MASK_MAX_FACES
                and len(mesh.vertices_m) <= SILHOUETTE_MASK_MAX_VERTICES):
            masked.append((mesh, matrix))
        else:
            hull = _silhouette(_silhouette_points(mesh), matrix, pose, camera)
            if hull is not None:
                hulls.append(hull)
    grid = _silhouette_grid(masked, pose, camera) if masked else None
    if grid is None:
        # A mesh behind the camera (or a degenerate projection) cannot be masked; keep the
        # affine hull fallback rather than rejecting an otherwise valid target.
        for mesh, matrix in masked:
            hull = _silhouette(_silhouette_points(mesh), matrix, pose, camera)
            if hull is not None:
                hulls.append(hull)
        return _TargetShapes(tuple(hulls), None, ())
    masks = tuple(_rasterize_mask(mesh, matrix, pose, camera, grid, triangles=True)
                  for mesh, matrix in masked)
    return _TargetShapes(tuple(hulls), grid, masks)


def _polygon_area(polygon: np.ndarray) -> float:
    if len(polygon) < 3:
        return 0.0
    x, y = polygon[:, 0], polygon[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _clip_to_convex(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    """Sutherland-Hodgman: the part of ``subject`` inside the convex ``clip`` polygon."""
    output = subject
    for index in range(len(clip)):
        if len(output) == 0:
            return output
        a, b = clip[index], clip[(index + 1) % len(clip)]
        edge = b - a
        inside = lambda point: edge[0] * (point[1] - a[1]) - edge[1] * (point[0] - a[0]) >= 0  # noqa: E731
        current = list(output)
        output = []
        for position, point in enumerate(current):
            previous = current[position - 1]
            if inside(point):
                if not inside(previous):
                    output.append(_intersection(previous, point, a, b))
                output.append(point)
            elif inside(previous):
                output.append(_intersection(previous, point, a, b))
        output = np.array(output) if output else np.empty((0, 2))
    return output


def _intersection(p1, p2, a, b):
    """Where segment ``p1-p2`` crosses the infinite line through ``a-b``."""
    r, s = p2 - p1, b - a
    denominator = r[0] * s[1] - r[1] * s[0]
    if abs(denominator) < 1e-12:
        return p2
    t = ((a[0] - p1[0]) * s[1] - (a[1] - p1[1]) * s[0]) / denominator
    return p1 + t * r


def _covered_share(target_hull, occluder_hull) -> float:
    """Share of the target silhouette hidden by the occluder, from projected convex hulls.

    This is the fallback for parts too dense to rasterise. Hulls over-state a concave or hollow
    outline; the mask path in :func:`_aimed_offset` is preferred whenever the mesh is small enough.
    """
    target_area = _polygon_area(target_hull)
    if target_area <= 0 or occluder_hull is None:
        return 0.0
    return _polygon_area(_clip_to_convex(occluder_hull, target_hull)) / target_area


def _aimed_offset(base, direction, mesh, scale, shapes: _TargetShapes, pose, config, rng, spread):
    """Sideways offset that should cover roughly the middle of the occlusion window.

    The renderer used to reject most proposals for over-covering: a box three quarters of the
    target's width, offset by a tenth of it, hides most of the silhouette. The offset is now
    bisected against the projected *mesh* silhouette, which falls monotonically as the occluder
    moves away from the target's centre. ``spread`` keeps the draw varied and bounds the search.
    """
    wanted = config.occlusion.ratio
    if wanted.maximum <= 0 or (not shapes.hulls and shapes.grid is None):
        return spread * rng.uniform(.1, .9)
    # Aim inside the *user's* closed window, never near it: an 80-90 % window must not be aimed at
    # 72 %, and a 0-1 % window must not be aimed at 2 %. Clamping after the midpoint keeps the
    # target valid for any interval, including a fixed ratio where minimum == maximum.
    aim = min(max((wanted.minimum + wanted.maximum) / 2, wanted.minimum), wanted.maximum)
    low, high = 0., max(spread * 2.5, 1e-9)

    def estimate(offset):
        matrix = np.eye(4)
        matrix[:3, :3] *= scale
        matrix[:3, 3] = base + direction * offset - matrix[:3, :3] @ mesh.bounds_m.mean(axis=0)
        share = 0.
        if shapes.hulls:
            hull = _silhouette(_silhouette_points(mesh), matrix, pose, config.camera)
            if hull is not None:
                share = max(_covered_share(target, hull) for target in shapes.hulls)
        if shapes.grid is not None:
            exact = (8 <= len(mesh.faces) <= SILHOUETTE_MASK_MAX_FACES
                     and len(mesh.vertices_m) <= SILHOUETTE_MASK_MAX_VERTICES)
            occluder_mask = _rasterize_mask(mesh, matrix, pose, config.camera, shapes.grid,
                                            triangles=exact)
            share = max([share, *(_mask_covered_share(target, occluder_mask)
                                  for target in shapes.masks)])
        return share

    for _ in range(10):
        middle = (low + high) / 2
        if estimate(middle) > aim:
            low = middle      # too much cover: move further aside
        else:
            high = middle
    # Bisection alone is only accurate to its step; a linear refinement against the bracket's own
    # estimates lands within a fraction of a percent, which tight windows (80-90 %, fixed ratios)
    # need. Three extra evaluations, three candidates, keep the closest.
    low_share, high_share = estimate(low), estimate(high)
    candidates = [(abs(low_share - aim), low), (abs(high_share - aim), high)]
    if low_share != high_share:
        guess = low + (low_share - aim) * (high - low) / (low_share - high_share)
        guess = min(max(guess, min(low, high)), max(low, high))
        candidates.append((abs(estimate(guess) - aim), guess))
    return min(candidates)[1]


def occluder_placements(config: ProjectConfig, meshes: Mapping[str, MeshData], pose: CameraPose,
                        anchor_key: str, rng, targets: Sequence[Placement] = ()) -> list[Placement] | Rejection:
    """Occluders for one camera: inside its segment to the target, offset sideways so the cover is partial.

    They used to be placed at a fraction of the scene-wide nominal radius, which put them behind the
    camera as soon as a view sat closer than that radius. A placement is now chosen by its depth from
    this camera: past the near clip plane, in front of the nearest target surface, and separated from
    every target and from the other occluders by a conservative AABB test. No room left is reported as
    a rejection instead of looping or drifting behind the camera.

    ``occlusion.scale`` describes how large the occluder *looks* next to the target, because that is
    what decides the coverage the dataset shows. Its physical size follows from the depth actually
    drawn: an occluder halfway to a target needs half the metres to look the same size. Sampling
    metres first and placing them at any depth made a 5 m view 20-30 % over-covered, since the same
    box three times closer to the lens projects three times wider.
    """
    anchor = meshes[anchor_key]
    center_size = float(np.max(np.ptp(anchor.bounds_m, axis=0)))
    camera = np.asarray(pose.position_m, dtype=float)
    distance = float(np.linalg.norm(camera))
    if distance <= 0:
        return Rejection('invalid_pivot', anchor_key)
    view = -camera / distance
    if not targets:
        # No placements were handed in: the anchor itself still bounds how far an occluder may go.
        targets = (Placement(anchor_key, None, None, np.eye(4), "target"),)
    right = np.cross(view, np.array([0., 0., 1.]))
    if np.linalg.norm(right) < 1e-6:
        right = np.array([1., 0., 0.])
    right /= np.linalg.norm(right)
    up = np.cross(right, view)
    boxes = [_world_aabb(meshes[p.asset_key].bounds_m, p.world_from_model)
             for p in targets if p.asset_key in meshes]
    # Screen-space silhouettes of every target, used to aim the occluder at the requested coverage.
    target_entries = [(meshes[p.asset_key], p.world_from_model)
                      for p in targets if p.asset_key in meshes]
    target_shapes = _target_shapes(target_entries, pose, config.camera)
    target_front = min((float(np.min((transform_points(bounds_corners(meshes[p.asset_key].bounds_m),
                                                     p.world_from_model) - camera) @ view))
                        for p in targets if p.asset_key in meshes), default=distance)
    placements = []
    for _ in range(int(rng.integers(config.occlusion.count[0], config.occlusion.count[1] + 1))):
        asset = config.occluders[int(rng.integers(len(config.occluders)))]
        mesh = meshes[asset.key]
        extent = np.ptp(mesh.bounds_m, axis=0)
        max_side = float(extent.max())
        if max_side <= 0:
            return Rejection('invalid_mesh', asset.key)
        apparent = center_size * rng.uniform(config.occlusion.scale.minimum,
                                             config.occlusion.scale.maximum)
        # Half the box as seen along the view ray; the diagonal would over-reserve depth. The upper
        # bound uses the occluder at the target's depth, which must still fit in the segment.
        clearance = min(OCCLUDER_CLEARANCE_FRACTION[0] * center_size,
                        OCCLUDER_CLEARANCE_FRACTION[1] * distance)
        tolerance = 1e-6 * distance        # float slack that scales with the scene, not with metres
        half_at_target = float((np.asarray([apparent / max_side]) * extent / 2) @ np.abs(view))
        if target_front - clearance - half_at_target <= float(pose.clip_near_m) + clearance + half_at_target + tolerance:
            return Rejection('no_occluder_room', asset.key)
        low = float(pose.clip_near_m) + clearance
        high = target_front - clearance
        for _ in range(OCCLUDER_DRAWS):
            depth = float(rng.uniform(low, high))
            ratio = depth / distance       # perspective: apparent size = physical size * distance / depth
            scale = apparent * ratio / max_side
            half_along_view = float((extent * scale / 2) @ np.abs(view))
            nearest = float(pose.clip_near_m) + clearance + half_along_view
            farthest = target_front - clearance - half_along_view
            if farthest <= nearest + tolerance:
                return Rejection('no_occluder_room', asset.key)
            depth = min(max(depth, nearest), farthest)
            angle = rng.uniform(0., 2 * np.pi)
            base = camera + view * depth
            direction = np.cos(angle) * right + np.sin(angle) * up
            center = base + direction * _aimed_offset(
                base, direction, mesh, scale, target_shapes, pose, config, rng, apparent * ratio)
            matrix = np.eye(4)
            matrix[:3, :3] *= scale
            matrix[:3, 3] = center - matrix[:3, :3] @ mesh.bounds_m.mean(axis=0)
            box = _world_aabb(mesh.bounds_m, matrix)
            if any(_boxes_overlap(box, other) for other in boxes):
                continue
            boxes.append(box)
            placements.append(Placement(asset.key, None, None, matrix, 'occluder'))
            break
        else:
            return Rejection('occluder_collision', asset.key)
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
        occluders = occluder_placements(config, meshes, pose, config.anchor_key, rng,
                                        targets=[p for p in scene.placements if p.role == 'target'])
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
