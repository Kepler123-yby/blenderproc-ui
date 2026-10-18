from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_core.config import *
from bproc_core.scene import *
from bproc_core.scene import (_covered_share, _maskable, _polygon_area, _silhouette,
                              _silhouette_points, _target_shapes, SILHOUETTE_GRID_SIZE,
                              SILHOUETTE_MASK_MAX_FACES, SILHOUETTE_MASK_MAX_VERTICES,
                              SILHOUETTE_POINT_LIMIT)
from tests.factories import BOX_FACES, box_surface, make_box


def fixture_scene():
    box = np.array([[.09, -.01, -.01], [.11, .01, .01]])
    meshes = {key: make_box(key, box[0], box[1]) for key in ('a', 'b')}
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
    meshes['o'] = make_box('o', box[0], box[1])
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
    meshes['o'] = make_box('o', small[0], small[1])
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
    meshes['o'] = make_box('o', heavy[0], heavy[1])
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
    meshes = {"a": make_box("a", target_box[0], target_box[1]),
              "o": make_box("o", occluder_box[0], occluder_box[1])}
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
    meshes = {"a": make_box("a", target_box[0], target_box[1]),
              "o": make_box("o", occluder_box[0], occluder_box[1])}
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


def _thin_plate_fixture(ratio):
    """100 x 100 x 1 mm plates, occluder 1.2x the target, top view at 0.5 m.

    The reviewer's counterexample: projected silhouettes are filled rectangles, so the hull
    estimate is the coverage itself and a mis-aimed window is visible as a number, not as noise.
    """
    target_box = np.array([[-.05, -.05, -.0005], [.05, .05, .0005]])
    occluder_box = np.array([[-.06, -.06, -.0006], [.06, .06, .0006]])
    meshes = {"a": make_box("a", target_box[0], target_box[1]),
              "o": make_box("o", occluder_box[0], occluder_box[1])}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(.5, .5), azimuth=Azimuth(0, 0, False),
                            elevation_deg=Range(90, 90)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=ratio, count=(1, 1),
                                scale=Range(1.2, 1.2)))
    pose = CameraPose(np.eye(4), np.array([0., 0., .5]), .5, 0., 0., .005, 50.)
    return config, meshes, pose


@pytest.mark.parametrize("window", [Range(.8, .9), Range(0., .01), Range(.3, .3), Range(.1, .5)])
def test_the_aim_stays_inside_the_requested_window(window):
    """Aiming outside the user's interval cannot be excused as estimator noise."""
    config, meshes, pose = _thin_plate_fixture(window)
    shares = []
    for seed in range(12):
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(seed), ())
        assert isinstance(placements, list), placements
        target = _silhouette(bounds_corners(meshes["a"].bounds_m), np.eye(4), pose, config.camera)
        hull = _silhouette(bounds_corners(meshes["o"].bounds_m), placements[0].world_from_model,
                          pose, config.camera)
        shares.append(_covered_share(target, hull))
    # A zero-width window is reachable because the convex share is an exact polygon ratio, not a
    # rasterised area: the estimate bisects it directly, so even a fixed ratio needs no slack.
    slack = 1e-4 if window.minimum == window.maximum else 0.
    assert all(window.minimum - slack <= share <= window.maximum + slack for share in shares), shares


