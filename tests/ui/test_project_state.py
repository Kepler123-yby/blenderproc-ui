from dataclasses import replace
import pytest

from bproc_ui.state import ProjectState
from tests.factories import make_mesh


def test_edit_selection_does_not_change_camera_center(state):
    original = state.config.anchor_key
    state.select_target("target-7")
    assert state.config.anchor_key == original
    assert not state.dirty
    state.set_anchor("target-7")
    assert state.config.anchor_key == "target-7"
    assert state.dirty


def test_project_load_restores_ids_units_and_clears_dirty(state, tmp_path):
    target = state.config.targets[1]
    state.update(targets=(state.config.targets[0], replace(target, asset=replace(target.asset, unit="mm"))))
    path = tmp_path / "项目.yaml"
    state.save(path)
    state.update(seed=123)
    state.load(path)
    assert [t.obj_id for t in state.config.targets] == [2, 7]
    assert [t.asset.unit for t in state.config.targets] == ["m", "mm"]
    assert state.config.seed == 42
    assert not state.dirty


def test_asset_loading_epoch_changes_for_reload_not_ordinary_edits(state, tmp_path):
    epoch = state.asset_load_epoch
    state.update(seed=state.config.seed + 1)
    state.set_meshes({})
    assert state.asset_load_epoch == epoch
    path = tmp_path / "same.yaml"
    state.save(path)
    state.load(path)
    assert state.asset_load_epoch == epoch + 1
    state.set_project(state.config)
    assert state.asset_load_epoch == epoch + 2


def test_cannot_anchor_disabled_or_missing_target(state):
    target = replace(state.config.targets[1], enabled=False)
    state.update(targets=(state.config.targets[0], target))
    with pytest.raises(ValueError):
        state.set_anchor(target.asset.key)
    with pytest.raises(ValueError):
        state.set_anchor("missing")


def test_mesh_cache_invalidated_when_same_key_changes_unit(state):
    key = state.config.targets[0].asset.key
    state.set_meshes({key: make_mesh(key)})
    target = state.config.targets[0]
    state.update(targets=(replace(target, asset=replace(target.asset, unit="mm")), state.config.targets[1]))
    assert key not in state.meshes


def test_candidate_and_selection_do_not_mark_scientific_config_dirty(state):
    state.set_candidate(object())
    state.select_target("target-7")
    assert not state.dirty


def test_context_revision_tracks_project_changes_only(state, tmp_path):
    start = state.context_revision
    state.set_candidate(object())
    state.select_target("target-7")
    state.set_meshes({})
    assert state.context_revision == start                 # preview work never invalidates a snapshot
    state.set_anchor("target-7")
    assert state.context_revision == start + 1
    state.set_project(replace(state.config, scene_count=9))
    assert state.context_revision == start + 2
    path = tmp_path / "saved.yaml"
    state.save(path)
    assert state.context_revision == start + 3             # save-as points at a different file
    state.save()
    assert state.context_revision == start + 3             # saving in place keeps the context


def test_load_sets_the_new_path_before_announcing_it(state, tmp_path):
    path = tmp_path / "loaded.yaml"
    state.save(path)
    seen = []
    state.project_changed.connect(lambda: seen.append(state.path))
    state.load(path)
    assert seen == [path.resolve()]
    assert state.dirty is False


def test_recolouring_the_target_keeps_the_candidate_on_screen(state):
    """A material change is appearance, not geometry: the viewports must not blank."""
    from dataclasses import replace
    from bproc_core.scene import SceneCandidate

    candidate = SceneCandidate.__new__(SceneCandidate)
    state.set_candidate(candidate)
    emitted = []
    state.candidate_changed.connect(emitted.append)
    state.update(material=replace(state.config.material, color="solid", base_color="#ff0000"))
    assert state.candidate is candidate
    assert emitted == []


def test_moving_the_camera_still_invalidates_the_candidate(state):
    from dataclasses import replace
    from bproc_core.config import Range

    state.set_candidate(object())
    state.update(camera=replace(state.config.camera, fx=800.0))
    assert state.candidate is None
