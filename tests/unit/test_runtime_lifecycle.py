"""A reserved dataset must never keep a stale "running" marker after a partial failure."""
from dataclasses import replace
import json
from pathlib import Path
import signal

import numpy as np
import pytest

from bproc_core.config import AssetSpec, CameraSpec, OutputSpec, ProjectConfig, TargetSpec
from bproc_core.project_io import save_project
from bproc_runtime import cli
from bproc_runtime.contracts import RenderedFrame, VisibilityEvidence
from bproc_runtime.output import JsonRunRecorder, OutputConflict, reserve_dataset
import bproc_runtime.run as runner
from bproc_runtime.records import FrameRef, RunSummary
from tests.factories import make_project


def project(output_format="rgb_masks"):
    return ProjectConfig(targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "a", 2),),
                         anchor_key="a", camera=CameraSpec(width=2, height=1), scene_count=1,
                         views_per_scene=1, output=OutputSpec(format=output_format))


class Backend:
    def __init__(self, fail=None):
        self.calls, self.fail, self.closed = [], fail, False

    def prepare(self, *args):
        pass

    def apply(self, candidate):
        self.calls.append(candidate)

    def assess(self, anchor):
        return VisibilityEvidence(np.ones((1, 2), bool), {2: np.ones((1, 2), bool)})

    def render(self, evidence):
        return RenderedFrame(np.zeros((1, 2, 3), np.uint8), None, {2: evidence.anchor_full}, evidence.visible)

    def close(self):
        self.closed = True


class Writer:
    def write(self, index, candidate, frame):
        return FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,))

    def finalize(self):
        pass


class RecordingRecorder:
    def __init__(self, begin_error=None, finish_error=None):
        self.begin_error, self.finish_error = begin_error, finish_error
        self.began = self.finished = False
        self.summary = None
        self.warnings = []

    def begin(self, config):
        if self.begin_error:
            raise self.begin_error
        self.began = True

    def record_frame(self, ref, candidate, evidence):
        pass

    def warn(self, code, message):
        self.warnings.append(code)

    def record_rejection(self, scene_index, view_index, attempt_index, reason, *, phase="view", details=None):
        pass

    def finish(self, summary):
        if self.finish_error:
            raise self.finish_error
        self.summary = summary
        self.finished = True


def test_finish_before_begin_still_writes_a_status(tmp_path):
    recorder = JsonRunRecorder(tmp_path / "run_metadata")
    recorder.finish(RunSummary("failed", 3, 0, 0, 3, {"fatal:RuntimeError": 1}, 1))
    payload = json.loads((tmp_path / "run_metadata" / "status.json").read_text())
    assert payload["status"] == "failed"
    assert "started_at" not in payload
    assert "world" not in payload


@pytest.mark.parametrize("broken", ["objects.json", "manifest.json"])
def test_a_half_written_begin_is_finalized(tmp_path, monkeypatch, broken):
    from bproc_runtime import output as output_module
    real = output_module.atomic_json
    calls = []

    def flaky(path, data):
        calls.append(Path(path).name)
        if Path(path).name == broken:
            raise OSError("磁盘已满")
        return real(path, data)

    monkeypatch.setattr(output_module, "atomic_json", flaky)
    dataset = reserve_dataset(tmp_path, "half")
    recorder = JsonRunRecorder(dataset / "run_metadata")
    summary = runner.run_project(project(), {}, Backend(), Writer(), recorder)
    assert summary.status == "failed" and summary.exit_code == 1
    payload = json.loads((dataset / "run_metadata" / "status.json").read_text())
    assert payload["status"] == "failed"
    assert calls[0] == "status.json"          # the running marker was already written


def test_a_failure_while_writing_the_project_snapshot_is_finalized(tmp_path, monkeypatch):
    monkeypatch.setattr("bproc_core.project_io.save_project",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("只读文件系统")))
    dataset = reserve_dataset(tmp_path, "snapshot")
    recorder = JsonRunRecorder(dataset / "run_metadata")
    summary = runner.run_project(project(), {}, Backend(), Writer(), recorder)
    assert summary.status == "failed" and recorder.finished is True
    payload = json.loads((dataset / "run_metadata" / "status.json").read_text())
    assert payload["status"] == "failed"


def test_run_project_finalizes_even_when_begin_raises(tmp_path):
    recorder = RecordingRecorder(begin_error=RuntimeError("begin 崩溃"))
    summary = runner.run_project(project(), {}, Backend(), Writer(), recorder)
    assert recorder.summary is not None and recorder.summary.status == "failed"
    assert summary.status == "failed"


def test_finish_failure_keeps_the_original_diagnosis(tmp_path, monkeypatch, capsys):
    recorder = RecordingRecorder(finish_error=OSError("只读文件系统"))
    summary = runner.run_project(project(), {}, Backend(), Writer(), recorder)
    assert summary.status == "failed"
    assert "status:OSError" in summary.failures_by_reason
    assert "写入最终状态失败" in capsys.readouterr().out
    # The handlers the process had before the run must be back in place.
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


class BoomBackend:
    def __init__(self, error):
        self.error = error

    def __getattr__(self, name):
        raise self.error