def _frame_fixture(window):
    """A square frame with a real hole: its convex hull fills the hole and over-states the area."""
    outer, inner, half_z = .05, .03, .005
    parts = [((-outer, -outer, -half_z), (outer, -inner, half_z)),
             ((-outer, inner, -half_z), (outer, outer, half_z)),
             ((-outer, -inner, -half_z), (-inner, inner, half_z)),
             ((inner, -inner, -half_z), (outer, inner, half_z))]
    vertices, faces, offset = [], [], 0
    for low, high in parts:
        part_vertices, part_faces = box_surface(low, high)
        vertices.append(part_vertices)
        faces.append(part_faces + offset)
        offset += len(part_vertices)
    vertices = np.vstack(vertices)
    faces = np.vstack(faces)
    bounds = np.array([vertices.min(axis=0), vertices.max(axis=0)])
    occluder_vertices, occluder_faces = box_surface((-.02, -.02, -.002), (.02, .02, .002))
    occluder_bounds = np.array([occluder_vertices.min(axis=0), occluder_vertices.max(axis=0)])
    meshes = {"a": MeshData("a", vertices, faces, None, bounds),
              "o": MeshData("o", occluder_vertices, occluder_faces, None, occluder_bounds)}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(.5, .5), azimuth=Azimuth(0, 0, False),
                            elevation_deg=Range(90, 90)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=window, count=(1, 1),
                                scale=Range(1., 1.)))
    pose = CameraPose(np.eye(4), np.array([0., 0., .5]), .5, 0., 0., .005, 50.)
    return config, meshes, pose


def _raster(mesh, world_from_model, pose, camera):
    from PIL import Image, ImageDraw
    from bproc_core.camera import project_points
    uv, valid = project_points(transform_points(mesh.vertices_m, world_from_model), pose, camera)
    image = Image.new("1", (camera.width, camera.height), 0)
    draw = ImageDraw.Draw(image)
    for triangle in mesh.faces:
        if valid[triangle].all():
            draw.polygon([tuple(uv[index]) for index in triangle], fill=1)
    return np.asarray(image, dtype=bool)


@pytest.mark.parametrize("window", [Range(.25, .35), Range(.05, .15), Range(.8, .9)])
def test_a_hollow_target_is_aimed_by_its_real_outline(window):
    """The old bounding-box hull aimed the frame's hole as if it were solid metal.

    A 100 x 60 x 100 mm frame has 36 % of its box area; estimating coverage from the outer hull
    over-covered every proposal. The sampler now rasterises the projected triangles, so the final
    full-resolution pixel share lands inside the requested window instead of 2-3x above it.
    """
    config, meshes, pose = _frame_fixture(window)
    target_mask = _raster(meshes["a"], np.eye(4), pose, config.camera)
    assert 0 < target_mask.sum() < np.prod(target_mask.shape)
    for seed in range(6):
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(seed), ())
        assert isinstance(placements, list), placements
        occluder_mask = _raster(meshes["o"], placements[0].world_from_model, pose, config.camera)
        share = float((target_mask & occluder_mask).sum()) / float(target_mask.sum())
        assert window.minimum - .01 <= share <= window.maximum + .01, (seed, share)



