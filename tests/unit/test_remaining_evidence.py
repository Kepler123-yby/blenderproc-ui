"""The unified evidence gate accepts only a complete index and rejects each missing gate."""
import json
from pathlib import Path

from tools.verify_remaining_evidence import verify_evidence


def write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build(root: Path) -> Path:
    write(root / "preflight.json", {"git_head": "c2433b5", "baseline_tests": {"passed": 567,
                                                                                "exit_code": 0}})
    screenshot = root / "lod" / "scene.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"png")
    camera = root / "lod" / "camera.png"
    camera.write_bytes(b"png")
    write(root / "lod" / "lod.json", {
        "fixture": {"input_faces": 1_000_000},
        "simplify": {"display_faces": 39_798, "cached": False},
        "wall_seconds": .23, "heartbeat": {"max_gap_ms": 18.0},
        "screenshots": {"scene_auto": {"path": str(screenshot), "non_background": .13},
                        "camera_auto": {"path": str(camera), "non_background": .8}},
        "status": {"full": "原始网格"}})
    cases = []
    for name, proven, lines in (("nonmultiple-tiled", True, 298), ("nonmultiple-untiled", False, 0),
                                ("below-tile", False, 0), ("exact-multiple", True, 74)):
        cases.append({"case": name, "dataset": str(root / "tiles" / "datasets" / name),
                      "scheduled_tiles_proven": proven, "tile_schedule_lines": lines,
                      "checker_exit_code": 0})
    write(root / "tiles" / "commands.json", {"cases": cases})
    write(root / "tiles" / "compare.json", {
        "camera_equal": True, "scene_gt_equal": True, "depth_exact": True, "masks_exact": True,
        "rgb_aggregate": {"rmse_max": .01, "max_diff": 2}})
    rows = []
    for group in (1, 2):
        rows.append({"group": group, "dataset": str(root / "reframe" / f"ds{group}"),
                     "job": str(root / "reframe" / f"job{group}"), "frame_count": 2,
                     "radii_inside": True, "lights_inside": True,
                     "source_project_sha256": "a" * 64, "scale_factor": group / 10,
                     "reference_scale_m": 0.2, "group_scale_m": 0.1 * group})
    write(root / "reframe" / "consistency.json", rows)
    hidden = root / "silhouette" / "mask.png"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_bytes(b"png")
    write(root / "silhouette" / "silhouette.json", {
        "fixture": {"faces": 2_002_000},
        "cold": {"seconds": .99, "working_peak_mib": 3.8},
        "rss": {"extra_mib": 0.0},
        "hole": {"centre_empty": True, "solid_covered": True,
                 "mask_256": str(hidden), "mask_512": str(hidden)},
        "proposal_summary": {"inside_window": 30}})
    (root / "production").mkdir(parents=True)
    (root / "production-fixture" / "evidence").mkdir(parents=True)
    return root


def runners():
    return {"checker": lambda dataset: 0, "auditor": lambda dataset, job: True,
            "production_verify": lambda destination: 0 if Path(destination).name == "production" else 1}


def test_a_complete_synthetic_index_passes(tmp_path):
    report = verify_evidence(build(tmp_path / "evidence"), **runners())
    assert report["passed"] is True
    assert {check["check"] for check in report["checks"]} == {
        "preflight", "lod", "tiles", "reframe", "silhouette", "production", "fixture_guard"}


def test_missing_preflight_or_lod_fails(tmp_path):
    root = build(tmp_path / "evidence")
    (root / "preflight.json").unlink()
    assert verify_evidence(root, **runners())["passed"] is False
    root = build(tmp_path / "second")
    (root / "lod" / "lod.json").unlink()
    assert verify_evidence(root, **runners())["passed"] is False


def test_tile_pixel_differences_and_missing_schedule_fail(tmp_path):
    root = build(tmp_path / "evidence")
    compare = json.loads((root / "tiles" / "compare.json").read_text())
    compare["rgb_aggregate"]["max_diff"] = 5
    (root / "tiles" / "compare.json").write_text(json.dumps(compare))
    assert verify_evidence(root, **runners())["passed"] is False

    root = build(tmp_path / "second")
    commands = json.loads((root / "tiles" / "commands.json").read_text())
    commands["cases"][0]["scheduled_tiles_proven"] = False
    (root / "tiles" / "commands.json").write_text(json.dumps(commands))
    assert verify_evidence(root, **runners())["passed"] is False


def test_silhouette_and_reframe_gates_fail_when_tampered(tmp_path):
    root = build(tmp_path / "evidence")
    silhouette = json.loads((root / "silhouette" / "silhouette.json").read_text())
    silhouette["hole"]["centre_empty"] = False
    (root / "silhouette" / "silhouette.json").write_text(json.dumps(silhouette))
    assert verify_evidence(root, **runners())["passed"] is False

    root = build(tmp_path / "second")
    rows = json.loads((root / "reframe" / "consistency.json").read_text())
    rows[0]["lights_inside"] = False
    (root / "reframe" / "consistency.json").write_text(json.dumps(rows))
    assert verify_evidence(root, **runners())["passed"] is False


def test_fixture_production_cannot_pass_the_gate(tmp_path):
    root = build(tmp_path / "evidence")
    report = verify_evidence(root, checker=lambda dataset: 0,
                             auditor=lambda dataset, job: True,
                             production_verify=lambda destination: 1)
    assert report["passed"] is False
    production = next(check for check in report["checks"] if check["check"] == "production")
    assert production["passed"] is False


def test_a_failing_real_checker_fails_the_gate(tmp_path):
    root = build(tmp_path / "evidence")
    report = verify_evidence(root, checker=lambda dataset: 1,
                             auditor=lambda dataset, job: True,
                             production_verify=lambda destination: 0)
    assert report["passed"] is False


def test_cli_emits_one_json_report_when_production_verify_prints(tmp_path, monkeypatch, capsys):
    from tools import validate_production_resources as production
    from tools import verify_remaining_evidence as gate

    root = build(tmp_path / "evidence")
    monkeypatch.setattr(gate, "_default_checker", lambda dataset: 0)
    monkeypatch.setattr(gate, "_default_auditor", lambda dataset, job: True)

    def noisy_verify(destination):
        print(json.dumps({"helper": "production verify", "destination": str(destination)}))
        return 2 if Path(destination).name == "production" else 1

    monkeypatch.setattr(production, "verify", noisy_verify)
    assert gate.main(["--evidence", str(root)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["passed"] is False
    assert [row["check"] for row in report["checks"] if not row["passed"]] == ["production"]
    assert "production verify" in captured.err
