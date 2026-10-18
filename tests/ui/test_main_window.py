from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QWidget, QDialog

from bproc_core.config import FixedCameraSpec, RegionSpec
from bproc_ui.main_window import MainWindow


class FakeScene(QWidget):
    def fit_scene(self):
        pass

    def set_selection(self, key):
        self.selected = key

    def set_scene(self, candidate, meshes, target_rgba=None):
        self.candidate = candidate
        self.target_rgba = target_rgba

    def set_regions(self, config, selected_key=None):
        self.regions = config.regions

    def set_candidates(self, candidates):
        pass


class FakeCamera(QDialog):
    def show_preview(self):
        self.show()

    def set_scene(self, candidate, meshes, target_rgba=None):
        self.candidate = candidate
        self.target_rgba = target_rgba

    def set_camera(self, camera, pose):
        self.camera = camera


def test_editor_multi_selection_preserves_center_and_edits(state, qtbot):
    window = MainWindow(state, scene_factory=FakeScene, camera_factory=FakeCamera, restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    root = window.objects.topLevelItem(0)
    first, second = root.child(0), root.child(1)
    first.setSelected(True)
    second.setSelected(True)
    window.objects.setCurrentItem(second, 0, QItemSelectionModel.NoUpdate)
    window._tree_selection()
    assert state.edited_target_key == "target-7"
    assert state.config.anchor_key == "target-2"
    assert set(window.resources.selected_keys) == {"target-2", "target-7"}
    assert not state.dirty
    window._set_workspace(2)
    assert window.properties.currentIndex() == 2
    assert window.bottom.currentIndex() == 1
    qtbot.wait(220)
    window._closing = True


def test_large_preview_seed_is_not_truncated_and_refresh_increments(state, qtbot):
    large = 2 ** 72 + 7
    state.update(seed=large)
    window = MainWindow(state, scene_factory=FakeScene, camera_factory=FakeCamera, restore_settings=False)
    qtbot.addWidget(window)
    assert window.preview_seed.value() == large
    window._refresh_clicked()
    assert window.preview_seed.value() == large + 1
    assert state.config.seed == large
    qtbot.wait(220)
    window._closing = True


def test_preview_index_walks_scene_views_and_locks_for_pinned_cameras(state, qtbot):
    state.update(views_per_scene=5, scene_count=40)
    window = MainWindow(state, scene_factory=FakeScene, camera_factory=FakeCamera, restore_settings=False)
    qtbot.addWidget(window)
    assert window.sample_index.maximum() == 5, "a preview shows one scene, not the whole dataset"

    state.update(camera_mode="fixed", regions=(), fixed_cameras=(FixedCameraSpec(key="c1"),))
    assert window.sample_index.maximum() == 1
    assert window.sample_index.value() == 1
    assert not window.sample_index.isEnabled(), "a pinned camera has exactly one view"

    state.update(camera_mode="random", regions=(RegionSpec(key="r1"),))
    assert window.sample_index.maximum() == 5 and window.sample_index.isEnabled()
    window._closing = True


def test_switching_projects_clears_the_exported_command_preview(state, qtbot, tmp_path):
    window = MainWindow(state, scene_factory=FakeScene, camera_factory=FakeCamera, restore_settings=False)
    qtbot.addWidget(window)
    window.export_panel.export_function = lambda config, path: SimpleNamespace(
        directory=Path(path), command="A 命令", preview_command="A 试渲染")
    window.export_panel.export_to(tmp_path / "job-a")
    assert window.command_preview.toPlainText() == "A 命令"
    window.export_panel.state.set_project(replace(state.config, scene_count=3))
    assert window.command_preview.toPlainText() == ""
    assert "切换前" in window.command_preview.placeholderText()
    window._closing = True
