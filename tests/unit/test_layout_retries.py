"""Layout retries are their own bounded, deterministic budget, shared by a scene's views."""
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from bproc_core.config import (AssetSpec, Box, CameraSpec, ConfigError, OutputSpec, ProjectConfig,
                               Range, RegionSpec, TargetSpec, validate_project)
from bproc_core.project_io import project_from_dict, project_to_dict
from bproc_core.sampling import scene_rng
from bproc_core.scene import Rejection, propose_candidate, propose_scene, sample_scene
from bproc_runtime.contracts import RenderedFrame, VisibilityEvidence
from bproc_runtime.records import FrameRef
import bproc_runtime.run as runner
from tests.factories import make_mesh


def shared(scene):
    """Everything a scene keeps for all of its views, as plain values (the dataclasses hold arrays)."""
    light = scene.light
    return (scene.layout_attempt_index, scene.hdri_strength, scene.hdri_rotation_deg, scene.wants_unoccluded,
            scene.material,
            None if light is None else (tuple(np.round(light.position_m, 12)), light.energy_w,
                                         tuple(light.color_rgb)))


def fingerprint(scene):
    return shared(scene) + (tuple(tuple(map(tuple, placement.world_from_model.tolist()))
                                 for placement in scene.placements),)


def meshes():
    return {"a": make_mesh("a", size=.1), "b": make_mesh("b", size=.1)}


def project(**overrides):
    """Two 10 cm cubes; the second one's offset range decides whether a layout collides."""
    spec = dict(
        targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "a", 2),
                 TargetSpec(AssetSpec("b", Path("b.ply")), "b", 7,
                            offset=Box(Range(-.2, .2), Range(0., 0.), Range(0., 0.)))),
        anchor_key="a", camera=CameraSpec(), scene_count=2, views_per_scene=3,
        regions=(RegionSpec(radius_m=Range(1.0, 1.6)),),
        layout_max_attempts=30, output=OutputSpec(format="rgb_masks"))
    spec.update(overrides)
    return ProjectConfig(**spec)


def solo(**overrides):
    """One target: nothing can collide, so every layout draw is accepted."""
    spec = dict(targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "a", 2),), anchor_key="a",
                camera=CameraSpec(), scene_count=1, views_per_scene=1, layout_max_attempts=30,
                output=OutputSpec(format="rgb_masks"))
    spec.update(overrides)
    return ProjectConfig(**spec)


class Backend:
    def __init__(self):
        self.calls = []

    def prepare(self, *args):
        self.calls.append("prepare")

    def apply(self, candidate):
        self.calls.append("apply")

    def assess(self, anchor_id):
        self.calls.append("assess")
        return VisibilityEvidence(np.ones((240, 320), bool), {2: np.ones((240, 320), bool)})

    def render(self, evidence):
        self.calls.append("render")
        return RenderedFrame(np.zeros((240, 320, 3), np.uint8), None, {2: evidence.anchor_full},
                             evidence.visible)

    def close(self):
        self.calls.append("close")


class Writer:
    def __init__(self):
        self.refs = []

    def write(self, index, candidate, frame):
        ref = FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,))
        self.refs.append(ref)
        return ref

    def finalize(self):
        pass


class Recorder:
    def __init__(self):
        self.rejections, self.frames, self.summary = [], [], None
        self.warnings = []

    def begin(self, config):
        pass

    def record_frame(self, ref, candidate, evidence):
        self.frames.append(ref)

    def warn(self, code, message):
        self.warnings.append(code)

    def record_rejection(self, scene_index, view_index, attempt_index, reason, *, phase="view", details=None):
        self.rejections.append((scene_index, view_index, attempt_index, reason, phase))

    def finish(self, summary):
        self.summary = summary


def test_layout_retry_keeps_the_accepted_layout_for_every_view():
    # Seed 5 draws a colliding layout first and a valid one on the second attempt.
    config = project(seed=5)
    scene = sample_scene(config, meshes(), 0)

    assert not isinstance(scene, Rejection)
    assert scene.layout_attempt_index == 1
    candidates = [propose_candidate(config, meshes(), scene, view, 0) for view in range(config.views)]

    assert all(not isinstance(c, Rejection) for c in candidates)
    assert {c.layout_attempt_index for c in candidates} == {1}
    assert {fingerprint(candidate) for candidate in candidates} == {fingerprint(scene)}


