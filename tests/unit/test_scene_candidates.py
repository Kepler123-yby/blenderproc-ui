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


def test_center_origin_units_and_reproducibility():
    config, meshes = fixture_scene()
    before = meshes['a'].vertices_m.copy()
    candidate = propose_candidate(config, meshes, 0, 0)
    assert isinstance(candidate, SceneCandidate)
    center = transform_points(meshes['a'].bounds_m.mean(axis=0)[None], candidate.placements[0].world_from_model)[0]
    np.testing.assert_allclose(center, [0, 0, 0])
    assert candidate_to_dict(candidate) == candidate_to_dict(propose_candidate(config, meshes, 0, 0))
    np.testing.assert_array_equal(before, meshes['a'].vertices_m)
    position = transform_points(meshes['b'].bounds_m.mean(axis=0)[None], candidate.placements[1].world_from_model)[0]
    assert .06 <= position[1] <= .08


def test_collision_frame_and_no_retry_category_changes():
    config, meshes = fixture_scene()
    t = replace(config.targets[1], offset=Box(Range(0, 0), Range(0, 0), Range(0, 0)))
    assert propose_candidate(replace(config, targets=(config.targets[0], t)), meshes, 0, 0).reason == 'collision'
    t = replace(config.targets[1], offset=Box(Range(0, 0), Range(20, 20), Range(0, 0)))
    assert propose_candidate(replace(config, targets=(config.targets[0], t)), meshes, 0, 0).reason == 'out_of_frame'
    config = replace(config, num_samples=5, occlusion=replace(config.occlusion, none_fraction=.2))
    for request in range(5):
        assert propose_candidate(config, meshes, request, 0).wants_unoccluded == propose_candidate(config, meshes, request, 1).wants_unoccluded
