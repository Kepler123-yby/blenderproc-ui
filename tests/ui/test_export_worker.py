"""The export button must not block the event loop, and old snapshots must never look current."""
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

from bproc_ui.panels.export import ExportPanel


def result_for(path, tag="A"):
    return SimpleNamespace(directory=Path(path), command=f"{tag} 完整命令", preview_command=f"{tag} 试渲染命令")


class Blocking:
    """Export function that reports when it started and waits for the test to release it."""

    def __init__(self):
        self.started, self.release = Event(), Event()
        self.calls = 0

    def __call__(self, config, path):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10), "the test never released the export"
        return result_for(path)


def test_export_runs_off_the_event_thread(state, qtbot, tmp_path):
    blocking = Blocking()
    panel = ExportPanel(state, export_function=blocking)
    qtbot.addWidget(panel)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    assert panel.start_export(tmp_path / "job-a")
    assert blocking.started.wait(10)
    QTest.qWait(120)
    assert len(ticks) >= 5, "the UI thread was blocked by the export"
    assert panel.export_button.text() == "正在导出…"
    assert not panel.export_button.isEnabled()
    assert panel.start_export(tmp_path / "job-b") is False      # a second click queues nothing
    with qtbot.waitSignal(panel.exported, timeout=10000):
        blocking.release.set()
    assert blocking.calls == 1
    assert panel.export_button.isEnabled() and panel.export_button.text() == "导出脚本与配置"
    assert panel.command.toPlainText() == "A 完整命令"
    assert panel.last_result is not None


def test_failure_restores_the_button_and_reports_the_reason(state, qtbot, tmp_path):
    blocking = Blocking()
    blocking.release.set()
    panel = ExportPanel(state, export_function=lambda config, path: 1 / 0)
    qtbot.addWidget(panel)
    with qtbot.waitSignal(panel.message, timeout=10000):
        assert panel.start_export(tmp_path / "job-a")
    assert panel.export_button.isEnabled()
    assert "ZeroDivisionError" in panel.validation.text()


def test_a_result_from_the_previous_project_is_not_shown_as_current(state, qtbot, tmp_path):
    blocking = Blocking()
    panel = ExportPanel(state, export_function=blocking)
    qtbot.addWidget(panel)
    stale = []
    panel.stale.connect(lambda: stale.append(1))
    assert panel.start_export(tmp_path / "job-a")
    assert blocking.started.wait(10)
    state.set_project(replace(state.config, scene_count=3))      # switch projects mid-export
    with qtbot.waitSignal(panel.stale, timeout=10000):
        blocking.release.set()
    assert panel.command.toPlainText() == ""
    assert panel.preview_command.toPlainText() == ""
    assert panel.last_result is None
    assert stale, "the stale result was not announced"
    assert "已过期" in panel.validation.text()
    assert "job-a" in panel.validation.text()                   # the disk copy stays discoverable


def test_a_late_failure_does_not_clear_a_newer_export(state, qtbot, tmp_path):
    from bproc_ui.export_worker import new_request

    panel = ExportPanel(state, export_function=lambda config, path: result_for(path, "B"))
    qtbot.addWidget(panel)
    request_a = new_request(state, tmp_path / "job-a")      # an export started in the old project
    state.set_project(replace(state.config, scene_count=3))
    panel.export_to(tmp_path / "job-b")
    assert panel.command.toPlainText() == "B 完整命令"
    stale = []
    panel.stale.connect(lambda: stale.append(1))
    panel._export_failed(request_a, "旧失败：RuntimeError: 磁盘已满")
    assert panel.command.toPlainText() == "B 完整命令"
    assert panel.preview_command.toPlainText() == "B 试渲染命令"
    assert panel.last_result is not None and stale


def test_switching_project_hides_the_previous_command(state, qtbot, tmp_path):
    panel = ExportPanel(state, export_function=lambda config, path: result_for(path))
    qtbot.addWidget(panel)
    panel.export_to(tmp_path / "job-a")
    assert panel.command.toPlainText() == "A 完整命令"
    stale = []
    panel.stale.connect(lambda: stale.append(1))
    state.set_project(replace(state.config, scene_count=3))
    assert panel.command.toPlainText() == "" and panel.preview_command.toPlainText() == ""
    assert panel.last_result is None and stale
    assert "已隐藏" in panel.validation.text()


def test_refreshing_a_candidate_does_not_invalidate_an_export(state, qtbot, tmp_path):
    panel = ExportPanel(state, export_function=lambda config, path: result_for(path))
    qtbot.addWidget(panel)
    panel.export_to(tmp_path / "job-a")
    state.set_candidate(None)          # a preview refresh must not expire the exported snapshot
    assert panel.command.toPlainText() == "A 完整命令"


def test_changing_settings_hides_the_previous_command(state, qtbot, tmp_path):
    panel = ExportPanel(state, export_function=lambda config, path: result_for(path))
    qtbot.addWidget(panel)
    panel.export_to(tmp_path / "job-a")
    state.update(scene_count=5)
    assert panel.command.toPlainText() == ""
    assert "已隐藏" in panel.validation.text()
