from dataclasses import replace
from types import SimpleNamespace

from bproc_core.scene import Rejection, propose_candidate, propose_scene, candidate_to_dict
from bproc_core.assets import load_mesh
from bproc_ui.preview import PreviewController


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
