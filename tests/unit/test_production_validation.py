"""Production acceptance orchestration: manifest safety, orchestration, signals and verify."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal

import pytest

from bproc_core.project_io import load_project, save_project
from tests.factories import make_mesh, make_project, write_mesh
from tools import validate_production_resources as vpr


class FakeExecutor:
    def __init__(self, *, fail=None, partial=False, send_signal=None, datasets=2):
        self.fail = fail
        self.partial = partial
        self.send_signal = send_signal
        self.datasets = datasets
        self.commands = []
        self.calls = []

    def export(self, config, job_dir, *, grouped=False):
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True)
        save_project(config, job_dir / "scene.yaml")
        dataset = Path(config.output.root) / config.output.dataset_name
        dataset.mkdir(parents=True)
        (dataset / "run_metadata").mkdir(exist_ok=True)
        saved = 1 if self.partial else self.datasets
        (dataset / "run_metadata" / "status.json").write_text(json.dumps({
            "status": "partial" if self.partial else "completed", "requested": self.datasets,
            "written_frames": self.datasets, "saved": saved,
            "skipped": self.datasets - saved, "failures_by_reason": {}}), encoding="utf-8")
        frames = [{"frame_index": index} for index in range(self.datasets)]
        (dataset / "run_metadata" / "frames.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in frames), encoding="utf-8")
        (dataset / "run_metadata" / "rejections.jsonl").write_text(
            json.dumps({"reason": "occlusion", "phase": "view"}) + "\n", encoding="utf-8")
        self.calls.append(("export", grouped))
        return [vpr.JobPlan(job_dir, dataset)]

    def check_only(self, job, log):
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("check-only\n", encoding="utf-8")
        self.calls.append(("check_only", 0))
        return 3 if self.fail == "check_only" else 0

    def render(self, job, log):
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("render\n", encoding="utf-8")
        self.calls.append(("render", 0))
        if self.send_signal is not None:
            signal.raise_signal(self.send_signal)
        return 4 if self.fail == "render" else 0

    def checker(self, job, log):
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("checker\n", encoding="utf-8")
        self.calls.append(("checker", 0))
        return 5 if self.fail == "checker" else 0

    def audit(self, job, log, report):
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("audit\n", encoding="utf-8")
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text("{}", encoding="utf-8")
        self.calls.append(("audit", 0))
        return 0


def build_manifest(tmp_path, *, classification="fixture", second_hdri=False, missing_resource=False):
    project = make_project(tmp_path / "assets")
    hdri = project.hdri.files[0]
    extra = hdri.with_name("haven2.hdr")
    extra.write_bytes(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 2 +X 2\n" + bytes([128, 128, 128, 129]) * 4)
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    resources = [{"path": str(target.asset.path), "role": "model",
                  "sha256": vpr._sha256(target.asset.path),
                  "source": "unit fixture", "classification": classification}
                 for target in project.targets if target.enabled]
    resources.append({"path": str(hdri), "role": "hdri", "sha256": vpr._sha256(hdri),
                      "source": "unit fixture", "classification": classification})
    manifest = {
        "schema_version": 1,
        "resources": resources,
        "cases": [{"id": "case-a", "project": str(tmp_path / "assets" / "project.yaml"),
                   "hdris": [str(hdri)], "scenes": 2, "seed": 42, "grouped": False, "reframe": False}],
    }
    if second_hdri:
        manifest["resources"].append({"path": str(extra), "role": "hdri",
                                      "sha256": vpr._sha256(extra), "source": "unit fixture",
                                      "classification": classification})
        manifest["cases"][0]["hdris"].append(str(extra))
    # make_project does not write a YAML; save one so preflight can load the project.
    save_project(project, tmp_path / "assets" / "project.yaml")
    path = manifest_dir / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, project, extra


def test_manifest_rejects_bad_hash_missing_file_duplicate_case_and_counts(tmp_path):
    path, project, _ = build_manifest(tmp_path)
    payload = json.loads(path.read_text())
    payload["resources"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(vpr.ManifestError, match="哈希不符"):
        vpr.preflight(vpr.load_manifest(path))

    payload["resources"][0]["sha256"] = vpr._sha256(project.targets[0].asset.path)
    payload["cases"][0]["scenes"] = 0
    path.write_text(json.dumps(payload))
    with pytest.raises(vpr.ManifestError, match="scenes"):
        vpr.load_manifest(path)

    payload["cases"][0]["scenes"] = 1
    payload["cases"].append(dict(payload["cases"][0]))
    path.write_text(json.dumps(payload))
    with pytest.raises(vpr.ManifestError, match="唯一"):
        vpr.load_manifest(path)


def test_manifest_rejects_a_project_resource_that_is_not_covered(tmp_path):
    path, project, _ = build_manifest(tmp_path)
    payload = json.loads(path.read_text())
    payload["resources"] = [entry for entry in payload["resources"] if entry["role"] == "hdri"]
    path.write_text(json.dumps(payload))
    with pytest.raises(vpr.ManifestError, match="没有写进 manifest"):
        vpr.preflight(vpr.load_manifest(path))


def test_destination_refuses_existing_symlink_and_dangling_link(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    assert vpr.run(path, existing, executor=FakeExecutor()) == 2
    link = tmp_path / "link"
    link.symlink_to(existing)
    assert vpr.run(path, link, executor=FakeExecutor()) == 2
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")
    assert vpr.run(path, dangling, executor=FakeExecutor()) == 2


def test_a_fixture_run_is_never_production_verified(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    executor = FakeExecutor()
    assert vpr.run(path, destination, executor=executor) == 0
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["status"] == "completed"
    assert summary["classification"] == "fixture"
    assert summary["production_verified"] is False
    entry = summary["cases"][0]
    assert entry["status"] == "completed"
    counters = entry["jobs"][0]["counters"]
    assert counters["requested"] == counters["saved"] == 2 and counters["skipped"] == 0
    assert counters["proposal_accept_rate"] == pytest.approx(2 / 3)
    assert counters["frame_yield"] == 1.0
    assert (destination / "commands.json").is_file()
    assert vpr.verify(destination) == 1


def test_two_hdris_become_two_deterministic_cases(tmp_path):
    path, _project, extra = build_manifest(tmp_path, second_hdri=True)
    destination = tmp_path / "evidence"
    executor = FakeExecutor()
    assert vpr.run(path, destination, executor=executor) == 0
    summary = json.loads((destination / "summary.json").read_text())
    assert len(summary["cases"]) == 2
    assert {entry["hdri"] for entry in summary["cases"]} == {str(extra), str(_project.hdri.files[0])}
    assert any(call == ("export", False) for call in executor.calls)


def test_num_scenes_overrides_every_case(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    seen = []

    class Recorder(FakeExecutor):
        def export(self, config, job_dir, *, grouped=False):
            seen.append(config.scene_count)
            return super().export(config, job_dir, grouped=grouped)

    assert vpr.run(path, tmp_path / "evidence", num_scenes=1, executor=Recorder()) == 0
    assert seen == [1]


@pytest.mark.parametrize("failure,expected_status", [
    ("check_only", "failed"), ("render", "failed"), ("checker", "failed")])
def test_every_step_failure_is_nonzero_and_keeps_evidence(tmp_path, failure, expected_status):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    executor = FakeExecutor(fail=failure)
    assert vpr.run(path, destination, executor=executor) != 0
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["status"] == expected_status
    assert summary["cases"][0]["status"] == "failed"
    assert summary["cases"][0]["error"]
    assert (destination / "logs").exists()


def test_a_partial_dataset_fails(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    assert vpr.run(path, tmp_path / "evidence", executor=FakeExecutor(partial=True)) != 0
    summary = json.loads((tmp_path / "evidence" / "summary.json").read_text())
    assert summary["status"] == "failed"
    assert "partial" in summary["cases"][0]["error"]


@pytest.mark.parametrize("signum,expected", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_signals_interrupt_after_the_current_task_and_mark_the_rest_not_started(tmp_path, signum, expected):
    path, _project, _ = build_manifest(tmp_path, second_hdri=True)
    destination = tmp_path / "evidence"
    executor = FakeExecutor(send_signal=signum)
    assert vpr.run(path, destination, executor=executor) == expected
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["status"] == "interrupted"
    assert summary["cases"][0]["status"] == "interrupted"
    assert summary["cases"][1]["status"] == "not_started"


def test_rerunning_needs_a_fresh_destination(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    assert vpr.run(path, destination, executor=FakeExecutor()) == 0
    assert vpr.run(path, destination, executor=FakeExecutor()) == 2


def test_verify_runs_the_real_checker_on_incomplete_data(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    assert vpr.run(path, destination, executor=FakeExecutor()) == 0
    summary = json.loads((destination / "summary.json").read_text())
    # Forge a production label: verification must still fail because the real checker finds no data.
    summary["classification"] = "production"
    summary["production_verified"] = True
    summary["device"] = "NVIDIA unit test"
    (destination / "summary.json").write_text(json.dumps(summary))
    assert vpr.verify(destination) == 1
