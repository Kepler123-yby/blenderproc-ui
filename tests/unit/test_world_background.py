"""World background follows the candidate, including projects that never picked an HDRI."""

from dataclasses import replace
from types import SimpleNamespace
import math

import pytest

from bproc_runtime import blender_backend
from bproc_runtime.blender_backend import NEUTRAL_WORLD, BlenderBackend


class Socket:
    def __init__(self):
        self.links = []
        self.default_value = None


class Node:
    def __init__(self, kind):
        self.type = kind
        self.image = None
        self.inputs = {"Color": Socket(), "Strength": Socket(), "Rotation": Socket(),
                       "Vector": Socket()}
        self.outputs = {"Color": object()}


class Links:
    def __init__(self):
        self.items = []

    def new(self, source, target):
        link = (source, target)
        self.items.append(link)
        target.links.append(link)

    def remove(self, link):
        self.items.remove(link)
        link[1].links.remove(link)


class Images:
    def __init__(self):
        self.loaded = []

    def load(self, path, check_existing=False):
        self.loaded.append(path)
        return SimpleNamespace(path=path)


def make_backend():
    environment = Node("TEX_ENVIRONMENT")
    background = Node("BACKGROUND")
    nodes = [Node("TEX_COORD"), Node("MAPPING"), environment, background, Node("OUTPUT_WORLD")]
    tree = SimpleNamespace(nodes=nodes, links=Links())
    tree.links.new(environment.outputs["Color"], background.inputs["Color"])
    world = SimpleNamespace(node_tree=tree)
    images = Images()
    backend = BlenderBackend()
    backend.bpy = SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(world=world)),
                                  data=SimpleNamespace(images=images))
    return backend, tree, environment, background, images


def candidate(hdri=None, strength=0.7, rotation=90.0):
    return SimpleNamespace(hdri_path=hdri, hdri_strength=strength, hdri_rotation_deg=rotation)


def test_project_without_an_hdri_previews_on_neutral_grey():
    backend, tree, environment, background, images = make_backend()
    backend._apply_world(candidate(None))
    assert background.inputs["Color"].default_value == (*NEUTRAL_WORLD, 1.0)
    assert background.inputs["Color"].links == []
    assert environment.image is None
    assert images.loaded == []


def test_hdri_is_loaded_and_relinked_after_a_grey_preview():
    backend, tree, environment, background, images = make_backend()
    backend._apply_world(candidate(None))
    backend._apply_world(candidate("/hdri/studio.hdr"))
    assert environment.image.path == "/hdri/studio.hdr"
    assert images.loaded == ["/hdri/studio.hdr"]
    assert len(tree.links.items) == 1
    assert background.inputs["Color"].links == tree.links.items


def test_strength_and_rotation_come_from_the_candidate():
    backend, _, _, background, _ = make_backend()
    backend._apply_world(candidate("/hdri/studio.hdr", strength=1.25, rotation=45.0))
    assert background.inputs["Strength"].default_value == 1.25
    mapping = next(node for node in backend.bpy.context.scene.world.node_tree.nodes
                   if node.type == "MAPPING")
    assert mapping.inputs["Rotation"].default_value == (0.0, 0.0, math.radians(45.0))
