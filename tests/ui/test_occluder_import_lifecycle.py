"""Real ResourcesPanel/ProjectState/AssetLoader lifecycle for occluder imports.

The defects this locks down were reproduced with real widgets before the fix (see
``.artifacts/review16-occluder-reference/run-20261020-141959/before/ui/lifecycle.json``):
the import unit followed the target control, one batch created several assets for the same
file, changing a unit dropped the row selection, mixed units were hidden, and clearing the
list raised ``StopIteration`` from ``_sync_occluder_unit``.
"""
from pathlib import Path

import numpy as np
import pytest

from bproc_core.assets import load_mesh
from bproc_core.config import AssetSpec, new_project
from bproc_core.project_io import load_project
from bproc_ui.asset_loader import AssetLoader
from bproc_ui.panels.resources import ResourcesPanel
from bproc_ui.state import ProjectState

CORE = Path('/home/kepler/文档/cores.ply')
BOP = Path('/home/kepler/bproc_project/dataset_rendering/asserts/bop/models')
FILES = sorted(BOP.glob('*.ply'))[:4]

pytestmark = pytest.mark.skipif(not CORE.is_file() or len(FILES) < 4,
                                reason='real cores/BOP resources are required')


def _raw(path, unit):
    return load_mesh(AssetSpec('probe', path, unit))


@pytest.fixture
def panel(tmp_path):
    state = ProjectState(new_project(tmp_path / 'workspace'))
    widget = ResourcesPanel(state, loader=AssetLoader())
    yield widget
    widget.deleteLater()


def _wait_meshes(qtbot, state, keys=None, timeout=120000):
    keys = list(keys) if keys is not None else None
    qtbot.waitUntil(lambda: state.meshes and (keys is None or all(key in state.meshes for key in keys)),
                    timeout=timeout)


def test_target_and_occluder_import_units_are_independent(panel, qtbot):
    state = panel.state
    panel.import_unit.setCurrentText('m')
    assert panel.occluder_import_unit.currentText() == 'mm'
    target_key = panel.add_target(CORE)
    qtbot.waitUntil(lambda: target_key in state.meshes, timeout=120000)
    target_before = next(t for t in state.config.targets if t.asset.key == target_key)

    panel.add_occluders([FILES[0]])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 1 and
                    state.config.occluders[0].key in state.meshes, timeout=120000)
    asset = state.config.occluders[0]
    assert asset.unit == 'mm'
    assert state.config.targets[0].asset.unit == 'm'
    assert state.config.targets[0].asset == target_before.asset
    # The canonical mesh is the raw model value converted exactly once.
    raw = _raw(FILES[0], 'm')
    assert np.array_equal(state.meshes[asset.key].bounds_m, raw.bounds_m * .001)
    assert np.allclose(np.ptp(state.meshes[asset.key].bounds_m, axis=0),
                       np.ptp(raw.bounds_m, axis=0) * .001, rtol=0, atol=1e-15)
    assert 'mm →' in panel.occluder_size_label.text() and 'm（' in panel.occluder_size_label.text()


def test_same_batch_duplicates_and_path_aliases_collapse(panel, qtbot, tmp_path):
    state = panel.state
    panel.import_unit.setCurrentText('m')
    target_key = panel.add_target(CORE)
    qtbot.waitUntil(lambda: target_key in state.meshes, timeout=120000)
    alias = tmp_path / 'alias.ply'
    alias.symlink_to(FILES[0])
    panel.add_occluders([FILES[0], FILES[1], FILES[0], alias, FILES[1]])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 2 and
                    all(a.key in state.meshes for a in state.config.occluders), timeout=120000)
    assert {a.path for a in state.config.occluders} == {FILES[0].resolve(), FILES[1].resolve()}
    assert len({a.key for a in state.config.occluders}) == 2
    # A second batch with the same files adds nothing.
    panel.add_occluders([FILES[0], FILES[1], FILES[2]])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 3, timeout=120000)
    panel.add_occluders([FILES[0], FILES[1], FILES[2]])
    assert len(state.config.occluders) == 3


