"""One occlusion rule serves both the renderer and the offline dataset checker."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bproc_core.config import (AssetSpec, CameraSpec, OcclusionSpec, OutputSpec, ProjectConfig,
                               Range, TargetSpec, validate_project)
from bproc_core.occlusion import (effective_occlusion_range, enabled_target_count,
                                  has_dedicated_occluder, occlusion_accepted)
from bproc_runtime.contracts import RenderedFrame, VisibilityEvidence
from bproc_runtime.records import FrameRef
import bproc_runtime.run as runner


def target(key, obj_id, *, enabled=True):
    return TargetSpec(AssetSpec(key, Path(f"{key}.ply")), key, obj_id, enabled=enabled)


def config(**overrides):
    spec = dict(targets=(target("a", 2),), anchor_key="a", camera=CameraSpec(width=20, height=20),
                scene_count=1, views_per_scene=1,
                occlusion=OcclusionSpec(ratio=Range(.1, .5), max_attempts=4), occluders=(),
                output=OutputSpec(format="rgb_masks"))
    spec.update(overrides)
    return ProjectConfig(**spec)


OCCLUDER = (AssetSpec("occ", Path("occ.ply")),)
TWO = (target("a", 2), target("b", 7))


def test_policy_table_covers_every_combination_that_can_actually_occlude():
    ratio = (.1, .5)
    cases = [
        ({}, True, (0., 0.)),                                   # explicit no-occlusion category
        ({}, False, (0., 0.)),                                  # one target, nothing to cover it
        ({"occluders": OCCLUDER}, False, ratio),                # a usable dedicated occluder
        ({"occluders": OCCLUDER, "occlusion": OcclusionSpec(ratio=Range(.1, .5), count=(0, 0))},
         False, (0., 0.)),                                      # imported but never instantiated
        ({"targets": TWO}, False, ratio),                       # targets occlude each other
        ({"targets": (target("a", 2), target("b", 7, enabled=False))}, False, (0., 0.)),
        ({"targets": TWO}, True, (0., 0.)),                     # a scene the user wants unobstructed
    ]
    for overrides, wants_unoccluded, expected in cases:
        required = effective_occlusion_range(config(**overrides), wants_unoccluded)
        assert (required.minimum, required.maximum) == expected, (overrides, wants_unoccluded)


def test_policy_helpers_describe_why_a_range_degrades():
    assert enabled_target_count(config(targets=TWO)) == 2
    assert enabled_target_count(config(targets=tuple(replace(t, enabled=False) for t in TWO))) == 0
    assert has_dedicated_occluder(config(occluders=OCCLUDER))
    assert not has_dedicated_occluder(config(occluders=OCCLUDER, occlusion=OcclusionSpec(count=(0, 0))))
    assert not has_dedicated_occluder(config())


def test_acceptance_includes_the_configured_endpoints_but_zero_stays_exact():
    ratio = Range(.1, .5)
    assert occlusion_accepted(.1, ratio) and occlusion_accepted(.5, ratio)
    assert not occlusion_accepted(.0999, ratio) and not occlusion_accepted(.5001, ratio)
    zero = Range(0., 0.)
    assert occlusion_accepted(0.0, zero)
    assert not occlusion_accepted(1e-9, zero), "a covered pixel must not pass as unoccluded"


def test_validation_warns_only_when_the_ratio_cannot_be_reached():
    warning = [issue for issue in validate_project(config(), check_files=False)
               if issue.field == "occluders" and issue.severity == "warning"]
    assert warning and "无遮挡" in warning[0].message

    reachable = [issue for issue in validate_project(config(targets=TWO), check_files=False)
                 if issue.field == "occluders"]
    assert not reachable, "two targets can occlude each other, so the ratio stays reachable"

    with_occluder = [issue for issue in validate_project(config(occluders=OCCLUDER), check_files=False)
                     if issue.field == "occluders"]
    assert not with_occluder


def evidence(occluded_fraction=0.0, extra_visible=True):
    """20x20 anchor silhouette with a square hole; the second target only needs pixels."""
    full = np.ones((20, 20), bool)
    visible = full.copy()
    side = int(round(20 * np.sqrt(occluded_fraction)))
    if side:
        visible[:side, :side] = False
    other = np.zeros((20, 20), bool)
    other[19, 0] = True
    masks = {2: visible}
    if extra_visible:
        masks[7] = other
    return VisibilityEvidence(full, masks)


class Backend:
    def __init__(self, frame_evidence):
        self.frame_evidence = frame_evidence
        self.calls = []

    def prepare(self, *args):
        pass

    def apply(self, candidate):
        self.calls.append(candidate.view_index)

    def assess(self, anchor):
        return self.frame_evidence

    def render(self, ev):
        return RenderedFrame(np.zeros((20, 20, 3), np.uint8), None, {2: ev.anchor_full}, ev.visible)

    def close(self):
        pass


class Writer:
    def __init__(self):
        self.refs = []

    def write(self, index, candidate, frame):
        ref = FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2, 7))
        self.refs.append(ref)
        return ref

    def finalize(self):
        pass


class Recorder:
    def __init__(self):
        self.frames, self.rejections, self.summary = [], [], None

    def begin(self, cfg):
        pass

    def record_frame(self, ref, candidate, ev):
        self.frames.append(ref)

    def record_rejection(self, scene_index, view_index, attempt_index, reason, *, phase="view", details=None):
        self.rejections.append((scene_index, view_index, attempt_index, reason, phase))

    def finish(self, summary):
        self.summary = summary


def stub(monkeypatch, *, wants_unoccluded):
    monkeypatch.setattr(runner, "sample_scene", lambda cfg, meshes, index, **kw: SimpleNamespace(
        scene_index=index, wants_unoccluded=wants_unoccluded, placements=(), layout_attempt_index=0))
    monkeypatch.setattr(runner, "propose_candidate", lambda cfg, meshes, scene, view, attempt: SimpleNamespace(
        scene_index=scene.scene_index, view_index=view, attempt_index=attempt,
        wants_unoccluded=wants_unoccluded))


@pytest.mark.parametrize("occluders, count", [((), (1, 3)), (OCCLUDER, (0, 0))])
def test_natural_occlusion_is_independent_of_unused_occluder_assets(monkeypatch, occluders, count):
    """Both "no occluder imported" and "occluder imported but never placed" allow self-occlusion."""
    cfg = config(targets=TWO, occluders=occluders,
                 occlusion=OcclusionSpec(ratio=Range(.1, .5), count=count, max_attempts=4))
    stub(monkeypatch, wants_unoccluded=False)
    recorder = Recorder()

    summary = runner.run_project(cfg, {}, Backend(evidence(.25)), Writer(), recorder)

    assert (summary.status, summary.saved, summary.skipped) == ("completed", 1, 0)
    assert recorder.rejections == []


def test_single_target_without_occluders_degrades_to_no_occlusion(monkeypatch):
    stub(monkeypatch, wants_unoccluded=False)
    recorder = Recorder()
    summary = runner.run_project(config(), {}, Backend(evidence(0.0, extra_visible=False)), Writer(), recorder)
    assert (summary.status, summary.saved) == ("completed", 1)

    rejected = Recorder()
    blocked = config()
    summary = runner.run_project(blocked, {}, Backend(evidence(.25, extra_visible=False)), Writer(), rejected)
    assert (summary.status, summary.skipped) == ("partial", 1), "skipped counts views, not attempts"
    assert len(rejected.rejections) == blocked.occlusion.max_attempts
    assert {(reason, phase) for *_, reason, phase in rejected.rejections} == {("occlusion", "view")}


def test_none_fraction_scenes_still_require_zero_occlusion_with_several_targets(monkeypatch):
    """A natural occluder is still an occluder: the "unoccluded" category must not average it away."""
    cfg = config(targets=TWO, occlusion=OcclusionSpec(none_fraction=1., ratio=Range(.1, .5), max_attempts=4))
    stub(monkeypatch, wants_unoccluded=True)
    recorder = Recorder()

    summary = runner.run_project(cfg, {}, Backend(evidence(.25)), Writer(), recorder)

    assert (summary.status, summary.saved, summary.skipped) == ("partial", 0, 1)
    assert len(recorder.rejections) == cfg.occlusion.max_attempts
    assert {reason for *_, reason, _ in recorder.rejections} == {"occlusion"}
