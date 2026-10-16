from dataclasses import replace

from bproc_core.scene import Rejection, propose_candidate, candidate_to_dict
from bproc_core.assets import load_mesh
from bproc_ui.preview import PreviewController


def test_preview_uses_production_proposal_without_editing_persistent_seed(state):
    state.set_meshes({t.asset.key: load_mesh(t.asset) for t in state.config.targets})
    controller = PreviewController(state)
    candidate = controller.refresh(state.config.regions[0].key, 0, 71)
    assert candidate is not None
    expected = propose_candidate(replace(state.config, seed=71), state.meshes, 0, candidate.attempt_index)
    assert candidate_to_dict(candidate) == candidate_to_dict(expected)
    assert state.config.seed == 42
    assert not state.dirty


def test_preview_impossible_layout_is_bounded_and_clears_candidate(state, monkeypatch):
    calls = []
    def reject(config, meshes, request, attempt):
        calls.append(attempt)
        return Rejection("collision", "overlap")
    monkeypatch.setattr("bproc_ui.preview.propose_candidate", reject)
    controller = PreviewController(state)
    state.set_candidate(object())
    assert controller.refresh() is None
    assert len(calls) == 30
    assert state.candidate is None
    assert not state.dirty
