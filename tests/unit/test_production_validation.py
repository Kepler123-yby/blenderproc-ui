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
    def __init__(self, *, fail=None, partial=False, send_signal=None, datasets=2,
                 layout_rejections=0, view_rejections=1, grouped_jobs=1,
                 missing_status=False, missing_counters_for_signal=False):
        self.fail = fail
        self.partial = partial
        self.send_signal = send_signal
        self.datasets = datasets
        self.layout_rejections = layout_rejections
        self.view_rejections = view_rejections
        self.grouped_jobs = grouped_jobs
        self.missing_status = missing_status
        self.missing_counters_for_signal = missing_counters_for_signal
        self.commands = []
        self.calls = []

    def _job(self, config, directory, *, missing_status=False, dataset_name=None):
        directory = Path(directory)
        directory.mkdir(parents=True)
        save_project(config, directory / "scene.yaml")
        dataset = Path(config.output.root) / (dataset_name or config.output.dataset_name)
        dataset.mkdir(parents=True)
        (dataset / "run_metadata").mkdir(exist_ok=True)
        if not missing_status:
            saved = 1 if self.partial else self.datasets
            (dataset / "run_metadata" / "status.json").write_text(json.dumps({
                "status": "partial" if self.partial else "completed", "requested": self.datasets,
                "written_frames": self.datasets, "saved": saved,
                "skipped": self.datasets - saved, "failures_by_reason": {}}), encoding="utf-8")
        frames = [{"frame_index": index} for index in range(self.datasets)]
        (dataset / "run_metadata" / "frames.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in frames), encoding="utf-8")
        rejections = [json.dumps({"reason": "occlusion", "phase": "view"}) + "\n"] * self.view_rejections
        rejections += [json.dumps({"reason": "collision", "phase": "layout",
                                   "view_index": -1}) + "\n"] * self.layout_rejections
        (dataset / "run_metadata" / "rejections.jsonl").write_text(
            "".join(rejections), encoding="utf-8")
        return vpr.JobPlan(directory, dataset)

    def export(self, config, job_dir, *, grouped=False):
        job_dir = Path(job_dir)
        jobs = []
        count = self.grouped_jobs if grouped else 1
        for index in range(count):
            directory = job_dir if count == 1 else job_dir.with_name(f"{job_dir.name}-size-{index + 1}")
            missing = self.missing_status or (self.missing_counters_for_signal and self.send_signal)
            dataset_name = (None if count == 1
                            else f"{config.output.dataset_name}-size-{index + 1}")
            jobs.append(self._job(config, directory, missing_status=missing,
                                  dataset_name=dataset_name))
        self.calls.append(("export", grouped))
        return jobs

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
    counters = summary["cases"][0]["jobs"][0]["counters"]
    assert counters["readable"] is True and counters["status"] == "partial"
    assert counters["requested"] == 2 and counters["saved"] == 1
    assert counters["skipped"] == 1 and counters["frame_yield"] == pytest.approx(.5)


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


# -- R6: counters are read after the current child finalizes, even on signals ----------------
def _current_job_counters(destination):
    summary = json.loads((destination / "summary.json").read_text())
    return summary, summary["cases"][0]["jobs"][0]["counters"]


@pytest.mark.parametrize("signum,expected", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_signals_keep_the_current_job_counters_and_leave_later_cases_not_started(
        tmp_path, signum, expected):
    path, _project, _ = build_manifest(tmp_path, second_hdri=True)
    destination = tmp_path / "evidence"
    executor = FakeExecutor(send_signal=signum)
    assert vpr.run(path, destination, executor=executor) == expected
    summary, counters = _current_job_counters(destination)
    assert summary["status"] == "interrupted" and summary["exit_code"] == expected
    assert summary["cases"][0]["status"] == "interrupted"
    assert summary["cases"][1]["status"] == "not_started"
    assert counters["readable"] is True
    assert counters["requested"] == counters["written"] == counters["saved"] == 2
    assert counters["skipped"] == 0 and counters["exit_code"] == 0
    assert counters["journal_frames"] == 2
    recorded = json.loads((Path(summary["cases"][0]["jobs"][0]["dataset"])
                           / "run_metadata" / "status.json").read_text())
    for name in ("requested", "saved", "skipped"):
        assert counters[name] == recorded[name]
    assert counters["exit_code"] == recorded.get("exit_code", 0)
    assert counters["written"] == recorded["written_frames"]


def test_a_render_failure_still_records_the_current_job_counters(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    assert vpr.run(path, destination, executor=FakeExecutor(fail="render")) != 0
    summary, counters = _current_job_counters(destination)
    assert summary["cases"][0]["status"] == "failed"
    assert counters["readable"] is True
    assert counters["requested"] == 2 and counters["saved"] == 2
    assert summary["cases"][0]["error"]


def test_unreadable_current_counters_record_a_reason_instead_of_zeroes(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    executor = FakeExecutor(send_signal=signal.SIGINT, missing_counters_for_signal=True)
    assert vpr.run(path, destination, executor=executor) == 130
    summary, counters = _current_job_counters(destination)
    assert summary["cases"][0]["status"] == "interrupted"
    assert counters["readable"] is False
    assert counters["error"]
    assert "requested" not in counters or counters["requested"] is None


def test_layout_and_view_rejections_are_counted_separately(tmp_path):
    path, _project, _ = build_manifest(tmp_path)
    destination = tmp_path / "evidence"
    executor = FakeExecutor(layout_rejections=2, view_rejections=3, datasets=5)
    assert vpr.run(path, destination, executor=executor) == 0
    summary, counters = _current_job_counters(destination)
    assert counters["view_rejections"] == 3
    assert counters["layout_rejections"] == 2
    assert counters["proposal_attempts"] == 8          # 5 frames + 3 view rejections
    assert counters["proposal_accept_rate"] == pytest.approx(5 / 8)
    assert counters["frame_yield"] == pytest.approx(1.0)
    assert counters["rejections_by_phase"]["view"] == {"occlusion": 3}
    assert counters["rejections_by_phase"]["layout"] == {"collision": 2}
    assert counters["rejections_by_reason"] == {"occlusion": 3, "collision": 2}
    totals = summary["totals"]
    assert totals["view_rejections"] == 3 and totals["layout_rejections"] == 2
    assert totals["proposal_attempts"] == 8
    assert totals["proposal_accept_rate"] == pytest.approx(5 / 8)


def test_totals_aggregate_multiple_cases_hdris_and_grouped_jobs(tmp_path):
    path, _project, _ = build_manifest(tmp_path, second_hdri=True)
    destination = tmp_path / "evidence"
    executor = FakeExecutor(datasets=2, grouped_jobs=2, layout_rejections=1, view_rejections=1)
    # Grouped export: one case per HDRI, two jobs each, each job has its own counters.
    manifest = json.loads(path.read_text())
    for case in manifest["cases"]:
        case["grouped"] = True
    path.write_text(json.dumps(manifest))
    assert vpr.run(path, destination, executor=executor) == 0
    summary = json.loads((destination / "summary.json").read_text())
    assert len(summary["cases"]) == 2
    assert all(len(entry["jobs"]) == 2 for entry in summary["cases"])
    totals = summary["totals"]
    assert totals["jobs"] == 4
    assert totals["requested"] == totals["written"] == totals["saved"] == 8
    assert totals["view_rejections"] == 4 and totals["layout_rejections"] == 4
    assert totals["proposal_attempts"] == 12
    assert totals["proposal_accept_rate"] == pytest.approx(8 / 12)
    assert totals["rejections_by_phase"]["layout"] == {"collision": 4}
