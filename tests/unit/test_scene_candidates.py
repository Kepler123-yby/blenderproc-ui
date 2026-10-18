from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_core.config import *
from bproc_core.scene import *
from bproc_core.scene import _covered_share, _silhouette


def fixture_scene():
    box = np.array([[.09, -.01, -.01], [.11, .01, .01]])
    vertices = bounds_corners(box)
    faces = np.array([[0, 1, 2]])
    meshes = {key: MeshData(key, vertices.copy(), faces, None, box.copy()) for key in ('a', 'b')}
    targets = (TargetSpec(AssetSpec('a', Path('a.ply')), 'A', 2),
               TargetSpec(AssetSpec('b', Path('b.ply')), 'B', 7,
                          offset=Box(Range(0, 0), Range(.06, .08), Range(0, 0))))
    config = ProjectConfig(targets=targets, anchor_key='a', hdri=HdriSpec(files=(Path('env.hdr'),)),
                           regions=(RegionSpec(radius_m=Range(.5, .5), azimuth=Azimuth(0, 0, False),
                                               elevation_deg=Range(0, 0)),), occlusion=OcclusionSpec(none_fraction=1))
    return config, meshes


def candidate(config, meshes, scene_index=0, view_index=0, attempt=0):
    state = propose_scene(config, meshes, scene_index)
    assert isinstance(state, SceneState), state
    return propose_candidate(config, meshes, state, view_index, attempt)


def test_center_origin_units_and_reproducibility():
    config, meshes = fixture_scene()
    before = meshes['a'].vertices_m.copy()
    shot = candidate(config, meshes)
    assert isinstance(shot, SceneCandidate)
    center = transform_points(meshes['a'].bounds_m.mean(axis=0)[None], shot.placements[0].world_from_model)[0]
    np.testing.assert_allclose(center, [0, 0, 0])
    assert candidate_to_dict(shot) == candidate_to_dict(candidate(config, meshes))
    np.testing.assert_array_equal(before, meshes['a'].vertices_m)
    position = transform_points(meshes['b'].bounds_m.mean(axis=0)[None], shot.placements[1].world_from_model)[0]
    assert .06 <= position[1] <= .08


def test_collision_is_a_scene_level_rejection_and_visibility_is_per_view():
    config, meshes = fixture_scene()
    t = replace(config.targets[1], offset=Box(Range(0, 0), Range(0, 0), Range(0, 0)))
    collision = propose_scene(replace(config, targets=(config.targets[0], t)), meshes, 0)
    assert isinstance(collision, Rejection) and collision.reason == 'collision'
    t = replace(config.targets[1], offset=Box(Range(0, 0), Range(20, 20), Range(0, 0)))
    assert candidate(replace(config, targets=(config.targets[0], t)), meshes).reason == 'out_of_frame'
    config = replace(config, scene_count=5, occlusion=replace(config.occlusion, none_fraction=.2))
    for scene_index in range(5):
        state = propose_scene(config, meshes, scene_index)
        assert state.wants_unoccluded == propose_scene(config, meshes, scene_index).wants_unoccluded


def test_scene_state_is_shared_by_every_view_and_only_the_camera_changes():
    config, meshes = fixture_scene()
    config = replace(config, camera_mode='random', scene_count=2, views_per_scene=3,
                     regions=(RegionSpec(key='r1', radius_m=Range(.5, .6), azimuth=Azimuth(0, 120, False),
                                         elevation_deg=Range(10, 40)),
                              RegionSpec(key='r2', radius_m=Range(.5, .6), azimuth=Azimuth(120, 240, False),
                                         elevation_deg=Range(10, 40))))
    state = propose_scene(config, meshes, 1)
    views = [propose_candidate(config, meshes, state, index, 0) for index in range(3)]
    assert all(isinstance(view, SceneCandidate) for view in views)
    assert all(view.scene_index == 1 for view in views)
    assert [view.view_index for view in views] == [0, 1, 2]
    assert [view.region_key for view in views] == ['r1', 'r1', 'r2']
    assert all([p.asset_key for p in view.placements] == [p.asset_key for p in views[0].placements]
               for view in views)
    assert not np.array_equal(views[0].camera.world_from_camera, views[1].camera.world_from_camera)


