from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import shlex

import pytest

from bproc_core.project_io import load_project
from bproc_core.config import Azimuth, Range, validate_project
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
    panel.views.setValue(1)
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
    assert "--num-scenes 1" in result.preview_command


def test_export_reports_incomplete_draft_without_creating_output(state, qtbot, tmp_path):
    state.update(targets=())
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    with pytest.raises(ValueError):
        panel.export_to(tmp_path / "job")
    assert "targets" in panel.validation.text()
    assert not (tmp_path / "job").exists()


def test_draft_without_an_hdri_is_not_a_validation_error(state, qtbot):
    state.update(hdri=replace(state.config.hdri, files=()))
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    assert "hdri.files" not in panel.validation.text()
    fields = {issue.field for issue in validate_project(state.config, check_files=False)
              if issue.severity == "error"}
    assert "hdri.files" not in fields


def test_export_reaches_the_bundler_without_an_hdri(state, qtbot, tmp_path):
    state.update(hdri=replace(state.config.hdri, files=()))
    seen = {}

    def export(config, path):
        seen["files"] = config.hdri.files
        return SimpleNamespace(directory=Path(path), command="cmd", preview_command="preview")

    panel = ExportPanel(state, export_function=export)
    qtbot.addWidget(panel)
    panel.export_to(tmp_path / "job")
    assert seen["files"] == ()

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
    panel.scene_count.setValue(4)
    panel.views.setValue(2)
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


def test_gpu_backends_and_surface_controls_update_only_selected_fields(state, qtbot):
    angle, threshold = 31.1234567890123, .01234567890123
    state.update(render=replace(state.config.render, smooth_angle_deg=angle,
                                noise_threshold=threshold))
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    assert [panel.device.itemText(i) for i in range(panel.device.count())] == [
        "OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"]
    assert panel.device.currentText() == "OPTIX"
    assert panel.smooth_angle.isEnabled()
    panel.device.setCurrentText("CUDA")
    assert state.config.render.smooth_angle_deg == angle
    assert state.config.render.noise_threshold == threshold
    panel.shading_mode.setCurrentIndex(panel.shading_mode.findData("smooth"))
    assert state.config.render.shading_mode == "smooth"
    assert not panel.smooth_angle.isEnabled()
    assert state.config.render.smooth_angle_deg == angle
    panel.weighted_normals.setChecked(True)
    assert state.config.render.weighted_normals is True
    assert state.config.render.noise_threshold == threshold
    panel.denoiser.setCurrentIndex(panel.denoiser.findData("none"))
    assert state.config.render.denoiser == "none"
    panel.shading_mode.setCurrentIndex(panel.shading_mode.findData("auto"))
    assert panel.smooth_angle.isEnabled()
    panel.smooth_angle.setValue(45)
    assert state.config.render.smooth_angle_deg == 45
    assert state.config.render.noise_threshold == threshold
    panel.noise_threshold.setValue(0)
    assert state.config.render.noise_threshold == 0
    assert state.config.render.device == "CUDA"


@pytest.mark.parametrize("mode", ["rgb_masks", "bop"])
@pytest.mark.parametrize("backend,denoiser", [("OPTIX", "OPTIX"), ("CUDA", "auto")])
def test_gpu_surface_settings_save_and_export_same_snapshot(state, qtbot, tmp_path, monkeypatch, mode, backend, denoiser):
    monkeypatch.setattr("bproc_export.bundle.locate_runtime", lambda config: {"schema_version": 1, "ready": True})
    panel = ExportPanel(state)
    qtbot.addWidget(panel)
    panel.choose_format(mode)
    panel.device.setCurrentText(backend)
    panel.shading_mode.setCurrentIndex(panel.shading_mode.findData("smooth"))
    panel.weighted_normals.setChecked(True)
    panel.denoiser.setCurrentIndex(panel.denoiser.findData(denoiser))
    panel.noise_threshold.setValue(.005)
    saved_path = tmp_path / "gpu-project.yaml"
    state.save(saved_path)
    receipt = panel.export_to(tmp_path / f"gpu-{mode}")
    exported = load_project(receipt.directory / "scene.yaml")
    assert load_project(saved_path).render == exported.render == state.config.render
    assert exported.render.device == backend
    assert exported.render.shading_mode == "smooth"
    assert exported.render.weighted_normals is True
    assert exported.render.denoiser == denoiser
    assert exported.render.noise_threshold == .005


def test_fixed_cameras_are_counted_and_edited_independently(state, qtbot):
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.mode_fixed.setChecked(True)
    panel.add_fixed_camera()
    assert state.config.camera_mode == "fixed"
    assert len(state.config.fixed_cameras) == 1 and state.config.views == 1
    assert state.config.scene_count == 250
    panel.fixed_radius.setValue(.42)
    assert state.config.fixed_cameras[0].radius_m == pytest.approx(.42)
    assert state.config.total_shots == 250
    state.set_candidate(SimpleNamespace(camera=SimpleNamespace(radius_m=.42, azimuth_deg=10., elevation_deg=20.)))
    panel.capture_fixed_camera()
    assert len(state.config.fixed_cameras) == 2 and state.config.views == 2
    assert state.config.fixed_cameras[1].radius_m == pytest.approx(.42)
    assert state.config.total_shots == 500
    panel.remove_fixed_camera()
    assert [c.key for c in state.config.fixed_cameras] == [state.config.fixed_cameras[0].key]
    assert state.config.views == 1


