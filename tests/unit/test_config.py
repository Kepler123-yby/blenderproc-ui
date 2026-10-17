from dataclasses import replace
from pathlib import Path
import pytest

from bproc_core.config import AssetSpec, Azimuth, Box, ConfigError, Range, RegionSpec, new_project, validate_project
from bproc_core.project_io import load_project, save_project, project_to_dict
from tests.factories import make_project


def error_fields(config, check_files=False):
    return {issue.field for issue in validate_project(config, check_files=check_files) if issue.severity == "error"}


def test_defaults_and_complete_roundtrip(tmp_path):
    project = make_project(tmp_path)
    path = tmp_path / "中文 ' $(touch should-not-exist) 项目.yaml"
    save_project(project, path)
    assert load_project(path) == project
    assert project.camera.width == 640 and project.camera.fx == 572.4114
    assert project.output.format == "rgb_masks" and project.occlusion.max_attempts == 30
    assert not error_fields(project, check_files=True)
    assert not (tmp_path / "should-not-exist").exists()
    assert project.render.device == "OPTIX" and project.render.shading_mode == "auto"


def test_incomplete_draft_can_be_saved_and_reloaded(tmp_path):
    draft = new_project(tmp_path)
    save_project(draft, tmp_path / "draft.yaml")
    assert load_project(tmp_path / "draft.yaml") == draft
    assert {"targets", "anchor_key"} <= error_fields(draft)
    assert "hdri.files" not in error_fields(draft)


@pytest.mark.parametrize("change,expected", [
    (lambda p: replace(p, anchor_key="missing"), "anchor_key"),
    (lambda p: replace(p, targets=(replace(p.targets[0], enabled=False), p.targets[1])), "anchor_key"),
    (lambda p: replace(p, targets=(p.targets[0], replace(p.targets[1], obj_id=2))), "targets[1].obj_id"),
    (lambda p: replace(p, targets=(p.targets[0], replace(p.targets[1], asset=p.targets[0].asset))), "targets[1].asset.key"),
    (lambda p: replace(p, regions=(), camera_mode="random"), "regions"),
    (lambda p: replace(p, regions=(replace(p.regions[0], radius_m=Range(0, 1)),)), "regions[0].radius_m.minimum"),
    (lambda p: replace(p, camera=replace(p.camera, fx=float("nan"))), "camera.fx"),
    (lambda p: replace(p, scene_count=True), "scene_count"),
    (lambda p: replace(p, camera=replace(p.camera, height=False)), "camera.height"),
    (lambda p: replace(p, output=replace(p.output, format="rgb")), "output.format"),
    (lambda p: replace(p, occlusion=replace(p.occlusion, ratio=Range(.8, .2))), "occlusion.ratio"),
])
def test_invalid_configs_have_specific_fields(tmp_path, change, expected):
    assert expected in error_fields(change(make_project(tmp_path)))


@pytest.mark.parametrize("text", [
    "schema_version: 2\n",
    "schema_version: 1\nschema_version: 1\n",
    "schema_version: 1\ncamera:\n  skew: 1\n",
    "schema_version: 1\nscene_count: true\n",
    "schema_version: 1\ncamera:\n  fx: .nan\n",
    "schema_version: 1\ncamera:\n  width: 10.5\n",
    "schema_version: 1\nocclusion:\n  count: [1, 2, 3]\n",
    "schema_version: 1\noutput:\n  format: rgb\n",
])
def test_strict_yaml_rejects_unknown_duplicate_and_invalid_scalars(tmp_path, text):
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_project(path)