def test_pinned_cameras_hold_one_pose_per_view():
    config, meshes = fixture_scene()
    config = replace(config, camera_mode='fixed', scene_count=2, views_per_scene=7,
                     fixed_cameras=(FixedCameraSpec('pin', '钉子', .6, 10, 20),
                                    FixedCameraSpec('side', '侧', .7, 120, 30)))
    shots = [candidate(config, meshes, scene_index, view_index)
             for scene_index in range(2) for view_index in range(2)]
    assert [shot.camera_kind for shot in shots] == ['fixed'] * 4
    assert [shot.region_key for shot in shots] == ['pin', 'side', 'pin', 'side']
    first = [shot.camera.world_from_camera.tolist() for shot in shots[:2]]
    assert [shot.camera.world_from_camera.tolist() for shot in shots[2:]] == first
    assert candidate_from_dict(candidate_to_dict(shots[0])).camera_kind == 'fixed'
    assert candidate_to_dict(shots[0])['scene_index'] == 0 and candidate_to_dict(shots[1])['view_index'] == 1


def test_random_mode_allocates_views_and_reports_indices():
    config, meshes = fixture_scene()
    config = replace(config, camera_mode='random', scene_count=2, views_per_scene=4)
    shot = candidate(config, meshes, 1, 3)
    assert shot.camera_kind == 'random' and shot.region_key == 'region-1'
    payload = candidate_to_dict(shot)
    assert payload['scene_index'] == 1 and payload['view_index'] == 3
    assert candidate_to_dict(candidate_from_dict(payload)) == payload


def test_different_scenes_sample_different_arrangements():
    config, meshes = fixture_scene()
    config = replace(config, camera_mode='random', scene_count=6, views_per_scene=2)
    states = [propose_scene(config, meshes, index) for index in range(6)]
    offsets = {round(float(state.placements[1].world_from_model[1, 3]), 6) for state in states}
    assert len(offsets) > 1
    for state in states:
        assert propose_candidate(config, meshes, state, 0, 0).placements == state.placements


def occluded_fixture():
    config, meshes = fixture_scene()
    box = np.array([[-.05, -.05, -.05], [.05, .05, .05]])
    meshes['o'] = MeshData('o', bounds_corners(box), np.array([[0, 1, 2]]), None, box)
    config = replace(config, occluders=(AssetSpec('o', Path('o.ply')),),
                     occlusion=OcclusionSpec(none_fraction=0., ratio=Range(.1, .5), max_attempts=30,
                                             count=(2, 2), scale=Range(.4, .6)), scene_count=1)
    return config, meshes


def _front_depth(shot, meshes):
    """Depth from this camera to the nearest target surface along the view axis."""
    camera = np.asarray(shot.camera.position_m)
    view = -camera / np.linalg.norm(camera)
    corners = np.vstack([transform_points(bounds_corners(meshes[p.asset_key].bounds_m), p.world_from_model)
                         for p in shot.placements if p.role == 'target'])
    return float(np.min((corners - camera) @ view))


def _occluder_corners(shot, meshes):
    return [(p.asset_key, transform_points(bounds_corners(meshes[p.asset_key].bounds_m), p.world_from_model))
            for p in shot.placements if p.role == 'occluder']


def _aabb(points):
    return points.min(axis=0), points.max(axis=0)


def _shots(config, meshes, attempts=30, radius=None):
    if radius is not None:
        config = replace(config, regions=(RegionSpec(radius_m=Range(radius, radius), azimuth=Azimuth(0, 0, False),
                                                     elevation_deg=Range(20, 20)),))
    state = propose_scene(config, meshes, 0)
    assert isinstance(state, SceneState), state
    return [propose_candidate(config, meshes, state, 0, attempt) for attempt in range(attempts)]


