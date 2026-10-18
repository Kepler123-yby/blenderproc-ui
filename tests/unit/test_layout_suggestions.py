"""Suggested layout ranges must be geometrically feasible, not merely plausible."""
from dataclasses import replace

import numpy as np
import pytest

from bproc_core.assets import content_bounds, suggest_layout
from bproc_core.config import AssetSpec, Box, CameraSpec, HdriSpec, ProjectConfig, Range, RegionSpec, TargetSpec
from bproc_core.scene import SceneState, placement_bounds, propose_scene
from tests.factories import make_mesh

STILL = Box(Range(0., 0.), Range(0., 0.), Range(0., 0.))


def _targets(specs, offsets=None):
    targets = []
    for index, (key, obj_id) in enumerate(specs, 1):
        target = TargetSpec(AssetSpec(key, None, "m"), key, obj_id)
        if offsets and key in offsets:
            target = replace(target, offset=offsets[key])
        targets.append(target)
    return tuple(targets)


def _config(targets, radius, anchor):
    return ProjectConfig(targets=targets, anchor_key=anchor, occluders=(), hdri=HdriSpec(),
                         regions=(RegionSpec(radius_m=radius),))


def _gap(first, second):
    """Smallest separation between two AABBs; negative means they intersect."""
    overlap = np.minimum(first[1], second[1]) - np.maximum(first[0], second[0])
    return float(overlap.min())


def test_suggest_layout_has_disjoint_target_ranges(tmp_path):
    meshes = {key: make_mesh(key, size=.1) for key in ("left", "right")}
    targets = _targets([("left", 1), ("right", 2)])
    boxes, radius = suggest_layout(meshes, targets, "left", CameraSpec())

    assert boxes["left"] == STILL, "the render centre must not jitter"
    assert boxes["right"].x.minimum > 0, "the second target needs its own side of the centre"
    assert boxes["right"].y.minimum < 0 < boxes["right"].y.maximum
    near_edge = boxes["right"].x.minimum - .1 / 2
    assert near_edge - .1 / 2 >= .1 * .1 - 1e-9, "at least 10% of the largest target must stay clear"

    config = _config(tuple(replace(target, offset=boxes[target.asset.key]) for target in targets), radius, "left")
    assert radius.minimum > 0
    for scene_index in range(250):
        scene = propose_scene(config, meshes, scene_index)
        assert isinstance(scene, SceneState), f"scene {scene_index} rejected: {getattr(scene, 'reason', '')}"
        first, second = (placement_bounds(placement, meshes) for placement in scene.placements)
        assert _gap(first, second) < 1e-9, f"scene {scene_index} overlaps: {_gap(first, second)}"


def test_suggest_layout_keeps_gap_for_the_largest_target():
    meshes = {"big": make_mesh("big", size=.2), "small": make_mesh("small", size=.02)}
    targets = _targets([("big", 1), ("small", 2)])
    boxes, _ = suggest_layout(meshes, targets, "big", CameraSpec())
    small = boxes["small"]
    assert small.x.minimum - .01 - .1 >= .2 * .1 - 1e-9
    # A 10x smaller neighbour still gets a usable, non-degenerate band.
    assert small.x.maximum - small.x.minimum >= .02


def test_suggest_layout_places_every_target_clear_of_the_centre():
    meshes = {key: make_mesh(key, size=.05) for key in ("a", "b", "c")}
    targets = _targets([("a", 1), ("b", 2), ("c", 3)])
    boxes, _ = suggest_layout(meshes, targets, "a", CameraSpec())
    ordered = sorted((boxes[key].x.minimum for key in ("b", "c")))
    assert ordered[0] - .025 >= .05 / 2 + .05 * .1 - 1e-9
    assert ordered[1] - ordered[0] >= .05, "each further target owns a later band"


def test_suggest_layout_can_anchor_on_a_later_target():
    meshes = {key: make_mesh(key, size=.1) for key in ("left", "right")}
    targets = _targets([("left", 1), ("right", 2)])
    boxes, _ = suggest_layout(meshes, targets, "right", CameraSpec())
    assert boxes["right"] == STILL
    assert boxes["left"].x.maximum < 0, "the free target moves to the other side of the new centre"


def test_suggest_layout_leaves_disabled_targets_untouched():
    meshes = {key: make_mesh(key, size=.1) for key in ("left", "right", "idle")}
    authored = Box(Range(.4, .6), Range(-.1, .1), Range(-.1, .1))
    targets = _targets([("left", 1), ("right", 2)], {"right": authored})
    targets = targets + (replace(TargetSpec(AssetSpec("idle", None), "idle", 3), enabled=False, offset=authored),)
    boxes, _ = suggest_layout(meshes, targets, "left", CameraSpec())
    assert "idle" not in boxes


def test_suggest_layout_rejects_missing_anchor_or_meshes():
    meshes = {"left": make_mesh("left", size=.1)}
    targets = _targets([("left", 1), ("right", 2)])
    with pytest.raises(ValueError, match="right"):
        suggest_layout(meshes, targets, "left", CameraSpec())
    with pytest.raises(ValueError, match="渲染中心"):
        suggest_layout(meshes, targets[:1], "right", CameraSpec())
    with pytest.raises(ValueError, match="渲染中心"):
        suggest_layout(meshes, targets[:1], None, CameraSpec())


def test_suggest_layout_drives_the_framing_distance_it_reports():
    meshes = {key: make_mesh(key, size=.1) for key in ("left", "right")}
    targets = _targets([("left", 1), ("right", 2)])
    boxes, radius = suggest_layout(meshes, targets, "left", CameraSpec())
    placed = tuple(replace(target, offset=boxes[target.asset.key]) for target in targets)
    bounds = content_bounds(meshes, placed)
    assert radius.minimum > np.ptp(bounds, axis=0).max() / 2, "the band must sit outside the content"
    assert radius.maximum == pytest.approx(radius.minimum * 1.35)
