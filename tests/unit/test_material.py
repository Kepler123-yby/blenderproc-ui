"""Target-only surface randomisation, mirroring bproc_project's Roughness/Specular sampler."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bproc_core.config import *
from bproc_core.scene import (
    MaterialState, SceneState, candidate_from_dict,
    candidate_to_dict, propose_candidate, propose_scene,
)
from bproc_runtime.blender_backend import BlenderBackend
from tests.factories import make_box


def fixture_scene(material=None):
    box = np.array([[-.05, -.05, -.05], [.05, .05, .05]])
    # A closed box surface, not one triangle: a single face would misdescribe the target silhouette.
    meshes = {'a': make_box('a', box[0], box[1])}
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
    """Links live on both ends: ``socket.links`` is what the backend removes."""

    def __init__(self):
        self.created = []

    def remove(self, link):
        self.created.remove(link)
        target = link[1]
        if link in target.links:
            target.links.remove(link)

    def new(self, source, target):
        link = (source, target)
        self.created.append(link)
        target.links.append(link)


class Node:
    def __init__(self, kind="BSDF_PRINCIPLED"):
        self.type = kind
        self.name = kind
        self.layer_name = ""
        self.outputs = {"Color": Socket()}
        self.inputs = {"Base Color": Socket((.1, .1, .1, 1.)), "Roughness": Socket(.65),
                       "Metallic": Socket(0.), "Specular IOR Level": Socket(.5)}


class Nodes(list):
    def new(self, kind):
        node = Node(kind)
        self.append(node)
        return node

    def get(self, name, default=None):
        return next((node for node in self if node.name == name), default)


class Material:
    def __init__(self, name="material"):
        self.name = name
        self.use_nodes = True
        self.use_fake_user = False
        self.node_tree = SimpleNamespace(nodes=Nodes([Node()]), links=Links())


class Materials(dict):
    """A material slot list that is also addressable by name, like ``bpy.data.materials``."""

    def new(self, name):
        material = Material(name)
        self[name] = material
        return material

    def append(self, material):
        self[material.name] = material


class Mesh:
    def __init__(self):
        self.materials = Materials()


class Bpy:
    """Just enough of ``bpy.data`` for the material path to re-resolve by name."""

    def __init__(self, materials=(), objects=()):
        self.data = SimpleNamespace(materials=Materials(), objects=dict(objects))
        for name in materials:
            self.data.materials.new(name)


def test_solid_mode_repaints_targets_and_random_mode_restores_vertex_colour():
    backend = BlenderBackend()
    backend.bpy = Bpy(materials=("target", "occluder"))
    backend.config = replace(fixture_scene()[0], material=MaterialSpec(color="solid", base_color="#ff0000"))
    backend._material_names = {"target": "target"}
    backend._vertex_layers = {"target": True}
    backend._vertex_nodes = {"target": "bproc_vertex_colour"}
    target = backend.bpy.data.materials["target"]
    vertex = target.node_tree.nodes.new("ShaderNodeVertexColor")
    vertex.name = "bproc_vertex_colour"
    shader = target.node_tree.nodes[0]
    target.node_tree.links.new(vertex.outputs["Color"], shader.inputs["Base Color"])

    backend._apply_material(SimpleNamespace(material=MaterialState(.12, .87, .6)))
    assert shader.inputs["Base Color"].default_value[:3] == pytest.approx((1., 0., 0.))
    assert shader.inputs["Roughness"].default_value == .12
    assert shader.inputs["Metallic"].default_value == .6
    assert shader.inputs["Specular IOR Level"].default_value == .87
    # A solid paint drops the vertex-colour link; original mode puts it back.
    assert target.node_tree.links.created == []

    backend.config = replace(backend.config, material=MaterialSpec(color="original"))
    backend._apply_material(SimpleNamespace(material=MaterialState(.3, .4, 0.)))
    assert (vertex.outputs["Color"], shader.inputs["Base Color"]) in target.node_tree.links.created


def test_a_purged_material_is_rebuilt_instead_of_crashing():
    """BlenderProc's segmap pass purges orphans; a cached bpy struct would raise ReferenceError."""
    backend = BlenderBackend()
    backend.bpy = Bpy()
    backend.bpy.data.objects["target_000001"] = SimpleNamespace(type="MESH", data=Mesh())
    backend.config = replace(fixture_scene()[0], material=MaterialSpec(color="solid", base_color="#00ff00"))
    backend._material_names = {"target_000001": "target_000001_material"}
    backend._vertex_layers = {"target_000001": False}
    backend._apply_material(SimpleNamespace(material=MaterialState(.2, .3, .4)))
    rebuilt = backend.bpy.data.materials["target_000001_material"]
    shader = rebuilt.node_tree.nodes[0]
    assert shader.inputs["Base Color"].default_value[:3] == pytest.approx((0., 1., 0.), abs=1e-3)
    assert shader.inputs["Roughness"].default_value == .2
    assert rebuilt.use_fake_user is True


def test_fixed_source_renders_the_same_surface_for_every_scene():
    config, meshes = fixture_scene(MaterialSpec(source="fixed", fixed=MaterialState(.3, .2, .9),
                                                roughness=Range(.8, .95), metallic=Range(0., 0.)))
    materials = {propose_scene(config, meshes, index).material for index in range(4)}
    assert materials == {MaterialState(.3, .2, .9)}


def test_random_source_still_follows_the_ranges():
    config, meshes = fixture_scene(MaterialSpec(source="random", metallic=Range(0., 1.)))
    metals = {propose_scene(config, meshes, index).material.metallic for index in range(200)}
    assert len(metals) > 5 and metals != {0.0}


def test_validation_rejects_a_fixed_value_above_one():
    config, _ = fixture_scene(MaterialSpec(source="fixed", fixed=MaterialState(1.4, .5, 0.)))
    fields = {issue.field for issue in validate_project(config, check_files=False)
              if issue.severity == 'error'}
    assert 'material.fixed.roughness' in fields


def test_validation_rejects_out_of_range_material():
    config, _ = fixture_scene(MaterialSpec(roughness=Range(.2, 1.4)))
    fields = {issue.field for issue in validate_project(config, check_files=False)
              if issue.severity == 'error'}
    assert 'material.roughness' in fields
    ok, _ = fixture_scene(replace(config.material, roughness=Range(.2, 1.0)))
    assert not {'material.roughness'} & {issue.field for issue in validate_project(ok, check_files=False)
                                         if issue.severity == 'error'}
