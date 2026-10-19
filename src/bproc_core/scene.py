"""Geometry-only proposals shared by the desktop preview and render runtime."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Mapping, Sequence
from itertools import product
from threading import Lock
import weakref
import numpy as np
from PIL import Image, ImageDraw

from .assets import MeshData
from .camera import CameraPose, project_points, sample_camera
from .config import MaterialState, ProjectConfig
from .sampling import camera_for_view, scene_rng, unoccluded_scenes, view_rng
from .silhouette import stream_coverage


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
    silhouette_diagnostics: tuple = ()


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

# The sideways offset is found by a bounded coarse scan over the target's whole projected span and
# a local bisection on the best window crossing. A single bisection from the target's centre is
# invalid for the shapes this sampler now measures: a hollow frame is empty at the hole's centre
# and a small occluder only starts covering at the rim, so coverage rises before it falls again.
OFFSET_SCAN_MIN_SAMPLES = 32      # equal coarse probes across the target's whole projected span
OFFSET_SCAN_MAX_SAMPLES = 64      # cap: a target far wider than the occluder does not need more
OFFSET_SCAN_MAX_REFINEMENTS = 8   # bisection steps inside one crossing bracket (rasterised share)
OFFSET_SCAN_EXACT_REFINEMENTS = 24  # bisection steps for the quantisation-free convex share
OFFSET_SCAN_SHARE_TOLERANCE = 1e-6  # share error that counts as hitting a fixed ratio exactly
OFFSET_SCAN_MAX_BRACKETS = 3      # crossing brackets refined per aim; the rest are only measured

# The aiming estimate needs the projected *shape*, not its bounding box: a hollow or U-shaped
# part has a convex hull several times its real silhouette. Any mesh with triangles is rasterised
# exactly on a small screen-space grid, however few faces it has; these caps only bound the cost,
# so heavier parts fall back to the hull of a bounded vertex sample, which keeps a million-face
# preview responsive.
SILHOUETTE_MASK_MAX_FACES = 6000
SILHOUETTE_MASK_MAX_VERTICES = 20000
SILHOUETTE_POINT_LIMIT = 4096
SILHOUETTE_GRID_SIZE = 256
# Past the triangle budget a mesh is not abandoned to its hull: its visible outline is the set of
# projected edges where a front-facing face meets a back-facing one, plus every boundary edge of an
# open surface. Those edges are built once per mesh (vectorised sort, no per-edge Python) and kept in
# a bounded cache, so every pose only reruns the facing test and one scanline fill through the shared
# grid. These caps are explicit support limits, and hitting one is recorded as a hull fallback
# instead of being confused with real geometry.
SILHOUETTE_TOPOLOGY_MAX_FACES = 2_000_000    # faces one cached edge topology may cover
SILHOUETTE_TOPOLOGY_MAX_EDGES = 4_000_000    # undirected edges one cached topology may cover
SILHOUETTE_TOPOLOGY_CACHE_MESHES = 4         # meshes kept in the edge-topology cache
SILHOUETTE_TOPOLOGY_CACHE_FACES = 2_000_000  # faces the whole edge-topology cache may hold
SILHOUETTE_SCANLINE_MAX_CROSSINGS = 1_000_000  # edge/scanline pairs one pose may rasterise
SILHOUETTE_FACING_BLOCK = 1 << 18           # faces/edges per pass, bounding temporary memory
SILHOUETTE_WELD_TOLERANCE = 1e-6             # of the mesh extent: joins split PLY/OBJ vertices
# A projection whose true outline fills this much of its own convex envelope is treated as convex,
# where the envelope *is* the silhouette and the polygon ratio is exact. Below it the shape has a
# real opening, the envelope over-states the area, and only the rasterised outline can be trusted.
CONVEX_SILHOUETTE_RATIO = 0.9


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
class _EdgeTopology:
    """Undirected edge -> adjacent faces for one mesh, built once and cached.

    ``faces`` are welded triangles in model space and ``edges`` every undirected welded edge with
    its first two adjacent faces (``face_b`` is -1 on an open boundary). The rare edges shared by
    three or more faces keep their whole incidence in ``multi_*`` so a non-manifold junction is
    decided from all of its faces instead of from a guessed pair. Everything is built with one
    sort and one grouping pass, so a million-face part pays for it once, not once per pose.
    """
    vertices_m: np.ndarray
    faces: np.ndarray
    edges: np.ndarray
    face_a: np.ndarray
    face_b: np.ndarray
    multi_edges: np.ndarray
    multi_offsets: np.ndarray
    multi_faces: np.ndarray
    multi_counts: np.ndarray


def _weld_vertices(vertices: np.ndarray, faces: np.ndarray):
    """Remap faces onto coincident vertices, so a PLY that split its vertices still has topology.

    ``trimesh.load(process=False)`` keeps duplicated vertices, and every edge between two
    separately stored copies then looks like an open boundary: the visible outline would become a
    lattice of cell borders instead of one loop. Coincident positions are merged on a grid of
    ``SILHOUETTE_WELD_TOLERANCE`` times the mesh extent, which is far below any real feature and
    far above the float noise of a written PLY.
    """
    extent = float(np.max(np.ptp(vertices, axis=0))) if len(vertices) else 0.
    if not extent > 0:
        return vertices, faces
    tolerance = extent * SILHOUETTE_WELD_TOLERANCE
    keys = np.rint((vertices - vertices.min(axis=0)) / tolerance).astype(np.int64)
    keys -= keys.min(axis=0)
    # Fold the three integer axes into one sortable key. The span of each axis is extent/tolerance
    # = 1e6 by construction, so the key stays inside int64 instead of overflowing.
    linear = (keys[:, 0] * 1_000_003 + keys[:, 1]) * 1_000_003 + keys[:, 2]
    _, first, inverse = np.unique(linear, return_index=True, return_inverse=True)
    return vertices[first], inverse[faces]


def _build_edge_topology(mesh: MeshData):
    """Build the edge/face incidence of one mesh, or explain why it is not supported."""
    faces = np.asarray(mesh.faces)
    vertices = np.asarray(mesh.vertices_m, dtype=float)
    if faces.ndim != 2 or len(faces) == 0:
        return None, 'no_faces'
    if faces.shape[1] != 3:
        return None, 'non_triangular'
    if faces.max() >= len(vertices) or faces.min() < 0:
        return None, 'invalid_faces'
    if len(faces) > SILHOUETTE_TOPOLOGY_MAX_FACES:
        return None, 'topology_budget'
    points, welded = _weld_vertices(vertices, faces)
    welded = np.asarray(welded, dtype=np.int32)
    # Which way each corner traverses its edge: two triangles that share an edge with a coherent
    # surface orientation must walk it in opposite directions. A mesh that fails this has no
    # well-defined front and back, so its outline is undecidable rather than merely expensive.
    forward = np.concatenate([welded[:, 0] < welded[:, 1], welded[:, 1] < welded[:, 2],
                              welded[:, 2] < welded[:, 0]])
    pairs = np.concatenate([welded[:, [0, 1]], welded[:, [1, 2]], welded[:, [2, 0]]])
    pairs.sort(axis=1)
    owner = np.tile(np.arange(len(welded), dtype=np.int32), 3)
    keys = pairs[:, 0].astype(np.int64) * np.int64(len(points)) + pairs[:, 1]
    del pairs
    order = np.argsort(keys, kind='stable')
    keys = keys[order]
    owner = owner[order]
    forward = forward[order]
    # The keys are sorted, so grouping is one comparison pass instead of a second sort.
    starts = np.empty(len(keys), dtype=bool)
    starts[0] = True
    np.not_equal(keys[1:], keys[:-1], out=starts[1:])
    start = np.nonzero(starts)[0]
    counts = np.diff(np.append(start, len(keys)))
    unique = keys[start]
    del keys, starts
    if len(unique) > SILHOUETTE_TOPOLOGY_MAX_EDGES:
        return None, 'topology_budget'
    edges = np.stack([unique // len(points), unique % len(points)], axis=1).astype(np.int32)
    del unique
    paired = np.nonzero(counts == 2)[0]
    paired = paired[edges[paired, 0] != edges[paired, 1]]
    if np.any(forward[start[paired]] == forward[start[paired] + 1]):
        return None, 'inconsistent_winding'
    face_a = owner[start].astype(np.int32)
    face_b = np.where(counts > 1, owner[np.minimum(start + 1, len(owner) - 1)], -1).astype(np.int32)
    # Keep the full incidence of the edges shared by three or more faces; everything else needs at
    # most the two faces already stored. The compressed arrays cost nothing on a manifold mesh.
    multi = counts > 2
    multi_edges = np.nonzero(multi)[0].astype(np.int32)
    multi_counts = counts[multi].astype(np.int32)
    multi_faces = owner[np.repeat(multi, counts)].astype(np.int32)
    multi_offsets = np.zeros(len(multi_edges) + 1, dtype=np.int64)
    if len(multi_edges):
        np.cumsum(multi_counts, out=multi_offsets[1:])
    return _EdgeTopology(points, welded, edges, face_a, face_b, multi_edges, multi_offsets,
                         multi_faces, multi_counts), None


_TOPOLOGY_CACHE: "OrderedDict[int, tuple]" = OrderedDict()
_TOPOLOGY_LOCK = Lock()


def _trim_topology_cache() -> None:
    """Evict least-recently-used topologies until both cache ceilings hold. Caller holds the lock."""
    def cached_faces():
        return sum(len(entry[1].faces) for entry in _TOPOLOGY_CACHE.values()
                   if entry[1] is not None)

    while len(_TOPOLOGY_CACHE) > 1:
        if (len(_TOPOLOGY_CACHE) <= SILHOUETTE_TOPOLOGY_CACHE_MESHES
                and cached_faces() <= SILHOUETTE_TOPOLOGY_CACHE_FACES):
            return
        _TOPOLOGY_CACHE.popitem(last=False)


def _edge_topology(mesh: MeshData):
    """Cached :func:`_build_edge_topology` result for one live mesh object.

    The key is the object identity, validated through a weak reference: a collected mesh whose id
    is later reused rebuilds instead of silently reusing another mesh's edges. Failures are cached
    too, so an over-budget part is not re-sorted on every pose.
    """
    key = id(mesh)
    with _TOPOLOGY_LOCK:
        entry = _TOPOLOGY_CACHE.get(key)
        if entry is not None and entry[0]() is mesh:
            _TOPOLOGY_CACHE.move_to_end(key)
            return entry[1], entry[2]
        if entry is not None:
            del _TOPOLOGY_CACHE[key]
    topology, reason = _build_edge_topology(mesh)
    with _TOPOLOGY_LOCK:
        _TOPOLOGY_CACHE[key] = (weakref.ref(mesh), topology, reason)
        _TOPOLOGY_CACHE.move_to_end(key)
        _trim_topology_cache()
    return topology, reason


def _front_facing(topology: _EdgeTopology, world_from_model: np.ndarray, pose: CameraPose) -> np.ndarray:
    """Per-face front/back verdict for one pose, from the model-space camera position.

    The camera is moved into model space once, so the test only needs the mesh normals and a dot
    product: it stays exact for any rigid placement, including a rotation, and needs no stored
    world-space normals. A zero-area face has no side and counts as back-facing, which makes its
    neighbours report a transition and keeps the edge instead of dropping real geometry.
    """
    inverse = np.linalg.inv(np.asarray(world_from_model, dtype=float))
    camera_model = inverse[:3, :3] @ np.asarray(pose.position_m, dtype=float) + inverse[:3, 3]
    points, faces = topology.vertices_m, topology.faces
    front = np.zeros(len(faces), dtype=bool)
    for start in range(0, len(faces), SILHOUETTE_FACING_BLOCK):
        stop = min(start + SILHOUETTE_FACING_BLOCK, len(faces))
        block = faces[start:stop]
        first = points[block[:, 0]]
        normal = np.cross(points[block[:, 1]] - first, points[block[:, 2]] - first)
        front[start:stop] = np.einsum('ij,ij->i', normal, camera_model - first) > 0.
    return front


def _outline_edges(topology: _EdgeTopology, front: np.ndarray) -> np.ndarray:
    """Edges on the visible boundary of the surface: the outline even-odd filling needs.

    An edge between a front and a back face is where the surface turns away from the camera; an
    open boundary belongs to the outline whatever its side, so a one-sided plate still shows its
    real shape. An edge shared by three or more faces is kept only when *some* of its faces are
    front-facing and some are not, which is the same transition rule applied to its whole
    incidence rather than to an arbitrary pair.
    """
    selected = np.zeros(len(topology.edges), dtype=bool)
    open_boundary = topology.face_b < 0
    selected[open_boundary] = True
    paired = topology.face_b >= 0
    if len(topology.multi_edges):
        paired[topology.multi_edges] = False
    selected[paired] = (front[topology.face_a[paired]] != front[topology.face_b[paired]])
    if len(topology.multi_edges):
        incidence = front[topology.multi_faces].astype(np.int64)
        front_count = np.add.reduceat(incidence, topology.multi_offsets[:-1])
        selected[topology.multi_edges] = ((front_count > 0)
                                          & (front_count < topology.multi_counts))
    return selected


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
        # A mesh past the vertex budget is bounded by its box corners instead of every vertex. The
        # pinhole projection of a convex box contains the projection of everything inside it, so
        # the grid still covers the mask without touching half a million vertices on every pose.
        points = (mesh.vertices_m if len(mesh.vertices_m) <= SILHOUETTE_MASK_MAX_VERTICES
                  else bounds_corners(mesh.bounds_m))
        uv, valid = project_points(transform_points(points, matrix), pose, camera)
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


def _scanline_fill(segments: np.ndarray, grid: _SilhouetteGrid,
                   budget: int | None = None) -> np.ndarray | None:
    """Even-odd fill of one grid mask from projected outline segments.

    A pixel is inside when the number of outline crossings to its left is odd, so a hole stays a
    hole and two touching outlines do not invent an overlap. The work is the number of
    edge/scanline pairs, counted while walking the edges in bounded blocks: past ``budget`` the
    fill returns ``None`` instead of running away, and the caller keeps its envelope.
    """
    if budget is None:
        budget = SILHOUETTE_SCANLINE_MAX_CROSSINGS
    size = grid.size
    flips = np.zeros((size, size + 1), dtype=np.int64)
    crossings = 0
    for start in range(0, len(segments), SILHOUETTE_FACING_BLOCK):
        block = np.asarray(segments[start:start + SILHOUETTE_FACING_BLOCK], dtype=float)
        x0, y0, x1, y1 = block[:, 0], block[:, 1], block[:, 2], block[:, 3]
        low = np.minimum(y0, y1)
        high = np.maximum(y0, y1)
        # Half-open [low, high) on pixel-centre rows: an edge through a vertex is counted once.
        first = np.ceil(np.clip(low - .5, -2., size + 2.)).astype(np.int64)
        last = np.ceil(np.clip(high - .5, -2., size + 2.)).astype(np.int64)
        rows = np.clip(first, 0, size)
        counts = np.maximum(np.clip(last, 0, size) - rows, 0)
        total = int(counts.sum())
        crossings += total
        if crossings > budget:
            return None
        if not total:
            continue
        index = np.repeat(np.arange(len(block)), counts)
        offsets = np.cumsum(counts) - counts
        row = rows[index] + (np.arange(total) - np.repeat(offsets, counts))
        delta_y = (y1 - y0)[index]
        fraction = ((row + .5) - y0[index]) / delta_y
        column = np.floor(np.clip(x0[index] + fraction * (x1 - x0)[index] + .5,
                                  -1., size + 1.)).astype(np.int64)
        flat = row * (size + 1) + np.clip(column, 0, size)
        flips += np.bincount(flat, minlength=size * (size + 1)).reshape(size, size + 1)
    inside = (np.cumsum(flips, axis=1)[:, :size] & 1).astype(bool)
    return inside


@dataclass(frozen=True)
class _SilhouetteOutline:
    """The model-space edges that outline one mesh from one viewpoint.

    The edge set is chosen once with a single facing pass on the cached topology, so a translating
    occluder re-projects a few thousand endpoints per probe instead of rebuilding its topology or
    restating which of its faces face the camera. A rigid translation keeps each face's side fixed
    except for the faces that are exactly edge-on, which is why this is used inside one aim only.
    """
    vertices_m: np.ndarray
    edges: np.ndarray

    def mask(self, world_from_model, pose: CameraPose, camera,
             grid: _SilhouetteGrid) -> tuple:
        """Fill this outline on ``grid``, or say why the fill is not available."""
        pixels, valid = project_points(transform_points(self.vertices_m, world_from_model),
                                       pose, camera)
        if not valid.all():
            return None, 'behind_camera'
        mapped = grid.map(pixels)
        segments = np.concatenate([mapped[self.edges[:, 0]], mapped[self.edges[:, 1]]], axis=1)
        filled = _scanline_fill(segments, grid)
        if filled is None:
            return None, 'scanline_budget'
        return (filled, None) if filled.any() else (None, 'empty_outline')


def _outline_for(mesh: MeshData, world_from_model, pose: CameraPose, camera) -> tuple:
    """Outline of ``mesh`` as seen from ``pose``, or ``(None, reason)`` when it is undecidable.

    The outline must close: every welded vertex carries an even number of selected edges. An odd
    degree means the visible boundary has a loose end, and an even-odd fill would then invent or
    erase area, so the caller's envelope is the safe answer.
    """
    topology, reason = _edge_topology(mesh)
    if topology is None:
        return None, reason or 'no_topology'
    front = _front_facing(topology, world_from_model, pose)
    selected = _outline_edges(topology, front)
    if not selected.any():
        return None, 'empty_outline'
    edges = topology.edges[selected]
    degree = np.bincount(edges.ravel(), minlength=len(topology.vertices_m))
    if np.any(degree & 1):
        return None, 'open_outline'
    used = np.unique(edges)
    if len(used) < 3:
        return None, 'degenerate_outline'
    local = np.searchsorted(used, edges)
    return _SilhouetteOutline(topology.vertices_m[used], local), None


def _outline_mask(mesh: MeshData, world_from_model, pose: CameraPose, camera,
                  grid: _SilhouetteGrid) -> tuple:
    """Rasterised outline of a mesh too dense for its triangles, or ``(None, reason)``."""
    outline, reason = _outline_for(mesh, world_from_model, pose, camera)
    if outline is None:
        return None, reason
    return outline.mask(world_from_model, pose, camera, grid)


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


def _maskable(mesh: MeshData) -> bool:
    """True when the rasteriser can fill this mesh's own triangles inside its budget.

    The face count is a cost ceiling, never a quality gate: a six-face U-shaped part is a valid
    production surface and its projected triangles are its real silhouette. Only a mesh with no
    triangles at all, or one above the mask budget, falls back to the convex hull.
    """
    return (bool(len(mesh.faces)) and len(mesh.faces) <= SILHOUETTE_MASK_MAX_FACES
            and len(mesh.vertices_m) <= SILHOUETTE_MASK_MAX_VERTICES)


@dataclass(frozen=True)
class _TargetShape:
    """One target measured on the current pose, ready for either coverage estimator.

    ``hull`` is the exact projected convex envelope in screen units; ``mask`` is the rasterised
    true outline on the shared grid (``None`` when the part is too heavy to rasterise);
    ``envelope_mask`` is that same envelope filled on the grid. ``convex`` says the outline *is* its
    envelope, measured from this pair of real projections, so a solid box is convex and a frame or
    U-shaped plate is not, without a face-count threshold. ``fallback`` names why a mesh that could
    have been rasterised was not, so a support limit is reported instead of silently changing the
    measured shape.
    """
    hull: np.ndarray | None
    mask: np.ndarray | None
    envelope_mask: np.ndarray | None
    convex: bool
    fallback: str | None = None
    method: str = "triangles"
    diagnostic: dict | None = None


@dataclass(frozen=True)
class _TargetShapes:
    """Every target of this view, on one shared grid when any of them can be rasterised."""
    shapes: tuple[_TargetShape, ...]
    grid: _SilhouetteGrid | None

    @property
    def hulls(self) -> tuple[np.ndarray, ...]:
        """Targets measurable only by their convex hull: not supported, or not projectable."""
        return tuple(shape.hull for shape in self.shapes
                     if shape.mask is None and shape.hull is not None)

    @property
    def masks(self) -> tuple[np.ndarray, ...]:
        """Rasterised true outlines, without the envelopes measured alongside them."""
        return tuple(shape.mask for shape in self.shapes if shape.mask is not None)

    @property
    def fallbacks(self) -> tuple[str, ...]:
        """Why each unmasked target kept its envelope: a support limit, never a shape verdict."""
        return tuple(shape.fallback for shape in self.shapes
                     if shape.mask is None and shape.fallback is not None)

    @property
    def diagnostics(self) -> tuple[dict, ...]:
        """Public per-target measurements, including the reason a dense path was unavailable."""
        return tuple(shape.diagnostic for shape in self.shapes if shape.diagnostic is not None)


def _is_convex_silhouette(mask: np.ndarray, envelope_mask: np.ndarray) -> bool:
    """Whether the true outline fills its own envelope on this projection.

    Both rasters come from the pose actually sampled, so a solid box fills its envelope and a frame
    or U-shaped plate does not (it keeps its hole). Six faces can be either shape, which is why the
    test is the projected area ratio rather than a face count.
    """
    envelope = int(np.count_nonzero(envelope_mask))
    return bool(envelope) and int(np.count_nonzero(mask)) / envelope >= CONVEX_SILHOUETTE_RATIO


def _target_shapes(entries, pose: CameraPose, camera) -> _TargetShapes:
    """Measure each target silhouette once per pose: exact outline, envelope, and convexity.

    Triangles are rasterised while they fit the mask budget. Past it the part is not abandoned to
    its envelope: its cached silhouette edges are filled with the even-odd scanline, so a hollow
    high-face frame still keeps its hole. The envelope stays the answer only when the topology or
    the scan budget says no, and that reason travels with the shape.
    """
    masked, shapes = [], []
    for mesh, matrix in entries:
        hull = _silhouette(_silhouette_points(mesh), matrix, pose, camera)
        if _maskable(mesh):
            masked.append((mesh, matrix, hull, True))
        elif len(mesh.faces):
            masked.append((mesh, matrix, hull, False))
        elif hull is not None:
            shapes.append(_TargetShape(hull, None, None, True, 'no_faces'))
    grid = (_silhouette_grid([(mesh, matrix) for mesh, matrix, _, _ in masked], pose, camera)
            if masked else None)
    if grid is None:
        # A mesh behind the camera (or a degenerate projection) cannot be masked; keep the
        # affine hull fallback rather than rejecting an otherwise valid target.
        for mesh, matrix, hull, _ in masked:
            if hull is not None:
                shapes.append(_TargetShape(hull, None, None, True, 'no_projection'))
        return _TargetShapes(tuple(shapes), None)
    for mesh, matrix, hull, triangles in masked:
        method, diagnostic = "triangles", None
        if triangles:
            mask, reason = _rasterize_mask(mesh, matrix, pose, camera, grid, triangles=True), None
        else:
            # Past the triangle raster budget, true block-wise triangle union is the primary
            # path: it keeps holes/repeated/overlapping components and needs no whole topology.
            mask, diagnostic = stream_coverage(mesh, matrix, pose, camera, grid)
            method = "triangle_stream"
            reason = None if mask is not None else f"stream:{diagnostic.fallback_reason}"
            if mask is None:
                # Cached edge outlines stay a fallback for meshes the streaming limits reject.
                mask, outline_reason = _outline_mask(mesh, matrix, pose, camera, grid)
                method = "outline"
                reason = None if mask is not None else f"{reason}/{outline_reason}"
        if mask is None:
            if hull is not None:
                shapes.append(_TargetShape(hull, None, None, True, reason, "hull",
                                           {"asset_key": mesh.key, "method": "hull",
                                            "estimate_only": True, "fallback_reason": reason,
                                            "grid_size": grid.size, "faces": len(mesh.faces)}))
            continue
        envelope_mask = _rasterize_mask(mesh, matrix, pose, camera, grid, triangles=False)
        shapes.append(_TargetShape(hull, mask, envelope_mask,
                                   _is_convex_silhouette(mask, envelope_mask), reason,
                                   method, None if diagnostic is None else diagnostic.as_dict()))
    return _TargetShapes(tuple(shapes), grid)


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


def _offset_span(base, direction, mesh, scale, points, pose):
    """Interval of sideways offsets at which the occluder can still touch a target.

    The occluder moves at its own depth, so a target point's bearing is converted into the offset
    that puts the occluder on the same ray with the camera's ``depth / point_depth`` factor: an
    occluder halfway to the lens needs half the sideways shift for the same screen position. The
    interval spans every target, which is what lets a small occluder inside a large hole reach the
    rim instead of stopping after ``2.5 * spread`` near the hole's centre. The occluder's own
    half-width widens the interval so a rim exactly at the edge is still sampled.
    """
    if points is None or not len(points):
        return None
    camera = np.asarray(pose.position_m, dtype=float)
    to_base = np.asarray(base, dtype=float) - camera
    depth = float(np.linalg.norm(to_base))
    if depth <= 0:
        return None
    view = to_base / depth
    points = np.asarray(points, dtype=float)
    ray_depth = (points - camera) @ view
    if np.any(ray_depth <= depth * 1e-6):
        return None        # a point at (or behind) the lens has no finite aim on this ray
    perpendicular = points - camera - ray_depth[:, None] * view
    along = (perpendicular @ direction) * (depth / ray_depth)
    half = float((np.ptp(mesh.bounds_m, axis=0) * scale / 2) @ np.abs(direction))
    low, high = float(along.min()) - half, float(along.max()) + half
    return (low, high, half) if high - low > 0 else None


def _aimed_offset(base, direction, mesh, scale, shapes: _TargetShapes, pose, config, rng, spread,
                  target_points=None, diagnostics_out: list | None = None):
    """Sideways offset that should cover roughly the middle of the occlusion window.

    The renderer used to reject most proposals for over-covering: a box three quarters of the
    target's width, offset by a tenth of it, hides most of the silhouette. Coverage is measured
    against the projected *mesh* silhouette, but it does not fall monotonically from the target's
    centre: a hollow frame is empty in the hole, so a small occluder has to travel to the rim
    before it covers anything. The search therefore scans the target's whole projected span
    (``_offset_span``), keeps the crossing of the user's window that brackets the aim most
    tightly, and bisects only inside that bracket. Every proposal has a fixed probe budget; when
    no offset can reach the window the best measured candidate is returned and the render's own
    pixel acceptance rejects it, rather than the sampler looping or inventing coverage.
    """
    wanted = config.occlusion.ratio
    if wanted.maximum <= 0 or not shapes.shapes:
        return spread * rng.uniform(.1, .9)
    # Aim inside the *user's* closed window, never near it: an 80-90 % window must not be aimed at
    # 72 %, and a 0-1 % window must not be aimed at 2 %. Clamping after the midpoint keeps the
    # target valid for any interval, including a fixed ratio where minimum == maximum.
    aim = min(max((wanted.minimum + wanted.maximum) / 2, wanted.minimum), wanted.maximum)

    def matrix_at(offset):
        matrix = np.eye(4)
        matrix[:3, :3] *= scale
        matrix[:3, 3] = base + direction * offset - matrix[:3, :3] @ mesh.bounds_m.mean(axis=0)
        return matrix

    # Convexity is translation invariant, so the occluder is classified once at the search origin
    # rather than on every probe: a solid box has an exact envelope, a hollow one does not. Dense
    # parts are measured by the same streamed triangle union as the target, never by the legacy
    # even-odd outline first: that fill cancels the overlap of separate components.
    grid = shapes.grid
    outline_cache: dict = {}
    reported_stream_failures: set = set()
    reported_empty_reasons: set = set()

    def record_stream_failure(matrix, diagnostic, method):
        if diagnostics_out is None:
            return
        reason = diagnostic.fallback_reason or "unavailable"
        if reason in reported_stream_failures:
            return
        reported_stream_failures.add(reason)
        diagnostics_out.append({
            "asset_key": mesh.key, "method": method, "estimate_only": False,
            "fallback_reason": f"stream:{reason}", "grid_size": None if grid is None else grid.size,
            "faces": len(mesh.faces),
        })

    def occluder_real(matrix):
        """The occluder's own surface on the shared grid, or None when only its envelope is left."""
        if grid is None:
            return None
        if _maskable(mesh):
            return _rasterize_mask(mesh, matrix, pose, config.camera, grid, triangles=True)
        mask, diagnostic = stream_coverage(mesh, matrix, pose, config.camera, grid)
        if mask is not None:
            return mask
        if diagnostic.fallback_reason in ("empty_coverage", "degenerate_surface", "behind_camera"):
            # The shared grid only has to cover the targets. A probe whose occluder projects
            # entirely outside it (or behind the camera) cannot cover any target pixel, so an
            # explicit empty surface is the true answer and no legacy outline needs building.
            if diagnostics_out is not None and diagnostic.fallback_reason not in reported_empty_reasons:
                reported_empty_reasons.add(diagnostic.fallback_reason)
                diagnostics_out.append({
                    "asset_key": mesh.key, "method": "triangle_stream", "estimate_only": False,
                    "fallback_reason": None, "grid_size": grid.size, "faces": len(mesh.faces),
                    "note": diagnostic.fallback_reason,
                })
            return np.zeros((grid.size, grid.size), dtype=bool)
        if "outline" not in outline_cache:
            # The legacy outline is a fallback for inputs the streaming limits reject, not the
            # primary dense path. Its topology is still built once per aim.
            outline_cache["outline"] = _outline_for(mesh, matrix, pose, config.camera)
        outline, _reason = outline_cache["outline"]
        if outline is None:
            record_stream_failure(matrix, diagnostic, "unavailable")
            return None
        result = outline.mask(matrix, pose, config.camera, grid)[0]
        record_stream_failure(matrix, diagnostic, "outline")
        return result

    origin = occluder_real(matrix_at(0.))
    occluder_convex = grid is None or (origin is not None and _is_convex_silhouette(
        origin, _rasterize_mask(mesh, matrix_at(0.), pose, config.camera, grid, triangles=False)))

    def estimate(offset):
        """Covered share of the best-covered target: exact polygons when convex, else real masks.

        A convex target's projected envelope *is* its silhouette, and so is a convex occluder's, so
        their overlap ratio is an exact polygon measure with no rasterisation quantum; that is the
        only way a fixed window such as 0.3 -> 0.3 is reachable. A concave or hollow shape keeps its
        real rasterised outline, because the envelope would fill a hole the render still shows.
        """
        matrix = matrix_at(offset)
        regions = {}

        def envelope():
            """The occluder's projected convex envelope, projected only if an aim ends up using it."""
            if "hull" not in regions:
                regions["hull"] = _silhouette(_silhouette_points(mesh), matrix, pose, config.camera)
            return regions["hull"]

        def occluder_region():
            """The occluder's exact envelope when it is convex, else its real surface."""
            if occluder_convex:
                if "convex" not in regions:
                    regions["convex"] = _rasterize_mask(mesh, matrix, pose, config.camera, grid,
                                                        triangles=False)
                return regions["convex"]
            if "mask" not in regions:
                regions["mask"] = occluder_real(matrix)
            return regions["mask"]

        shares = []
        for shape in shapes.shapes:
            target_mask = shape.envelope_mask if shape.convex else shape.mask
            if shape.convex and occluder_convex and envelope() is not None:
                # Both envelopes are the shapes themselves: an exact polygon ratio, no grid quantum.
                shares.append(_covered_share(shape.hull, envelope()))
                continue
            region = occluder_region() if target_mask is not None and grid is not None else None
            if region is not None:
                shares.append(_mask_covered_share(target_mask, region))
                continue
            hull = envelope()
            if hull is not None and shape.hull is not None:
                # Nothing could be rasterised for this pose: the envelopes are all that is left.
                shares.append(_covered_share(shape.hull, hull))
        return max(shares, default=0.)

    span = _offset_span(base, direction, mesh, scale, target_points, pose)
    if span is None:
        # No target geometry to derive a span from: keep the old conservative bound.
        low, high, half = 0., max(spread * 2.5, 1e-9), 0.
    else:
        low, high, half = span
    width = high - low
    # Four probes per occluder width (``half`` is its half-width) so a narrow crossing is not
    # stepped over, but never fewer than OFFSET_SCAN_MIN_SAMPLES equal intervals: the window of a
    # small occluder can be a fraction of its own width, and a large hollow target's span is what
    # the old ``half`` spacing crossed with a handful of points.
    step = min(half / 2 if half > 0 else width, width / OFFSET_SCAN_MIN_SAMPLES)
    samples = int(np.clip(np.ceil(width / step) if step > 0 else OFFSET_SCAN_MIN_SAMPLES,
                          OFFSET_SCAN_MIN_SAMPLES, OFFSET_SCAN_MAX_SAMPLES))
    measured = [(float(estimate(offset)), float(offset))
                for offset in np.linspace(low, high, samples + 1)]
    best_share, best_offset = min(measured, key=lambda item: abs(item[0] - aim))

    # Coverage may cross the aim several times (a hollow target crossed at either rim) or jump over
    # the window between two coarse probes. Every sign change brackets a crossing; the ones whose
    # ends sit closest to the aim are refined, and the best measured offset wins. A plateau of equal
    # estimates never counts as a crossing, so an unreachable window is not invented here.
    brackets = [(measured[index], measured[index + 1]) for index in range(len(measured) - 1)
                if (measured[index][0] - aim) * (measured[index + 1][0] - aim) < 0]
    brackets.sort(key=lambda pair: max(abs(pair[0][0] - aim), abs(pair[1][0] - aim)))
    # The refinement budget follows the estimator. A rasterised share moves in whole grid pixels, so
    # a bracket finer than a thousandth of the probed span cannot improve it; the convex polygon
    # share has no quantum and is bisected until the aim itself is met, which is what makes a fixed
    # window reachable instead of merely approached.
    exact = occluder_convex and all(shape.convex for shape in shapes.shapes)
    if exact:
        refinements, span_tolerance, share_tolerance = (
            OFFSET_SCAN_EXACT_REFINEMENTS, width * 1e-9, OFFSET_SCAN_SHARE_TOLERANCE)
    else:
        refinements, span_tolerance, share_tolerance = (
            OFFSET_SCAN_MAX_REFINEMENTS, width * 1e-3, 0.)
    for (left_share, left), (_, right) in brackets[:OFFSET_SCAN_MAX_BRACKETS]:
        for _ in range(refinements):
            if right - left <= span_tolerance or abs(best_share - aim) <= share_tolerance:
                break
            middle = (left + right) / 2
            middle_share = estimate(middle)
            if abs(middle_share - aim) < abs(best_share - aim):
                best_share, best_offset = middle_share, middle
            if (middle_share - aim) * (left_share - aim) <= 0:
                right = middle
            else:
                left, left_share = middle, middle_share
    return best_offset


