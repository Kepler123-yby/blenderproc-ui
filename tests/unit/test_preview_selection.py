from dataclasses import replace

import pytest

from bproc_core.assets import load_mesh
from bproc_core.config import FixedCameraSpec, RegionSpec
from bproc_core.preview import candidate_for_preview
from bproc_core.scene import Rejection, candidate_to_dict, propose_candidate, sample_scene
from tests.factories import make_project


@pytest.mark.parametrize("fixed", [False, True])
def test_full_production_candidates_and_round_trip(tmp_path, fixed):
    config = make_project(tmp_path)
    config = replace(config, seed=71, scene_count=2, views_per_scene=3,
                     camera_mode="fixed" if fixed else "random",
                     fixed_cameras=(FixedCameraSpec(key="a"), FixedCameraSpec(key="b", azimuth_deg=90)),
                     regions=(RegionSpec(key="east"), RegionSpec(key="west")),
                     occlusion=replace(config.occlusion, none_fraction=.5))
    meshes = {t.asset.key: load_mesh(t.asset) for t in config.targets}
    seen = {}
    for scene_index, view_index in [(s, v) for s in range(2) for v in range(config.views)] + [(0, 0)]:
        result = candidate_for_preview(config, meshes, scene_index, view_index, deadline_s=10)
        assert not isinstance(result, Rejection)
        scene = sample_scene(config, meshes, scene_index)
        expected = propose_candidate(config, meshes, scene, view_index, result.attempt_index)
        payload = candidate_to_dict(result)
        assert payload == candidate_to_dict(expected)
        key = (scene_index, view_index)
        assert payload == seen.setdefault(key, payload)
    assert {p["wants_unoccluded"] for p in seen.values()} == {True, False}
    assert {p["region_key"] for p in seen.values()} == ({"a", "b"} if fixed else {"east", "west"})


def test_invalid_cancelled_and_bounded_queries(tmp_path, monkeypatch):
    config = make_project(tmp_path)
    assert candidate_for_preview(config, {}, -1, 0).reason == "invalid_index"
    assert candidate_for_preview(config, {}, 0, config.views).reason == "invalid_index"
    assert candidate_for_preview(config, {}, 0, 0, cancelled=lambda: True).reason == "cancelled"
    assert candidate_for_preview(config, {}, 0, 0, deadline_s=0).reason == "timeout"
    assert candidate_for_preview(config, {}, 0, 0).reason == "missing_mesh"
    calls = []
    monkeypatch.setattr("bproc_core.preview.sample_scene", lambda *a, **kw: object())
    def reject(*args):
        calls.append(args[-1])
        return Rejection("collision", "test collision")
    monkeypatch.setattr("bproc_core.preview.propose_candidate", reject)
    result = candidate_for_preview(replace(config, scene_count=100000), {}, 99999, 0, max_attempts=3)
    assert result.detail == "test collision"
    assert calls == [0, 1, 2]
