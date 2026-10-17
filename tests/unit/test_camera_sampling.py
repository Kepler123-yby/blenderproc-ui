from dataclasses import replace
import numpy as np
import pytest

from bproc_core.camera import sample_camera, project_points, projection_matrix, resize_camera
from bproc_core.config import CameraSpec, RegionSpec, Range, Azimuth
from bproc_core.sampling import allocate_regions, view_rng, unoccluded_scenes, camera_for_view


def fixed(phi=0, alpha=0):
    return RegionSpec(radius_m=Range(1, 1), azimuth=Azimuth(phi, phi, False), elevation_deg=Range(alpha, alpha))


def test_fixed_camera_pixel_axes_and_matrix():
    cam = CameraSpec()
    p = sample_camera(fixed(), np.zeros(3), np.eye(3), cam, view_rng(42, 0, 0, 0))
    uv, valid = project_points([[0, 0, 0], [0, .05, 0], [0, 0, .05]], p, cam)
    assert valid.all()
    assert uv[0] == pytest.approx([cam.cx, cam.cy])
    assert uv[1, 0] > uv[0, 0] and uv[2, 1] < uv[0, 1]
    assert p.position_m == pytest.approx([1, 0, 0])
    P = projection_matrix(cam, .001, 10)
    assert P[0, 2] == pytest.approx(1 - 2 * (cam.cx + .5) / cam.width)
    assert P[1, 2] == pytest.approx(2 * (cam.cy + .5) / cam.height - 1)


def test_wrapping_fixed_full_and_poles():
    cam = CameraSpec()
    rng = view_rng(8, 3, 1, 0)
    r = replace(fixed(), azimuth=Azimuth(170, -170, False))
    phi = [sample_camera(r, np.zeros(3), np.eye(3), cam, rng).azimuth_deg for _ in range(100)]
    assert all(abs(x) >= 170 for x in phi)
    full = replace(r, azimuth=Azimuth(0, 0, True))
    phi = [sample_camera(full, np.zeros(3), np.eye(3), cam, rng).azimuth_deg for _ in range(100)]
    assert min(phi) < -100 and max(phi) > 100
    for alpha in [-90, 90]:
        pose = sample_camera(fixed(alpha=alpha), np.zeros(3), np.eye(3), cam, rng)
        assert pose.world_from_camera[:3, :3].T @ pose.world_from_camera[:3, :3] == pytest.approx(np.eye(3))


def test_distribution_rotation_determinism_and_resize():
    assert allocate_regions(5, ['a', 'b', 'c']) == {'a': 2, 'b': 2, 'c': 1}
    assert len(unoccluded_scenes(7, .2, 42)) == 1
    rng = view_rng(42, 0, 0, 0)
    region = replace(fixed(), elevation_deg=Range(0, 90))
    vals = [sample_camera(region, np.zeros(3), np.eye(3), CameraSpec(), rng).elevation_deg for _ in range(3000)]
    assert np.mean(np.sin(np.deg2rad(vals))) == pytest.approx(.5, abs=.02)
    rot = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    pose = sample_camera(fixed(), np.zeros(3), rot, CameraSpec(), view_rng(42, 0, 0, 0))
    assert pose.position_m == pytest.approx([0, 1, 0])
    cam = resize_camera(CameraSpec(), 1280, 960)
    assert cam.fx == pytest.approx(1144.8228)
    assert cam.cx == pytest.approx(650.5222)
    assert resize_camera(replace(CameraSpec(), auto_scale=False), 1280, 960).fx == CameraSpec().fx


def test_pinned_camera_repeats_exactly_and_views_map_to_cameras():
    from bproc_core.config import FixedCameraSpec

    pinned = FixedCameraSpec('c1', '正面', .7, -90., 25.)
    poses = [sample_camera(pinned.region(), np.zeros(3), np.eye(3), CameraSpec(), view_rng(42, 0, i, 0))
             for i in range(5)]
    assert all(np.array_equal(p.world_from_camera, poses[0].world_from_camera) for p in poses)
    assert poses[0].radius_m == pytest.approx(.7)
    assert poses[0].azimuth_deg == pytest.approx(-90) and poses[0].elevation_deg == pytest.approx(25)
    assert poses[0].position_m == pytest.approx([0, -.7 * np.cos(np.deg2rad(25)), .7 * np.sin(np.deg2rad(25))])

    assert [camera_for_view('fixed', ['c1', 'c2'], [], 0, i) for i in range(2)] == [('fixed', 'c1'), ('fixed', 'c2')]
    assert ([camera_for_view('random', [], ['r1', 'r2'], 4, i) for i in range(4)]
            == [('random', 'r1'), ('random', 'r1'), ('random', 'r2'), ('random', 'r2')])
    for arguments in (("fixed", ['c1'], [], 0, 1), ("fixed", [], [], 0, 0),
                      ("random", [], ['r1'], 1, 1), ("random", [], [], 1, 0), ("mixed", [], ['r1'], 1, 0)):
        with pytest.raises(ValueError):
            camera_for_view(*arguments)


def test_scene_and_view_streams_are_independent_and_stable():
    from bproc_core.sampling import scene_rng
    scene = [scene_rng(11, 3).random() for _ in range(3)]
    assert all(value == scene[0] for value in scene)
    assert scene_rng(11, 4).random() != scene[0]
    assert view_rng(11, 3, 0, 0).random() != scene_rng(11, 3).random()
    assert all(view_rng(11, 3, 2, 0).random() == view_rng(11, 3, 2, 0).random() for _ in range(2))
    assert view_rng(11, 3, 2, 0).random() != view_rng(11, 3, 2, 1).random()


def test_fit_radius_scales_with_the_model_and_not_a_fixed_distance():
    from bproc_core.camera import fit_radius_m
    camera = CameraSpec()
    small = np.array([[-.0005, -.0005, -.0005], [.0005, .0005, .0005]])
    large = small * 100
    assert fit_radius_m(large, camera) == pytest.approx(100 * fit_radius_m(small, camera))
    visible = fit_radius_m(small, camera)
    assert visible > 0 and visible < .05


def test_fitted_distance_keeps_the_whole_box_inside_the_frame():
    from bproc_core.camera import fit_radius_m, CameraPose
    from bproc_core.camera import project_points
    from bproc_core.scene import bounds_corners
    camera = CameraSpec()
    bounds = np.array([[-.05, -.05, -.05], [.05, .05, .05]])
    radius = fit_radius_m(bounds, camera, fill=1.0)
    world_from_camera = np.eye(4)
    world_from_camera[2, 3] = radius
    pose = CameraPose(world_from_camera, np.array([0., 0., radius]), radius, 0., 0., .01, 10.)
    uv, valid = project_points(bounds_corners(bounds), pose, camera)
    assert valid.all()
    assert uv[:, 0].min() >= 0 and uv[:, 0].max() <= camera.width
    assert uv[:, 1].min() >= 0 and uv[:, 1].max() <= camera.height
    assert fit_radius_m(bounds, camera, fill=.5) == pytest.approx(2 * radius)