def test_unit_edit_keeps_selection_and_converts_once(panel, qtbot):
    state = panel.state
    panel.import_unit.setCurrentText('m')
    target_key = panel.add_target(CORE)
    qtbot.waitUntil(lambda: target_key in state.meshes, timeout=120000)
    panel.add_occluders(FILES[:2])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 2 and
                    all(a.key in state.meshes for a in state.config.occluders), timeout=120000)
    keys = [a.key for a in state.config.occluders]
    for row, key in enumerate(keys):
        panel.occluders.item(row).setSelected(True)
    panel.occluders.setCurrentRow(1)
    before = {item.data(0x0100) for item in panel.occluders.selectedItems()}
    assert before == set(keys)

    panel.occluder_unit.setCurrentText('cm')
    qtbot.waitUntil(lambda: all(a.unit == 'cm' for a in state.config.occluders), timeout=15000)
    qtbot.waitUntil(lambda: all(a.key in state.meshes for a in state.config.occluders), timeout=120000)
    after = {item.data(0x0100) for item in panel.occluders.selectedItems()}
    assert after == set(keys), 'changing the unit must not drop the row selection'
    assert panel.occluders.currentRow() == 1
    raw = _raw(FILES[0], 'm')
    assert np.array_equal(state.meshes[keys[0]].bounds_m, raw.bounds_m * .01)

    panel.occluder_unit.setCurrentText('mm')
    qtbot.waitUntil(lambda: all(a.unit == 'mm' for a in state.config.occluders), timeout=15000)
    qtbot.waitUntil(lambda: all(a.key in state.meshes for a in state.config.occluders), timeout=120000)
    assert np.array_equal(state.meshes[keys[0]].bounds_m, raw.bounds_m * .001)
    assert state.config.targets[0].asset.unit == 'm'


def test_mixed_unit_selection_is_explicit(panel, qtbot):
    state = panel.state
    panel.import_unit.setCurrentText('m')
    target_key = panel.add_target(CORE)
    qtbot.waitUntil(lambda: target_key in state.meshes, timeout=120000)
    panel.add_occluders(FILES[:2])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 2 and
                    all(a.key in state.meshes for a in state.config.occluders), timeout=120000)
    first = state.config.occluders[0].key
    panel.occluders.setCurrentRow(0)
    panel.occluder_unit.setCurrentText('cm')
    qtbot.waitUntil(lambda: next(a.unit for a in state.config.occluders if a.key == first) == 'cm',
                    timeout=15000)
    panel.occluders.item(0).setSelected(True)
    panel.occluders.item(1).setSelected(True)
    panel.occluders.item(1).setSelected(True)
    assert panel.occluder_unit.currentIndex() == -1
    assert '混合' in panel.occluder_unit.placeholderText()
    # An explicit choice applies to every selected row and clears the mixed state.
    panel.occluder_unit.setCurrentText('mm')
    qtbot.waitUntil(lambda: all(a.unit == 'mm' for a in state.config.occluders), timeout=15000)
    assert panel.occluder_unit.currentIndex() != -1


def test_remove_clear_and_reload_do_not_leave_stale_state(panel, qtbot, tmp_path):
    state = panel.state
    panel.import_unit.setCurrentText('m')
    target_key = panel.add_target(CORE)
    qtbot.waitUntil(lambda: target_key in state.meshes, timeout=120000)
    panel.add_occluders(FILES[:3])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 3 and
                    all(a.key in state.meshes for a in state.config.occluders), timeout=120000)
    panel.occluders.setCurrentRow(1)
    removed = panel.occluders.currentItem().data(0x0100)
    panel._remove_occluders()
    assert removed not in {a.key for a in state.config.occluders}
    assert removed not in state.meshes
    panel.occluders.clearSelection()
    for row in range(panel.occluders.count()):
        panel.occluders.item(row).setSelected(True)
    panel._remove_occluders()
    assert state.config.occluders == ()
    assert all(key not in state.meshes for key in {removed})
    assert panel.occluders.count() == 0

    panel.add_occluders(FILES[:2])
    qtbot.waitUntil(lambda: len(state.config.occluders) == 2, timeout=120000)
    saved = tmp_path / 'project.yaml'
    state.save(saved)
    state.load(saved)
    assert load_project(saved).occluders == state.config.occluders
    assert [a.unit for a in state.config.occluders] == ['mm', 'mm']
    assert state.config.targets[0].asset.unit == 'm'
    assert panel.occluders.count() == 2


