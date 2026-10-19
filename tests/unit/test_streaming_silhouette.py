"""True streaming silhouettes past the legacy caps: union coverage, holes and diagnostics."""
import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_core.camera import CameraPose, project_points
from bproc_core.config import CameraSpec
from bproc_core.scene import candidate_from_dict, candidate_to_dict, SceneCandidate, Placement, LightState
from bproc_core import silhouette as sil
from bproc_core import scene as scene_module


def top_pose(height=1.0):
    world = np.eye(4)
    world[2, 3] = height
    return CameraPose(world, np.array([0., 0., height]), height, 0., 90., height * 1e-3, height * 100.)


def hollow_disc(radial=1002, angular=1000, inner=.10, outer=.30, key="hollow"):
    """A flat annulus with a real hole and (radial-1)*angular*2 triangles."""
    rings = np.linspace(inner, outer, radial)
    angles = np.linspace(0, 2 * np.pi, angular, endpoint=False)
    radius, theta = np.meshgrid(rings, angles, indexing="ij")
    points = np.column_stack([(radius * np.cos(theta)).ravel(),
                              (radius * np.sin(theta)).ravel(),
                              np.zeros(radial * angular)])
    index = np.arange(radial * angular).reshape(radial, angular)
    a, b = index[:-1, :], np.roll(index[:-1, :], -1, axis=1)
    c, d = np.roll(index[1:, :], -1, axis=1), index[1:, :]
    faces = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3),
                            np.stack([a, c, d], -1).reshape(-1, 3)]).astype(np.int64)
    bounds = np.array([points.min(axis=0), points.max(axis=0)])
    return MeshData(key, points, faces, None, bounds)


def grid_for(mesh, pose, camera=None):
    return scene_module._silhouette_grid([(mesh, np.eye(4))], pose, camera or CameraSpec())


def grid_cell(mesh, point, pose, grid, camera=None):
    camera = camera or CameraSpec()
    pixels, valid = project_points(np.atleast_2d(point), pose, camera)
    assert valid.all()
    mapped = grid.map(pixels)[0]
    column, row = int(round(mapped[0])), int(round(mapped[1]))
    return row, column


@pytest.fixture(scope="module")
def hollow():
    mesh = hollow_disc()
    assert len(mesh.faces) > 2_000_000
    return mesh


def test_a_two_million_face_hollow_disc_keeps_its_hole(hollow):
    camera = CameraSpec()
    pose = top_pose()
    grid = grid_for(hollow, pose, camera)
    mask, diagnostic = sil.stream_coverage(hollow, np.eye(4), pose, camera, grid)
    assert mask is not None and mask.shape == (grid.size, grid.size)
    assert diagnostic.method == "triangle_stream"
    assert diagnostic.faces > 2_000_000
    assert diagnostic.estimate_only is False
    # The centre lies inside the 10 cm hole: no triangle may fill it.
    row, column = grid_cell(hollow, (0., 0., 0.), pose, grid, camera)
    assert not mask[row - 3:row + 4, column - 3:column + 4].any()
    # A point inside the solid ring (20 cm) is covered, including its neighbourhood.
    row, column = grid_cell(hollow, (.2, 0., 0.), pose, grid, camera)
    assert mask[row - 2:row + 3, column - 2:column + 3].all()
    # The outer silhouette radius maps near the ring's outer edge, not the hull of the hole.
    covered = mask.sum()
    assert covered > 0


def test_the_same_pose_is_cached_without_recomputing(hollow):
    sil.clear_cache()
    camera = CameraSpec()
    pose = top_pose()
    grid = grid_for(hollow, pose, camera)
    first, diagnostic = sil.stream_coverage(hollow, np.eye(4), pose, camera, grid)
    second, cached = sil.stream_coverage(hollow, np.eye(4), pose, camera, grid)
    assert second is first
    assert cached.cached is True and cached.seconds == 0.0
    assert diagnostic.cached is False


def test_repeated_and_overlapping_components_are_unioned_not_xored():
    # Two overlapping quads at the same depth: an even-odd fill would cancel the overlap.
    points = np.array([[-.2, -.2, 0.], [.2, -.2, 0.], [.2, .2, 0.], [-.2, .2, 0.],
                       [-.1, -.1, 0.], [.3, -.1, 0.], [.3, .3, 0.], [-.1, .3, 0.]])
    faces = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 2], [0, 2, 3]])
    mesh = MeshData("overlap", points, faces, None,
                    np.array([points.min(axis=0), points.max(axis=0)]))
    pose = top_pose()
    camera = CameraSpec()
    grid = grid_for(mesh, pose, camera)
    mask, diagnostic = sil.stream_coverage(mesh, np.eye(4), pose, camera, grid)
    assert mask is not None
    row, column = grid_cell(mesh, (0., 0., 0.), pose, grid, camera)   # inside the overlap
    assert mask[row, column]
    row, column = grid_cell(mesh, (-.15, .15, 0.), pose, grid, camera)  # only the first quad
    assert mask[row, column]
    row, column = grid_cell(mesh, (.25, .25, 0.), pose, grid, camera)   # only the second quad
    assert mask[row, column]


