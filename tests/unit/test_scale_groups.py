from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bproc_core.config import (AssetSpec, Box, FixedCameraSpec, LightSpec, OutputSpec, Range,
                               RegionSpec, TargetSpec)
from bproc_core.scale_groups import MIXED_SCALE_FACTOR, split_by_scale
from bproc_export import bundle
from tests.factories import make_mesh, make_project


def _config(tmp_path, sizes, *, anchor="large"):
    project = make_project(tmp_path, ids=tuple(2 + index for index in range(len(sizes))))
    targets = []
    for index, (name, size, offset) in enumerate(sizes):
        asset = AssetSpec(name, tmp_path / f"{name}.ply", "m")
        targets.append(TargetSpec(asset, name, 2 + index, True,
                                  Box(Range(*offset[0]), Range(*offset[1]), Range(*offset[2]))))
    meshes = {name: make_mesh(name, size=size) for name, size, _ in sizes}
    config = replace(project, targets=tuple(targets), anchor_key=anchor,
                     output=OutputSpec(root=tmp_path / "renders", dataset_name="mix",
                                       depth_scale_mm=.05),
                     light=LightSpec(radius_m=Range(.5, 1.), energy_w=Range(80, 100)),
                     regions=(RegionSpec(key="front", radius_m=Range(.5, .8)),),
                     fixed_cameras=(FixedCameraSpec("pin", "钉", .6, 10., 20.),))
    return config, meshes


def test_a_normal_size_range_is_returned_untouched(tmp_path):
    config, meshes = _config(tmp_path, [("small", .01, [(0, 0)] * 3),
                                        ("large", .05, [(0, 0)] * 3)], anchor="small")
    config = replace(config, targets=config.targets + (
        replace(config.targets[1], name="disabled", enabled=False),))
    groups = split_by_scale(config, meshes)
    assert len(groups) == 1
    assert groups[0].config is config, "a non-mixed project must not be rebuilt"
    assert groups[0].label == "10-50mm"


def test_mixed_sizes_split_and_suffix_the_dataset(tmp_path):
    config, meshes = _config(tmp_path, [("small", .002, [(0, 0)] * 3),
                                        ("large", .04, [(.1, .12), (-.01, .01), (0, 0)])],
                             anchor="large")
    groups = split_by_scale(config, meshes)
    assert [group.label for group in groups] == ["2mm", "40mm"]
    assert [group.config.output.dataset_name for group in groups] == ["mix-size-1", "mix-size-2"]
    assert [group.config.anchor_key for group in groups] == ["small", "large"]
    assert [len(group.config.targets) for group in groups] == [1, 1]
    # The large group keeps the original render centre and absolute camera/light/depth settings.
    large = groups[1].config
    assert large.targets[0].offset == config.targets[1].offset
    assert large.regions == config.regions and large.fixed_cameras == config.fixed_cameras
    assert large.light == config.light and large.output.depth_scale_mm == .05


def test_a_new_anchor_keeps_the_relative_layout(tmp_path):
    config, meshes = _config(
        tmp_path,
        [("tiny", .001, [(.01, .02), (0, 0), (0, 0)]),
         ("medium", .005, [(.03, .04), (.01, .02), (0, 0)]),
         ("huge", .2, [(0, 0)] * 3)],
        anchor="huge")
    groups = split_by_scale(config, meshes)
    assert len(groups) == 2
    small = groups[0]
    assert small.config.anchor_key == "medium"
    assert small.config.output.dataset_name == "mix-size-1"
    tiny, medium = small.config.targets
    assert medium.offset == Box(Range(0., 0.), Range(0., 0.), Range(0., 0.))
    # medium's nominal offset was (0.035, 0.015, 0); tiny shifts by that amount.
    assert tiny.offset == Box(Range(.01 - .035, .02 - .035),
                              Range(0. - .015, 0. - .015), Range(0., 0.))
    assert small.config.regions == config.regions, "cameras stay absolute; only the target set changes"


def test_every_size_class_gets_its_own_group(tmp_path):
    config, meshes = _config(tmp_path, [("a", .001, [(0, 0)] * 3),
                                        ("b", .02, [(0, 0)] * 3),
                                        ("c", .4, [(0, 0)] * 3)], anchor="c")
    groups = split_by_scale(config, meshes)
    assert [group.label for group in groups] == ["1mm", "20mm", "400mm"]
    assert [group.config.output.dataset_name for group in groups] == [
        "mix-size-1", "mix-size-2", "mix-size-3"]


def test_grouped_export_uses_sibling_job_directories(tmp_path, monkeypatch):
    config, meshes = _config(tmp_path, [("small", .002, [(0, 0)] * 3),
                                        ("large", .04, [(0, 0)] * 3)], anchor="large")
    calls = []
    def fake_export(config, destination):
        calls.append((config, Path(destination)))
        return SimpleNamespace(directory=Path(destination), command="echo " + str(destination))
    monkeypatch.setattr(bundle, "export_job", fake_export)
    results = bundle.export_grouped_jobs(config, meshes, tmp_path / "jobs" / "render_job")
    assert [Path(result.directory).name for result in results] == [
        "render_job-size-1", "render_job-size-2"]
    assert [Path(call[1]).name for call in calls] == ["render_job-size-1", "render_job-size-2"]
    assert [call[0].anchor_key for call in calls] == ["small", "large"]
    assert [call[0].output.dataset_name for call in calls] == ["mix-size-1", "mix-size-2"]


def test_a_single_group_exports_to_the_requested_directory(tmp_path, monkeypatch):
    config, meshes = _config(tmp_path, [("a", .01, [(0, 0)] * 3),
                                        ("b", .02, [(0, 0)] * 3)], anchor="a")
    calls = []
    monkeypatch.setattr(bundle, "export_job",
                        lambda config, destination: calls.append(Path(destination)) or
                        SimpleNamespace(directory=Path(destination), command=""))
    results = bundle.export_grouped_jobs(config, meshes, tmp_path / "one_job")
    assert [Path(call).name for call in calls] == ["one_job"]
    assert len(results) == 1


def test_the_split_factor_must_be_finite_and_above_one(tmp_path):
    config, meshes = _config(tmp_path, [("a", .01, [(0, 0)] * 3),
                                        ("b", .02, [(0, 0)] * 3)])
    with pytest.raises(ValueError):
        split_by_scale(config, meshes, factor=1.)
