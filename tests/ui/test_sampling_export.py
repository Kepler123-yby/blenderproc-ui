from dataclasses import replace
import shlex

import pytest

from bproc_core.project_io import load_project
from bproc_core.config import Azimuth, Range
from bproc_ui.panels.sampling import SamplingPanel
from bproc_ui.panels.export import ExportPanel


def test_resolution_scales_intrinsics_once(state, qtbot):
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.resize_image(1280, 960)
    assert state.config.camera.fx == pytest.approx(1144.8228)
    assert state.config.camera.cx == pytest.approx(650.5222)
    panel._resolution_changed()
    assert state.config.camera.fx == pytest.approx(1144.8228)
    state.update(camera=replace(state.config.camera, auto_scale=False))
    panel.resize_image(640, 480)
    assert state.config.camera.fx == pytest.approx(1144.8228)


def test_fixed_and_crossing_azimuths_are_not_sorted(state, qtbot):
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.full_circle.setChecked(False)
    panel.azimuth_start.setValue(170)
    panel.azimuth_end.setValue(-170)
    assert state.config.regions[0].azimuth == Azimuth(170, -170, False)
    panel.azimuth_end.setValue(170)
    assert state.config.regions[0].azimuth == Azimuth(170, 170, False)


def test_region_duplicate_delete_and_allocation_warning(state, qtbot):
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    original = state.config.regions[0]
    duplicate = panel.duplicate_region()
    assert duplicate.key != original.key
    assert duplicate.radius_m == original.radius_m
    panel.total.setValue(1)
    assert "分配为 0" in panel.allocation.text()
    panel.remove_region()
    assert len(state.config.regions) == 1
    panel.remove_region()
    assert len(state.config.regions) == 1


@pytest.mark.parametrize("mode", ["rgb_masks", "bop"])
def test_output_switch_exports_selected_snapshot(state, qtbot, tmp_path, monkeypatch, mode):
    monkeypatch.setattr("bproc_export.bundle.locate_runtime", lambda config: {"schema_version": 1, "ready": True})
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    assert panel.format.count() == 2
    panel.choose_format(mode)
    result = panel.export_to(tmp_path / f"job-{mode}")
    assert load_project(result.directory / "scene.yaml").output.format == mode
    argv = shlex.split(result.command)
    assert "--config" in argv
    assert argv[argv.index("--config") + 1] == str(result.directory / "scene.yaml")
    assert "--num-samples 4" in result.preview_command


def test_export_reports_incomplete_draft_without_creating_output(state, qtbot, tmp_path):
    state.update(hdri=replace(state.config.hdri, files=()))
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    with pytest.raises(ValueError):
        panel.export_to(tmp_path / "job")
    assert "hdri.files" in panel.validation.text()
    assert not (tmp_path / "job").exists()


def test_large_yaml_seed_load_and_unrelated_edits_preserve_exact_integer(state, qtbot, tmp_path):
    large = 2 ** 72 + 4294967297
    state.update(seed=large)
    path = tmp_path / "large-seed.yaml"
    state.save(path)
    state.load(path)
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    assert panel.seed.value() == large
    assert panel.seed.text() == str(large)
    panel.total.setValue(123)
    assert state.config.seed == large
    panel.seed.setText(str(large + 1))
    panel.seed.editingFinished.emit()
    assert state.config.seed == large + 1


def test_unrelated_region_and_intrinsic_edits_preserve_source_precision(state, qtbot):
    original = state.config.regions[0]
    radius = Range(.000025123456789, .000035987654321)
    elevation = Range(12.123456789, 25.987654321)
    azimuth = Azimuth(17.123456789, 35.987654321, False)
    state.update(regions=(replace(original, radius_m=radius, elevation_deg=elevation, azimuth=azimuth),),
                 camera=replace(state.config.camera, fx=81.1234567890123, fy=67.9876543210987))
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.region_name.setText("微型模型范围")
    panel.region_name.editingFinished.emit()
    assert state.config.regions[0].radius_m == radius
    panel.full_circle.setChecked(True)
    assert state.config.regions[0].azimuth.start_deg == azimuth.start_deg
    assert state.config.regions[0].azimuth.end_deg == azimuth.end_deg
    assert state.config.regions[0].elevation_deg == elevation
    panel.auto_scale.setChecked(False)
    assert state.config.camera.fx == 81.1234567890123
    assert state.config.camera.fy == 67.9876543210987
    panel.intrinsics["cx"].setValue(24.5)
    panel.intrinsics["cx"].editingFinished.emit()
    assert state.config.camera.fx == 81.1234567890123
    assert state.config.camera.fy == 67.9876543210987


def test_range_endpoint_edit_keeps_other_endpoint_exact(state, qtbot):
    source = Range(80.1234567890123, 100.9876543210987)
    none_fraction = .1234567890123
    state.update(light=replace(state.config.light, energy_w=source),
                 occlusion=replace(state.config.occlusion, none_fraction=none_fraction))
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.energy.minimum.setValue(82.2)
    assert state.config.light.energy_w == Range(82.2, source.maximum)
    panel.attempts.setValue(31)
    assert state.config.occlusion.none_fraction == none_fraction


def test_output_format_and_device_changes_preserve_unrelated_numeric_precision(state, qtbot):
    scale = .123456789012345
    state.update(output=replace(state.config.output, depth_scale_mm=scale),
                 render=replace(state.config.render, samples=123456))
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    panel.choose_format("bop")
    assert state.config.output.depth_scale_mm == scale
    panel.dataset_name.setText("new-name")
    panel.dataset_name.editingFinished.emit()
    assert state.config.output.depth_scale_mm == scale
    panel.device.setCurrentText("CUDA")
    assert state.config.render.samples == 123456
