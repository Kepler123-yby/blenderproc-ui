from pathlib import Path
from types import SimpleNamespace

from bproc_ui.panels.export import ExportPanel
from tests.factories import make_mesh


def test_the_split_option_exports_every_size_group(state, qtbot, tmp_path, monkeypatch):
    state.set_meshes({"target-2": make_mesh(size=.002), "target-7": make_mesh(size=.04)})
    calls = []

    def fake_export(config, destination):
        calls.append((config, Path(destination)))
        return SimpleNamespace(directory=Path(destination), command=f"完整命令 {len(calls)}",
                               preview_command=f"试渲染 {len(calls)}")

    monkeypatch.setattr("bproc_export.bundle.export_job", fake_export)
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    panel.split_scales.setChecked(True)

    with qtbot.waitSignal(panel.exported, timeout=10000):
        assert panel.start_grouped_export(tmp_path / "render_job")

    assert [destination.name for _, destination in calls] == [
        "render_job-size-1", "render_job-size-2"]
    assert [[target.asset.key for target in config.targets] for config, _ in calls] == [
        ["target-2"], ["target-7"]]
    assert panel.command.toPlainText() == "完整命令 1\n\n完整命令 2"
    assert panel.preview_command.toPlainText() == "试渲染 1\n\n试渲染 2"
    assert str(tmp_path / "render_job-size-1") in panel.validation.text()
    assert str(tmp_path / "render_job-size-2") in panel.validation.text()
    assert isinstance(panel.last_result, list) and len(panel.last_result) == 2
    assert panel.job_path.text() == str(tmp_path / "render_job")


def test_a_single_size_group_uses_the_ordinary_export_path(state, qtbot, tmp_path, monkeypatch):
    state.set_meshes({"target-2": make_mesh(size=.04), "target-7": make_mesh(size=.05)})
    called = []

    def fake_export(config, destination):
        called.append(Path(destination))
        return SimpleNamespace(directory=Path(destination), command="普通", preview_command="试")

    panel = ExportPanel(state, export_function=fake_export)
    qtbot.addWidget(panel)
    panel.split_scales.setChecked(True)
    with qtbot.waitSignal(panel.exported, timeout=10000):
        assert panel.start_grouped_export(tmp_path / "one")
    assert len(called) == 1 and called[0].name == "one"
    assert panel.command.toPlainText() == "普通"
