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
                                   'specular': shot.material.specular,
                                   'metallic': shot.material.metallic}
    assert candidate_from_dict(payload).material == shot.material
    payload.pop('material')
    assert candidate_from_dict(payload).material == MaterialState(.65, .5, 0.)


class Socket:
    def __init__(self, value=None):
        self.default_value = value
        self.links = []


class Links:
    def __init__(self):
        self.created = []

    def remove(self, link):
        self.created.remove(link)

    def new(self, source, target):
        self.created.append((source, target))


class Node:
    def __init__(self):
        self.type = "BSDF_PRINCIPLED"
        self.inputs = {"Base Color": Socket((.1, .1, .1, 1.)), "Roughness": Socket(.65),
                       "Metallic": Socket(0.), "Specular IOR Level": Socket(.5)}


class Material:
    def __init__(self):
        self.node_tree = SimpleNamespace(nodes=[Node()], links=Links())


def test_solid_mode_repaints_targets_and_random_mode_restores_vertex_colour():
    backend = BlenderBackend()
    backend.config = replace(fixture_scene()[0], material=MaterialSpec(mode="solid", base_color="#ff0000"))
    target, occluder = Material(), Material()
    backend._bpy_materials = {"target": target, "occluder": occluder}
    vertex = SimpleNamespace()
    backend._vertex_links = {"target": vertex, "occluder": vertex}
    backend._apply_material(SimpleNamespace(material=MaterialState(.12, .87, .6)))
    shader = target.node_tree.nodes[0]
    assert shader.inputs["Base Color"].default_value[:3] == pytest.approx((1., 0., 0.))
    assert shader.inputs["Roughness"].default_value == .12
    assert shader.inputs["Metallic"].default_value == .6
    assert shader.inputs["Specular IOR Level"].default_value == .87
    # A solid paint drops the vertex-colour link; original mode puts it back.
    assert target.node_tree.links.created == []
    backend.config = replace(backend.config, material=MaterialSpec(mode="original"))
    backend._apply_material(SimpleNamespace(material=MaterialState(.3, .4, 0.)))
    assert (vertex, shader.inputs["Base Color"]) in target.node_tree.links.created


def test_validation_rejects_out_of_range_material():
    config, _ = fixture_scene(MaterialSpec(roughness=Range(.2, 1.4)))
    fields = {issue.field for issue in validate_project(config, check_files=False)
              if issue.severity == 'error'}
    assert 'material.roughness' in fields
    ok, _ = fixture_scene(replace(config.material, roughness=Range(.2, 1.0)))
    assert not {'material.roughness'} & {issue.field for issue in validate_project(ok, check_files=False)
                                         if issue.severity == 'error'}