def test_dense_mesh_silhouette_projection_stays_bounded():
    """A mesh past the mask budget must not sort every vertex for the hull fallback."""
    box = np.array([[-1., -1., -1.], [1., 1., 1.]])
    vertices = np.repeat(bounds_corners(box), 3000, axis=0)          # 24 000 vertices > 20 000
    faces = np.tile(BOX_FACES, (len(vertices) // 8, 1))               # 30 000 faces > 6 000
    mesh = MeshData("dense", vertices, faces, None, box)
    assert not _maskable(mesh), "the dense fixture must be past the mask budget to need a hull"
    points = _silhouette_points(mesh)
    assert len(points) <= SILHOUETTE_POINT_LIMIT + 8
    assert len(points) >= 8
    np.testing.assert_allclose(points.min(axis=0), box[0])
    np.testing.assert_allclose(points.max(axis=0), box[1])


def _flat_u_mesh(key="u"):
    """A U-shaped plate: three rectangles, six triangles, an opening a hull would fill.

    Six faces is what a real low-poly production part looks like, so it must not be treated as an
    unfinished fixture: the face count is a rasteriser cost ceiling, not a licence to invent a hull.
    """
    spans = [((-.05, -.05), (.05, -.03)), ((-.05, -.03), (-.03, .05)), ((.03, -.03), (.05, .05))]
    vertices, faces = [], []
    for (x0, y0), (x1, y1) in spans:
        index = len(vertices)
        vertices.extend([(x0, y0, 0.), (x1, y0, 0.), (x1, y1, 0.), (x0, y1, 0.)])
        faces.extend([[index, index + 1, index + 2], [index, index + 2, index + 3]])
    vertices, faces = np.array(vertices, dtype=float), np.array(faces)
    bounds = np.array([vertices.min(axis=0), vertices.max(axis=0)])
    return MeshData(key, vertices, faces, None, bounds)


def _top_pose(distance=.5):
    """One fixed top-down view, so mask and hull areas are measured on the same projection."""
    return CameraPose(np.eye(4), np.array([0., 0., distance]), distance, 0., 0., .005, 50.)


def _shape_counts(mesh):
    """How ``_target_shapes`` classifies one target: hulls or masks, and on which grid."""
    return _target_shapes([(mesh, np.eye(4))], _top_pose(), CameraSpec())


def _hull_pixels(mesh, shapes):
    """Convex hull of the mesh's bounds, in the area units of the shared mask grid."""
    hull = _silhouette(bounds_corners(mesh.bounds_m), np.eye(4), _top_pose(), CameraSpec())
    return _polygon_area(hull) * shapes.grid.pixels_per_px ** 2


def _u_fixture(window):
    """The six-face U target with an occluder and a top-down view, like the frame fixture."""
    mesh = _flat_u_mesh("a")
    occluder = make_box("o", (-.02, -.02, -.002), (.02, .02, .002))
    meshes = {"a": mesh, "o": occluder}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(.5, .5), azimuth=Azimuth(0, 0, False),
                            elevation_deg=Range(90, 90)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=window, count=(1, 1),
                                scale=Range(1., 1.)))
    pose = CameraPose(np.eye(4), np.array([0., 0., .5]), .5, 0., 0., .005, 50.)
    return config, meshes, pose


def test_a_six_face_u_target_is_rasterised_by_its_real_outline():
    """The reviewer's counterexample: six faces, a real opening, a hull that hides it."""
    mesh = _flat_u_mesh()
    shapes = _shape_counts(mesh)
    assert (len(shapes.hulls), len(shapes.masks)) == (0, 1), (len(shapes.hulls), len(shapes.masks))
    share = float(shapes.masks[0].sum()) / _hull_pixels(mesh, shapes)
    assert .4 < share < .75, f"U covers {share:.2%} of its hull; the rasteriser lost the opening"


def test_a_single_triangle_target_is_rasterised_from_its_own_surface():
    """Three vertices and one face are a real surface, so the mask must show half a square, not a hull."""
    square = np.array([[-.05, -.05, 0.], [.05, -.05, 0.], [.05, .05, 0.], [-.05, .05, 0.]])
    bounds = np.array([[-.05, -.05, 0.], [.05, .05, 0.]])
    triangle = MeshData("t", square[:3].copy(), np.array([[0, 1, 2]]), None, bounds)
    plate = MeshData("p", square, np.array([[0, 1, 2], [0, 2, 3]]), None, bounds)
    for mesh in (triangle, plate):
        shapes = _shape_counts(mesh)
        assert (len(shapes.hulls), len(shapes.masks)) == (0, 1), mesh.key
    share = (float(_shape_counts(triangle).masks[0].sum())
             / float(_shape_counts(plate).masks[0].sum()))
    assert .47 < share < .53, f"one triangle covers {share:.2%} of the square it is half of"


def test_a_solid_box_target_is_masked_with_the_same_area_as_its_hull():
    """A convex part must not lose area to the rasteriser now that every surface takes the mask path."""
    mesh = make_box("b", (-.05, -.05, -.05), (.05, .05, .05))
    shapes = _shape_counts(mesh)
    assert (len(shapes.hulls), len(shapes.masks)) == (0, 1)
    # Filling by pixel centres drops roughly half a boundary pixel per side, i.e. a few percent on
    # the 256px grid; anything beyond that would mean the rasteriser missed part of the box.
    share = float(shapes.masks[0].sum()) / _hull_pixels(mesh, shapes)
    assert .93 < share < 1.02, f"solid box mask covers {share:.2%} of its hull"


