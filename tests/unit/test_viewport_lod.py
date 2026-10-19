"""Viewport LOD: display-only simplification, bounded cache and stale-result rejection."""
from dataclasses import replace
import hashlib
import threading

import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_core.config import ProjectConfig, ViewportSpec, validate_project
from bproc_core.project_io import project_from_dict, project_to_dict
from bproc_ui import viewport_lod as vl
from bproc_ui.viewport_lod import (MAX_CACHE_MESHES, DisplayMesh, ViewportLodController,
                                   original_display_mesh, simplify_display_mesh)


def sphere(key="big", rings=501, steps=1000, color=None):
    """A closed triangulated surface with exactly ``(rings-1)*steps*2`` faces."""
    u = np.linspace(0, 2 * np.pi, steps, endpoint=False)
    v = np.linspace(-np.pi / 2, np.pi / 2, rings)
    grid_u, grid_v = np.meshgrid(u, v)
    points = np.column_stack([np.cos(grid_v).ravel() * np.cos(grid_u).ravel(),
                              np.cos(grid_v).ravel() * np.sin(grid_u).ravel(),
                              np.sin(grid_v).ravel()])
    index = np.arange(rings * steps).reshape(rings, steps)
    a = index[:-1, :]
    b = np.roll(index[:-1, :], -1, axis=1)
    c = np.roll(index[1:, :], -1, axis=1)
    d = index[1:, :]
    faces = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3),
                            np.stack([a, c, d], -1).reshape(-1, 3)]).astype(np.int64)
    colors = None if color is None else np.tile(np.asarray(color, dtype=np.uint8), (len(points), 1))
    return MeshData(key, points, faces, colors, np.array([points.min(axis=0), points.max(axis=0)]))


def digest(mesh):
    sha = hashlib.sha256()
    sha.update(np.ascontiguousarray(mesh.vertices_m).tobytes())
    sha.update(np.ascontiguousarray(mesh.faces).tobytes())
    if mesh.vertex_rgba is not None:
        sha.update(np.ascontiguousarray(mesh.vertex_rgba).tobytes())
    sha.update(np.ascontiguousarray(mesh.bounds_m).tobytes())
    return sha.hexdigest()


@pytest.fixture(scope="module")
def million():
    mesh = sphere()
    assert len(mesh.faces) >= 1_000_000
    return mesh


class ManualPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


def test_viewport_defaults_roundtrip_and_validation(tmp_path):
    default = ProjectConfig()
    assert default.viewport == ViewportSpec("auto", 100_000, 50_000)
    document = project_to_dict(default)
    assert document["viewport"] == {"lod_mode": "auto", "lod_threshold_faces": 100_000,
                                    "lod_target_faces": 50_000}
    assert project_from_dict({"schema_version": 1}).viewport == default.viewport   # old YAML
    assert project_to_dict(project_from_dict(document))["viewport"] == document["viewport"]
    non_default = replace(default, viewport=ViewportSpec("full", 20_000, 5_000))
    assert project_from_dict(project_to_dict(non_default)).viewport == non_default.viewport


@pytest.mark.parametrize("spec", [
    ViewportSpec("auto", 1000, 2000),
    ViewportSpec("auto", 0, 0),
    ViewportSpec("auto", True, 1),
    ViewportSpec("fast", 10, 5),                       # type: ignore[arg-type]
])
def test_viewport_validation_rejects_illegal_values(spec):
    fields = {issue.field for issue in validate_project(ProjectConfig(viewport=spec), check_files=False)
              if issue.severity == "error"}
    assert fields & {"viewport.lod_mode", "viewport.lod_threshold_faces", "viewport.lod_target_faces"}


def test_small_mesh_keeps_the_exact_canonical_arrays():
    mesh = sphere("small", rings=20, steps=20)
    display = simplify_display_mesh(mesh, target_faces=50_000)
    assert not display.simplified
    assert display.vertices_m is mesh.vertices_m
    assert display.faces is mesh.faces
    assert display.vertex_rgba is mesh.vertex_rgba
    assert display.display_faces == len(mesh.faces)
    assert np.array_equal(display.bounds_m, mesh.bounds_m)


