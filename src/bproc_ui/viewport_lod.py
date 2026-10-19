"""Display-only mesh simplification and its background controller.

Both VisPy viewports draw the meshes returned here; the canonical :class:`MeshData` used by
placement, collision, sampling, export and native rendering is never modified or replaced. A
heavy mesh is simplified in a worker thread, and until that result exists the viewport draws a
wireframe proxy of the canonical bounds instead of uploading the full arrays.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time
import weakref

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from bproc_core.assets import MeshData


ALGORITHM_VERSION = 1
MAX_CACHE_MESHES = 8
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_SIMPLIFY_ROUNDS = 12
GROWTH_FACTOR = 1.35
# A lattice cell may not exceed this many per axis; the count is bounded by the face budget.
MAX_CELLS_PER_AXIS = 4096


@dataclass(frozen=True)
class DisplayMesh:
    """What a viewport may upload. ``bounds_m`` always comes from the canonical mesh."""

    key: str
    vertices_m: np.ndarray
    faces: np.ndarray
    vertex_rgba: np.ndarray | None
    bounds_m: np.ndarray
    source_faces: int
    display_faces: int
    simplified: bool
    method: str
    reason: str | None = None
    elapsed_s: float = 0.0
    worker_thread: str = ""

    @property
    def nbytes(self) -> int:
        total = int(self.vertices_m.nbytes) + int(self.faces.nbytes) + int(self.bounds_m.nbytes)
        if self.vertex_rgba is not None:
            total += int(self.vertex_rgba.nbytes)
        return total

    def as_dict(self) -> dict:
        return {"key": self.key, "source_faces": int(self.source_faces),
                "display_faces": int(self.display_faces), "simplified": bool(self.simplified),
                "method": self.method, "reason": self.reason,
                "elapsed_s": float(self.elapsed_s), "worker_thread": self.worker_thread,
                "bytes": self.nbytes}


def original_display_mesh(mesh: MeshData) -> DisplayMesh:
    """Full-resolution display data sharing the canonical arrays object-for-object."""
    return DisplayMesh(mesh.key, mesh.vertices_m, mesh.faces, mesh.vertex_rgba, mesh.bounds_m,
                       len(mesh.faces), len(mesh.faces), False, "original")


def _cluster(vertices: np.ndarray, faces: np.ndarray, colors: np.ndarray | None, cell: float):
    """One lattice-clustering pass. Returns (vertices, faces, colours) or ``None`` on empty."""
    low = vertices.min(axis=0)
    extent = np.ptp(vertices, axis=0)
    cells = np.maximum(1, np.ceil(extent / cell).astype(np.int64))
    cells = np.minimum(cells, MAX_CELLS_PER_AXIS)
    keys = np.floor((vertices - low) / cell).astype(np.int64)
    keys -= keys.min(axis=0)
    keys = np.minimum(keys, cells - 1)
    linear = (keys[:, 0] * cells[1] + keys[:, 1]) * cells[2] + keys[:, 2]
    unique, inverse, counts = np.unique(linear, return_inverse=True, return_counts=True)
    del unique
    if len(counts) == 0:
        return None
    inverse = inverse.reshape(-1)
    points = np.column_stack([np.bincount(inverse, weights=vertices[:, axis], minlength=len(counts))
                              / counts for axis in range(3)])
    vertex_colors = None
    if colors is not None:
        values = np.asarray(colors, dtype=np.float64)
        alpha = np.full((len(counts), 1), float(values[:, 3].min()))
        rgb = np.column_stack([np.bincount(inverse, weights=values[:, axis], minlength=len(counts))
                               / counts for axis in range(3)])
        vertex_colors = np.clip(np.rint(np.hstack([rgb, alpha])), 0, 255).astype(np.uint8)
    welded = inverse[faces]
    a, b, c = welded[:, 0], welded[:, 1], welded[:, 2]
    valid = (a != b) & (b != c) & (a != c)
    clipped = welded[valid]
    if len(clipped) == 0:
        return points, clipped.reshape(0, 3), vertex_colors
    ordered = np.sort(clipped, axis=1)
    n = len(points)
    keys = (ordered[:, 0] * n + ordered[:, 1]) * n + ordered[:, 2]
    _, first = np.unique(keys, return_index=True)
    return points, ordered[first], vertex_colors


def simplify_display_mesh(mesh: MeshData, target_faces: int, *,
                          version: int = ALGORITHM_VERSION) -> DisplayMesh:
    """Deterministic vertex-cluster LOD, or the original arrays when already within budget.

    The representative vertex of a cluster is the mean of its members, so the result keeps the
    canonical metre frame (no re-centring or scaling). Colours are averaged per cluster and
    alpha keeps the darkest member, which never invents transparency. Degenerate and duplicate
    triangles are dropped; the face count is strictly bounded by ``target_faces`` whenever a
    non-empty clustering exists.
    """
    if isinstance(target_faces, bool) or not isinstance(target_faces, int) or target_faces < 1:
        raise ValueError("目标面数必须是正整数")
    if version != ALGORITHM_VERSION:
        raise ValueError("不支持的视口 LOD 算法版本")
    started = time.perf_counter()
    thread = threading.current_thread().name
    faces = np.asarray(mesh.faces)
    if len(faces) <= target_faces:
        return DisplayMesh(mesh.key, mesh.vertices_m, mesh.faces, mesh.vertex_rgba, mesh.bounds_m,
                           len(faces), len(faces), False, "original", worker_thread=thread)
    vertices = np.asarray(mesh.vertices_m, dtype=np.float64)
    extent = float(np.ptp(vertices, axis=0).max())
    if not np.isfinite(extent) or extent <= 0:
        return DisplayMesh(mesh.key, mesh.vertices_m, mesh.faces, mesh.vertex_rgba, mesh.bounds_m,
                           len(faces), 0, False, "failed", "degenerate_bounds", worker_thread=thread)
    # Start around a lattice that should produce roughly the requested number of surface cells.
    cell = extent / max(2.0, np.sqrt(target_faces / 6.0))
    best = None
    best_faces = None
    for _ in range(MAX_SIMPLIFY_ROUNDS):
        clustered = _cluster(vertices, faces, mesh.vertex_rgba, cell)
        if clustered is not None and len(clustered[1]):
            points, new_faces, colors = clustered
            if best_faces is None or len(new_faces) < best_faces:
                best, best_faces = (points, new_faces, colors), len(new_faces)
            if len(new_faces) <= target_faces:
                return DisplayMesh(mesh.key, points, new_faces, colors, mesh.bounds_m,
                                   len(faces), len(new_faces), True, "vertex_cluster",
                                   worker_thread=thread,
                                   elapsed_s=time.perf_counter() - started)
        cell *= GROWTH_FACTOR
    if best is None or best_faces is None:
        return DisplayMesh(mesh.key, mesh.vertices_m, mesh.faces, mesh.vertex_rgba, mesh.bounds_m,
                           len(faces), 0, False, "failed", "no_valid_surface", worker_thread=thread)
    points, new_faces, colors = best
    return DisplayMesh(mesh.key, points, new_faces, colors, mesh.bounds_m, len(faces),
                       len(new_faces), True, "vertex_cluster", "target_not_met",
                       time.perf_counter() - started, thread)


@dataclass(frozen=True)
class LodRequest:
    key: str
    mesh: MeshData
    target_faces: int
    threshold_faces: int
    generation: int
    params: tuple


class _Signals(QObject):
    finished = Signal(object)


class _LodTask(QRunnable):
    def __init__(self, request: LodRequest, signals: _Signals):
        super().__init__()
        self.request = request
        self.signals = signals

    def run(self):
        try:
            display = simplify_display_mesh(self.request.mesh, self.request.target_faces)
            payload = (self.request, display, None)
        except Exception as error:       # noqa: BLE001 - reported as a failed LOD, never re-raised
            payload = (self.request, None, f"{type(error).__name__}: {error}")
        self.signals.finished.emit(payload)


class ViewportLodController(QObject):
    """One background LOD task at a time, latest request wins, bounded identity-checked cache."""

    ready = Signal(str, object)
    failed = Signal(str, str)
    status = Signal(str, str)

    def __init__(self, parent=None, pool=None):
        super().__init__(parent)
        self.pool = pool or QThreadPool(self)
        if pool is None:
            self.pool.setMaxThreadCount(1)
        self._signals = _Signals(self)
        self._signals.finished.connect(self._finished)
        self._cache: "OrderedDict[int, tuple]" = OrderedDict()
        self._generation = 0
        self._inflight = False
        self._pending: LodRequest | None = None
        self._task = None
        self.closed = False
        self.cache_hits = 0
        self.computes = 0
        self.last_worker_thread = ""
        self.main_thread = threading.current_thread().ident
        self.context_revision = 0
        self.mesh_revision = 0
        self.target_faces = 0
        self.threshold_faces = 0

    # -- context -----------------------------------------------------------------------------
    def set_context(self, context_revision: int, mesh_revision: int | None = None) -> None:
        changed = (int(context_revision) != self.context_revision
                   or (mesh_revision is not None and int(mesh_revision) != self.mesh_revision))
        self.context_revision = int(context_revision)
        if mesh_revision is not None:
            self.mesh_revision = int(mesh_revision)
        if changed:
            self.invalidate()

    def set_parameters(self, target_faces: int, threshold_faces: int) -> None:
        if (target_faces, threshold_faces) != (self.target_faces, self.threshold_faces):
            self.target_faces, self.threshold_faces = int(target_faces), int(threshold_faces)
            self.invalidate()

    def invalidate(self) -> None:
        """Drop pending work and cached displays; in-flight results are discarded on arrival."""
        self._generation += 1
        self._pending = None
        self._cache.clear()
        if self.closed:
            return

    def clear(self) -> None:
        self.invalidate()

    def close(self) -> None:
        self.closed = True
        self._generation += 1
        self._pending = None
        self._cache.clear()
        wait = getattr(self.pool, "waitForDone", None)
        if wait is not None:
            wait(30_000)                     # do not leave a worker emitting into a deleted window

    # -- requests ----------------------------------------------------------------------------
    def cached(self, mesh: MeshData) -> DisplayMesh | None:
        entry = self._cache.get(id(mesh))
        if entry is None:
            return None
        if entry[0]() is not mesh:
            self._purge_dead()
            return None
        params = (ALGORITHM_VERSION, self.target_faces, self.threshold_faces)
        if entry[1] != params:
            return None
        self._cache.move_to_end(id(mesh))
        self.cache_hits += 1
        return entry[2]

    def request(self, key: str, mesh: MeshData, target_faces: int | None = None,
                threshold_faces: int | None = None) -> DisplayMesh | None:
        """A display mesh if available, else ``None`` while a background task is scheduled."""
        target = self.target_faces if target_faces is None else int(target_faces)
        threshold = self.threshold_faces if threshold_faces is None else int(threshold_faces)
        self.set_parameters(target, threshold)
        cached = self.cached(mesh)
        if cached is not None:
            return cached
        if self.closed:
            return None
        if len(mesh.faces) <= threshold:
            # A mesh inside the threshold is already the canonical display: sharing its arrays
            # keeps face count, colours and framing byte-identical. Only meshes above the
            # threshold are simplified, and only those run in the background.
            display = original_display_mesh(mesh)
            self._store(mesh, display)
            return display
        request = LodRequest(key, mesh, target, threshold, self._generation,
                             (ALGORITHM_VERSION, target, threshold))
        if self._inflight:
            running = self._task.request if self._task is not None else None
            if (running is not None and running.mesh is mesh
                    and running.params == request.params
                    and running.generation == request.generation):
                return None                      # the same current-generation result is running
            if (self._pending is not None and self._pending.mesh is mesh
                    and self._pending.params == request.params
                    and self._pending.generation == request.generation):
                return None
            self._pending = request              # only the newest pending request survives
            self.status.emit(key, "waiting")
            return None
        self._start(request)
        return None

    def pending_keys(self) -> tuple[str, ...]:
        keys = []
        if self._inflight and self._task is not None:
            keys.append(self._task.request.key)
        if self._pending is not None:
            keys.append(self._pending.key)
        return tuple(keys)

    def _start(self, request: LodRequest) -> None:
        self._inflight = True
        self.computes += 1
        self.status.emit(request.key, "running")
        task = _LodTask(request, self._signals)
        self._task = task
        self.pool.start(task)

    def _finished(self, payload) -> None:
        request, display, error = payload
        self._inflight = False
        self._task = None
        if display is not None:
            self.last_worker_thread = display.worker_thread
        valid = (not self.closed and request.generation == self._generation
                 and (ALGORITHM_VERSION, self.target_faces, self.threshold_faces) == request.params)
        if valid and error is None and display is not None:
            self._store(request.mesh, display)
            self.ready.emit(request.key, display)
        elif error is not None and valid:
            self.failed.emit(request.key, error)
        # A stale generation must still release whatever the *current* generation queued while
        # its task was running; otherwise invalidate-then-request leaves the new mesh waiting
        # forever with no worker. The pending request is judged on its own generation/params.
        pending, self._pending = self._pending, None
        params = (ALGORITHM_VERSION, self.target_faces, self.threshold_faces)
        if (pending is not None and not self.closed and pending.generation == self._generation
                and pending.params == params):
            self._start(pending)

    def _store(self, mesh: MeshData, display: DisplayMesh) -> None:
        key = id(mesh)
        self._cache[key] = (weakref.ref(mesh),
                            (ALGORITHM_VERSION, self.target_faces, self.threshold_faces), display)
        self._cache.move_to_end(key)
        self._evict()
        if key in self._cache:       # an oversized single display is delivered, not cached
            self.status.emit(display.key, "cached")

    def _purge_dead(self) -> None:
        for key in list(self._cache):
            if self._cache[key][0]() is None:
                del self._cache[key]

    def _evict(self) -> None:
        self._purge_dead()
        while len(self._cache) > MAX_CACHE_MESHES:
            self._cache.popitem(last=False)
        # The byte cap is absolute: an entry larger than the whole budget is evicted even when it
        # is the only one. The caller still receives that display through the ready signal.
        while self._cache and self.cache_bytes() > MAX_CACHE_BYTES:
            self._cache.popitem(last=False)

    def cache_bytes(self) -> int:
        self._purge_dead()
        return sum(entry[2].nbytes for entry in self._cache.values())

    def cache_entries(self) -> int:
        self._purge_dead()
        return len(self._cache)