def test_relative_resource_paths_resolve_against_project_not_cwd(tmp_path, monkeypatch):
    document = tmp_path / "project.yaml"
    document.write_text("schema_version: 1\ntargets:\n- asset:\n    key: one\n    path: assets/模型.ply\n    unit: mm\n  name: 模型\n  obj_id: 7\nanchor_key: one\nhdri:\n  files: ['assets/light.hdr']\noutput:\n  root: results\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = load_project(document)
    assert config.targets[0].asset.path == tmp_path / "assets/模型.ply"
    assert config.hdri.files == (tmp_path / "assets/light.hdr",)
    assert config.output.root == tmp_path / "results"


def test_fixed_and_wrapping_azimuth_valid_and_existing_output_rejected(tmp_path):
    config = make_project(tmp_path)
    regions = tuple(replace(config.regions[0], key=f"r{i}", azimuth=azimuth) for i, azimuth in enumerate((Azimuth(170, -170, False), Azimuth(20, 20, False), Azimuth())))
    config = replace(config, regions=regions)
    assert not error_fields(config)
    existing = config.output.root / config.output.dataset_name
    existing.parent.mkdir()
    existing.symlink_to(tmp_path / "missing-dir", target_is_directory=True)
    assert "output.root" in error_fields(config, check_files=True)


def test_atomic_save_rejects_nonfinite_without_changing_previous_document(tmp_path):
    path = tmp_path / "project.yaml"
    project = make_project(tmp_path)
    save_project(project, path)
    original = path.read_bytes()
    with pytest.raises(ConfigError):
        save_project(replace(project, light=replace(project.light, energy_w=Range(1, float("inf")))), path)
    assert path.read_bytes() == original


def test_gpu_quality_roundtrip_and_legacy_cpu_migration(tmp_path):
    config = make_project(tmp_path)
    config = replace(config, render=replace(config.render, device="OPTIX", shading_mode="smooth",
                     smooth_angle_deg=47.25, weighted_normals=True, denoiser="OPTIX", noise_threshold=.025))
    path = tmp_path / 'quality.yaml'
    save_project(config, path)
    assert load_project(path) == config
    legacy = path.read_text().replace('device: OPTIX', 'device: CPU')
    path.write_text(legacy)
    loaded = load_project(path)
    assert loaded.render.device == 'OPTIX' and loaded.render.weighted_normals is True
    assert path.read_text() == legacy  # Loading never rewrites a user's project.


@pytest.mark.parametrize('changes,field', [
    ({'device':'CPU'},'render.device'),
    ({'shading_mode':'bevel'},'render.shading_mode'),
    ({'smooth_angle_deg':181.},'render.smooth_angle_deg'),
    ({'smooth_angle_deg':float('nan')},'render.smooth_angle_deg'),
    ({'weighted_normals':1},'render.weighted_normals'),
    ({'denoiser':'INTEL'},'render.denoiser'),
    ({'device':'HIP','denoiser':'OPTIX'},'render.denoiser'),
    ({'device':'CUDA','denoiser':'OPTIX'},'render.denoiser'),
    ({'noise_threshold':-.01},'render.noise_threshold'),
    ({'noise_threshold':True},'render.noise_threshold'),
])
def test_gpu_quality_validation(tmp_path, changes, field):
    project = make_project(tmp_path)
    assert field in error_fields(replace(project,render=replace(project.render,**changes)))


def test_fixed_cameras_validate_roundtrip_and_rescale(tmp_path):
    from bproc_core.config import FixedCameraSpec
    project = make_project(tmp_path)
    pinned = (FixedCameraSpec('cam-a', '正面', .7, -90, 25), FixedCameraSpec('cam-b', '侧面', .6, 0, 10))
    config = replace(project, fixed_cameras=pinned, camera_mode='fixed', scene_count=2,
                     views_per_scene=9)
    assert config.views == 2 and config.total_shots == 4
    assert config.with_total_shots(10).scene_count == 5
    path = tmp_path / 'cameras.yaml'
    save_project(config, path)
    assert load_project(path) == config
    assert not {field for field in error_fields(config) if field.startswith('fixed')}

    assert 'fixed_cameras[1].key' in error_fields(replace(config, fixed_cameras=(pinned[0], replace(pinned[1], key='cam-a'))))
    assert 'fixed_cameras[0].elevation_deg' in error_fields(
        replace(config, fixed_cameras=(replace(pinned[0], elevation_deg=91.), pinned[1])))
    assert 'fixed_cameras' in error_fields(replace(config, fixed_cameras=()))
    assert 'views_per_scene' in error_fields(replace(config, camera_mode='random', views_per_scene=0))
    assert 'regions' in error_fields(replace(config, camera_mode='random', regions=()))
    assert 'total_shots' not in error_fields(config)
    assert 'scene_count' in error_fields(replace(config, scene_count=0))


def test_random_mode_counts_views_per_scene(tmp_path):
    project = make_project(tmp_path)
    config = replace(project, camera_mode='random', scene_count=7, views_per_scene=3)
    assert config.views == 3 and config.total_shots == 21
    assert config.with_total_shots(3).scene_count == 1
    assert config.with_total_shots(0).scene_count == 0
