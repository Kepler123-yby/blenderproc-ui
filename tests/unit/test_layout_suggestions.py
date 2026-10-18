"""Suggested layout ranges must be geometrically feasible, not merely plausible."""
from dataclasses import replace

import numpy as np
import pytest

from bproc_core.assets import content_bounds, suggest_layout
from bproc_core.config import AssetSpec, Box, CameraSpec, HdriSpec, ProjectConfig, Range, RegionSpec, TargetSpec
from bproc_core.scene import SceneState, placement_bounds, propose_scene, sample_scene
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
                         scene_count=250, layout_max_attempts=30,
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
    assert near_edge >= .1 / 2 + .1 * .1 - 1e-9, "at least 10% of the largest target must stay clear"

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
    # small's centroid moves to at least (big's half size + gap + small's half size)
    assert small.x.minimum - .01 >= .1 + .2 * .1 - 1e-9
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



def _intersects(first, second):
    """True when two AABBs share volume on every axis."""
    return bool(np.all(np.minimum(first[1], second[1]) > np.maximum(first[0], second[0])))


def test_suggest_layout_respects_cad_origins_that_are_not_centred():
    """Bands describe where a model's centroid goes, so a corner origin cannot shift them."""
    meshes = {"corner": make_mesh("corner", center=(.05, 0, 0), size=.1),
              "offset": make_mesh("offset", center=(-.03, .02, 0), size=.1)}
    targets = _targets([("corner", 1), ("offset", 2)])
    boxes, radius = suggest_layout(meshes, targets, "corner", CameraSpec())

    half = float(np.ptp(meshes["offset"].bounds_m[:, 0])) / 2
    free = boxes["offset"]
    assert free.x.minimum - half >= .05 + .1 * .1 - 1e-9, "the near face keeps the gap clear"
    assert free.x.maximum > free.x.minimum

    config = _config(tuple(replace(target, offset=boxes[target.asset.key]) for target in targets), radius, "corner")
    for scene_index in range(60):
        scene = propose_scene(config, meshes, scene_index)
        assert isinstance(scene, SceneState), f"scene {scene_index} rejected: {getattr(scene, 'reason', '')}"
        first, second = (placement_bounds(placement, meshes) for placement in scene.placements)
        assert not _intersects(first, second), f"scene {scene_index} overlaps"


@pytest.mark.parametrize("origin", [(0., 0., 0.), (.11, 0., 0.), (10., -4., 2.)])
def test_layout_is_invariant_to_cad_origin(origin):
    """Where a PLY's local origin sits must not change the suggestion or the framing distance."""
    meshes = {"a": make_mesh("a", center=origin, size=.1), "b": make_mesh("b", center=origin, size=.1)}
    targets = _targets([("a", 1), ("b", 2)])
    boxes, radius = suggest_layout(meshes, targets, "a", CameraSpec())

    assert boxes["b"].x.minimum == pytest.approx(.11), "centroid band must not move with the origin"
    assert boxes["b"].x.maximum == pytest.approx(.21)
    assert boxes["b"].y.minimum == pytest.approx(-.015)
    assert radius.minimum == pytest.approx(expected_radius())


def expected_radius():
    meshes = {"a": make_mesh("a", size=.1), "b": make_mesh("b", size=.1)}
    return suggest_layout(meshes, _targets([("a", 1), ("b", 2)]), "a", CameraSpec())[1].minimum


def test_offset_cad_origin_layout_survives_all_scene_attempts():
    """The reported counterexample: origins at 0 and .11 put 250/250 scenes into collision."""
    meshes = {"a": make_mesh("a", center=(0., 0., 0.), size=.1),
              "b": make_mesh("b", center=(.11, 0., 0.), size=.1)}
    targets = _targets([("a", 1), ("b", 2)])
    boxes, radius = suggest_layout(meshes, targets, "a", CameraSpec())
    placed = tuple(replace(target, offset=boxes[target.asset.key]) for target in targets)
    config = replace(_config(placed, radius, "a"), layout_max_attempts=30)

    for scene_index in range(250):
        scene = sample_scene(config, meshes, scene_index)
        assert isinstance(scene, SceneState), f"scene {scene_index}: {getattr(scene, 'reason', '')}"
        first, second = (placement_bounds(placement, meshes) for placement in scene.placements)
        assert not _intersects(first, second), f"scene {scene_index} produced overlapping targets"


def test_suggest_layout_leaves_meshes_and_ids_untouched():
    meshes = {key: make_mesh(key, center=(.02, 0, 0), size=.1) for key in ("a", "b")}
    before = {key: (mesh.key, mesh.vertices_m.copy(), mesh.bounds_m.copy()) for key, mesh in meshes.items()}
    targets = _targets([("a", 1), ("b", 2)])

    suggest_layout(meshes, targets, "a", CameraSpec())

    for key, mesh in meshes.items():
        name, vertices, bounds = before[key]
        assert mesh.key == name
        assert np.array_equal(mesh.vertices_m, vertices), "suggestions must not rewrite geometry"
        assert np.array_equal(mesh.bounds_m, bounds), "suggestions must not rescale or recentre"


def test_suggest_layout_keeps_the_authored_offset_of_a_disabled_target():
    meshes = {"a": make_mesh("a", size=.1), "b": make_mesh("b", size=.1), "idle": make_mesh("idle", size=.1)}
    authored = Box(Range(.4, .6), Range(-.1, .1), Range(-.1, .1))
    targets = _targets([("a", 1), ("b", 2)]) + (
        replace(TargetSpec(AssetSpec("idle", None), "idle", 3), enabled=False, offset=authored),)

    boxes, radius = suggest_layout(meshes, targets, "a", CameraSpec())

    assert set(boxes) == {"a", "b"}, "a disabled target must not get a suggestion"
    assert "idle" not in boxes
    placed = tuple(replace(target, offset=boxes[target.asset.key]) if target.enabled else target
                   for target in targets)
    assert placed[-1].offset == authored
    everything = content_bounds(meshes, placed)
    enabled_only = content_bounds(meshes, [target for target in placed if target.enabled])
    assert np.array_equal(enabled_only, everything), "a disabled target must not widen the framing box"
    assert everything[1][0] < .4, "the disabled target's authored offset must not leak in"