def test_million_face_mesh_is_simplified_without_touching_canonical_geometry(million):
    before, bounds_before = digest(million), million.bounds_m.copy()
    display = simplify_display_mesh(million, target_faces=50_000)
    assert len(million.faces) >= 1_000_000
    assert display.simplified and 0 < display.display_faces <= 50_000
    assert display.display_faces < display.source_faces
    assert np.array_equal(display.bounds_m, bounds_before)               # same metre frame
    assert digest(million) == before                                     # canonical bytes untouched
    assert display.vertices_m is not million.vertices_m
    # No re-centring: the simplified bounds still surround the original points' frame.
    low, high = display.vertices_m.min(axis=0), display.vertices_m.max(axis=0)
    assert np.all(low >= million.bounds_m[0] - 1e-9)
    assert np.all(high <= million.bounds_m[1] + 1e-9)


def test_cluster_colours_are_kept_opaque():
    mesh = sphere("coloured", rings=60, steps=120, color=(10, 20, 30, 255))
    display = simplify_display_mesh(mesh, target_faces=500)
    assert display.simplified and display.vertex_rgba is not None
    assert display.vertex_rgba.shape == (len(display.vertices_m), 4)
    assert int(display.vertex_rgba[:, 3].min()) == 255
    assert np.all(display.vertex_rgba[:, :3] == [10, 20, 30])


def test_target_faces_must_be_a_real_positive_integer():
    mesh = sphere("tiny", rings=4, steps=4)
    for bad in (0, -1, True, 1.5, "10"):
        with pytest.raises(ValueError):
            simplify_display_mesh(mesh, bad)          # type: ignore[arg-type]


def test_controller_runs_away_from_the_gui_thread_and_caches(qtbot):
    mesh = sphere()
    controller = ViewportLodController()
    controller.set_parameters(50_000, 100_000)
    with qtbot.waitSignal(controller.ready, timeout=60_000) as blocker:
        assert controller.request("big", mesh) is None
    key, display = blocker.args
    assert key == "big" and display.simplified and display.display_faces <= 50_000
    assert controller.last_worker_thread != threading.current_thread().name
    computes = controller.computes
    cached = controller.request("big", mesh)
    assert cached is display
    assert controller.computes == computes and controller.cache_hits == 1


def test_controller_rejects_late_results_after_context_or_resource_change(qtbot):
    mesh = sphere()
    pool = ManualPool()
    controller = ViewportLodController(pool=pool)
    controller.set_parameters(50_000, 100_000)
    assert controller.request("big", mesh) is None
    task = pool.tasks[0]
    controller.set_context(7, 3)                 # project switch / resource replacement
    with qtbot.assertNotEmitted(controller.ready):
        task.run()
    assert controller.cache_entries() == 0

    # A new generation computes normally and is stored.
    assert controller.request("big", mesh) is None
    assert len(pool.tasks) == 2
    with qtbot.waitSignal(controller.ready, timeout=60_000):
        pool.tasks[1].run()
    assert controller.cache_entries() == 1


def test_latest_pending_request_wins_and_close_drops_everything():
    mesh_a, mesh_b = sphere("a", rings=201, steps=400), sphere("b", rings=201, steps=400)
    pool = ManualPool()
    controller = ViewportLodController(pool=pool)
    controller.set_parameters(5_000, 10_000)
    assert controller.request("a", mesh_a) is None
    assert controller.request("b", mesh_b) is None
    assert controller.pending_keys() == ("a", "b")
    pool.tasks[0].run()                          # 'a' finishes, 'b' starts
    assert len(pool.tasks) == 2
    assert pool.tasks[1].request.key == "b"
    controller.close()
    pool.tasks[1].run()
    assert controller.cache_entries() == 0


def test_cache_is_bounded_by_entries_and_bytes(monkeypatch):
    monkeypatch.setattr(vl, "MAX_CACHE_MESHES", 2)
    controller = ViewportLodController()
    controller.set_parameters(1000, 100_000)
    meshes = [sphere(f"m{i}", rings=40, steps=40) for i in range(4)]
    for mesh in meshes:
        assert controller.request(mesh.key, mesh) is not None
    assert controller.cache_entries() <= 2
    one = simplify_display_mesh(meshes[0], 1000).nbytes
    monkeypatch.setattr(vl, "MAX_CACHE_BYTES", one + 10)
    controller.request("m8", sphere("m8", rings=40, steps=40))
    controller.request("m9", sphere("m9", rings=40, steps=40))
    assert controller.cache_entries() == 1


def test_original_display_mesh_is_independent_of_lod_state():
    mesh = sphere("tiny", rings=8, steps=8)
    display = original_display_mesh(mesh)
    assert isinstance(display, DisplayMesh) and not display.simplified
    assert display.vertices_m is mesh.vertices_m and display.faces is mesh.faces