def test_stale_mesh_result_cannot_restore_an_old_unit(panel, qtbot):
    state = panel.state
    panel.import_unit.setCurrentText('m')
    target_key = panel.add_target(CORE)
    qtbot.waitUntil(lambda: target_key in state.meshes, timeout=120000)
    requests = []
    original = panel.loader.request

    def capture(asset, generation):
        requests.append((asset, generation))
        original(asset, generation)

    panel.loader.request = capture
    panel.add_occluders([FILES[0]])
    asset, generation = requests[-1]
    stale = load_mesh(asset)
    panel.occluders.setCurrentRow(0)
    panel.occluder_unit.setCurrentText('cm')
    qtbot.waitUntil(lambda: next(a.unit for a in state.config.occluders) == 'cm', timeout=15000)
    panel.loader.loaded.emit(asset.key, generation, stale)
    assert state.meshes.get(asset.key) is None or not np.array_equal(
        state.meshes[asset.key].bounds_m, stale.bounds_m)
    new_asset, new_generation = next(pair for pair in reversed(requests) if pair[0].key == asset.key)
    assert new_generation > generation
    qtbot.waitUntil(lambda: asset.key in state.meshes, timeout=120000)


class _FakeLoader(__import__('PySide6.QtCore', fromlist=['QObject']).QObject):
    loaded = __import__('PySide6.QtCore', fromlist=['Signal']).Signal(str, int, object)
    failed = __import__('PySide6.QtCore', fromlist=['Signal']).Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, asset, generation):
        self.requests.append((asset, generation))


def test_stale_success_and_failure_never_restore_an_invalidated_occluder(tmp_path):
    from dataclasses import replace
    from bproc_core.config import new_project
    from tests.factories import make_mesh
    state = ProjectState(new_project(tmp_path))
    loader = _FakeLoader()
    panel = ResourcesPanel(state, loader=loader)

    def fresh_pair():
        panel.add_occluders([FILES[0]])
        asset, generation = loader.requests[-1]
        return asset, generation

    # 1) unit change
    asset, generation = fresh_pair()
    panel.occluders.setCurrentRow(0)
    panel.occluder_unit.setCurrentText('cm')
    assert next(a.unit for a in state.config.occluders if a.key == asset.key) == 'cm'
    new_asset, new_generation = loader.requests[-1]
    assert new_generation > generation and new_asset.unit == 'cm'
    panel.loader.loaded.emit(asset.key, generation, make_mesh(asset.key, size=99.))
    panel.loader.failed.emit(asset.key, generation, 'stale failure')
    assert asset.key not in state.meshes or np.ptp(state.meshes[asset.key].bounds_m) < 1.
    panel.loader.loaded.emit(new_asset.key, new_generation, make_mesh(new_asset.key, size=.01))
    assert np.ptp(state.meshes[asset.key].bounds_m) < .1
    panel._remove_occluders()

    # 2) remove, clear and a same-key path change all invalidate the old result
    asset, generation = fresh_pair()
    panel.occluders.setCurrentRow(0)
    panel._remove_occluders()
    panel.loader.loaded.emit(asset.key, generation, make_mesh(asset.key, size=99.))
    assert asset.key not in state.meshes

    asset, generation = fresh_pair()
    panel.occluders.item(0).setSelected(True)
    panel._remove_occluders()
    panel.loader.loaded.emit(asset.key, generation, make_mesh(asset.key, size=99.))
    assert asset.key not in state.meshes

    asset, generation = fresh_pair()
    changed = replace(asset, path=FILES[1].resolve())
    state.update(occluders=(changed,))
    assert state.meshes.get(asset.key) is None
    panel.loader.loaded.emit(asset.key, generation, make_mesh(asset.key, size=99.))
    assert asset.key not in state.meshes
    new_asset, new_generation = loader.requests[-1]
    assert new_generation > generation and new_asset.path == FILES[1].resolve()
    panel.loader.loaded.emit(asset.key, new_generation, make_mesh(asset.key, size=.02))
    assert np.ptp(state.meshes[asset.key].bounds_m) < .1
