"""Viewport framing and base colours: the observer camera and both viewports must agree."""
from types import SimpleNamespace

import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_core.camera import CameraPose
from bproc_core.scene import LightState, Placement, SceneCandidate
from bproc_ui.views import camera_view as cv
from bproc_ui.views import scene_view as sv
from bproc_ui.views.scene_view import SceneView, draw_placements, placed_content_bounds, target_preview_rgba


def cube(key, low=(0., 0., 0.), size=(.05, .05, .05), rgba=None):
    low = np.asarray(low, dtype=float)
    high = low + np.asarray(size, dtype=float)
    vertices = np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1])
                         for z in (low[2], high[2])])
    faces = np.array([[0, 1, 2], [1, 3, 2], [4, 5, 6], [5, 7, 6]])
    return MeshData(key, vertices, faces, rgba, np.array([low, high]))


def placement(asset_key, translation=(0., 0., 0.), role="target", rotation=None, target_key=None):
    matrix = np.eye(4)
    if rotation is not None:
        matrix[:3, :3] = rotation
    matrix[:3, 3] = translation
    key = target_key if target_key is not None else (asset_key if role == "target" else None)
    return Placement(asset_key, key, 1 if role == "target" else None, matrix, role)


def candidate(placements, anchor="a"):
    pose = CameraPose(np.eye(4), np.array([0., 0., 1.]), 1., 0., 0., .05, 100.)
    return SceneCandidate(0, 0, 0, "region-1", anchor, pose, tuple(placements), None, 1., 0.,
                          LightState(np.array([0., 0., 1.]), 100., (1., 1., 1.)), False)


def test_placed_content_bounds_span_every_placed_object():
    meshes = {"a": cube("a"), "b": cube("b")}
    bounds = placed_content_bounds(candidate((placement("a"), placement("b", (1., 0., 0.)))), meshes)
    np.testing.assert_allclose(bounds, [[0., 0., 0.], [1.05, .05, .05]])
    np.testing.assert_allclose((bounds[0] + bounds[1]) / 2, [.525, .025, .025])


def test_placed_content_bounds_follow_rotated_placements():
    meshes = {"a": cube("a", low=(0., 0., 0.), size=(.4, .1, .05))}
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    bounds = placed_content_bounds(candidate((placement("a", (2., 0., 0.), rotation=rotation),)), meshes)
    np.testing.assert_allclose(bounds, [[1.9, 0., 0.], [2.0, .4, .05]], atol=1e-9)


def test_placed_content_bounds_keep_asymmetric_cad_origins():
    mesh = cube("a", low=(-.3, -.2, -.1), size=(.3, .2, .1))
    bounds = placed_content_bounds(candidate((placement("a"),)), {"a": mesh})
    np.testing.assert_allclose(bounds, [[-.3, -.2, -.1], [0., 0., 0.]])
    np.testing.assert_allclose((bounds[0] + bounds[1]) / 2, [-.15, -.1, -.05])


def test_placed_content_bounds_ignore_helpers_and_missing_meshes():
    meshes = {"a": cube("a")}
    assert placed_content_bounds(candidate(()), meshes) is None
    assert placed_content_bounds(None, meshes) is None
    assert placed_content_bounds(candidate((placement("missing"),)), meshes) is None
    only_occluder = placed_content_bounds(candidate((placement("o", (0., 0., -.3), role="occluder"),)),
                                          {"o": cube("o")})
    np.testing.assert_allclose(only_occluder, [[0., 0., -.3], [.05, .05, -.25]])


def test_fit_scene_frames_the_union_and_keeps_the_user_rotation(qtbot):
    view = SceneView()
    qtbot.addWidget(view)
    meshes = {"a": cube("a"), "b": cube("b")}
    shot = candidate((placement("a"), placement("b", (1., 0., 0.))))
    azimuth = view.view.camera.azimuth
    view.set_scene(shot, meshes)
    np.testing.assert_allclose(view.view.camera.center, [.525, .025, .025])
    assert view.view.camera.azimuth == azimuth            # the fit never rotates the observer