def occluder_placements(config: ProjectConfig, meshes: Mapping[str, MeshData], pose: CameraPose,
                        anchor_key: str, rng, targets: Sequence[Placement] = (),
                        diagnostics_out: list | None = None) -> list[Placement] | Rejection:
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
    if diagnostics_out is not None:
        diagnostics_out.extend(target_shapes.diagnostics)
    # World corners bound where the occluder may have to travel: the offset search scans this span
    # so hollow targets are reachable at their rim, not only near their centre.
    target_points = (np.vstack([transform_points(bounds_corners(mesh.bounds_m), matrix)
                                for mesh, matrix in target_entries]) if target_entries else None)
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
                base, direction, mesh, scale, target_shapes, pose, config, rng, apparent * ratio,
                target_points, diagnostics_out)
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
    diagnostics: list = []
    if not scene.wants_unoccluded and config.occluders:
        occluders = occluder_placements(config, meshes, pose, config.anchor_key, rng,
                                        targets=[p for p in scene.placements if p.role == 'target'],
                                        diagnostics_out=diagnostics)
        if isinstance(occluders, Rejection):
            return occluders
        placements.extend(occluders)
    return SceneCandidate(scene.scene_index, view_index, attempt_index, region_key, config.anchor_key, pose,
                          tuple(placements), scene.hdri_path, scene.hdri_strength, scene.hdri_rotation_deg,
                          scene.light, scene.wants_unoccluded, kind, material=scene.material,
                          layout_attempt_index=scene.layout_attempt_index,
                          silhouette_diagnostics=tuple(diagnostics))


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
        int(payload.get("layout_attempt_index", 0)),
        tuple(payload.get("silhouette_diagnostics") or ()))