def test_occluders_stay_between_the_camera_and_the_nearest_target_surface():
    """The old placement used the scene-wide nominal radius, so a 0.5 m view put it behind the camera."""
    config, meshes = occluded_fixture()
    for radius in (.5, 5.):
        shots = _shots(config, meshes, radius=radius)
        accepted = [shot for shot in shots if isinstance(shot, SceneCandidate)]
        assert len(accepted) >= 25, (radius, len(accepted))
        for shot in accepted:
            camera = np.asarray(shot.camera.position_m)
            view = -camera / np.linalg.norm(camera)
            front = _front_depth(shot, meshes)
            for key, corners in _occluder_corners(shot, meshes):
                depths = (corners - camera) @ view
                assert float(depths.min()) > shot.camera.clip_near_m, (radius, key, depths)
                assert float(depths.max()) < front, (radius, key, depths, front)


def test_occluders_never_intersect_targets_or_each_other():
    config, meshes = occluded_fixture()
    small = np.array([[-.01, -.01, -.01], [.01, .01, .01]])
    meshes['o'] = MeshData('o', bounds_corners(small), np.array([[0, 1, 2]]), None, small)
    config = replace(config, occlusion=OcclusionSpec(none_fraction=0., ratio=Range(.1, .5), max_attempts=30,
                                                     count=(2, 3), scale=Range(1., 1.8)))
    accepted = [shot for shot in _shots(config, meshes, attempts=20) if isinstance(shot, SceneCandidate)]
    assert accepted
    for shot in accepted:
        boxes = [(_aabb(transform_points(bounds_corners(meshes[p.asset_key].bounds_m), p.world_from_model)), p.asset_key)
                 for p in shot.placements]
        for i, (a, key_a) in enumerate(boxes):
            for b, key_b in boxes[i + 1:]:
                overlapping = np.all(a[0] < b[1]) and np.all(b[0] < a[1])
                assert not overlapping, (key_a, key_b)


def test_occluders_are_reproducible_for_a_seed():
    config, meshes = occluded_fixture()
    first = candidate(config, meshes)
    second = candidate(config, meshes)
    assert isinstance(first, SceneCandidate) and isinstance(second, SceneCandidate)
    assert candidate_to_dict(first) == candidate_to_dict(second)


def test_occluders_cover_random_bands_from_close_and_far_views():
    config, meshes = occluded_fixture()
    config = replace(config, camera_mode='random', scene_count=3, views_per_scene=4,
                     regions=(RegionSpec(radius_m=Range(.5, 5.), azimuth=Azimuth(-90, 90, False),
                                         elevation_deg=Range(0, 60)),))
    for scene_index in range(3):
        state = propose_scene(config, meshes, scene_index)
        assert isinstance(state, SceneState), state
        for view_index in range(4):
            shot = propose_candidate(config, meshes, state, view_index, 0)
            assert isinstance(shot, SceneCandidate), shot
            camera = np.asarray(shot.camera.position_m)
            view = -camera / np.linalg.norm(camera)
            front = _front_depth(shot, meshes)
            for key, corners in _occluder_corners(shot, meshes):
                depths = (corners - camera) @ view
                assert float(depths.min()) > shot.camera.clip_near_m, (key, depths)
                assert float(depths.max()) < front, (key, depths, front)


def test_a_segment_too_short_for_an_occluder_is_an_explainable_rejection():
    config, meshes = occluded_fixture()
    heavy = np.array([[-.1, -.1, -.1], [.1, .1, .1]])
    meshes['o'] = MeshData('o', bounds_corners(heavy), np.array([[0, 1, 2]]), None, heavy)
    config = replace(config, occlusion=OcclusionSpec(none_fraction=0., ratio=Range(.1, .5), max_attempts=30,
                                                     count=(1, 1), scale=Range(60., 60.)))
    shot = candidate(config, meshes)
    assert isinstance(shot, Rejection), shot
    assert shot.reason == 'no_occluder_room'