def test_separated_components_all_cover_and_do_not_erase_each_other():
    points = []
    faces = []
    for offset in (-.3, 0., .3):
        start = len(points)
        points.extend([[-.05 + offset, -.05, 0.], [.05 + offset, -.05, 0.],
                       [.05 + offset, .05, 0.], [-.05 + offset, .05, 0.]])
        faces.extend([[start, start + 1, start + 2], [start, start + 2, start + 3]])
    points = np.array(points)
    mesh = MeshData("separated", points, np.array(faces), None,
                    np.array([points.min(axis=0), points.max(axis=0)]))
    pose = top_pose()
    camera = CameraSpec()
    grid = grid_for(mesh, pose, camera)
    mask, _ = sil.stream_coverage(mesh, np.eye(4), pose, camera, grid)
    for offset in (-.3, 0., .3):
        row, column = grid_cell(mesh, (offset, 0., 0.), pose, grid, camera)
        assert mask[row, column]


def test_more_than_one_million_scanline_crossings_are_supported():
    count = 7000
    xs = np.linspace(-.4, .4, count + 1)
    points = []
    for x in xs:
        points.extend([[x, -.4, 0.], [x, .4, 0.]])
    points = np.array(points)
    faces = np.array([[2 * i, 2 * i + 1, 2 * i + 2] for i in range(count)])
    mesh = MeshData("crossings", points, faces, None,
                    np.array([points.min(axis=0), points.max(axis=0)]))
    pose = top_pose()
    camera = CameraSpec()
    grid = grid_for(mesh, pose, camera)
    mask, diagnostic = sil.stream_coverage(mesh, np.eye(4), pose, camera, grid)
    assert mask is not None and diagnostic.fallback_reason is None
    assert count * grid.size > scene_module.SILHOUETTE_SCANLINE_MAX_CROSSINGS


@pytest.mark.parametrize("mesh,reason", [
    (MeshData("none", np.zeros((3, 3)), np.zeros((0, 3), dtype=np.int64), None,
              np.array([[0., 0., 0.], [1., 1., 1.]])), "no_faces"),
    (MeshData("badindex", np.zeros((3, 3)), np.array([[0, 1, 5]]), None,
              np.array([[0., 0., 0.], [1., 1., 1.]])), "invalid_faces"),
    (MeshData("nonfinite", np.array([[0., 0., 0.], [1., np.nan, 0.], [0., 1., 0.]]),
              np.array([[0, 1, 2]]), None, np.array([[0., 0., 0.], [1., 1., 1.]])),
     "non_finite_vertices"),
    (MeshData("line", np.array([[0., 0., 0.], [1., 0., 0.], [2., 0., 0.]]),
              np.array([[0, 1, 2]]), None, np.array([[0., 0., 0.], [2., 0., 0.]])),
     "degenerate_surface"),
    (MeshData("behind", np.array([[0., 0., 3.], [1., 0., 3.], [0., 1., 3.]]),
              np.array([[0, 1, 2]]), None, np.array([[0., 0., 3.], [1., 1., 3.]])),
     "behind_camera"),
])
def test_invalid_geometry_returns_stable_public_diagnostics(mesh, reason):
    pose = top_pose()
    camera = CameraSpec()
    grid = sil.CoverageGrid(-320., -240., 0.5, 256)
    mask, diagnostic = sil.stream_coverage(mesh, np.eye(4), pose, camera, grid)
    assert mask is None
    assert diagnostic.fallback_reason == reason
    assert diagnostic.method == "unavailable"
    payload = diagnostic.as_dict()
    assert payload["asset_key"] == mesh.key and payload["estimate_only"] is False


def test_the_legacy_caps_stay_in_place():
    assert scene_module.SILHOUETTE_TOPOLOGY_MAX_FACES == 2_000_000
    assert scene_module.SILHOUETTE_TOPOLOGY_MAX_EDGES == 4_000_000
    assert scene_module.SILHOUETTE_SCANLINE_MAX_CROSSINGS == 1_000_000
    assert scene_module.SILHOUETTE_MASK_MAX_FACES == 6000


def test_candidate_serialization_carries_silhouette_diagnostics():
    pose = top_pose()
    candidate = SceneCandidate(
        0, 0, 0, "region", "a", pose,
        (Placement("a", "a", 2, np.eye(4), "target"),), None, 1., 0.,
        LightState(np.array([0., 0., 1.]), 100., (1., 1., 1.)), False,
        silhouette_diagnostics=({"asset_key": "a", "method": "triangle_stream",
                                 "grid_size": 256, "faces": 2_000_100, "degenerate_faces": 0,
                                 "fallback_reason": None, "estimate_only": False},))
    payload = candidate_to_dict(candidate)
    assert payload["silhouette_diagnostics"][0]["method"] == "triangle_stream"
    restored = candidate_from_dict(payload)
    assert restored.silhouette_diagnostics == candidate.silhouette_diagnostics
    old = dict(payload)
    old.pop("silhouette_diagnostics")
    assert candidate_from_dict(old).silhouette_diagnostics == ()
