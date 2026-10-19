"""Both real viewports must draw the display mesh, keep canonical framing and show LOD status."""
from types import SimpleNamespace

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QWidget

from bproc_core.assets import MeshData
from bproc_core.camera import CameraPose
from bproc_core.config import ViewportSpec
from bproc_core.scene import LightState, Placement, SceneCandidate
from bproc_ui.main_window import MainWindow
from bproc_ui.state import ProjectState
from bproc_ui.views import camera_view as cv
from bproc_ui.views.scene_view import SceneView
from bproc_ui.viewport_lod import simplify_display_mesh
from tests.factories import make_mesh, make_project


def sphere(key="big", rings=501, steps=1000):
    u = np.linspace(0, 2 * np.pi, steps, endpoint=False)
    v = np.linspace(-np.pi / 2, np.pi / 2, rings)
    grid_u, grid_v = np.meshgrid(u, v)
    points = np.column_stack([np.cos(grid_v).ravel() * np.cos(grid_u).ravel(),
                              np.cos(grid_v).ravel() * np.sin(grid_u).ravel(),
                              np.sin(grid_v).ravel()])
    index = np.arange(rings * steps).reshape(rings, steps)
    a, b = index[:-1, :], np.roll(index[:-1, :], -1, axis=1)
    c, d = np.roll(index[1:, :], -1, axis=1), index[1:, :]
    faces = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3),
                            np.stack([a, c, d], -1).reshape(-1, 3)]).astype(np.int64)
    return MeshData(key, points, faces, None, np.array([points.min(axis=0), points.max(axis=0)]))


def placement(key, role="target"):
    return Placement(key, key if role == "target" else None, 1 if role == "target" else None,
                     np.eye(4), role)


def candidate(*placements):
    pose = CameraPose(np.eye(4), np.array([0., 0., 1.]), 1., 0., 0., .05, 100.)
    return SceneCandidate(0, 0, 0, "region-1", placements[0].asset_key if placements else "big",
                          pose, tuple(placements), None, 1., 0.,
                          LightState(np.array([0., 0., 1.]), 100., (1., 1., 1.)), False)


def test_scene_view_draws_the_display_mesh_not_the_canonical_arrays(qtbot):
    mesh = sphere()
    display = simplify_display_mesh(mesh, 50_000)
    view = SceneView()
    qtbot.addWidget(view)
    view.set_scene(candidate(placement("big")), {"big": mesh}, {"big": display})
    visual = view._objects[0]
    assert len(visual.mesh_data.get_vertices()) == len(display.vertices_m)
    assert len(display.faces) < len(mesh.faces)
    # Framing still uses the canonical bounds.
    np.testing.assert_allclose(view.view.camera.center, mesh.bounds_m.mean(axis=0))


def test_scene_view_draws_only_a_bounds_proxy_while_lod_is_pending(qtbot):
    mesh = sphere()
    view = SceneView()
    qtbot.addWidget(view)
    view.set_scene(candidate(placement("big")), {"big": mesh}, {}, {"big"})
    assert len(view._objects) == 1
    proxy = view._objects[0]
    assert type(proxy).__name__ == "Line"
    # The proxy spans the canonical bounds; no Mesh visual with million faces was created.
    assert len(proxy.pos) == 24
    assert not hasattr(proxy, "mesh_data")


