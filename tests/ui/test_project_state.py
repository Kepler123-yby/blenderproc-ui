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
