"""Physical-size occluder strategy: uniform SO(3), one full transform, explicit rejections.

The reference sampler (``bproc_project/dataset_rendering/samplers/obstacle.py``) scales the model
by .001, samples a uniform SO3 rotation and picks objects without replacement. These tests lock the
same geometric contract into the current sampler *without* touching the historical ``aimed_view``
behaviour, which keeps its own snapshot below.
"""
from pathlib import Path

import numpy as np
import pytest

from bproc_core.camera import CameraPose
from bproc_core.config import (AssetSpec, CameraSpec, HdriSpec, OcclusionSpec, ProjectConfig, Range,
                               TargetSpec)
from bproc_core.occlusion import effective_occlusion_range
from bproc_core.scene import (Rejection, _linear_from, _offset_span, _rasterize_mask, _target_shapes,
                              _uniform_so3, bounds_corners, occluder_placements, placement_bounds,
                              transform_points)
from tests.factories import make_box

TARGET = np.array([[-.05, -.04, -.06], [.05, .04, .06]])
OCCLUDER = np.array([[.02, -.03, .01], [.12, .02, .031]])   # not centred on the origin, not cubic


def make_config(*, occluders=('o',), count=(1, 1), physical_scale=Range(1., 1.), ratio=Range(.1, .5),
                strategy='physical_view', scale=Range(.3, .6)):
    return ProjectConfig(
        targets=(TargetSpec(AssetSpec('a', Path('a.ply')), 'A', 2),), anchor_key='a',
        hdri=HdriSpec(files=(Path('env.hdr'),)),
        occluders=tuple(AssetSpec(key, Path(f'{key}.ply'), 'm') for key in occluders),
        occlusion=OcclusionSpec(none_fraction=0., ratio=ratio, count=count, max_attempts=10,
                                scale=scale, strategy=strategy, physical_scale=physical_scale),
        camera=CameraSpec(width=320, height=240))


def make_meshes(*keys):
    meshes = {'a': make_box('a', TARGET[0], TARGET[1])}
    for key in keys:
        meshes[key] = make_box(key, OCCLUDER[0], OCCLUDER[1])
    return meshes


def make_pose(distance=.6):
    return CameraPose(np.eye(4), np.array([0., 0., distance]), distance, 0., 0.,
                      1e-3 * distance, 100. * distance)


def placements(config, meshes, pose, seed=7):
    return occluder_placements(config, meshes, pose, 'a', np.random.default_rng(seed), ())


def test_uniform_so3_is_orthonormal_deterministic_and_full_rank():
    for seed in range(12):
        rotation = _uniform_so3(np.random.default_rng(seed))
        assert np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-10, rtol=0)
        assert abs(float(np.linalg.det(rotation)) - 1.) <= 1e-10
        assert np.array_equal(rotation, _uniform_so3(np.random.default_rng(seed)))
    rotations = np.array([_uniform_so3(np.random.default_rng(seed)) for seed in range(64)])
    assert not np.allclose(rotations[0], rotations[1])


def test_physical_placement_is_one_uniform_scale_times_rotation():
    for distance in (.4, 1.5):
        config = make_config(physical_scale=Range(.5, .5))
        pose = make_pose(distance)
        meshes = make_meshes('o')
        result = placements(config, meshes, pose)
        assert isinstance(result, list) and len(result) == 1, result
        matrix = result[0].world_from_model
        linear = matrix[:3, :3]
        singular = np.linalg.svd(linear, compute_uv=False)
        assert np.allclose(singular, .5, atol=1e-12, rtol=0)
        # Same physical size in a near and a far view: no distance-derived rescaling.
        for axis in range(3):
            assert np.linalg.norm(linear[:, axis]) == pytest.approx(.5, abs=1e-12)
        rotation = linear / .5
        assert np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-10, rtol=0)
        assert abs(float(np.linalg.det(rotation)) - 1.) <= 1e-10
        # The reported AABB is the conservative box of the rotated corners, not of the prototype.
        corners = transform_points(bounds_corners(meshes['o'].bounds_m), matrix)
        np.testing.assert_allclose(placement_bounds(result[0], meshes),
                                   np.array([corners.min(axis=0), corners.max(axis=0)]))


def test_physical_depth_and_collision_use_the_rotated_box():
    config = make_config(physical_scale=Range(.6, 1.2), count=(1, 1))
    pose = make_pose(.7)
    meshes = make_meshes('o', 'p', 'q')
    target = transform_points(bounds_corners(meshes['a'].bounds_m), np.eye(4))
    view = -pose.position_m / np.linalg.norm(pose.position_m)
    target_front = float(np.min((target - pose.position_m) @ view))
    for seed in range(10):
        result = occluder_placements(config, meshes, pose, 'a', np.random.default_rng(seed), ())
        assert isinstance(result, list) and len(result) == 1, (seed, result)
        matrix = result[0].world_from_model
        corners = transform_points(bounds_corners(meshes[result[0].asset_key].bounds_m), matrix)
        depth = (corners - pose.position_m) @ view
        assert float(depth.min()) > pose.clip_near_m
        assert float(depth.max()) < target_front
        occluder_box = np.array([corners.min(axis=0), corners.max(axis=0)])
        overlap = np.minimum(occluder_box[1], target.max(axis=0)) - np.maximum(occluder_box[0], target.min(axis=0))
        assert not np.all(overlap > 1e-9), f'seed {seed} intersects the target AABB'