def test_fit_distance_follows_the_loaded_mesh_size(state, qtbot):
    import numpy as np
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.add_fixed_camera()
    assert state.config.fixed_cameras[0].radius_m == pytest.approx(.7)
    bounds = np.array([[-.05, -.05, -.05], [.05, .05, .05]])
    state.set_meshes({state.config.anchor_key: SimpleNamespace(bounds_m=bounds)})
    band = panel._fit_band()
    assert panel.fit_fixed_distance()[0].radius_m == pytest.approx(band.minimum)
    assert panel.fit_region_distance()
    region = state.config.regions[0]
    assert region.radius_m.minimum == pytest.approx(band.minimum)
    assert region.radius_m.maximum == pytest.approx(band.maximum)


def test_reopening_a_project_fits_distances_to_the_loaded_mesh(state, qtbot):
    from tests.factories import make_mesh

    # Camera distances tuned for a metre-scale asset, then a 5 cm mesh is loaded.
    state.update(regions=(replace(state.config.regions[0], radius_m=Range(1.2, 2.5)),))
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    assert state.config.regions[0].radius_m.maximum == pytest.approx(2.5)

    meshes = {t.asset.key: make_mesh(t.asset.key, size=0.05) for t in state.config.targets if t.enabled}
    assert len(meshes) > 1, "the fixture needs several targets to exercise partial loading"
    state.set_meshes(meshes)
    panel.sync()
    band = panel._fit_band()
    assert state.config.regions[0].radius_m.maximum == pytest.approx(band.maximum)

    # A deliberate edit afterwards is not snapped back on the next sync.
    panel.radius.minimum.setValue(band.minimum * .9)
    panel.radius.maximum.setValue(band.maximum * 1.2)
    panel.sync()
    assert state.config.regions[0].radius_m.maximum == pytest.approx(band.maximum * 1.2, rel=1e-4)


def test_panel_states_distances_in_multiples_of_the_object_footprint(state, qtbot):
    import numpy as np
    from types import SimpleNamespace
    from bproc_ui.panels.sampling import SamplingPanel

    state.meshes = {state.config.anchor_key: SimpleNamespace(
        bounds_m=np.array([[-.0475, -.0474, -.075], [.0475, .0474, .075]]))}
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    text = panel.fixed_radius_note.text() + panel.radius_note.text()
    assert "物体 XY 半径" in text
    assert "0.0671 m" in text
    panel.deleteLater()


def test_material_source_switches_pages_and_project(state, qtbot):
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    assert panel.material_pages.currentIndex() == 0
    panel.material_source.setCurrentIndex(1)
    assert panel.material_pages.currentIndex() == 1
    assert state.config.material.source == 'fixed'
    panel.material_source.setCurrentIndex(0)
    assert panel.material_pages.currentIndex() == 0
    assert state.config.material.source == 'random'


def test_fixed_material_editors_commit_the_specified_values(state, qtbot):
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    panel.material_source.setCurrentIndex(1)
    panel.fixed_roughness.setValue(0.27)
    panel.fixed_metallic.setValue(0.83)
    assert state.config.material.fixed.roughness == pytest.approx(0.27)
    assert state.config.material.fixed.metallic == pytest.approx(0.83)
    # Switching back to random keeps the specified values for later.
    panel.material_source.setCurrentIndex(0)
    assert state.config.material.fixed.roughness == pytest.approx(0.27)


def test_occluder_strategy_switch_keeps_both_numbers_and_exports_them(state, qtbot, tmp_path, monkeypatch):
    from bproc_core.config import AssetSpec
    from tests.factories import make_mesh, write_mesh
    occluder = write_mesh(tmp_path / "遮盖物.ply", make_mesh("occ", size=.05))
    state.update(occluders=(AssetSpec("occ", occluder.resolve(), "mm"),),
                 occlusion=replace(state.config.occlusion, count=(1, 1)))
    panel = SamplingPanel(state)
    qtbot.addWidget(panel)
    assert state.config.occlusion.strategy == "aimed_view"
    assert panel.occluder_scale.isEnabled() and not panel.physical_scale.isEnabled()
    panel.occluder_scale.minimum.setValue(.2)
    panel.occluder_scale.maximum.setValue(.4)
    assert state.config.occlusion.scale == Range(.2, .4)

    panel.occlusion_strategy.setCurrentIndex(panel.occlusion_strategy.findData("physical_view"))
    assert state.config.occlusion.strategy == "physical_view"
    assert state.config.occlusion.scale == Range(.2, .4), "switching must not overwrite the aimed value"
    assert panel.physical_scale.isEnabled() and not panel.occluder_scale.isEnabled()
    panel.physical_scale.minimum.setValue(.5)
    panel.physical_scale.maximum.setValue(1.5)
    assert state.config.occlusion.physical_scale == Range(.5, 1.5)
    panel.occlusion_strategy.setCurrentIndex(panel.occlusion_strategy.findData("aimed_view"))
    assert state.config.occlusion.scale == Range(.2, .4)
    assert state.config.occlusion.physical_scale == Range(.5, 1.5)
    panel.occlusion_strategy.setCurrentIndex(panel.occlusion_strategy.findData("physical_view"))

    from bproc_core.project_io import save_project
    saved = tmp_path / "策略.yaml"
    save_project(state.config, saved)
    reloaded = load_project(saved)
    assert reloaded.occlusion.strategy == "physical_view"
    assert reloaded.occlusion.physical_scale == Range(.5, 1.5)
    assert reloaded.occlusion.scale == Range(.2, .4)

    monkeypatch.setattr("bproc_export.bundle.locate_runtime", lambda config: {"schema_version": 1, "ready": True})
    export = ExportPanel(state)
    qtbot.addWidget(export)
    receipt = export.export_to(tmp_path / "job-physical")
    exported = load_project(receipt.directory / "scene.yaml")
    assert exported.occlusion.strategy == "physical_view"
    assert exported.occlusion.physical_scale == Range(.5, 1.5)
    assert exported.occlusion.scale == Range(.2, .4)
    assert panel.occlusion_strategy.currentData() == "physical_view"
