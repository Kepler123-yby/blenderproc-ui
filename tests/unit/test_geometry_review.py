"""Independent regression checks at geometry/API boundaries."""
from dataclasses import replace
import numpy as np
import pytest

from bproc_core.camera import project_points, projection_matrix, sample_camera
from bproc_core.config import Azimuth, CameraSpec, Range, RegionSpec
from bproc_core.sampling import allocate_regions, view_rng, unoccluded_scenes


@pytest.mark.parametrize("total", [True, 1.5, "5", None])
def test_region_allocation_requires_integer_count(total):
    with pytest.raises(ValueError):
        allocate_regions(total, ["a", "b"])


def test_asymmetric_intrinsics_pixel_boundaries_match_gl_projection():
    camera = CameraSpec(width=321, height=197, fx=203.25, fy=244.75, cx=105.75, cy=123.125)
    region = RegionSpec(radius_m=Range(1.2, 1.2), azimuth=Azimuth(43, 43, False), elevation_deg=Range(31, 31))
    pose = sample_camera(region, np.array([.2, -.3, .4]), np.eye(3), camera, view_rng(7, 2, 1, 0))
    pixels = np.array([[-.5, -.5], [camera.width - .5, camera.height - .5], [camera.cx, camera.cy], [21.125, 175.75]])
    depth = np.array([.5, 2., .7, 3.])
    local = np.column_stack([(pixels[:, 0] - camera.cx) / camera.fx * depth,
                             -(pixels[:, 1] - camera.cy) / camera.fy * depth, -depth])
    world = local @ pose.world_from_camera[:3, :3].T + pose.position_m
    actual, valid = project_points(world, pose, camera)
    assert valid.all()
    np.testing.assert_allclose(actual, pixels, atol=1e-10)
    clip = np.column_stack([local, np.ones(len(local))]) @ projection_matrix(camera, pose.clip_near_m, pose.clip_far_m).T
    ndc = clip[:, :3] / clip[:, 3:]
    from_gl = np.column_stack([(ndc[:, 0] + 1) * camera.width / 2 - .5, (1 - ndc[:, 1]) * camera.height / 2 - .5])
    np.testing.assert_allclose(from_gl, pixels, atol=1e-10)
    _, behind_valid = project_points(pose.position_m[None] + pose.world_from_camera[None, :3, 2], pose, camera)
    assert not behind_valid[0]


def test_rotated_polar_camera_is_right_handed_and_centered():
    rotation = np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]], dtype=float)
    center = np.array([.3, -.2, .1])
    camera = CameraSpec()
    for pole in (-90, 90):
        region = RegionSpec(radius_m=Range(.6, .6), azimuth=Azimuth(12, 12, False), elevation_deg=Range(pole, pole))
        pose = sample_camera(region, center, rotation, camera, view_rng(1, 0, 0, 0))
        basis = pose.world_from_camera[:3, :3]
        assert np.linalg.det(basis) == pytest.approx(1)
        np.testing.assert_allclose(basis.T @ basis, np.eye(3), atol=1e-12)
        pixels, valid = project_points(center[None], pose, camera)
        assert valid[0]
        np.testing.assert_allclose(pixels[0], [camera.cx, camera.cy], atol=1e-10)


def test_default_sample_golden_and_sampling_does_not_touch_global_rng():
    before = np.random.get_state()
    try:
        pose = sample_camera(RegionSpec(), np.zeros(3), np.eye(3), CameraSpec(), view_rng(42, 0, 0, 0))
        assert [pose.radius_m, pose.azimuth_deg, pose.elevation_deg] == pytest.approx([.732186814566789, -22.003761689261154, 53.38949966173828])
        assert len(unoccluded_scenes(13, .2, 42)) == 3
        after = np.random.get_state()
        assert before[0] == after[0] and before[2:] == after[2:]
        np.testing.assert_array_equal(before[1], after[1])
    finally:
        np.random.set_state(before)
