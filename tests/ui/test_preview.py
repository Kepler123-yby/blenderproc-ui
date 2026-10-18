from dataclasses import replace

from bproc_core.config import Range
from types import SimpleNamespace

from bproc_core.config import Azimuth, FixedCameraSpec, RegionSpec
from bproc_core.scene import Rejection, propose_candidate, propose_scene, candidate_to_dict
from bproc_core.assets import load_mesh
from bproc_ui.preview import PreviewController


def _loaded(state):
    state.set_meshes({t.asset.key: load_mesh(t.asset) for t in state.config.targets})
    return state


def test_preview_uses_production_proposal_without_editing_persistent_seed(state):
    state.set_meshes({t.asset.key: load_mesh(t.asset) for t in state.config.targets})
    controller = PreviewController(state)
    candidate = controller.refresh(state.config.regions[0].key, 0, 71)
    assert candidate is not None
    config = replace(state.config, seed=71)
    expected = propose_candidate(config, state.meshes, propose_scene(config, state.meshes, 0),
                                 candidate.view_index, candidate.attempt_index)
    assert candidate_to_dict(candidate) == candidate_to_dict(expected)
    assert state.config.seed == 42
    assert not state.dirty


def test_preview_impossible_layout_is_bounded_and_clears_candidate(state, monkeypatch):
    calls = []
    def reject(config, meshes, scene, view, attempt):
        calls.append(attempt)
        return Rejection("collision", "overlap")
    monkeypatch.setattr("bproc_ui.preview.propose_candidate", reject)
    monkeypatch.setattr("bproc_ui.preview.sample_scene",
                        lambda config, meshes, index, **kwargs: SimpleNamespace(scene_index=index))
    controller = PreviewController(state)
    state.set_candidate(object())
    assert controller.refresh() is None
    assert len(calls) == 30
    assert state.candidate is None
    assert not state.dirty


def test_preview_covers_every_view_of_a_multi_view_project(state):
    _loaded(state)
    state.update(views_per_scene=4, scene_count=1)
    dirty_before = state.dirty
    controller = PreviewController(state)
    region = state.config.regions[0]

    for index in (0, 1, 3):
        candidate = controller.refresh(region.key, index, 71)
        assert candidate is not None, f"视角 {index} 没有拿到候选"
        assert candidate.view_index == index
        narrowed = replace(state.config, camera_mode="random", regions=(region,), fixed_cameras=(),
                           scene_count=1, seed=71)
        scene = propose_scene(narrowed, state.meshes, 0)
        expected = propose_candidate(narrowed, state.meshes, scene, index, candidate.attempt_index)
        assert candidate_to_dict(candidate) == candidate_to_dict(expected)

    assert state.config.seed == 42 and state.dirty == dirty_before, "a preview must not edit the project"


def test_preview_index_is_clamped_to_the_view_count_and_never_wraps(state):
    _loaded(state)
    state.update(views_per_scene=3, scene_count=1)
    controller = PreviewController(state)

    candidate = controller.refresh(state.config.regions[0].key, 99, 71)

    assert candidate is not None and candidate.view_index == 2
    assert controller.sample_index == 2
    assert 0 <= controller.sample_index < state.config.views


def test_preview_switches_regions_including_a_wrapped_azimuth(state):
    _loaded(state)
    east = RegionSpec(key="r1", name="东", radius_m=Range(0.5, 0.6),
                      azimuth=Azimuth(start_deg=-40.0, end_deg=40.0, full_circle=False))
    west = RegionSpec(key="r2", name="西", radius_m=Range(0.5, 0.6),
                      azimuth=Azimuth(start_deg=170.0, end_deg=-170.0, full_circle=False))
    state.update(regions=(east, west), views_per_scene=2, scene_count=1)
    controller = PreviewController(state)

    for key in ("r2", "r1", "r2"):
        candidate = controller.refresh(key, 1, 5)
        assert candidate is not None, f"区域 {key} 没有拿到候选"
        assert controller.region_key == key


def test_second_fixed_camera_previews_at_view_index_zero(state):
    _loaded(state)
    cameras = (FixedCameraSpec(key="c1", name="机位 1", radius_m=.5, azimuth_deg=-90.0, elevation_deg=25.0),
               FixedCameraSpec(key="c2", name="机位 2", radius_m=.6, azimuth_deg=90.0, elevation_deg=40.0))
    state.update(camera_mode="fixed", regions=(), fixed_cameras=cameras, scene_count=1)
    controller = PreviewController(state)

    candidate = controller.refresh("c2", 3, 9)

    assert candidate is not None and candidate.view_index == 0
    assert candidate.camera_kind == "fixed"
    assert controller.region_key == "c2" and controller.sample_index == 0


def test_preview_of_an_empty_draft_clears_the_candidate(state):
    state.set_meshes({})
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    state.set_candidate(object())

    state.update(regions=(), fixed_cameras=(), camera_mode="random")
    assert controller.refresh() is None

    assert state.candidate is None
    assert "导入目标" in statuses[-1]


def test_preview_falls_back_when_the_selected_region_disappears(state):
    _loaded(state)
    first = RegionSpec(key="r1", name="一")
    second = RegionSpec(key="r2", name="二")
    state.update(regions=(first, second), views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    assert controller.refresh("r2", 0, 71) is not None

    state.update(regions=(second,))
    assert controller.refresh("r1", 0, 71) is not None

    assert controller.region_key in {"r1", "r2"}
