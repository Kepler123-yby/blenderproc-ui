from dataclasses import replace
from pathlib import Path
import numpy as np

from bproc_core.assets import MeshData
from bproc_core.config import *
from bproc_core.scene import *


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


def test_occluders_sit_on_the_camera_ray_so_the_target_can_actually_be_covered():
    config, meshes = occluded_fixture()
    shot = candidate(config, meshes)
    assert isinstance(shot, SceneCandidate), shot
    ray = shot.camera.position_m / np.linalg.norm(shot.camera.position_m)
    nominal = float(np.linalg.norm(shot.camera.position_m))
    occluders = [p for p in shot.placements if p.role == 'occluder']
    assert len(occluders) == 2
    for placement in occluders:
        center = transform_points(meshes['o'].bounds_m.mean(axis=0)[None], placement.world_from_model)[0]
        along = float(center @ ray)
        assert .25 * nominal <= along <= .6 * nominal   # in front of the target, behind the camera
        assert float(np.linalg.norm(center - along * ray)) <= .06


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