def test_physical_selection_is_without_replacement_and_keeps_the_count():
    config = make_config(occluders=('o', 'p', 'q'), count=(3, 3))
    meshes = make_meshes('o', 'p', 'q')
    result = placements(make_config(occluders=('o', 'p', 'q'), count=(3, 3)), meshes, make_pose(), seed=3)
    assert isinstance(result, list) and len(result) == 3, result
    assert len({placement.asset_key for placement in result}) == 3
    assert len(result) == config.occlusion.count[1]


def test_physical_library_and_room_failures_are_explicit():
    meshes = make_meshes('o', 'p')
    assert placements(make_config(occluders=(), count=(1, 1)), make_meshes(), make_pose()).reason == \
        'missing_occluder_library'
    insufficient = placements(make_config(occluders=('o', 'p'), count=(3, 3)), meshes, make_pose())
    assert isinstance(insufficient, Rejection) and insufficient.reason == 'insufficient_occluder_library'
    too_large = placements(make_config(occluders=('o',), count=(1, 1),
                                       physical_scale=Range(4., 4.)), make_meshes('o'), make_pose(.4))
    assert isinstance(too_large, Rejection) and too_large.reason == 'no_occluder_room'


def test_offset_span_support_matches_the_rotated_world_corners():
    config = make_config()
    pose = make_pose()
    meshes = make_meshes('o')
    rotation = _uniform_so3(np.random.default_rng(5))
    linear = rotation * .7
    matrix = np.eye(4)
    matrix[:3, :3] = linear
    center = np.array([0., 0., .4])
    matrix[:3, 3] = center - linear @ meshes['o'].bounds_m.mean(axis=0)
    direction = np.array([1., 0., 0.])
    points = transform_points(bounds_corners(meshes['a'].bounds_m), np.eye(4))
    span = _offset_span(center, direction, meshes['o'], linear, points, pose)
    assert span is not None
    corners = transform_points(bounds_corners(meshes['o'].bounds_m), matrix) - center
    half = float(np.max(corners @ direction))
    assert span[2] == pytest.approx(half, abs=1e-12)
    # The scalar path keeps the historical formula exactly.
    scalar_span = _offset_span(center, direction, meshes['o'], .7, points, pose)
    assert scalar_span[2] == pytest.approx(float((np.ptp(meshes['o'].bounds_m, axis=0) * .7 / 2) @ np.abs(direction)))


def test_aimed_coverage_is_measured_on_the_same_rotated_matrix():
    config = make_config(ratio=Range(.3, .3), physical_scale=Range(.6, .6))
    pose = make_pose()
    meshes = make_meshes('o')
    result = placements(config, meshes, pose, seed=2)
    assert isinstance(result, list) and len(result) == 1, result
    shapes = _target_shapes([(meshes['a'], np.eye(4))], pose, config.camera)
    target_mask = _rasterize_mask(meshes['a'], np.eye(4), pose, config.camera, shapes.grid, triangles=True)
    occluder_mask = _rasterize_mask(meshes['o'], result[0].world_from_model, pose, config.camera,
                                    shapes.grid, triangles=True)
    share = float(np.count_nonzero(target_mask & occluder_mask)) / float(np.count_nonzero(target_mask))
    assert abs(share - .3) < .03, share


def test_physical_strategy_does_not_degrade_to_no_occlusion():
    config = make_config(occluders=(), count=(1, 1))
    assert effective_occlusion_range(config, False) == config.occlusion.ratio


def test_historical_aimed_view_candidates_are_byte_identical():
    """Snapshot from 2d84815 before the physical strategy existed (``git worktree`` at that commit)."""
    config = make_config(strategy='aimed_view', count=(1, 2), scale=Range(.3, .6), occluders=('o',))
    meshes = make_meshes('o')
    pose = make_pose()
    for seed, expected in (
        (7, [[[0.47319923352671095, 0.0, 0.0, -0.030614963573957578],
              [0.0, 0.47319923352671095, 0.0, 0.018341665462501364],
              [0.0, 0.0, 0.47319923352671095, 0.17460278267738732], [0.0, 0.0, 0.0, 1.0]],
             [[0.364550052108902, 0.0, 0.0, -0.04909681576956344],
              [0.0, 0.364550052108902, 0.0, 0.001042426986203254],
              [0.0, 0.0, 0.364550052108902, 0.12521463682977896], [0.0, 0.0, 0.0, 1.0]]]),
        (11, [[[0.29130690044458624, 0.0, 0.0, -0.020391483031121035],
               [0.0, 0.29130690044458624, 0.0, 0.001456534502222931],
               [0.0, 0.0, 0.29130690044458624, 0.27019797473032725], [0.0, 0.0, 0.0, 1.0]]]),
    ):
        result = occluder_placements(config, meshes, pose, 'a', np.random.default_rng(seed), ())
        assert isinstance(result, list) and len(result) == len(expected)
        for placement, matrix in zip(result, expected):
            np.testing.assert_array_equal(placement.world_from_model, np.asarray(matrix))