def test_only_meshes_beyond_the_mask_budget_fall_back_to_the_hull():
    """The hull is a cost fallback for oversized parts, never a substitute for a low-face surface."""
    bounds = np.array([[-.05, -.05, -.05], [.05, .05, .05]])
    corners = bounds_corners(bounds)
    repeated = np.tile(BOX_FACES, (SILHOUETTE_MASK_MAX_FACES // 12 + 1, 1))
    over_faces = MeshData("faces", corners, repeated[:SILHOUETTE_MASK_MAX_FACES + 1], None, bounds)
    over_vertices = MeshData("vertices", np.repeat(corners, SILHOUETTE_MASK_MAX_VERTICES // 8 + 1, axis=0),
                             BOX_FACES.copy(), None, bounds)
    no_surface = MeshData("none", corners, np.zeros((0, 3), int), None, bounds)
    for mesh in (over_faces, over_vertices, no_surface):
        assert not _maskable(mesh), mesh.key
        shapes = _shape_counts(mesh)
        assert (len(shapes.hulls), len(shapes.masks)) == (1, 0), (mesh.key, len(shapes.hulls), len(shapes.masks))


def _pixel_shares(config, meshes, pose, seeds=range(30)):
    """Full-resolution pixel share of the target hidden by the first occluder, per seed.

    This is the measure the renderer's own acceptance uses, not the sampler's estimate: the fine
    shared grid can be twice the camera's resolution, so a window is only reachable if the coarse
    image the dataset would ship shows the requested share.
    """
    target_mask = _raster(meshes["a"], np.eye(4), pose, config.camera)
    assert 0 < target_mask.sum() < np.prod(target_mask.shape)
    shares = []
    for seed in seeds:
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(seed), ())
        assert isinstance(placements, list), (seed, placements)
        occluder_mask = _raster(meshes["o"], placements[0].world_from_model, pose, config.camera)
        shares.append(float((target_mask & occluder_mask).sum()) / float(target_mask.sum()))
    return shares


def test_a_small_occluder_on_a_hollow_frame_is_aimed_at_its_rim():
    """A 1 %-sized occluder only covers anything at a 48-face frame's rim, not in its hole.

    The reviewer's counterexample aimed 16 of 30 seeds into the window: the occluder's own half
    width set the scan step, so a 0.5-1 % band next to the hole's edge was stepped over. The whole
    projected span is now probed at a fixed minimum density and the crossing is bisected, so every
    seed lands inside the window instead of on either shoulder of it.
    """
    config, meshes, pose = _frame_fixture(Range(.005, .01))
    config = replace(config, occlusion=replace(config.occlusion, none_fraction=0.,
                                               scale=Range(.1, .1)))
    shares = _pixel_shares(config, meshes, pose)
    assert len(shares) == 30
    assert all(.005 <= share <= .01 for share in shares), shares
    # Reaching the window at all proves the hole's centre was not treated as the whole target: a
    # 4 cm occluder aimed there hides 0 %.
    assert min(shares) > .005 and max(shares) < .01


@pytest.mark.parametrize("window", [Range(.25, .35)])
def test_a_narrow_high_window_on_a_six_face_u_target_holds_for_every_seed(window):
    """A 25-35 % window on the low-face U must not drop to 21 of 30 seeds again.

    The U's opening makes the coverage rise steeply, so a coarse scan spaced by the occluder's own
    width stepped from 0 % straight past the window and kept the nearest shoulder sample.
    """
    config, meshes, pose = _u_fixture(window)
    shapes = _target_shapes([(meshes["a"], np.eye(4))], pose, config.camera)
    assert (len(shapes.hulls), len(shapes.masks)) == (0, 1), (len(shapes.hulls), len(shapes.masks))
    shares = _pixel_shares(config, meshes, pose)
    assert len(shares) == 30
    assert all(window.minimum <= share <= window.maximum for share in shares), shares


def _solid_box_fixture(window, occluder_side):
    """A plain solid part: a 100 x 100 x 10 mm block and a smaller occluder box, top view."""
    meshes = {"a": make_box("a", (-.05, -.05, -.005), (.05, .05, .005)),
              "o": make_box("o", (-occluder_side / 2, -occluder_side / 2, -.002),
                            (occluder_side / 2, occluder_side / 2, .002))}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(.5, .5), azimuth=Azimuth(0, 0, False),
                            elevation_deg=Range(90, 90)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=window, count=(1, 1),
                                scale=Range(1., 1.)))
    pose = CameraPose(np.eye(4), np.array([0., 0., .5]), .5, 0., 0., .005, 50.)
    return config, meshes, pose


