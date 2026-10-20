"""Fast contract tests for the evidence CLI; no local assets or GPU required."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tools import verify_occluder_import_rendering as tool


@pytest.mark.parametrize("phase", [None, "before", "after"])
def test_run_defaults_to_after_and_preserves_explicit_phases(tmp_path, monkeypatch, phase):
    calls = []
    monkeypatch.setattr(tool, "run", lambda destination, selected: calls.append((destination, selected)) or 0)
    args = ["run", "--destination", str(tmp_path / "new-run")]
    if phase is not None:
        args += ["--phase", phase]
    assert tool.main(args) == 0
    assert calls == [(tmp_path / "new-run", phase or "after")]


@pytest.fixture
def recorded_run(tmp_path, monkeypatch):
    core = tmp_path / "core.ply"
    core.write_bytes(b"mocked core for the evidence contract")
    source = tmp_path / "background.hdr"
    source.write_bytes(b"unchanged source input")
    monkeypatch.setattr(tool, "CORE", core)
    monkeypatch.setattr(tool, "CORE_SHA256", tool.sha256(core))
    monkeypatch.setattr(tool, "_inputs", lambda: [core, source])
    monkeypatch.setattr(tool, "_source_digest", lambda: "current-source")
    mesh = SimpleNamespace(vertices_m=np.zeros((1035, 3)), faces=np.zeros((1837, 3), dtype=int),
                           vertex_rgba=np.full((1035, 4), 255, dtype=np.uint8))
    monkeypatch.setattr("bproc_core.assets.load_mesh", lambda asset: mesh)
    root = tmp_path / "evidence"
    tool.write_json(root / "provenance.json", {
        "core": {"sha256": tool.sha256(core)},
        "input_sha256_before": {str(p): tool.sha256(p) for p in (core, source)},
        "source_digest": "current-source", "tool_sha256": tool.sha256(Path(tool.__file__)),
    })
    tool.write_json(root / "summary.json", {
        "cases": [{"name": "recorded"}],
        "commands": [{"step": "checker", "exit_code": 0}],
    })
    tool.write_json(root / "logs/recorded-report.json", {
        "status": {"status": "completed", "skipped": 0}, "safety": {"problems": []},
    })
    # A prior report is evidence too: re-checking must neither replace nor touch it.
    (root / "verify.json").write_text("preserve prior result\n")
    return root, source


def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("corrupt_input", [False, True])
def test_verify_never_writes_or_touches_recorded_evidence(recorded_run, capsys, corrupt_input):
    root, source = recorded_run
    if corrupt_input:
        source.write_bytes(b"changed input must fail verification")
    before = snapshot(root)
    assert tool.main(["verify", "--destination", str(root)]) == (1 if corrupt_input else 0)
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is (not corrupt_input)
    assert snapshot(root) == before
    assert (root / "verify.json").read_text() == "preserve prior result\n"
