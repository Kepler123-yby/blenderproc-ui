"""Frame metadata must grow linearly, and the reader must distrust anything inconsistent."""
import json
from pathlib import Path
import warnings

import pytest

from bproc_core.config import AssetSpec, CameraSpec, OutputSpec, ProjectConfig, TargetSpec
from bproc_runtime import metadata
from bproc_runtime.metadata import MetadataError, manifest_payload, read_frame_records
from bproc_runtime.output import JsonRunRecorder
from bproc_runtime.records import FrameRef, RunSummary


def project(count=3, output_format="rgb_masks"):
    return ProjectConfig(targets=(TargetSpec(AssetSpec("a", Path("a.ply")), "a", 2),),
                         anchor_key="a", camera=CameraSpec(width=2, height=1), scene_count=count,
                         views_per_scene=1, output=OutputSpec(format=output_format))


class Evidence:
    def __init__(self):
        import numpy as np
        self.anchor_full = np.ones((1, 2), bool)
        self.visible = {2: np.ones((1, 2), bool)}


def row_for(index):
    return {"frame_index": index, "rgb_path": f"rgb/{index:06d}.png", "gt_obj_ids": [2],
            "candidate": {"scene_index": 0, "view_index": 0, "attempt_index": 0},
            "occlusion_ratio": .25, "seed": 1}


def recorder(tmp_path, count=1):
    instance = JsonRunRecorder(tmp_path / "run_metadata")
    instance.begin(project(count))
    return instance


