#!/usr/bin/env python3
"""Read-only verification of the remaining-boundaries evidence index.

This never renders. It re-runs the repository's real BOP checker and native-mask audit on the
datasets named by the evidence, checks the measurement gates (LOD, tiles, reframe, streaming
silhouettes), and requires a production-verified resource run. Any missing or failing item makes
the whole command non-zero.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))


def _read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _default_checker(dataset: Path) -> int:
    return subprocess.run([sys.executable, str(REPO_ROOT / "tests/blender/check_bop.py"), str(dataset)],
                          capture_output=True, text=True).returncode


def _default_auditor(dataset: Path, job: Path) -> bool:
    from tools.audit_native_masks import audit_dataset
    return bool(audit_dataset(dataset, job).get("passed"))


def _default_production_verify(destination: Path) -> int:
    from tools.validate_production_resources import verify
    return verify(destination)


def verify_evidence(evidence: Path, *, checker=None, auditor=None, production_verify=None) -> dict:
    evidence = Path(evidence).expanduser().resolve()
    checker = checker or _default_checker
    auditor = auditor or _default_auditor
    production_verify = production_verify or _default_production_verify
    checks = []

    def record(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    preflight_path = evidence / "preflight.json"
    try:
        preflight = _read(preflight_path)
        head = preflight.get("git_head") or preflight.get("git_head_at_preflight")
        record("preflight", preflight.get("baseline_tests", {}).get("passed") == 567
               and preflight.get("baseline_tests", {}).get("exit_code") == 0
               and bool(head), {"path": str(preflight_path), "git_head": head})
    except (OSError, ValueError) as exc:
        record("preflight", False, f"missing/invalid: {exc}")

    try:
        lod = _read(evidence / "lod" / "lod.json")
        shots = lod["screenshots"]
        ok = (lod["fixture"]["input_faces"] >= 1_000_000
              and 0 < lod["simplify"]["display_faces"] <= 50_000
              and lod["wall_seconds"] <= 60
              and lod["heartbeat"]["max_gap_ms"] <= 250
              and all(Path(shots[key]["path"]).is_file() for key in ("scene_auto", "camera_auto"))
              and shots["scene_auto"]["non_background"] > .01
              and shots["camera_auto"]["non_background"] > .01
              and "原始网格" in lod["status"]["full"])
        record("lod", ok, {"input_faces": lod["fixture"]["input_faces"],
                           "display_faces": lod["simplify"]["display_faces"],
                           "wall_s": lod["wall_seconds"],
                           "heartbeat_ms": lod["heartbeat"]["max_gap_ms"],
                           "screenshots": [shots["scene_auto"]["non_background"],
                                           shots["camera_auto"]["non_background"]]})
    except (OSError, KeyError, ValueError, TypeError) as exc:
        record("lod", False, f"missing/invalid: {exc}")

    try:
        commands = _read(evidence / "tiles" / "commands.json")["cases"]
        compare = _read(evidence / "tiles" / "compare.json")
        rows = []
        for row in commands:
            dataset = Path(row["dataset"])
            code = int(checker(dataset))
            rows.append({"case": row["case"], "checker": code,
                         "proven": row["scheduled_tiles_proven"],
                         "tile_lines": row["tile_schedule_lines"]})
        by_name = {row["case"]: row for row in rows}
        ok = (len(rows) >= 4 and all(row["checker"] == 0 for row in rows)
              and compare["camera_equal"] and compare["scene_gt_equal"]
              and compare["depth_exact"] and compare["masks_exact"]
              and compare["rgb_aggregate"]["rmse_max"] <= 1
              and compare["rgb_aggregate"]["max_diff"] <= 4
              and by_name["nonmultiple-tiled"]["proven"] and by_name["nonmultiple-tiled"]["tile_lines"] > 0
              and by_name["exact-multiple"]["proven"] and by_name["exact-multiple"]["tile_lines"] > 0
              and not by_name["nonmultiple-untiled"]["proven"]
              and not by_name["below-tile"]["proven"])
        record("tiles", ok, {"rows": rows, "rgb": compare["rgb_aggregate"]})
    except (OSError, KeyError, ValueError, TypeError) as exc:
        record("tiles", False, f"missing/invalid: {exc}")

    try:
        rows = _read(evidence / "reframe" / "consistency.json")
        audits = []
        checkers = []
        for row in rows:
            dataset, job = Path(row["dataset"]), Path(row["job"])
            checkers.append(int(checker(dataset)))
            audits.append(bool(auditor(dataset, job)))
        ok = (len(rows) == 2 and all(row["frame_count"] >= 2 for row in rows)
              and all(row["radii_inside"] and row["lights_inside"] for row in rows)
              and len({row["source_project_sha256"] for row in rows}) == 1
              and all(code == 0 for code in checkers) and all(audits)
              and (rows[0]["group_scale_m"] / rows[0]["reference_scale_m"])
                  != (rows[1]["group_scale_m"] / rows[1]["reference_scale_m"]))
        record("reframe", ok, {"rows": [{**{k: row[k] for k in ("group", "frame_count", "radii_inside",
                                                                 "lights_inside")},
                                                "scale_factor": row["group_scale_m"] / row["reference_scale_m"]}
                                       for row in rows],
                               "checkers": checkers, "audits": audits})
    except (OSError, KeyError, ValueError, TypeError) as exc:
        record("reframe", False, f"missing/invalid: {exc}")

    try:
        silhouette = _read(evidence / "silhouette" / "silhouette.json")
        ok = (silhouette["fixture"]["faces"] > 2_000_000
              and silhouette["cold"]["seconds"] <= 120
              and silhouette["cold"]["working_peak_mib"] <= 64
              and silhouette["rss"]["extra_mib"] <= 256
              and silhouette["hole"]["centre_empty"] and silhouette["hole"]["solid_covered"]
              and silhouette["proposal_summary"]["inside_window"] == 30
              and Path(silhouette["hole"]["mask_256"]).is_file()
              and Path(silhouette["hole"]["mask_512"]).is_file())
        record("silhouette", ok, {"faces": silhouette["fixture"]["faces"],
                                  "cold_s": silhouette["cold"]["seconds"],
                                  "working_mib": silhouette["cold"]["working_peak_mib"],
                                  "extra_rss_mib": silhouette["rss"]["extra_mib"],
                                  "proposals": silhouette["proposal_summary"]["inside_window"]})
    except (OSError, KeyError, ValueError, TypeError) as exc:
        record("silhouette", False, f"missing/invalid: {exc}")

    production = evidence / "production"
    try:
        code = int(production_verify(production))
        record("production", code == 0, {"destination": str(production), "verify_exit": code})
    except Exception as exc:                 # noqa: BLE001 - the gate reports the reason
        record("production", False, f"production resources blocked/missing: {type(exc).__name__}: {exc}")

    fixture = evidence / "production-fixture" / "evidence"
    try:
        code = int(production_verify(fixture))
        record("fixture_guard", code != 0,
               {"destination": str(fixture), "verify_exit": code,
                "expectation": "fixture evidence must never verify as production"})
    except Exception as exc:                 # noqa: BLE001
        record("fixture_guard", False, f"fixture evidence missing: {type(exc).__name__}: {exc}")

    passed = all(check["passed"] for check in checks)
    return {"evidence": str(evidence), "passed": passed, "checks": checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    report = verify_evidence(args.evidence)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
