from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from bproc_core.config import Box, Range
from bproc_ui.panels.resources import ResourcesPanel
from tests.factories import make_mesh


class FakeLoader(QObject):
    loaded = Signal(str, int, object)
    failed = Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, asset, generation):
        self.requests.append((asset, generation))


@pytest.fixture
def panel(state, qtbot):
    loader = FakeLoader()
    panel = ResourcesPanel(state, loader=loader)
    qtbot.addWidget(panel)
    return panel


def test_stale_mesh_result_cannot_restore_removed_target(panel, tmp_path):
    key = panel.add_target(tmp_path / "a.ply")
    asset, generation = panel.loader.requests[-1]
    panel.remove_target(key)
    panel.loader.loaded.emit(key, generation, make_mesh(key))
    assert key not in panel.state.meshes
    assert key not in {t.asset.key for t in panel.state.config.targets}


def test_unit_change_rejects_old_async_result(panel):
    asset, generation = panel.loader.requests[0]
    panel.state.select_target(asset.key)
    panel.unit.setCurrentText("mm")
    panel.loader.loaded.emit(asset.key, generation, make_mesh(asset.key))
    assert asset.key not in panel.state.meshes
    new_asset, new_generation = next(pair for pair in reversed(panel.loader.requests) if pair[0].key == asset.key)
    assert new_generation > generation
    panel.loader.loaded.emit(asset.key, new_generation, make_mesh(asset.key, size=.0001))
    assert asset.key in panel.state.meshes


def test_selection_does_not_change_anchor_and_center_is_protected(panel):
    panel.state.select_target("target-7")
    assert panel.state.config.anchor_key == "target-2"
    with pytest.raises(ValueError, match="先"):
        panel.remove_target("target-2")
    with pytest.raises(ValueError, match="先"):
        panel.set_target_enabled("target-2", False)
    panel.state.set_anchor("target-7")
    panel.remove_target("target-2")
    assert panel.state.config.anchor_key == "target-7"


def test_bulk_offsets_keep_distinct_ids_and_anchor_fixed(panel):
    box = Box(Range(.2, .3), Range(-.1, .1), Range(0, .1))
    original_anchor_offset = panel.state.config.targets[0].offset
    panel.apply_offsets_to_selected(box, ["target-2", "target-7"])
    assert [t.obj_id for t in panel.state.config.targets] == [2, 7]
    assert panel.state.config.targets[0].offset == original_anchor_offset
    assert panel.state.config.targets[1].offset == box


def test_hdri_directory_scans_recursively_and_ignores_other_files(panel, tmp_path):
    root = tmp_path / "haven"
    (root / "child").mkdir(parents=True)
    (root / "a.hdr").touch()
    (root / "child" / "b.EXR").touch()
    (root / "texture.png").touch()
    found = panel.scan_hdri(root)
    assert {path.name for path in found} == {"a.hdr", "b.EXR"}
    assert len(panel.state.config.hdri.files) == 3


def test_ids_are_stable_when_removing_a_noncenter_target(panel, tmp_path):
    panel.remove_target("target-7")
    key = panel.add_target(tmp_path / "new.ply")
    assert panel.state.config.targets[0].obj_id == 2
    assert panel.state.config.targets[1].obj_id == 1


def test_hdri_toggle_preserves_range_precision(panel):
    strength = Range(.12345678901234, .98765432109876)
    rotation = Range(17.123456789012, 123.987654321098)
    panel.state.update(hdri=replace(panel.state.config.hdri, strength=strength, rotation_deg=rotation))
    panel.random_hdri.setChecked(False)
    assert panel.state.config.hdri.strength == strength
    assert panel.state.config.hdri.rotation_deg == rotation
