"""Bounded true-coverage silhouettes for meshes past the legacy topology caps.

The production sampler used global even-odd outline filling, which cannot promise a union when
separate components overlap and needs whole-mesh edge topology. This module projects the
canonical triangles in fixed face blocks and accumulates per-scanline x intervals into a
difference array; ``cumsum > 0`` is the union of every triangle. Duplicate, separated and
overlapping components therefore add coverage instead of cancelling it, while holes stay empty.
Extra working memory is bounded by :data:`STREAM_WORK_BUDGET_BYTES`; the canonical mesh itself is
never copied as a whole.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import threading
import time
import weakref

import numpy as np

from .assets import MeshData
from .camera import CameraPose, project_points
from .config import CameraSpec


STREAM_FACE_CHUNK = 8192
STREAM_GRID_SIZE = 256
STREAM_WORK_BUDGET_BYTES = 64 * 1024 * 1024
STREAM_CACHE_ENTRIES = 16
STREAM_CACHE_BYTES = 32 * 1024 * 1024
# Conservative bytes per triangle/scanline pair: row index, three edge arrays, x/min/max,
# interval ends and the difference-array indices. Measured peaks stay below this estimate.
_BYTES_PER_PAIR = 192


@dataclass(frozen=True)
class CoverageGrid:
    left_px: float
    top_px: float
    pixels_per_px: float
    size: int = STREAM_GRID_SIZE

    def map(self, uv) -> np.ndarray:
        return (np.asarray(uv, dtype=float) - [self.left_px, self.top_px]) * self.pixels_per_px


@dataclass(frozen=True)
class SilhouetteDiagnostic:
    asset_key: str
    method: str
    grid_size: int
    faces: int
    degenerate_faces: int
    fallback_reason: str | None
    estimate_only: bool
    chunks: int
    seconds: float
    covered_pixels: int
    cached: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _CachedMask:
    mesh_id: int
    reference: "weakref.ReferenceType"
    key: tuple
    mask: np.ndarray
    diagnostic: SilhouetteDiagnostic


_CACHE: "OrderedDict[int, _CachedMask]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_key(mesh: MeshData, matrix, pose: CameraPose, camera: CameraSpec,
               grid: CoverageGrid) -> tuple:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(np.ascontiguousarray(matrix, dtype=np.float64).tobytes())
    digest.update(np.ascontiguousarray(pose.world_from_camera, dtype=np.float64).tobytes())
    digest.update(np.ascontiguousarray(pose.position_m, dtype=np.float64).tobytes())
    digest.update(np.asarray([pose.radius_m, pose.azimuth_deg, pose.elevation_deg,
                              pose.clip_near_m, pose.clip_far_m], dtype=np.float64).tobytes())
    digest.update(np.asarray([camera.width, camera.height, camera.fx, camera.fy,
                              camera.cx, camera.cy], dtype=np.float64).tobytes())
    digest.update(np.asarray([grid.left_px, grid.top_px, grid.pixels_per_px, grid.size],
                             dtype=np.float64).tobytes())
    return digest.digest()


def _cached(mesh, key):
    with _CACHE_LOCK:
        entry = _CACHE.get(id(mesh))
        if entry is None or entry.reference() is not mesh or entry.key != key:
            return None
        _CACHE.move_to_end(id(mesh))
        return entry


def _remember(mesh, key, mask, diagnostic):
    with _CACHE_LOCK:
        _CACHE[id(mesh)] = _CachedMask(id(mesh), weakref.ref(mesh), key, mask, diagnostic)
        _CACHE.move_to_end(id(mesh))
        while len(_CACHE) > STREAM_CACHE_ENTRIES:
            _CACHE.popitem(last=False)
        while len(_CACHE) > 1 and sum(e.mask.nbytes for e in _CACHE.values()) > STREAM_CACHE_BYTES:
            _CACHE.popitem(last=False)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def cache_entries() -> int:
    with _CACHE_LOCK:
        return len(_CACHE)


def _failure(mesh, reason, *, faces=0, degenerate=0):
    diagonal = SilhouetteDiagnostic(str(mesh.key), "unavailable", STREAM_GRID_SIZE, int(faces),
                                    int(degenerate), reason, False, 0, 0.0, 0)
    return None, diagonal


def _raster_block(corners: np.ndarray, first: np.ndarray, last: np.ndarray,
                  size: int, delta_flat: np.ndarray) -> int:
    """Union-fill one face sub-block; returns the number of triangle/row pairs processed."""
    counts = np.maximum(last - first + 1, 0)
    total = int(counts.sum())
    if not total:
        return 0
    triangle = np.repeat(np.arange(len(corners)), counts)
    offsets = np.cumsum(counts) - counts
    rows = first[triangle] + (np.arange(total) - np.repeat(offsets, counts))
    y = rows + 0.5
    points = corners[triangle]                       # (total, 3, 2)
    xmin = np.full(total, np.inf)
    xmax = np.full(total, -np.inf)
    for a, b in ((0, 1), (1, 2), (2, 0)):
        ya, yb = points[:, a, 1], points[:, b, 1]
        low, high = np.minimum(ya, yb), np.maximum(ya, yb)
        active = (low <= y) & (y < high)
        if not active.any():
            continue
        dy = np.where(active, yb - ya, 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            x = points[:, a, 0] + (y - ya) * (points[:, b, 0] - points[:, a, 0]) / dy
        xmin = np.where(active, np.minimum(xmin, x), xmin)
        xmax = np.where(active, np.maximum(xmax, x), xmax)
    valid = np.isfinite(xmin) & np.isfinite(xmax)
    if not valid.any():
        return total
    left = np.floor(xmin[valid] + 0.5)
    right = np.floor(xmax[valid] + 0.5)
    left = np.clip(left, 0, size - 1).astype(np.int64)
    right = np.clip(right, 0, size - 1).astype(np.int64)
    keep = right >= left
    if not keep.any():
        return total
    row = rows[valid][keep].astype(np.int64)
    left, right = left[keep], right[keep]
    stride = size + 1
    np.add.at(delta_flat, row * stride + left, 1)
    np.add.at(delta_flat, row * stride + right + 1, -1)
    return total


def stream_coverage(mesh: MeshData, world_from_model, pose: CameraPose, camera: CameraSpec,
                    grid: CoverageGrid) -> tuple[np.ndarray | None, SilhouetteDiagnostic]:
    """True projected union of every triangle, or a stable reason why it is unavailable."""
    started = time.perf_counter()
    vertices = np.asarray(mesh.vertices_m)
    faces = np.asarray(mesh.faces)
    if faces.ndim != 2 or faces.shape[1] != 3:
        return _failure(mesh, "non_triangular")
    if len(faces) == 0:
        return _failure(mesh, "no_faces")
    if (not np.isfinite(vertices).all() or vertices.ndim != 2 or vertices.shape[1] != 3):
        return _failure(mesh, "non_finite_vertices", faces=len(faces))
    if faces.min() < 0 or faces.max() >= len(vertices):
        return _failure(mesh, "invalid_faces", faces=len(faces))
    key = _cache_key(mesh, world_from_model, pose, camera, grid)
    hit = _cached(mesh, key)
    if hit is not None:
        diagnostic = SilhouetteDiagnostic(**{**hit.diagnostic.as_dict(), "cached": True,
                                             "seconds": 0.0})
        return hit.mask, diagnostic
    size = int(grid.size)
    delta = np.zeros((size, size + 1), dtype=np.int32)
    delta_flat = delta.reshape(-1)
    matrix = np.asarray(world_from_model, dtype=np.float64)
    rotation, translation = matrix[:3, :3], matrix[:3, 3]
    pair_budget = max(size, STREAM_WORK_BUDGET_BYTES // _BYTES_PER_PAIR)
    sub_faces = max(1, min(STREAM_FACE_CHUNK, pair_budget // size))
    degenerate = 0
    chunks = 0
    projected_any = False
    for start in range(0, len(faces), STREAM_FACE_CHUNK):
        block = faces[start:start + STREAM_FACE_CHUNK]
        chunks += 1
        world = vertices[block] @ rotation.T + translation
        uv, valid = project_points(world.reshape(-1, 3), pose, camera)
        uv = uv.reshape(-1, 3, 2)
        valid = valid.reshape(-1, 3)
        all_valid = valid.all(axis=1)
        if not all_valid.any():
            continue
        projected_any = True
        pixel = grid.map(uv)
        area = np.abs((pixel[:, 1, 0] - pixel[:, 0, 0]) * (pixel[:, 2, 1] - pixel[:, 0, 1])
                      - (pixel[:, 1, 1] - pixel[:, 0, 1]) * (pixel[:, 2, 0] - pixel[:, 0, 0]))
        usable = all_valid & (area > 1e-12)
        degenerate += int(np.count_nonzero(all_valid & ~(area > 1e-12)))
        if not usable.any():
            continue
        chosen = pixel[usable]
        ymin = chosen[:, :, 1].min(axis=1)
        ymax = chosen[:, :, 1].max(axis=1)
        first = np.clip(np.ceil(ymin - 0.5), 0, size - 1).astype(np.int64)
        last = np.clip(np.ceil(ymax - 0.5) - 1, 0, size - 1).astype(np.int64)
        for sub in range(0, len(chosen), sub_faces):
            _raster_block(chosen[sub:sub + sub_faces], first[sub:sub + sub_faces],
                          last[sub:sub + sub_faces], size, delta_flat)
    elapsed = time.perf_counter() - started
    if not projected_any:
        return _failure(mesh, "behind_camera", faces=len(faces), degenerate=degenerate)
    mask = np.cumsum(delta, axis=1)[:, :size] > 0
    covered = int(np.count_nonzero(mask))
    if covered == 0:
        return _failure(mesh, "degenerate_surface" if degenerate else "empty_coverage",
                        faces=len(faces), degenerate=degenerate)
    diagnostic = SilhouetteDiagnostic(str(mesh.key), "triangle_stream", size, len(faces),
                                      degenerate, None, False, chunks, elapsed, covered)
    _remember(mesh, key, mask, diagnostic)
    return mask, diagnostic
