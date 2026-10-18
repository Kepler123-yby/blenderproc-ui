from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import time

from PySide6.QtCore import QCoreApplication, QEventLoop

from bproc_core.project_io import load_project
from bproc_ui.panels.export import ExportPanel
from bproc_ui.state import ProjectState
from tests.factories import make_mesh, make_project, make_runtime, write_mesh


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


def _three_target_state(tmp_path, *, loaded=(2, 7, 9)):
    project = make_project(tmp_path / "assets", ids=(2, 7, 9))
    config = replace(project, runtime=make_runtime(tmp_path / "runtime"))
    for target, size in zip(config.targets, (.002, .04, .02)):
        write_mesh(target.asset.path, make_mesh(target.asset.key, size=size))
    state = ProjectState(config)
    state.set_meshes({target.asset.key: make_mesh(target.asset.key, size=size)
                      for target, size in zip(config.targets, (.002, .04, .02))
                      if target.obj_id in loaded})
    return state


def _wait_for_export(panel, qtbot, path):
    panel.split_scales.setChecked(True)
    started = panel.start_grouped_export(path)
    deadline = time.monotonic() + 15
    while panel._export_task is not None and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 10)
        time.sleep(.002)
    return started


def test_an_unloaded_enabled_target_refuses_the_grouped_export(qtbot, tmp_path):
    state = _three_target_state(tmp_path, loaded=(2, 7))
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    exported = []
    panel.exported.connect(exported.append)
    destination = tmp_path / "grouped-job"
    started = _wait_for_export(panel, qtbot, destination)
    assert started is False
    assert exported == []
    assert panel._export_task is None
    assert "target-9" in panel.validation.text()
    assert panel.validation.objectName() == "errorLabel"
    assert "已按尺寸拆分导出" not in panel.validation.text()
    assert not destination.exists()
    assert not list(tmp_path.glob("grouped-job*"))


def test_a_resource_that_cannot_be_loaded_is_reported_without_job_directories(qtbot, tmp_path):
    state = _three_target_state(tmp_path)
    (state.config.targets[2].asset.path).write_text("not a ply file\n")
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    exported = []
    panel.exported.connect(exported.append)
    destination = tmp_path / "broken-job"
    started = _wait_for_export(panel, qtbot, destination)
    assert started is True, "the pre-flight happens on the worker, not in the panel"
    assert exported == []
    assert panel.validation.objectName() == "errorLabel"
    assert "target-9" in panel.validation.text()
    assert not destination.exists()
    assert not list(tmp_path.glob("broken-job*"))


def test_every_enabled_target_reaches_exactly_one_real_job(qtbot, tmp_path):
    state = _three_target_state(tmp_path)
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    destination = tmp_path / "grouped-job"
    started = _wait_for_export(panel, qtbot, destination)
    assert started is True
    assert panel.validation.objectName() == "successLabel"
    results = panel.last_result
    assert isinstance(results, list) and len(results) == 2
    exported = []
    for result in results:
        project = load_project(result.directory / "scene.yaml")
        assert [target.asset.key for target in project.targets] == [
            target.asset.key for target in project.targets if target.enabled]
        exported.extend((target.asset.key, target.obj_id) for target in project.targets)
    assert sorted(key for key, _ in exported) == ["target-2", "target-7", "target-9"]
    assert len(exported) == len(set(exported)) == 3
    assert sorted(obj_id for _, obj_id in exported) == [2, 7, 9]


def test_the_split_button_reports_an_unloaded_target_instead_of_raising(qtbot, tmp_path):
    state = _three_target_state(tmp_path, loaded=(2, 7))
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    panel.split_scales.setChecked(True)
    destination = tmp_path / "clicked-job"
    panel.job_path.setText(str(destination))
    panel._export_clicked()
    assert panel._export_task is None
    assert panel.validation.objectName() == "errorLabel"
    assert "target-9" in panel.validation.text()
    assert not list(tmp_path.glob("clicked-job*"))