def test_the_scene_itself_holds_no_occluders_because_they_follow_each_camera():
    config, meshes = occluded_fixture()
    state = propose_scene(config, meshes, 0)
    assert isinstance(state, SceneState)
    assert [p.role for p in state.placements] == ['target', 'target']


def test_a_retry_moves_the_occluder_to_search_for_the_ratio_window():
    config, meshes = occluded_fixture()
    state = propose_scene(config, meshes, 0)
    centers = []
    for attempt in range(6):
        shot = propose_candidate(config, meshes, state, 0, attempt)
        assert isinstance(shot, SceneCandidate), shot
        centers.append(np.round(shot.placements[-1].world_from_model[:3, 3], 6).tolist())
    assert len({tuple(center) for center in centers}) >= 5


def _scaled_fixture(scale):
    """Target, occluder and camera shrunk together; only the clearance used to stay in centimetres."""
    size, distance = .1 * scale, .5 * scale
    target_box = np.array([[-size / 2] * 3, [size / 2] * 3])
    occluder_box = np.array([[-size] * 3, [size] * 3])
    meshes = {"a": MeshData("a", bounds_corners(target_box), np.array([[0, 1, 2]]), None, target_box),
              "o": MeshData("o", bounds_corners(occluder_box), np.array([[0, 1, 2]]), None, occluder_box)}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(distance, distance)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=Range(.1, .5), count=(1, 1),
                                scale=Range(.4, .6)))
    pose = CameraPose(np.eye(4), np.array([0., 0., distance]), distance, 0., 0.,
                      .01 * scale, 100. * scale)
    return config, meshes, pose, occluder_box


@pytest.mark.parametrize("scale", [1., .01])
def test_occluder_clearance_scales_with_scene(scale):
    """1 mm target + 5 mm camera used to end in no_occluder_room for all 30 attempts."""
    config, meshes, pose, occluder_box = _scaled_fixture(scale)
    placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(7), ())

    assert isinstance(placements, list) and len(placements) == 1, placements
    camera = np.asarray(pose.position_m)
    view = -camera / np.linalg.norm(camera)
    corners = transform_points(bounds_corners(occluder_box), placements[0].world_from_model)
    depths = (corners - camera) @ view
    assert float(depths.min()) > float(pose.clip_near_m)
    target_front = float(np.linalg.norm(camera)) - .05 * scale
    assert float(depths.max()) < target_front, "an occluder must stay in front of the target"
    target_low, target_high = -.05 * scale, .05 * scale
    low, high = corners.min(axis=0), corners.max(axis=0)
    assert not (low[0] < target_high and high[0] > target_low
                and low[1] < target_high and high[1] > target_low
                and low[2] < target_high and high[2] > target_low), "the occluder may not intersect the target"


class _FarthestRng:
    """Draws the largest allowed value everywhere, so a placement lands at `farthest`."""

    def integers(self, low, high=None, **kwargs):
        return (high if high is not None else low) - 1

    def uniform(self, low=0.0, high=1.0, **kwargs):
        return high


def _front_gap_when_farthest(scale):
    """Gap between the far corner and the target's near face for the deepest legal placement."""
    config, meshes, pose, occluder_box = _scaled_fixture(scale)
    camera = np.asarray(pose.position_m)
    view = -camera / np.linalg.norm(camera)
    target_front = float(np.linalg.norm(camera)) - .05 * scale
    placements = occluder_placements(config, meshes, pose, "a", _FarthestRng(), ())
    assert isinstance(placements, list), placements
    corners = transform_points(bounds_corners(occluder_box), placements[0].world_from_model)
    return target_front - float(np.max((corners - camera) @ view))