@pytest.mark.parametrize("window, occluder_side", [(Range(.05, .15), .04), (Range(.8, .9), .12)])
def test_an_ordinary_solid_part_keeps_its_low_and_high_windows(window, occluder_side):
    """The scan change must not disturb an ordinary solid target at either end of the range."""
    config, meshes, pose = _solid_box_fixture(window, occluder_side)
    shares = _pixel_shares(config, meshes, pose)
    assert len(shares) == 30
    assert all(window.minimum <= share <= window.maximum for share in shares), shares


def _eccentric_hole_fixture(window):
    """The same frame with its 30 x 30 mm opening away from the middle of the plate."""
    outer, half_z = .05, .005
    (x0, y0), (x1, y1) = (-.01, -.03), (.02, .0)
    parts = [((-outer, -outer, -half_z), (outer, y0, half_z)),
             ((-outer, y1, -half_z), (outer, outer, half_z)),
             ((-outer, y0, -half_z), (x0, y1, half_z)),
             ((x1, y0, -half_z), (outer, y1, half_z))]
    vertices, faces, offset = [], [], 0
    for low, high in parts:
        part_vertices, part_faces = box_surface(low, high)
        vertices.append(part_vertices)
        faces.append(part_faces + offset)
        offset += len(part_vertices)
    vertices, faces = np.vstack(vertices), np.vstack(faces)
    meshes = {"a": MeshData("a", vertices, faces, None,
                            np.array([vertices.min(axis=0), vertices.max(axis=0)])),
              "o": make_box("o", (-.02, -.02, -.002), (.02, .02, .002))}
    config = ProjectConfig(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "A", 2),), anchor_key="a",
        occluders=(AssetSpec("o", Path("o.ply")),), hdri=HdriSpec(),
        regions=(RegionSpec(radius_m=Range(.5, .5), azimuth=Azimuth(0, 0, False),
                            elevation_deg=Range(90, 90)),),
        occlusion=OcclusionSpec(none_fraction=1., ratio=window, count=(1, 1),
                                scale=Range(1., 1.)))
    pose = CameraPose(np.eye(4), np.array([0., 0., .5]), .5, 0., 0., .005, 50.)
    return config, meshes, pose


@pytest.mark.parametrize("window", [Range(.05, .15), Range(.25, .35)])
def test_an_eccentric_hole_is_aimed_by_its_own_rims(window):
    """An opening away from the middle is why the scan is over the span, not around the centre."""
    config, meshes, pose = _eccentric_hole_fixture(window)
    shares = _pixel_shares(config, meshes, pose)
    assert len(shares) == 30
    assert all(window.minimum <= share <= window.maximum for share in shares), shares


@pytest.mark.parametrize("window", [Range(.25, .35), Range(.05, .15), Range(.8, .9)])
def test_a_low_face_concave_target_is_aimed_by_its_real_outline(window):
    """Aiming a six-face U by its hull over-covers; the rasterised opening keeps it in the window."""
    config, meshes, pose = _u_fixture(window)
    target_mask = _raster(meshes["a"], np.eye(4), pose, config.camera)
    assert 0 < target_mask.sum() < np.prod(target_mask.shape)
    shares = []
    for seed in range(6):
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(seed), ())
        assert isinstance(placements, list), placements
        occluder_mask = _raster(meshes["o"], placements[0].world_from_model, pose, config.camera)
        shares.append(float((target_mask & occluder_mask).sum()) / float(target_mask.sum()))
    assert all(window.minimum - .01 <= share <= window.maximum + .01 for share in shares), shares


