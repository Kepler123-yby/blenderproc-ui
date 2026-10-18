"""A run records which world lit the frames, whether an HDRI was chosen or not."""

from dataclasses import replace
import json
from pathlib import Path

import numpy as np

from bproc_core.assets import load_mesh
from bproc_core.config import DEFAULT_WORLD_COLOR, HdriSpec, world_metadata
from bproc_core.scene import propose_candidate, propose_scene
from bproc_runtime.contracts import VisibilityEvidence
from bproc_runtime.output import JsonRunRecorder
from bproc_runtime.records import FrameRef, RunSummary
from tests.factories import make_project


def _candidate(config):
    meshes = {asset.key: load_mesh(asset)
              for asset in [*(t.asset for t in config.targets if t.enabled), *config.occluders]}
    return propose_candidate(config, meshes, propose_scene(config, meshes, 0), 0, 0)


def test_world_metadata_names_the_hdri_set(tmp_path):
    config = make_project(tmp_path)
    assert world_metadata(config) == {"kind": "hdri",
                                      "files": [str(path) for path in config.hdri.files]}


def test_world_metadata_falls_back_to_the_neutral_background(tmp_path):
    config = make_project(tmp_path)
    bare = replace(config, hdri=HdriSpec(files=()))
    assert world_metadata(bare) == {"kind": "neutral", "color": list(DEFAULT_WORLD_COLOR)}
    assert _candidate(bare).hdri_path is None


def test_recorder_keeps_the_world_after_frames_are_recorded(tmp_path):
    config = replace(make_project(tmp_path), hdri=HdriSpec(files=()), scene_count=1, views_per_scene=1)
    candidate = _candidate(config)
    anchor = candidate.anchor_key
    obj_id = next(t.obj_id for t in config.targets if t.asset.key == anchor)
    evidence = VisibilityEvidence(anchor_full=np.ones((2, 2), bool),
                                  visible={obj_id: np.zeros((2, 2), bool)})
    recorder = JsonRunRecorder(tmp_path / "metadata")
    recorder.begin(config)
    recorder.record_frame(FrameRef(frame_index=0, relative_rgb="rgb/0.png"), candidate, evidence)
    from bproc_runtime.metadata import read_frame_records
    manifest = json.loads(Path(tmp_path / "metadata/manifest.json").read_text(encoding="utf-8"))
    assert manifest["world"] == {"kind": "neutral", "color": list(DEFAULT_WORLD_COLOR)}
    assert manifest["frames"] == []                     # frames live in the journal until finish
    journal = read_frame_records(tmp_path / "metadata")
    assert len(journal) == 1
    assert journal[0]["candidate"]["hdri_path"] is None
    recorder.finish(RunSummary(status="completed", requested=1, written_frames=1, saved=1,
                               skipped=0, failures_by_reason={}, exit_code=0))
    status = json.loads(Path(tmp_path / "metadata/status.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["world"] == {"kind": "neutral", "color": list(DEFAULT_WORLD_COLOR)}
    assert (tmp_path / "metadata/project.yaml").is_file()