def test_an_occluder_never_crosses_the_target_front_face():
    for scale in (1., .01):
        config, meshes, pose, occluder_box = _scaled_fixture(scale)
        camera = np.asarray(pose.position_m)
        view = -camera / np.linalg.norm(camera)
        target_front = float(np.linalg.norm(camera)) - .05 * scale
        for seed in range(30):
            placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(seed), ())
            assert isinstance(placements, list), placements
            corners = transform_points(bounds_corners(occluder_box), placements[0].world_from_model)
            gap = target_front - float(np.max((corners - camera) @ view))
            assert gap > 0, (scale, seed, gap)


def test_the_clearance_follows_the_scene_not_a_fixed_centimetre():
    """The gap at the deepest legal placement shrinks with the scene instead of staying in centimetres."""
    millimetre = _front_gap_when_farthest(.01)
    centimetre = _front_gap_when_farthest(1.)
    assert millimetre < .0015, f"gap {millimetre:.6f} m is still centimetre-sized for a 1 mm fixture"
    assert centimetre < .0065, "the 10 cm fixture must use its relative clearance, not a huge one"
    assert centimetre > 5 * millimetre, f"clearance did not scale with the scene ({centimetre:.4f} vs {millimetre:.4f})"


def _distance_fixture(distance, size=.1):
    """A 10 cm target with a 2 cm occluder seen from `distance` metres."""
    target_box = np.array([[-size / 2] * 3, [size / 2] * 3])
    occluder_box = np.array([[-size] * 3, [size] * 3])
    meshes = {"a": MeshData("a", bounds_corners(target_box), np.array([[0, 1, 2]]), None, target_box),
              "o": MeshData("o", bounds_corners(occluder_box), np.array([[0, 1, 2]]), None, occluder_box)}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(distance, distance)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=Range(.1, .5), count=(1, 1),
                                scale=Range(.4, .6)))
    pose = CameraPose(np.eye(4), np.array([0., 0., distance]), distance, 0., 0.,
                      .01 * distance, 100. * distance)
    return config, meshes, pose, size


def _apparent_ratio(placement, mesh, pose, distance, size):
    """How wide the occluder looks next to the target, in target-widths, as the camera sees it."""
    field = np.ptp(mesh.bounds_m, axis=0)
    scale = float(np.linalg.norm(placement.world_from_model[:3, 0]))
    camera = np.asarray(pose.position_m)
    view = -camera / np.linalg.norm(camera)
    depth = float((placement.world_from_model[:3, 3] - camera) @ view)
    return float(field.max() * scale / depth * distance / size)


def test_occluder_apparent_size_does_not_depend_on_the_distance():
    """A box three times closer projects three times wider, so its metres must shrink to compensate.

    Without this, a 5 m view of a 10 cm target showed 66.7 % occlusion against a 10-50 % plan while
    the 0.5 m view of the same target showed 23.4 %.
    """
    measured = []
    for distance in (.5, 5.):
        config, meshes, pose, size = _distance_fixture(distance)
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(7), ())
        assert isinstance(placements, list), placements
        measured.append(_apparent_ratio(placements[0], meshes["o"], pose, distance, size))
    assert measured[1] == pytest.approx(measured[0], rel=.05), measured
    assert .4 <= measured[0] <= .6, measured


def test_occluder_offset_is_aimed_at_the_occlusion_window():
    """Placement bisects the sideways offset against the projected silhouettes.

    Random offsets used to over-cover: every sampled 240-frame rejection was above the window
    (0.51-0.97 against 0.05-0.5), so most proposals died for hiding too much. Aiming the offset at
    the middle of the window turns the coarse estimate into a placement the renderer keeps.
    """
    for distance in (.5, 5.):
        config, meshes, pose, size = _distance_fixture(distance)
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(11), ())
        assert isinstance(placements, list), placements
        target = _silhouette(bounds_corners(meshes["a"].bounds_m), np.eye(4), pose, config.camera)
        hull = _silhouette(bounds_corners(meshes["o"].bounds_m), placements[0].world_from_model,
                           pose, config.camera)
        share = _covered_share(target, hull)
        assert config.occlusion.ratio.minimum <= share <= config.occlusion.ratio.maximum, share
