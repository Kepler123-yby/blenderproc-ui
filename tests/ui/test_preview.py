from dataclasses import replace

from bproc_core.config import Range
from types import SimpleNamespace

from bproc_core.config import AssetSpec, Azimuth, FixedCameraSpec, RegionSpec
from bproc_core.scene import Rejection, propose_candidate, propose_scene, candidate_to_dict
from bproc_core.assets import load_mesh
from bproc_ui.preview import PreviewController

import time

from PySide6.QtCore import QCoreApplication, QEventLoop


def _preview(controller, *args):
    """Ask for a candidate and pump the event loop until the worker reports back."""
    controller.refresh(*args)
    deadline = time.monotonic() + 10
    while controller.waiting() and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.002)
    assert not controller.waiting(), "the proposal worker did not finish in time"
    return controller.state.candidate


def _loaded(state):
    state.set_meshes({t.asset.key: load_mesh(t.asset) for t in state.config.targets})
    return state


def test_preview_uses_production_proposal_without_editing_persistent_seed(state):
    state.set_meshes({t.asset.key: load_mesh(t.asset) for t in state.config.targets})
    controller = PreviewController(state)
    candidate = _preview(controller, state.config.regions[0].key, 0, 71)
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
    monkeypatch.setattr("bproc_core.preview.propose_candidate", reject)
    monkeypatch.setattr("bproc_core.preview.sample_scene",
                        lambda config, meshes, index, **kwargs: SimpleNamespace(scene_index=index))
    controller = PreviewController(state)
    state.set_candidate(object())
    assert _preview(controller) is None
    assert len(calls) == 30
    assert state.candidate is None
    assert not state.dirty


def test_preview_covers_every_view_of_a_multi_view_project(state):
    _loaded(state)
    state.update(views_per_scene=4, scene_count=2,
                 regions=(RegionSpec(key="a"), RegionSpec(key="b")))
    dirty_before = state.dirty
    controller = PreviewController(state)
    region = state.config.regions[0]

    for index in (0, 1, 3):
        controller.scene_index = 1
        candidate = _preview(controller, region.key, index, 71)
        assert candidate is not None, f"视角 {index} 没有拿到候选"
        assert candidate.view_index == index
        effective = replace(state.config, seed=71)
        scene = propose_scene(effective, state.meshes, 1)
        expected = propose_candidate(effective, state.meshes, scene, index, candidate.attempt_index)
        assert candidate.scene_index == 1
        assert state.candidate_config == effective
        assert candidate_to_dict(candidate) == candidate_to_dict(expected)

    assert state.config.seed == 42 and state.dirty == dirty_before, "a preview must not edit the project"


def test_preview_index_is_clamped_to_the_view_count_and_never_wraps(state):
    _loaded(state)
    state.update(views_per_scene=3, scene_count=1)
    controller = PreviewController(state)

    candidate = _preview(controller, state.config.regions[0].key, 99, 71)

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
        candidate = _preview(controller, key, None, 5)
        assert candidate is not None, f"区域 {key} 没有拿到候选"
        assert controller.region_key == key


def test_second_fixed_camera_retains_production_view_index(state):
    _loaded(state)
    cameras = (FixedCameraSpec(key="c1", name="机位 1", radius_m=.5, azimuth_deg=-90.0, elevation_deg=25.0),
               FixedCameraSpec(key="c2", name="机位 2", radius_m=.6, azimuth_deg=90.0, elevation_deg=40.0))
    state.update(camera_mode="fixed", regions=(), fixed_cameras=cameras, scene_count=1)
    controller = PreviewController(state)

    candidate = _preview(controller, "c2", 3, 9)

    assert candidate is not None and candidate.view_index == 1
    assert candidate.camera_kind == "fixed"
    assert controller.region_key == "c2" and controller.sample_index == 1
    effective = replace(state.config, seed=9)
    expected = propose_candidate(effective, state.meshes, propose_scene(effective, state.meshes, 0),
                                 1, candidate.attempt_index)
    assert candidate_to_dict(candidate) == candidate_to_dict(expected)


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
    assert _preview(controller, "r2", 0, 71) is not None

    state.update(regions=(second,))
    assert _preview(controller, "r1", 0, 71) is not None

    assert controller.region_key in {"r1", "r2"}


def _heavy_state(state, count=60):
    """The reviewer's worst case: many occluders, each aimed against a sampled silhouette."""
    from dataclasses import replace as _replace
    from bproc_core.config import AssetSpec, OcclusionSpec
    from bproc_core.assets import load_mesh

    base = state.config.targets[0].asset
    occluders = tuple(AssetSpec(f"o{index}", base.path) for index in range(count))
    config = _replace(state.config, occluders=occluders,
                      occlusion=_replace(state.config.occlusion, count=(count, count), scale=Range(.4, .6)))
    state.update(occluders=config.occluders, occlusion=config.occlusion)
    meshes = {t.asset.key: load_mesh(t.asset) for t in state.config.targets}
    mesh = load_mesh(base)
    meshes.update({occluder.key: mesh for occluder in state.config.occluders})
    state.set_meshes(meshes)
    return state


def test_a_heavy_proposal_never_blocks_the_caller(state):
    from PySide6.QtCore import QTimer

    _heavy_state(state)
    controller = PreviewController(state)
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()

    started = time.monotonic()
    controller.refresh()
    returned = time.monotonic() - started

    assert returned < .2, f"refresh blocked the caller for {returned:.2f}s"
    assert controller.waiting(), "the heavy search was never started"
    _preview(controller)
    timer.stop()
    assert ticks, "the event loop did not run while the search was in flight"


def test_a_superseded_search_cannot_overwrite_the_newer_candidate(state):
    _loaded(state)
    state.update(views_per_scene=3, scene_count=1)
    controller = PreviewController(state)
    region = state.config.regions[0].key
    controller.refresh(region, 0, 71)
    controller.refresh(region, 2, 71)  # supersedes the first search
    _preview(controller)
    for _ in range(200):
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
        if not controller.waiting():
            break
        time.sleep(.002)
    assert state.candidate is not None and state.candidate.view_index == 2