def test_each_frame_appends_exactly_one_journal_line(tmp_path):
    instance = recorder(tmp_path)
    for index in range(3):
        instance.record_frame(FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    lines = (tmp_path / "run_metadata" / "frames.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["schema_version"] == 2
    manifest = json.loads((tmp_path / "run_metadata" / "manifest.json").read_text())
    assert manifest["frames"] == [] and manifest["schema_version"] == 2   # no per-frame snapshots


def test_finish_writes_the_complete_manifest_once(tmp_path):
    instance = recorder(tmp_path, count=2)
    for index in range(2):
        instance.record_frame(FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    instance.finish(RunSummary("completed", 2, 2, 2, 0, {}, 0))
    manifest = json.loads((tmp_path / "run_metadata" / "manifest.json").read_text())
    assert [row["frame_index"] for row in manifest["frames"]] == [0, 1]
    assert len(read_frame_records(tmp_path / "run_metadata")) == 2


def test_cumulative_journal_bytes_grow_linearly(tmp_path, monkeypatch):
    """A 4x longer run may not write ~16x the bytes (the old per-frame snapshot did)."""

    def measure(count):
        folder = tmp_path / f"run-{count}" / "run_metadata"
        instance = recorder(tmp_path / f"run-{count}", count=count)
        written = {"bytes": 0}
        real_open = Path.open

        class Counting:
            def __init__(self, stream):
                self.stream = stream

            def write(self, text):
                written["bytes"] += len(text.encode())
                return self.stream.write(text)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

        def counting_open(self, *args, **kwargs):
            stream = real_open(self, *args, **kwargs)
            return Counting(stream) if self.name == "frames.jsonl" else stream

        monkeypatch.setattr(Path, "open", counting_open)
        try:
            for index in range(count):
                instance.record_frame(FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,)),
                                      _candidate(), Evidence())
        finally:
            monkeypatch.setattr(Path, "open", real_open)
        instance._frames = []          # keep memory out of the comparison
        return written["bytes"]

    small, medium, large = measure(100), measure(200), measure(400)
    assert large < 4.6 * small, (small, large)
    assert small < medium < large
    assert large / medium < 2.6 and medium / small < 2.6


def test_reader_accepts_a_complete_journal(tmp_path):
    instance = recorder(tmp_path, count=2)
    for index in range(2):
        instance.record_frame(FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    assert [row["frame_index"] for row in read_frame_records(tmp_path / "run_metadata")] == [0, 1]


def test_reader_rejects_a_truncated_tail_unless_recovering(tmp_path):
    instance = recorder(tmp_path, count=1)
    instance.record_frame(FrameRef(0, "rgb/000000.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    journal = tmp_path / "run_metadata" / "frames.jsonl"
    journal.write_text(journal.read_text() + '{"schema_version": 2, "frame_index": 1, "rgb_')
    with pytest.raises(MetadataError):
        read_frame_records(tmp_path / "run_metadata")
    with pytest.warns(RuntimeWarning):
        assert len(read_frame_records(tmp_path / "run_metadata", allow_incomplete_tail=True)) == 1


def test_reader_rejects_a_broken_middle_line(tmp_path):
    instance = recorder(tmp_path, count=2)
    journal = tmp_path / "run_metadata" / "frames.jsonl"
    lines = [metadata.encode_row(row_for(0)), "{oops}"]
    journal.write_text("\n".join(lines) + "\n")
    with pytest.raises(MetadataError, match="第 2 行"):
        read_frame_records(tmp_path / "run_metadata")


def test_reader_rejects_duplicate_and_unknown_versions(tmp_path):
    folder = tmp_path / "meta"
    folder.mkdir()
    (folder / "frames.jsonl").write_text(metadata.encode_row(row_for(0)) + "\n" + metadata.encode_row(row_for(0)) + "\n")
    with pytest.raises(MetadataError, match="帧号不连续"):
        read_frame_records(folder)
    (folder / "frames.jsonl").write_text(json.dumps({"schema_version": 3, "frame_index": 0}) + "\n")
    with pytest.raises(MetadataError, match="版本未知"):
        read_frame_records(folder)


def test_reader_reports_a_manifest_journal_mismatch(tmp_path):
    instance = recorder(tmp_path, count=2)
    instance.record_frame(FrameRef(0, "rgb/000000.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    manifest_path = tmp_path / "run_metadata" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload(1, {}, [row_for(0), row_for(1)], final=True)))
    with pytest.raises(MetadataError, match="帧数不一致"):
        read_frame_records(tmp_path / "run_metadata")


def test_a_legacy_manifest_without_a_journal_is_still_readable(tmp_path):
    folder = tmp_path / "legacy"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"seed": 1, "world": {}, "frames": [row_for(0)]}))
    assert read_frame_records(folder)[0]["frame_index"] == 0


def _candidate():
    from bproc_core.scene import LightState, SceneCandidate
    from bproc_core.camera import CameraPose
    import numpy as np

    pose = CameraPose(np.eye(4), np.array([0., 0., 1.]), 1., 0., 0., .05, 100.)
    return SceneCandidate(0, 0, 0, "region-1", "a", pose, (), None, 1., 0.,
                          LightState(np.array([0., 0., 1.]), 100., (1., 1., 1.)), False)


def _rows_with_journal(tmp_path, count=2):
    instance = recorder(tmp_path, count=count)
    for index in range(count):
        instance.record_frame(FrameRef(index, f"rgb/{index:06d}.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    instance.finish(RunSummary("completed", count, count, count, 0, {}, 0))
    return tmp_path / "run_metadata" / "manifest.json"


def test_reader_rejects_same_index_different_content(tmp_path):
    manifest_path = _rows_with_journal(tmp_path)
    original = json.loads(manifest_path.read_text())
    for mutate in (lambda frame: frame.update(relative_rgb="rgb/wrong.png"),
                   lambda frame: frame["candidate"].update(scene_index=999),
                   lambda frame: frame["candidate"]["camera"].update(radius_m=9.0),
                   lambda frame: frame.update(occlusion_ratio=0.99)):
        manifest = json.loads(json.dumps(original))
        mutate(manifest["frames"][0])
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(MetadataError, match="第 0 帧内容不一致"):
            read_frame_records(manifest_path.parent)


def test_reader_rejects_unknown_manifest_version_with_journal(tmp_path):
    manifest_path = _rows_with_journal(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 999
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(MetadataError, match="版本不一致"):
        read_frame_records(manifest_path.parent)
    manifest["frames"] = "not-a-list"
    manifest["schema_version"] = 2
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(MetadataError):
        read_frame_records(manifest_path.parent)


def test_a_running_manifest_placeholder_does_not_trigger_the_journal_check(tmp_path):
    instance = recorder(tmp_path, count=2)
    instance.record_frame(FrameRef(0, "rgb/000000.png", gt_obj_ids=(2,)), _candidate(), Evidence())
    assert len(read_frame_records(tmp_path / "run_metadata")) == 1   # empty placeholder is expected mid-run
