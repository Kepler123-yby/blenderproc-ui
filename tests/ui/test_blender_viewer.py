"""The panel only enables Blender viewing when a candidate exists, and never blocks."""

from pathlib import Path

import pytest

from bproc_ui import blender_view as bridge
from bproc_ui.blender_view import BlenderViewer
from bproc_ui.panels.export import ExportPanel


class _Session:
    def __init__(self, blend):
        self.blend = Path(blend)
        self.log = Path(blend).parent / "blender.log"


@pytest.fixture
def panel(state, qtbot):
    widget = ExportPanel(state)
    qtbot.addWidget(widget)
    return widget


def test_opening_waits_for_a_candidate(panel):
    assert not panel.blender_open.isEnabled()


def test_opening_uses_the_current_candidate(panel, state, qtbot, monkeypatch, tmp_path):
    state.set_candidate(object())
    assert panel.blender_open.isEnabled()
    prepared = {}

    def prepare(config, candidate, workspace, script, *, geometry_only):
        assert geometry_only is True
        prepared["candidate"] = candidate
        return tmp_path / "scene.blend"

    monkeypatch.setattr(bridge, "prepare_scene", prepare)
    monkeypatch.setattr(bridge, "open_gui",
                        lambda config, blend, script, workspace: _Session(blend))
    with qtbot.waitSignal(panel.viewer.opened, timeout=5000) as opened:
        panel.viewer.open()
    assert opened.args[0].blend == tmp_path / "scene.blend"
    assert prepared["candidate"] is state.candidate
    assert "Blender" in panel.blender_state.text()


def test_failure_is_reported_without_a_candidate(panel, qtbot):
    with qtbot.waitSignal(panel.viewer.failed, timeout=1000) as failed:
        assert panel.viewer.open() is False
    assert "候选" in failed.args[0]


def test_build_failure_reaches_the_panel(panel, state, qtbot, monkeypatch):
    state.set_candidate(object())
    monkeypatch.setattr(bridge, "prepare_scene",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("没有 EGL")))
    with qtbot.waitSignal(panel.viewer.failed, timeout=5000) as failed:
        panel.viewer.open()
    assert "没有 EGL" in failed.args[0]
    assert "没有 EGL" in panel.blender_state.text()


def test_illegal_render_pair_opens_explicit_geometry_without_changing_config(panel, state, qtbot,
                                                                           monkeypatch, tmp_path):
    """Replaces the former GUI rejection: strict prepare_scene tests remain unchanged."""
    from dataclasses import replace
    from bproc_core.assets import load_mesh
    from bproc_core.preview import candidate_for_preview
    from bproc_export import blender_view
    from tests.unit.test_blender_view import _locator, _Done

    state.update(render=replace(state.config.render, device="CUDA", denoiser="OPTIX"))
    meshes = {t.asset.key: load_mesh(t.asset) for t in state.config.targets}
    effective = replace(state.config, seed=987)
    state.set_candidate(candidate_for_preview(effective, meshes, 0, 0), config=effective)
    state.path = tmp_path / "project.yaml"
    monkeypatch.setattr(blender_view, "locate_runtime", lambda config: _locator(tmp_path))
    seen = []
    def runner(command, **kwargs):
        seen.append(command)
        Path(command[-1]).write_bytes(b"BLENDER")
        return _Done(0)
    monkeypatch.setattr(bridge, "prepare_scene",
                        lambda *args, **kwargs: blender_view.prepare_scene(*args, runner=runner, **kwargs))
    monkeypatch.setattr(bridge, "open_gui", lambda config, blend, *args: _Session(blend))
    with qtbot.waitSignal(panel.viewer.opened, timeout=5000):
        panel.viewer.open()
    assert len(seen) == 1 and "--geometry-only" in seen[0]
    from bproc_core.project_io import load_project
    snapshot = load_project(tmp_path / ".blender-view/scene.yaml")
    assert snapshot.seed == 987
    assert snapshot.scene_count == state.config.scene_count
    assert snapshot.render == state.config.render
    assert state.config.render.device == "CUDA" and state.config.render.denoiser == "OPTIX"
    assert panel.viewer.busy is False


def test_missing_hdri_is_mentioned_once_the_scene_opens(panel, state, qtbot, monkeypatch, tmp_path):
    from dataclasses import replace
    from bproc_core.config import HdriSpec

    state.config = replace(state.config, hdri=HdriSpec(files=()))
    state.set_candidate(object())
    monkeypatch.setattr(bridge, "prepare_scene", lambda *args, **kwargs: tmp_path / "scene.blend")
    monkeypatch.setattr(bridge, "open_gui", lambda config, blend, script, workspace: _Session(blend))
    with qtbot.waitSignal(panel.viewer.opened, timeout=5000):
        panel.viewer.open()
    assert "中性灰" in panel.blender_state.text()


def test_workspace_sits_next_to_the_project(state, tmp_path):
    state.path = tmp_path / "project.yaml"
    viewer = BlenderViewer(state)
    assert viewer.workspace == tmp_path / ".blender-view"
    assert viewer.script.name == "blender_view.py"
    assert viewer.script.exists()
    assert not viewer.open()
