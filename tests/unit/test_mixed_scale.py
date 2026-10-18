"""Mixed target scales: what one dataset can and cannot describe."""

from dataclasses import replace
import json

import numpy as np
import pytest

from bproc_core.assets import MeshData
from bproc_runtime.output import JsonRunRecorder
from bproc_runtime.run import MIXED_SCALE_FACTOR, warn_mixed_scale
from tests.factories import make_project


class Recorder:
    def __init__(self):
        self.warnings = []

    def warn(self, code, message):
        self.warnings.append((code, message))


def cube(extent):
    corners = np.array([[x, y, z] for x in (0, extent) for y in (0, extent) for z in (0, extent)], float)
    return MeshData("k", corners, np.zeros((0, 3), int), None, corners)


def two_targets(tmp_path, small, large):
    config = make_project(tmp_path)
    first, second = config.targets[0], config.targets[1]
    meshes = {first.asset.key: cube(small), second.asset.key: cube(large)}
    return config, meshes


def test_comparable_sizes_need_no_warning(tmp_path):
    config, meshes = two_targets(tmp_path, .05, .07)
    recorder = Recorder()
    assert warn_mixed_scale(config, meshes, recorder) < MIXED_SCALE_FACTOR
    assert recorder.warnings == []


def test_a_single_target_is_never_mixed(tmp_path):
    config, meshes = two_targets(tmp_path, .05, .05)
    recorder = Recorder()
    assert warn_mixed_scale(replace(config, targets=config.targets[:1]), meshes, recorder) == 1.0
    assert recorder.warnings == []


def test_extreme_ratio_reports_the_occlusion_window(tmp_path):
    config, meshes = two_targets(tmp_path, .001, .02)
    config = replace(config, occlusion=replace(config.occlusion, ratio=replace(
        config.occlusion.ratio, minimum=.05, maximum=.5)))
    recorder = Recorder()
    assert warn_mixed_scale(config, meshes, recorder) == pytest.approx(20.0)
    codes = [code for code, _ in recorder.warnings]
    assert codes == ["mixed_scale_occlusion", "mixed_scale_tolerance"]
    assert "20×" in recorder.warnings[0][1]


def test_unoccluded_runs_only_report_the_tolerance_limit(tmp_path):
    config, meshes = two_targets(tmp_path, .001, .02)
    config = replace(config, occlusion=replace(config.occlusion, ratio=replace(
        config.occlusion.ratio, minimum=0.0, maximum=1.0)))
    recorder = Recorder()
    warn_mixed_scale(config, meshes, recorder)
    assert [code for code, _ in recorder.warnings] == ["mixed_scale_tolerance"]


def test_recorder_persists_warnings_into_the_final_status(tmp_path):
    recorder = JsonRunRecorder(tmp_path / "dataset")
    config = make_project(tmp_path)
    recorder.begin(config)
    recorder.warn("mixed_scale_tolerance", "尺寸相差 20×")
    recorder.warn("mixed_scale_tolerance", "尺寸相差 20×")   # same code, recorded once
    running = json.loads((recorder.root / "status.json").read_text())
    assert [row["code"] for row in running["warnings"]] == ["mixed_scale_tolerance"]
    from bproc_runtime.records import RunSummary
    recorder.finish(RunSummary(status="completed", requested=1, written_frames=1, saved=1,
                               skipped=0, exit_code=0, failures_by_reason={}))
    final = json.loads((recorder.root / "status.json").read_text())
    assert final["warnings"] == [{"code": "mixed_scale_tolerance", "message": "尺寸相差 20×"}]