def test_layout_budget_is_separate_from_the_view_budget():
    # Offsets within ±5 cm can never separate two 10 cm cubes, so every layout attempt collides.
    config = project(layout_max_attempts=30)
    config = replace(config, targets=(config.targets[0],
                                      replace(config.targets[1],
                                              offset=Box(Range(-.05, .05), Range(0., 0.), Range(0., 0.)))))
    backend, recorder = Backend(), Recorder()

    summary = runner.run_project(config, meshes(), backend, Writer(), recorder)

    assert (summary.status, summary.saved, summary.skipped, summary.exit_code) == ("partial", 0, 6, 2)
    assert len(recorder.rejections) == config.scene_count * config.layout_max_attempts
    assert {(scene, view, phase) for scene, view, _, _, phase in recorder.rejections} == {
        (0, -1, "layout"), (1, -1, "layout")}
    assert {reason for *_, reason, _ in recorder.rejections} == {"collision"}
    assert recorder.frames == []
    assert "assess" not in backend.calls and "render" not in backend.calls


def test_missing_mesh_is_not_retried(monkeypatch):
    seen = []

    def always_missing(config, meshes_by_key, scene_index, *, layout_attempt_index=0):
        seen.append(layout_attempt_index)
        return Rejection("missing_mesh", "b.ply")

    monkeypatch.setattr("bproc_core.scene.propose_scene", always_missing)

    result = sample_scene(project(), {}, 0, on_rejection=lambda index, rejection: seen.append("cb"))

    assert isinstance(result, Rejection) and result.reason == "missing_mesh"
    assert seen == [0], "a missing asset cannot be fixed by drawing another layout"


def test_layout_retry_reports_each_rejected_attempt(monkeypatch):
    from bproc_core.scene import SceneState

    def collide_twice(config, meshes_by_key, scene_index, *, layout_attempt_index=0):
        if layout_attempt_index < 2:
            return Rejection("collision", "a / b")
        return SceneState(scene_index, (), None, 1., 0., None, True, layout_attempt_index=layout_attempt_index)

    monkeypatch.setattr("bproc_core.scene.propose_scene", collide_twice)
    recorded = []

    scene = sample_scene(project(), meshes(), 3, on_rejection=lambda index, rejection: recorded.append(
        (index, rejection.reason)))

    assert scene.layout_attempt_index == 2
    assert recorded == [(0, "collision"), (1, "collision")]


def test_layout_attempt_zero_preserves_the_historical_stream():
    # Attempt 0 must equal the pre-retry stream, i.e. the historical [seed, scene_index, "SCEN"]
    # sequence alone, otherwise existing projects would re-render into different frames.
    historical = np.random.default_rng(np.random.SeedSequence([11, 4, 0x5343454E]))
    assert scene_rng(11, 4).uniform() == historical.uniform()
    assert scene_rng(11, 4, 0).uniform() == scene_rng(11, 4).uniform()
    # A retry adds the marker and the attempt after those two entries, and never touches the
    # process-global RNG.
    retry = np.random.default_rng(np.random.SeedSequence([11, 4, 0x5343454E, 1]))
    assert scene_rng(11, 4, 1).uniform() == retry.uniform()
    assert scene_rng(11, 4, 2).uniform() != scene_rng(11, 4, 1).uniform()


def test_layout_budget_round_trips_and_defaults():
    config = project(layout_max_attempts=7)
    payload = project_to_dict(config)
    assert payload["layout_max_attempts"] == 7
    assert project_from_dict(payload).layout_max_attempts == 7

    legacy = {key: value for key, value in payload.items() if key != "layout_max_attempts"}
    assert project_from_dict(legacy).layout_max_attempts == 30

    for bad in (0, -1):
        issues = validate_project(replace(config, layout_max_attempts=bad), check_files=False)
        assert any(issue.field == "layout_max_attempts" for issue in issues)


def test_sixty_four_bit_overflowing_seed_stays_deterministic():
    config = solo(seed=2 ** 70 + 5)
    first = propose_scene(config, meshes(), 0)
    second = propose_scene(config, meshes(), 0)
    third = propose_scene(config, meshes(), 0, layout_attempt_index=1)
    assert fingerprint(first) == fingerprint(second)
    assert fingerprint(first) != fingerprint(third)
