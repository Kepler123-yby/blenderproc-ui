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

    def prepare(config, candidate, workspace, script):
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


def test_workspace_sits_next_to_the_project(state, tmp_path):
    state.path = tmp_path / "project.yaml"
    viewer = BlenderViewer(state)
    assert viewer.workspace == tmp_path / ".blender-view"
    assert viewer.script.name == "blender_view.py"
    assert viewer.script.exists()
    assert not viewer.open()