def _hull_share(config, meshes, pose, placement, key="a"):
    """The test's own measure: share of the target's projected hull hidden by the occluder's hull."""
    target = _silhouette(bounds_corners(meshes[key].bounds_m), np.eye(4), pose, config.camera)
    occluder = _silhouette(bounds_corners(meshes["o"].bounds_m), placement.world_from_model, pose,
                           config.camera)
    return _covered_share(target, occluder)


def test_convex_and_hollow_targets_are_classified_from_the_same_projected_pose():
    """The envelope is only the outline for a shape that fills it, and a face count cannot tell.

    A 12-face solid box and a 6-face U plate are on opposite sides of the same judgement, which is
    made from the mask and envelope areas of the projection actually sampled.
    """
    config, meshes, pose = _solid_box_fixture(Range(.1, .5), .06)
    solid = _target_shapes([(meshes["a"], np.eye(4))], pose, config.camera).shapes[0]
    assert solid.convex, "a solid box's outline is its envelope"
    assert solid.mask.sum() / solid.envelope_mask.sum() > .98
    for mesh, expected_faces in ((_flat_u_mesh(), 6), (_frame_fixture(Range(.25, .35))[1]["a"], 48)):
        shape = _target_shapes([(mesh, np.eye(4))], pose, config.camera).shapes[0]
        assert len(mesh.faces) == expected_faces
        assert not shape.convex, f"{mesh.key}: a hollow part must not be judged by its envelope"
        assert shape.mask.sum() / shape.envelope_mask.sum() < .75
        assert (shape.envelope_mask & ~shape.mask).sum() > 0, "the opening must be a real hole"


def test_a_convex_target_is_aimed_by_the_exact_envelope_share():
    """A fixed window on a solid part is hit exactly, which a rasterised share cannot do.

    The convex envelope overlap is an exact polygon ratio, so a 0.3 -> 0.3 window closes to well
    below the 1e-4 the acceptance allows instead of stopping on a grid pixel.
    """
    config, meshes, pose = _solid_box_fixture(Range(.3, .3), .06)
    shapes = _target_shapes([(meshes["a"], np.eye(4))], pose, config.camera)
    assert [shape.convex for shape in shapes.shapes] == [True]
    shares = []
    for seed in range(12):
        placements = occluder_placements(config, meshes, pose, "a", np.random.default_rng(seed), ())
        assert isinstance(placements, list), placements
        shares.append(_hull_share(config, meshes, pose, placements[0]))
    assert all(abs(share - .3) <= 1e-4 for share in shares), shares
    # The envelope estimate is continuous, so it aims much closer than the grid's own resolution.
    assert max(abs(share - .3) for share in shares) < 1. / SILHOUETTE_GRID_SIZE


def test_mixed_targets_route_each_shape_through_its_own_estimator():
    """A convex and a hollow target on one pose are measured differently, and it matters.

    Both are classified from the same projection. The frame keeps its mask because its envelope is
    a third larger, a difference far beyond the 1e-4 a window is allowed to miss by, so one shared
    estimator cannot serve both shapes.
    """
    config, meshes, pose = _frame_fixture(Range(.25, .35))
    hollow = _target_shapes([(meshes["a"], np.eye(4))], pose, config.camera).shapes[0]
    solid = _target_shapes([(_solid_box_fixture(Range(.1, .5), .06)[1]["a"], np.eye(4))], pose,
                           config.camera).shapes[0]
    assert (solid.convex, hollow.convex) == (True, False)
    assert abs(hollow.envelope_mask.sum() / hollow.mask.sum() - 1.) > .3
    assert abs(solid.envelope_mask.sum() / solid.mask.sum() - 1.) < .02
    # And the hollow target's proposals still satisfy the window on the shipped camera image.
    shares = _pixel_shares(config, meshes, pose, seeds=range(12))
    assert all(.25 <= share <= .35 for share in shares), shares