def run_cli(tmp_path, monkeypatch, backend_factory):
    config = make_project(tmp_path / "assets")
    config = replace(config, output=OutputSpec(root=tmp_path / "out", dataset_name="run"))
    path = tmp_path / "project.yaml"
    save_project(config, path)
    monkeypatch.setattr(cli, "load_mesh", lambda asset: object())
    monkeypatch.setattr("bproc_runtime.blender_backend.BlenderBackend", backend_factory)
    return cli.main(["--config", str(path)])


def status_of(tmp_path):
    path = tmp_path / "out" / "run" / "run_metadata" / "status.json"
    return json.loads(path.read_text())["status"]


@pytest.mark.parametrize("error,expected_status,expected_code", [
    (KeyboardInterrupt(), "interrupted", 130),
    (RuntimeError("GPU 不可用"), "failed", 1),
])
def test_cli_finalizes_when_the_backend_cannot_be_built(tmp_path, monkeypatch, error, expected_status,
                                                        expected_code):
    def factory():
        raise error

    assert run_cli(tmp_path, monkeypatch, factory) == expected_code
    assert status_of(tmp_path) == expected_status


def test_cli_finalizes_when_the_writer_cannot_be_built(tmp_path, monkeypatch):
    monkeypatch.setattr("bproc_runtime.blender_backend.BlenderBackend", Backend)
    monkeypatch.setattr("bproc_runtime.rgb_masks.RgbMasksWriter",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("没有写权限")))
    assert run_cli(tmp_path, monkeypatch, Backend) == 1
    payload = json.loads((tmp_path / "out" / "run" / "run_metadata" / "status.json").read_text())
    assert payload["status"] == "failed"
    assert "没有写权限" in payload["detail"]


def test_cli_does_not_rewrite_the_status_after_run_project(tmp_path, monkeypatch):
    from bproc_runtime import output as output_module
    writes = []
    real = output_module.write_terminal_status

    def counted(*args, **kwargs):
        writes.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(output_module, "write_terminal_status", counted)
    monkeypatch.setattr("bproc_runtime.blender_backend.BlenderBackend", Backend)
    summary = RunSummary("completed", 1, 1, 1, 0, {}, 0)
    monkeypatch.setattr("bproc_runtime.run.run_project", lambda *args, **kwargs: summary)
    config = make_project(tmp_path / "assets")
    config = replace(config, output=OutputSpec(root=tmp_path / "out", dataset_name="run"))
    path = tmp_path / "project.yaml"
    save_project(config, path)
    monkeypatch.setattr(cli, "load_mesh", lambda asset: object())
    assert cli.main(["--config", str(path)]) == 0
    assert writes == []


def test_a_conflicting_dataset_is_left_untouched(tmp_path):
    existing = tmp_path / "keep"
    existing.mkdir()
    (existing / "run_metadata").mkdir()
    (existing / "run_metadata" / "status.json").write_text('{"status": "completed"}')
    with pytest.raises(OutputConflict):
        reserve_dataset(tmp_path, "keep")
    assert json.loads((existing / "run_metadata" / "status.json").read_text())["status"] == "completed"


def test_final_manifest_failure_matches_disk_status(tmp_path, monkeypatch):
    """A completed status next to an unfinished manifest would make later readers trust a lie."""
    from bproc_runtime import output as output_module
    real = output_module.atomic_json

    def flaky(path, data):
        if Path(path).name == "manifest.json" and data.get("final") is True:
            raise OSError("磁盘已满")
        return real(path, data)

    monkeypatch.setattr(output_module, "atomic_json", flaky)
    dataset = reserve_dataset(tmp_path, "manifest-failure")
    recorder = JsonRunRecorder(dataset / "run_metadata")
    from types import SimpleNamespace
    from bproc_core.scene import SceneState
    state = SceneState(0, (), None, 1., 0., None, True)
    monkeypatch.setattr(runner, "sample_scene", lambda config, meshes, index, *, on_rejection=None:
                        SimpleNamespace(scene=state, placements=(), material=None, scene_index=index,
                                        layout_attempt_index=0))
    from bproc_core.camera import CameraPose
    from bproc_core.scene import LightState, SceneCandidate

    def propose(config, meshes, scene, view, attempt):
        pose = CameraPose(np.eye(4), np.array([0., 0., .5]), .5, 0., 0., .05, 100.)
        return SceneCandidate(scene.scene_index, view, attempt, "region-1", "a", pose, (), None, 1., 0.,
                              LightState(np.array([0., 0., 1.]), 100., (1., 1., 1.)), True)

    monkeypatch.setattr(runner, "propose_candidate", propose)
    summary = runner.run_project(project(), {}, Backend(), Writer(), recorder)
    payload = json.loads((dataset / "run_metadata" / "status.json").read_text())
    assert summary.status == payload["status"] == "failed"
    assert summary.exit_code == payload["exit_code"] == 1
    assert payload["saved"] == 1                      # committed frames stay counted
    assert "manifest:OSError" in payload["failures_by_reason"]
    assert summary.failures_by_reason == payload["failures_by_reason"]
