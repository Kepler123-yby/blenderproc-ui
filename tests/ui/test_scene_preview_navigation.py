from dataclasses import replace

from PySide6.QtCore import Qt

from bproc_core.assets import load_mesh
from bproc_core.config import FixedCameraSpec, RegionSpec
from bproc_core.scene import candidate_to_dict, propose_candidate, sample_scene
from bproc_ui.main_window import MainWindow
from tests.ui.test_main_window import FakeCamera, FakeScene


def test_real_controls_navigate_full_scene_and_fixed_view_without_dirty(state, qtbot):
    config = replace(state.config, scene_count=2, views_per_scene=3,
                     regions=(RegionSpec(key="east"), RegionSpec(key="west")))
    state.set_project(config)
    state.set_meshes({t.asset.key: load_mesh(t.asset) for t in config.targets})
    window = MainWindow(state, scene_factory=FakeScene, camera_factory=FakeCamera, restore_settings=False)
    qtbot.addWidget(window)
    window._closing = True
    window.show()
    window.scene_index.selectAll()
    qtbot.keyClicks(window.scene_index, "2")
    qtbot.keyClick(window.scene_index, Qt.Key_Return)
    window.sample_index.selectAll()
    qtbot.keyClicks(window.sample_index, "3")
    qtbot.keyClick(window.sample_index, Qt.Key_Return)
    window.preview_seed.setValue(71)
    assert window.scene_index.value() == 2
    assert window.sample_index.value() == 3
    assert (window.preview.scene_index, window.preview.sample_index) == (1, 2)
    qtbot.waitUntil(lambda: state.candidate is not None and
                   (state.candidate.scene_index, state.candidate.view_index) == (1, 2), timeout=5000)
    candidate = state.candidate
    assert window.scene_view.candidate is candidate is window.camera_preview.candidate
    assert window.preview_region.currentData() == "west"
    assert not window.preview_region.isEnabled()
    assert "场景 2" in window.viewport_caption.text() and "视角 3" in window.viewport_caption.text()
    assert "未经像素遮挡验收" in window.preview_hint.text()
    effective = replace(config, seed=71)
    expected = propose_candidate(effective, state.meshes, sample_scene(effective, state.meshes, 1),
                                 2, candidate.attempt_index)
    assert candidate_to_dict(candidate) == candidate_to_dict(expected)
    assert state.candidate_config == effective
    assert state.config == config and not state.dirty

    state.update(camera_mode="fixed", fixed_cameras=(FixedCameraSpec(key="one"),
                 FixedCameraSpec(key="two", azimuth_deg=90)))
    qtbot.waitUntil(lambda: state.candidate is not None and state.candidate.camera_kind == "fixed", timeout=5000)
    assert window.sample_index.maximum() == 2 and window.sample_index.isEnabled()
    assert state.candidate.view_index == 1 and state.candidate.region_key == "two"
    assert window.preview_region.currentData() == "two"
    state.update(scene_count=1, fixed_cameras=(FixedCameraSpec(key="one"),))
    qtbot.waitUntil(lambda: state.candidate is not None and state.candidate.region_key == "one", timeout=5000)
    assert (window.scene_index.value(), window.sample_index.value()) == (1, 1)
    assert (state.candidate.scene_index, state.candidate.view_index) == (0, 0)
    assert window.scene_view.candidate is state.candidate is window.camera_preview.candidate
    state.update(targets=(), anchor_key=None)
    qtbot.waitUntil(lambda: "导入目标" in window.preview_state.text())
    assert state.candidate is state.candidate_config is None
    assert window.scene_view.candidate is window.camera_preview.candidate is None
    assert not window.preview.waiting()
    window.close()


def test_candidate_effective_config_is_cleared_with_project_and_meshes(state):
    effective = replace(state.config, seed=78)
    state.set_candidate(object(), config=effective)
    assert state.candidate_config is effective
    state.set_meshes({})
    assert state.candidate_config is None
    state.set_candidate(object(), config=effective)
    state.set_project(state.config)
    assert state.candidate_config is None