def test_fit_scene_does_not_reset_navigation_on_identical_refresh(qtbot):
    view = SceneView()
    qtbot.addWidget(view)
    meshes = {"a": cube("a")}
    shot = candidate((placement("a"),))
    view.set_scene(shot, meshes)
    view.view.camera.azimuth = 33.
    view.view.camera.center = (5., 5., 5.)
    view.set_scene(shot, meshes)                          # same geometry, refreshed view
    assert view.view.camera.azimuth == 33.
    np.testing.assert_allclose(view.view.camera.center, [5., 5., 5.])


def test_fit_scene_reframes_when_the_geometry_itself_moves(qtbot):
    view = SceneView()
    qtbot.addWidget(view)
    meshes = {"a": cube("a"), "b": cube("b")}
    view.set_scene(candidate((placement("a"), placement("b", (1., 0., 0.)))), meshes)
    view.view.camera.center = (5., 5., 5.)
    view.set_scene(candidate((placement("a"), placement("b", (3., 0., 0.)))), meshes)
    np.testing.assert_allclose(view.view.camera.center, [1.525, .025, .025])


def test_target_preview_rgba_only_paints_solid_base_colours():
    assert target_preview_rgba(SimpleNamespace(material=SimpleNamespace(color="original", base_color="#ff0000"))) is None
    rgba = target_preview_rgba(SimpleNamespace(material=SimpleNamespace(color="solid", base_color="#ff0000")))
    np.testing.assert_allclose(rgba, (1., 0., 0., 1.))
    assert target_preview_rgba(None) is None


def test_solid_colour_paints_targets_but_not_occluders():
    meshes = {"a": cube("a", rgba=np.full((8, 4), 200, dtype=np.uint8)),
              "o": cube("o", low=(0., 0., -.4))}
    shot = candidate((placement("a"), placement("o", (0., 0., -.4), role="occluder")))
    nodes = draw_placements(None, shot, meshes, target_rgba=(1., 0., 0., 1.))
    assert len(nodes) == 2
    target, occluder = nodes
    np.testing.assert_allclose(np.asarray(target.color.rgba).reshape(-1, 4)[0], (1., 0., 0., 1.), atol=1e-6)
    assert target.mesh_data.get_vertex_colors() is None
    assert not np.allclose(np.asarray(occluder.color.rgba).reshape(-1, 4)[0], (1., 0., 0., 1.))


def test_original_colour_keeps_the_imported_vertex_colours():
    rgba = np.tile(np.array([[12, 200, 40, 255]], dtype=np.uint8), (8, 1))
    meshes = {"a": cube("a", rgba=rgba)}
    nodes = draw_placements(None, candidate((placement("a"),)), meshes, target_rgba=None)
    colors = nodes[0].mesh_data.get_vertex_colors()
    assert colors is not None
    np.testing.assert_allclose(np.asarray(colors)[:, :3], np.tile([12, 200, 40], (len(colors), 1)) / 255, atol=1e-6)


def test_camera_preview_accepts_the_shared_colour(monkeypatch):
    seen = []

    def capture(parent, candidate, meshes, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(cv, "draw_placements", capture)
    preview = cv.CameraPreview.__new__(cv.CameraPreview)
    preview.view = SimpleNamespace(scene=None)
    preview.canvas = SimpleNamespace(update=lambda: None)
    preview.caption = SimpleNamespace(setText=lambda _: None)
    preview._objects = []
    preview.set_scene(candidate((placement("a"),)), {"a": cube("a")}, target_rgba=(1., 0., 0., 1.))
    assert seen == [{"target_rgba": (1., 0., 0., 1.)}]


def test_both_viewports_own_separate_visual_nodes(qtbot):
    meshes = {"a": cube("a")}
    shot = candidate((placement("a"),))
    view = SceneView()
    qtbot.addWidget(view)
    view.set_scene(shot, meshes)
    assert view._objects, "the scene viewport must draw the placement"
    assert all(node.parent is view.view.scene for node in view._objects)
