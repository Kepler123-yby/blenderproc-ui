"""Target-only surface randomisation, mirroring bproc_project's Roughness/Specular sampler."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_core.config import *
from bproc_core.scene import (
    MaterialState, SceneState, bounds_corners, candidate_from_dict,
    candidate_to_dict, propose_candidate, propose_scene,
)
from bproc_runtime.blender_backend import BlenderBackend


def fixture_scene(material=None):
    box = np.array([[-.05, -.05, -.05], [.05, .05, .05]])
    meshes = {'a': MeshData('a', bounds_corners(box), np.array([[0, 1, 2]]), None, box.copy())}
    targets = (TargetSpec(AssetSpec('a', Path('a.ply')), 'A', 2),)
    config = ProjectConfig(targets=targets, anchor_key='a', hdri=HdriSpec(files=(Path('env.hdr'),)),
                           regions=(RegionSpec(radius_m=Range(1, 1), azimuth=Azimuth(0, 0, False),
                                               elevation_deg=Range(0, 0)),),
                           occlusion=OcclusionSpec(none_fraction=1),
                           material=material or MaterialSpec())
    return config, meshes


def test_material_is_sampled_per_scene_within_ranges_and_reproducible():
    config, meshes = fixture_scene(MaterialSpec(roughness=Range(.2, .3), specular=Range(.6, .7)))
    first, again = propose_scene(config, meshes, 0), propose_scene(config, meshes, 0)
    assert isinstance(first, SceneState)
    assert .2 <= first.material.roughness <= .3
    assert .6 <= first.material.specular <= .7
    assert first.material == again.material
    values = {propose_scene(config, meshes, index).material.roughness for index in range(6)}
    assert len(values) > 1  # not one frozen value for the whole dataset


def test_material_rides_the_candidate_metadata_and_old_payloads_still_load():
    config, meshes = fixture_scene()
    state = propose_scene(config, meshes, 3)
    shot = propose_candidate(config, meshes, state, 0, 0)
    payload = candidate_to_dict(shot)
    assert payload['material'] == {'roughness': shot.material.roughness,
                                   'specular': shot.material.specular}
    assert candidate_from_dict(payload).material == shot.material
    payload.pop('material')
    assert candidate_from_dict(payload).material == MaterialState(.65, .5)


def test_occluders_are_untouched_and_only_targets_receive_values():
    backend = BlenderBackend()
    calls = []

    class Target:
        def __init__(self, name):
            self.name = name

        def set_principled_shader_value(self, key, value):
            calls.append((self.name, key, value))

    backend._target_materials = {2: Target('target'), 9: Target('other')}
    backend._apply_material(SimpleNamespace(material=MaterialState(.12, .87)))
    assert ('target', 'Roughness', .12) in calls
    assert all(name in {'target', 'other'} for name, _, _ in calls)
    assert len([1 for name, key, _ in calls if name == 'target']) == 2


def test_validation_rejects_out_of_range_material():
    config, _ = fixture_scene(MaterialSpec(roughness=Range(.2, 1.4)))
    fields = {issue.field for issue in validate_project(config, check_files=False)
              if issue.severity == 'error'}
    assert 'material.roughness' in fields
    ok, _ = fixture_scene(replace(config.material, roughness=Range(.2, 1.0)))
    assert not {'material.roughness'} & {issue.field for issue in validate_project(ok, check_files=False)
                                         if issue.severity == 'error'}
