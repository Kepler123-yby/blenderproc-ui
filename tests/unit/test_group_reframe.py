"""Opt-in group reframing: math contract, unchanged defaults and transactional publishing."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

import numpy as np
import pytest

from bproc_core.camera import (bounds_corners, bounds_fit_window, envelope_radius,
                               fit_bounds_radius, framing_window, project_points,
                               random_fit_radius, sample_camera)
from bproc_core.config import (CameraSpec, ConfigError, FixedCameraSpec, ProjectConfig, Range,
                               RegionSpec, ScaleGroupingSpec, Azimuth)
from bproc_core.project_io import load_project
from bproc_core.scale_groups import depth_capacity, split_by_scale
from bproc_export import bundle
from tests.factories import make_mesh, make_project, make_runtime, write_mesh


def two_scale_project(tmp_path, *, mode="fixed", reframe=True, margin=0.1, ids=(2, 17)):
    project = make_project(tmp_path / "assets", ids=ids)
    targets = (replace(project.targets[0], asset=replace(project.targets[0].asset, path=tmp_path / "tiny.ply")),
               replace(project.targets[1], asset=replace(project.targets[1].asset, path=tmp_path / "large.ply")))
    write_mesh(tmp_path / "tiny.ply", make_mesh(targets[0].asset.key, size=.002))
    write_mesh(tmp_path / "large.ply", make_mesh(targets[1].asset.key, size=.04))
    project = replace(project, targets=targets,
                      anchor_key=targets[0].asset.key,
                      scale_grouping=ScaleGroupingSpec(reframe, margin),
                      runtime=make_runtime(tmp_path / "runtime"))
    if mode == "fixed":
        project = replace(project, camera_mode="fixed", regions=(),
                          fixed_cameras=(FixedCameraSpec("cam-a", "正面", 0.7, -90., 25.),
                                         FixedCameraSpec("cam-b", "侧面", 0.9, 0., 15.)))
    return project


def meshes_for(project, *, tiny=.002, large=.04):
    return {project.targets[0].asset.key: make_mesh(project.targets[0].asset.key, size=tiny),
            project.targets[1].asset.key: make_mesh(project.targets[1].asset.key, size=large)}


def projected_span(bounds, camera, pose):
    pixels, valid = project_points(bounds_corners(bounds), pose, camera)
    assert valid.all()
    return float(np.ptp(pixels[:, 0])) / camera.width


def test_reframing_defaults_off_and_single_group_is_byte_identical(tmp_path):
    project = two_scale_project(tmp_path, reframe=False)
    meshes = meshes_for(project)
    groups = split_by_scale(project, meshes)
    assert len(groups) == 2
    for group, config in zip(groups, [project]):
        assert group.config.fixed_cameras == project.fixed_cameras
        assert group.config.regions == project.regions
        assert group.config.light == project.light
        assert group.grouping is None
    assert project.scale_grouping == ScaleGroupingSpec(False, .1)

    single = make_project(tmp_path / "single")
    single = replace(single, scale_grouping=ScaleGroupingSpec(True, .1),
                     targets=(replace(single.targets[0], asset=replace(single.targets[0].asset, path=tmp_path / "tiny.ply")),
                              replace(single.targets[1], asset=replace(single.targets[1].asset, path=tmp_path / "large.ply"))))
    single_meshes = {t.asset.key: make_mesh(t.asset.key, size=.02) for t in single.targets}
    groups = split_by_scale(single, single_meshes)
    assert len(groups) == 1
    assert groups[0].config == single          # reframe is ignored for one group
    assert groups[0].grouping is None


def test_fixed_cameras_keep_every_group_in_the_same_margin_window(tmp_path):
    project = two_scale_project(tmp_path, mode="fixed", margin=.12)
    meshes = meshes_for(project)
    groups = split_by_scale(project, meshes)
    assert len(groups) == 2
    window = framing_window(project.camera, .12)
    spans = []
    for group in groups:
        bounds = np.array([[-.001] * 3, [.001] * 3]) if group.index == 0 else None
        report = group.grouping
        assert report["reframe"] is True
        assert report["margin_fraction"] == .12
        assert report["unchanged"]["camera"]["fx"] == project.camera.fx
        assert report["unchanged"]["pixel_size"] == [project.camera.width, project.camera.height]
        assert report["unchanged"]["occlusion_ratio"] == [.1, .5]
        assert report["unchanged"]["depth_scale_mm"] == project.output.depth_scale_mm
        assert [c["key"] for c in report["cameras"]] == ["cam-a", "cam-b"]
        group_bounds = np.asarray(report["group_bounds_m"])
        for spec, camera_report in zip(group.config.fixed_cameras, report["cameras"]):
            pose = sample_camera(spec.region(), np.zeros(3), np.eye(3), project.camera,
                                 np.random.default_rng(0))
            assert bounds_fit_window(group_bounds, project.camera, spec.azimuth_deg,
                                     spec.elevation_deg, window, spec.radius_m)
            spans.append(projected_span(group_bounds, project.camera, pose))
        # Group bound == exact target extent: same shape, different size, so the projected span
        # of the group envelope is the framing that must match within 1 %.
    small_spans, large_spans = spans[:2], spans[2:]
    for small, large in zip(small_spans, large_spans):
        assert abs(small - large) <= .01 * max(small, large)


def test_random_regions_fit_hundred_poses_and_scale_both_endpoints(tmp_path):
    project = two_scale_project(tmp_path, mode="random", margin=.1)
    meshes = meshes_for(project)
    groups = split_by_scale(project, meshes)
    assert len(groups) == 2
    for group in groups:
        report = group.grouping
        region = group.config.regions[0]
        window = framing_window(project.camera, .1)
        radius = random_fit_radius(np.asarray(report["group_bounds_m"]), project.camera, .1)
        fits = 0
        for azimuth in np.linspace(-180, 180, 20, endpoint=False):
            for elevation in (-10., 10., 30., 50., 70.):
                candidate = RegionSpec("probe", radius_m=Range(radius, radius),
                                       azimuth=Azimuth(float(azimuth), float(azimuth), False),
                                       elevation_deg=Range(elevation, elevation))
                pose = sample_camera(candidate, np.zeros(3), np.eye(3), project.camera,
                                     np.random.default_rng(0))
                assert bounds_fit_window(report["group_bounds_m"], project.camera, azimuth,
                                         elevation, window, radius)
                fits += 1
        assert fits == 100
        before = report["cameras"][0]["old_radius_m"]
        assert region.radius_m.maximum > region.radius_m.minimum
        assert region.radius_m.minimum / region.radius_m.maximum == pytest.approx(
            before[0] / before[1])
    small, large = groups
    assert small.config.regions[0].radius_m.minimum < large.config.regions[0].radius_m.minimum
    assert small.config.regions[0].radius_m.maximum < large.config.regions[0].radius_m.maximum
    for left, right in zip((small.config.regions[0].radius_m.minimum, small.config.regions[0].radius_m.maximum),
                           (large.config.regions[0].radius_m.minimum, large.config.regions[0].radius_m.maximum)):
        assert right / left == pytest.approx(large.extents_m[0] / small.extents_m[0], rel=.01)


def test_light_scales_with_s_and_s_squared_and_hdri_is_unchanged(tmp_path):
    project = two_scale_project(tmp_path, mode="fixed", margin=.1)
    meshes = meshes_for(project)
    groups = split_by_scale(project, meshes)
    for group in groups:
        report = group.grouping
        light = report["light"]
        scale = light["scale"]
        assert scale == pytest.approx(report["group_scale_m"] / report["reference_scale_m"])
        assert group.config.light.radius_m.minimum == pytest.approx(project.light.radius_m.minimum * scale)
        assert group.config.light.radius_m.maximum == pytest.approx(project.light.radius_m.maximum * scale)
        assert group.config.light.energy_w.minimum == pytest.approx(project.light.energy_w.minimum * scale ** 2)
        assert group.config.light.energy_w.maximum == pytest.approx(project.light.energy_w.maximum * scale ** 2)
        assert group.config.light.elevation_deg == project.light.elevation_deg
        assert group.config.light.temperature_k == project.light.temperature_k
        assert group.config.hdri == project.hdri


def test_non_anchor_offset_and_non_contiguous_ids_partition_exactly_once(tmp_path):
    project = two_scale_project(tmp_path, mode="fixed", ids=(2, 17))
    meshes = meshes_for(project)
    groups = split_by_scale(project, meshes)
    exported = [(t.asset.key, t.obj_id) for group in groups for t in group.config.targets]
    assert sorted(exported) == sorted((t.asset.key, t.obj_id) for t in project.targets)
    assert len(exported) == len(set(exported)) == 2
    # The second group loses the original anchor, so its target is at the origin and the report
    # still uses the reference envelope including the original offsets.
    assert groups[1].config.anchor_key == project.targets[1].asset.key
    assert groups[1].config.targets[0].offset.x == Range(0., 0.)
    assert groups[1].grouping["reference_bounds_m"] != groups[1].grouping["group_bounds_m"]


def test_grouping_json_records_camera_light_and_source_hash(tmp_path, monkeypatch):
    project = two_scale_project(tmp_path, mode="fixed", margin=.1)
    meshes = meshes_for(project)
    calls = []

    def fake_export(config, destination):
        Path(destination).mkdir(parents=True)
        calls.append((config, Path(destination)))
        return SimpleNamespace(directory=Path(destination), command="cmd", preview_command="preview")

    monkeypatch.setattr(bundle, "export_job", fake_export)
    results = bundle.export_grouped_jobs(project, meshes, tmp_path / "jobs" / "render_job")
    assert [Path(r.directory).name for r in results] == ["render_job-size-1", "render_job-size-2"]
    documents = [json.loads((Path(r.directory) / "grouping.json").read_text(encoding="utf-8"))
                 for r in results]
    assert documents[0]["source_project_sha256"] == documents[1]["source_project_sha256"]
    assert documents[0]["group_index"] == 1 and documents[1]["group_count"] == 2
    assert documents[0]["reframe"] is True
    assert documents[0]["cameras_before_after"][0]["old_radius_m"] == 0.7
    assert documents[0]["light_change"]["scale"] > 0
    assert documents[0]["unchanged"]["obj_ids"] == [2]
    assert documents[1]["unchanged"]["obj_ids"] == [17]


def test_bop_depth_capacity_rejects_before_creating_any_job(tmp_path):
    project = two_scale_project(tmp_path, mode="random", margin=.1)
    project = replace(project, output=replace(project.output, format="bop", depth_scale_mm=0.001))
    meshes = meshes_for(project)
    destination = tmp_path / "jobs" / "render_job"
    with pytest.raises(ConfigError, match="depth_scale|深度"):
        bundle.export_grouped_jobs(project, meshes, destination)
    assert not destination.parent.exists()
    assert not list(tmp_path.glob("render_job-size-*"))


def test_a_later_group_failure_publishes_no_runnable_job(tmp_path, monkeypatch):
    project = two_scale_project(tmp_path, mode="fixed", margin=.1)
    meshes = meshes_for(project)
    calls = []

    def failing_export(config, destination):
        calls.append(Path(destination))
        if len(calls) == 2:
            raise ConfigError("second group broke")
        Path(destination).mkdir(parents=True)
        (Path(destination) / "launch.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(directory=Path(destination), command="cmd", preview_command="preview")

    monkeypatch.setattr(bundle, "export_job", failing_export)
    destination = tmp_path / "jobs" / "render_job"
    with pytest.raises(ConfigError, match="second group"):
        bundle.export_grouped_jobs(project, meshes, destination)
    assert not list(tmp_path.glob("render_job-size-*"))
    assert not list(tmp_path.rglob("launch.json"))


def test_a_later_destination_conflict_is_detected_up_front(tmp_path):
    project = two_scale_project(tmp_path, mode="fixed", margin=.1)
    meshes = meshes_for(project)
    (tmp_path / "render_job-size-2").mkdir()
    with pytest.raises(ConfigError, match="已存在"):
        bundle.export_grouped_jobs(project, meshes, tmp_path / "render_job")
    assert not (tmp_path / "render_job-size-1").exists()


def test_exported_snapshot_keeps_the_grouping_and_reframe_settings(tmp_path):
    project = two_scale_project(tmp_path, mode="fixed", margin=.15)
    project = replace(project, runtime=make_runtime(tmp_path / "runtime"))
    meshes = meshes_for(project)
    results = bundle.export_grouped_jobs(project, meshes, tmp_path / "render_job")
    for result in results:
        snapshot = load_project(Path(result.directory) / "scene.yaml")
        assert snapshot.scale_grouping.reframe is True
        assert snapshot.scale_grouping.margin_fraction == pytest.approx(.15)
    small, large = (load_project(Path(result.directory) / "scene.yaml") for result in results)
    assert small.fixed_cameras[0].radius_m < large.fixed_cameras[0].radius_m
    assert small.camera.fx == large.camera.fx == project.camera.fx
    assert small.output.depth_scale_mm == large.output.depth_scale_mm == project.output.depth_scale_mm


def test_cli_reframe_flags_change_the_exported_group_configuration(tmp_path, capsys):
    from bproc_core.project_io import save_project
    from tools.export_by_scale import main

    project = two_scale_project(tmp_path, mode="fixed", reframe=False)
    path = tmp_path / "project.yaml"
    save_project(project, path)
    code = main([str(path), str(tmp_path / "out"), "--dry-run",
                 "--reframe-groups", "--framing-margin", "0.2"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"][0]["reframe"] is True
    assert payload["groups"][0]["cameras_before_after"][0]["new_radius_m"] != 0.7
    assert payload["groups"][0]["scale_factor"] < payload["groups"][1]["scale_factor"] < 1
    assert payload["groups"][0]["light_change"]["scale"] < 1

    code = main([str(path), str(tmp_path / "out2"), "--dry-run", "--no-reframe-groups"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"][0]["reframe"] is False
    assert payload["groups"][0]["cameras_before_after"] == []


# -- R4: safe clamps in random regions must be recorded, not silently forced to [] --------------
def _tight_random_project(tmp_path, region):
    project = two_scale_project(tmp_path, mode="random", margin=.1)
    return replace(project, regions=(region,))


def _clamps_by_endpoint(report):
    return {row.get("endpoint"): row for row in report["clamps"]}


def test_random_reframe_records_both_clamped_endpoints_for_every_group(tmp_path):
    region = RegionSpec(key="tight", radius_m=Range(1e-5, 2e-5))
    project = _tight_random_project(tmp_path, region)
    groups = split_by_scale(project, meshes_for(project))
    assert len(groups) == 2
    safe_values = []
    for group in groups:
        report = group.grouping
        fit_reference = report["cameras"][0]["fit_reference_m"]
        clamps = _clamps_by_endpoint(report)
        assert set(clamps) == {"minimum", "maximum"}, report["clamps"]
        for endpoint, requested in (("minimum", 1e-5), ("maximum", 2e-5)):
            row = clamps[endpoint]
            assert row["requested_m"] == pytest.approx(requested)
            assert row["safe_m"] == pytest.approx(report["cameras"][0]["new_radius_m"][
                0 if endpoint == "minimum" else 1])
            assert row["reason"] and row["region"] == "tight"
            assert requested < fit_reference
        safe_values.append((group.config.regions[0].radius_m.minimum,
                            group.config.regions[0].radius_m.maximum))
    assert len(groups[0].grouping["clamps"]) == len(groups[1].grouping["clamps"]) == 2
    # The 2 mm and 40 mm groups clamp to their own safe fits, not to the same number.
    assert safe_values[0][0] < safe_values[1][0]


def test_random_reframe_records_only_the_endpoint_that_was_clamped(tmp_path):
    region = RegionSpec(key="mixed", radius_m=Range(1e-5, 100.0))
    project = _tight_random_project(tmp_path, region)
    for group in split_by_scale(project, meshes_for(project)):
        report = group.grouping
        clamps = _clamps_by_endpoint(report)
        assert set(clamps) == {"minimum"}, report["clamps"]
        assert clamps["minimum"]["requested_m"] == pytest.approx(1e-5)
        assert clamps["minimum"]["safe_m"] > clamps["minimum"]["requested_m"]
        output = group.config.regions[0].radius_m
        assert output.maximum > output.minimum


def test_random_reframe_records_no_clamp_when_the_request_is_already_safe(tmp_path):
    region = RegionSpec(key="safe", radius_m=Range(1.0, 2.0))
    project = _tight_random_project(tmp_path, region)
    for group in split_by_scale(project, meshes_for(project)):
        report = group.grouping
        assert report["clamps"] == []
        output = group.config.regions[0].radius_m
        assert output.minimum == pytest.approx(
            group.grouping["cameras"][0]["new_radius_m"][0])
        assert output.minimum < output.maximum


def test_fixed_reframe_keeps_its_existing_clamp_record(tmp_path):
    project = two_scale_project(tmp_path, mode="fixed", margin=.1)
    project = replace(project, fixed_cameras=(
        FixedCameraSpec("cam-a", "正面", 1e-5, -90., 25.),
        FixedCameraSpec("cam-b", "侧面", 1e-5, 0., 15.)))
    for group in split_by_scale(project, meshes_for(project)):
        report = group.grouping
        assert report["clamps"], "a fixed camera tighter than its fit must be recorded"
        assert {row["camera"] for row in report["clamps"]} == {"cam-a", "cam-b"}
        for row in report["clamps"]:
            assert row["requested_m"] == pytest.approx(1e-5)
            assert row["safe_m"] > row["requested_m"]
            assert row["reason"]


def test_grouping_json_exports_random_clamp_metadata(tmp_path):
    region = RegionSpec(key="tight", radius_m=Range(1e-5, 2e-5))
    project = _tight_random_project(tmp_path, region)
    meshes = meshes_for(project)
    groups = split_by_scale(project, meshes)
    results = bundle.export_grouped_jobs(project, meshes, tmp_path / "jobs" / "render_job")
    assert len(results) == 2
    for group, result in zip(groups, results):
        document = json.loads((Path(result.directory) / "grouping.json").read_text(encoding="utf-8"))
        assert document["clamps"] == group.grouping["clamps"]
        assert len(document["clamps"]) == 2
        assert json.loads(json.dumps(document))["clamps"] == document["clamps"]
        assert document["clamps"][0]["requested_m"] == pytest.approx(1e-5)
        assert document["clamps"][0]["safe_m"] > document["clamps"][0]["requested_m"]