def test_camera_preview_forwards_the_shared_display_meshes(monkeypatch):
    seen = {}

    def capture(parent, candidate, meshes, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(cv, "draw_placements", capture)
    preview = cv.CameraPreview.__new__(cv.CameraPreview)
    preview.view = SimpleNamespace(scene=None)
    preview.canvas = SimpleNamespace(update=lambda: None)
    preview.caption = SimpleNamespace(setText=lambda _: None)
    preview._objects = []
    mesh, display = sphere("a"), simplify_display_mesh(sphere("a"), 50_000)
    preview.set_scene(candidate(placement("a")), {"a": mesh}, (1., 0., 0., 1.),
                      {"a": display}, {"a"})
    assert seen["display_meshes"] == {"a": display}
    assert seen["pending"] == {"a"}


class RecordingScene(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []

    def fit_scene(self):
        pass

    def set_selection(self, key):
        self.selected = key

    def set_scene(self, candidate, meshes, display_meshes=None, pending=()):
        self.calls.append((candidate, meshes, display_meshes, pending))

    def set_regions(self, config, selected_key=None):
        pass

    def set_candidates(self, candidates):
        pass


class RecordingCamera(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []

    def show_preview(self):
        pass

    def set_scene(self, candidate, meshes, target_rgba=None, display_meshes=None, pending=()):
        self.calls.append((candidate, meshes, target_rgba, display_meshes, pending))

    def set_camera(self, camera, pose):
        pass

    def hide(self):
        pass


class _NoopLoader(QObject):
    """The resource panel must not replace the fixture meshes with its small files."""

    loaded = Signal(str, int, object)
    failed = Signal(str, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def request(self, asset, generation):
        pass


def test_main_window_lod_reaches_both_viewports_and_keeps_state_canonical(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("bproc_ui.panels.resources.AssetLoader",
                        lambda parent=None: _NoopLoader(parent))
    project = make_project(tmp_path / "assets")
    state = ProjectState(project)
    big, small = sphere("target-2"), make_mesh("target-7", size=0.05)
    state.set_meshes({"target-2": big, "target-7": small})
    scene, camera = RecordingScene(), RecordingCamera()
    window = MainWindow(state, scene_factory=lambda: scene, camera_factory=lambda parent: camera,
                         restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: "target-2" in window._display_meshes, timeout=120_000)
    display = window._display_meshes["target-2"]
    assert 0 < display.display_faces <= 50_000
    assert window._display_meshes["target-2"].display_faces <= 50_000
    assert state.meshes["target-2"] is big and len(state.meshes["target-2"].faces) >= 1_000_000
    assert scene.calls[-1][2]["target-2"] is window._display_meshes["target-2"]
    assert camera.calls[-1][3]["target-2"] is window._display_meshes["target-2"]
    assert "面" in window.lod_status.text()

    sample = candidate(placement("target-2"))
    state.set_candidate(sample)
    window.lod_mode.setCurrentIndex(window.lod_mode.findData("full"))
    assert state.candidate is sample                # a display-only setting keeps the candidate
    assert state.config.viewport == ViewportSpec("full", 100_000, 50_000)
    assert window._display_meshes["target-2"].simplified is False
    assert window._display_meshes["target-2"].faces is big.faces
    assert "原始网格" in window.lod_status.text()
    window._closing = True
    window.close()


def test_main_window_keeps_a_mesh_inside_the_threshold_canonical(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("bproc_ui.panels.resources.AssetLoader",
                        lambda parent=None: _NoopLoader(parent))
    project = make_project(tmp_path / "assets")
    state = ProjectState(project)
    threshold_mesh = sphere("target-2", rings=151, steps=200)   # 60,000 faces < 100,000
    assert len(threshold_mesh.faces) == 60_000
    state.set_meshes({"target-2": threshold_mesh, "target-7": make_mesh("target-7", size=0.05)})
    scene, camera = RecordingScene(), RecordingCamera()
    window = MainWindow(state, scene_factory=lambda: scene, camera_factory=lambda parent: camera,
                        restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: "target-2" in window._display_meshes, timeout=120_000)
    display = window._display_meshes["target-2"]
    assert not display.simplified
    assert display.vertices_m is threshold_mesh.vertices_m
    assert display.faces is threshold_mesh.faces
    assert display.display_faces == 60_000
    assert window._pending == frozenset()
    assert "未超过阈值" in window.lod_status.text()
    window._closing = True
    window.close()
