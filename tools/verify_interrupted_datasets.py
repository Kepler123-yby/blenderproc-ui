#!/usr/bin/env python3
"""Build, interrupt and verify four datasets from the current commit.

The previous acceptance evidence (``.artifacts/review4/signals.py``) guessed the output format
from a ``status.json`` field that does not exist, spelled BOP masks without their ground-truth
index and never propagated a wrong exit code or a broken artefact into its own exit status. This
script is the single acceptance path instead:

* the format is read from the dataset's ``run_metadata/project.yaml`` (the saved project snapshot),
* the repository checkers ``tests.blender.check_bop.verify_bop`` and
  ``tests.blender.check_rgb_masks.verify_rgb_masks`` are called with ``require_complete=False``
  because an interrupted dataset legitimately lacks most frames,
* every promised artefact is resolved the way the writer names it -- ``rgb/*``,
  ``mask/{frame:06d}_{obj_id:06d}.png``, ``mask_visib/{frame:06d}_{obj_id:06d}.png`` for
  ``rgb_masks``; ``train_pbr/{scene:06d}/rgb/{image:06d}.jpg``, ``depth/{image:06d}.png`` and
  ``mask*/{image:06d}_{gt_index:06d}.png`` for ``bop``,
* a mismatched exit code, status, counter, journal/manifest row count, ``final`` flag, missing
  file or checker diagnostic makes the process exit non-zero with the concrete failure printed.

Subcommands::

    run        export four fresh jobs from the current commit, interrupt them, verify everything
    verify     re-run the verification over already collected evidence (exit 0 only if all pass)
    check      verify one dataset directory (used for the negative fixtures and fault proofs)
    negatives  copy successful datasets, delete one referenced artefact per copy, expect failure
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bproc_core.project_io import load_project  # noqa: E402
from bproc_export.bundle import export_job  # noqa: E402
from tests.blender.check_bop import verify_bop  # noqa: E402
from tests.blender.check_rgb_masks import verify_rgb_masks  # noqa: E402

TEMPLATE = ROOT / ".artifacts/review4/signal-rgb-int/job/scene.yaml"
WORK = ROOT / ".artifacts/review10-signals"
CHECKERS = {"rgb_masks": verify_rgb_masks, "bop": verify_bop}
EXPECTED = {"requested": 48, "written": 2, "saved": 2, "skipped": 46, "journal": 2}
COUNTED = ("requested", "written", "saved", "skipped", "journal")


@dataclass(frozen=True)
class Case:
    key: str
    format: str
    signum: int
    signame: str
    expected_exit: int


def dataset_of(work: Path, key: str) -> Path:
    return Path(work) / key / "out" / "dataset"


CASES = (
    Case("signal-rgb-int", "rgb_masks", signal.SIGINT, "SIGINT", 130),
    Case("signal-rgb-term", "rgb_masks", signal.SIGTERM, "SIGTERM", 143),
    Case("signal-bop-int", "bop", signal.SIGINT, "SIGINT", 130),
    Case("signal-bop-term", "bop", signal.SIGTERM, "SIGTERM", 143),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path, problems: list[str]):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - every unreadable document is a reported failure
        problems.append(f"{path}: {type(exc).__name__}: {exc}")
        return None


def journal_rows(dataset: Path) -> int:
    path = Path(dataset) / "run_metadata" / "frames.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return len([line for line in lines if line.strip()])


def referenced_artifacts(dataset: Path, row: dict, label: str) -> list[tuple[str, Path]]:
    """Every artefact a journal row promises, named exactly as the writer names it."""
    dataset = Path(dataset)
    paths: list[tuple[str, Path]] = []
    relative = row.get("relative_rgb")
    if relative:
        paths.append(("rgb", dataset / relative))
    objs = list(row.get("gt_obj_ids") or ())
    if label == "rgb_masks":
        index = row["frame_index"]
        for obj_id in objs:
            name = f"{index:06d}_{obj_id:06d}.png"
            paths.append(("mask", dataset / "mask" / name))
            paths.append(("mask_visib", dataset / "mask_visib" / name))
    elif label == "bop":
        scene, image = row.get("bop_scene_id"), row.get("bop_image_id")
        if scene is None or image is None:
            paths.append(("bop_scene", dataset / "train_pbr" / "missing-bop-id"))
            return paths
        chunk = dataset / "train_pbr" / f"{int(scene):06d}"
        paths.append(("depth", chunk / "depth" / f"{int(image):06d}.png"))
        for gt_index, _ in enumerate(objs):
            name = f"{int(image):06d}_{gt_index:06d}.png"
            paths.append(("mask", chunk / "mask" / name))
            paths.append(("mask_visib", chunk / "mask_visib" / name))
    return paths


def counter_problems(status: dict, *, manifest_rows: int, journal: int, final, total_shots: int,
                     expected_exit: int, expected: dict) -> list[str]:
    """Exit code, status and counter problems; the numbers an interrupted run must report."""
    problems = []
    if not isinstance(status, dict):
        return ["status.json is not an object"]
    if status.get("status") != "interrupted":
        problems.append(f"status is {status.get('status')!r}, expected 'interrupted'")
    if status.get("exit_code") != expected_exit:
        problems.append(f"exit_code is {status.get('exit_code')!r}, expected {expected_exit}")
    for name in ("requested", "written_frames", "saved", "skipped", "exit_code"):
        value = status.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"status.{name} must be an integer, got {value!r}")
    measured = {"requested": status.get("requested"), "written": status.get("written_frames"),
                "saved": status.get("saved"), "skipped": status.get("skipped"), "journal": journal}
    for name in COUNTED:
        if measured[name] != expected[name]:
            problems.append(f"{name} is {measured[name]!r}, expected {expected[name]}")
    if isinstance(status.get("requested"), int) and status["requested"] != total_shots:
        problems.append(f"status.requested {status['requested']} differs from project total_shots {total_shots}")
    if manifest_rows != expected["journal"]:
        problems.append(f"manifest rows {manifest_rows} differ from expected {expected['journal']}")
    if final is not True:
        problems.append(f"manifest final is {final!r}, expected true")
    return problems


def checker_problems(label: str, dataset: Path) -> list[str]:
    checker = CHECKERS.get(label)
    if checker is None:
        return [f"no repository checker for format {label!r}"]
    return list(checker(Path(dataset), require_complete=False))


def verify_case(case: Case, *, expected_exit: int | None = None, expected: dict | None = None,
                dataset: Path | None = None) -> tuple[list[str], dict]:
    """Return ``(problems, facts)`` for one interrupted dataset directory."""
    dataset = Path(dataset) if dataset is not None else dataset_of(WORK, case.key)
    expected = dict(expected or EXPECTED)
    expected_exit = case.expected_exit if expected_exit is None else expected_exit
    meta = dataset / "run_metadata"
    problems: list[str] = []
    facts = {"case": case.key, "dataset": str(dataset), "format": case.format,
             "require_complete": False, "expected_exit": expected_exit, "expected": expected}
    for name in ("status.json", "frames.jsonl", "manifest.json", "project.yaml"):
        if not (meta / name).is_file():
            problems.append(f"missing run_metadata/{name}")
    if problems:
        facts["problems"] = problems
        return problems, facts
    try:
        config = load_project(meta / "project.yaml")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"project.yaml: {type(exc).__name__}: {exc}")
        facts["problems"] = problems
        return problems, facts
    # The format comes from the saved config, not from a status field.
    label = config.output.format
    facts["config_format"] = label
    facts["total_shots"] = config.total_shots
    if label != case.format:
        problems.append(f"config format is {label!r}, expected {case.format!r}")
    if config.total_shots != EXPECTED["requested"]:
        problems.append(f"project total_shots {config.total_shots} differs from {EXPECTED['requested']}")
    status = read_json(meta / "status.json", problems)
    manifest = read_json(meta / "manifest.json", problems)
    rows = []
    for number, line in enumerate((meta / "frames.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"frames.jsonl line {number}: {type(exc).__name__}: {exc}")
    manifest_rows = len(manifest.get("frames") or ()) if isinstance(manifest, dict) else 0
    final = manifest.get("final") if isinstance(manifest, dict) else None
    if isinstance(status, dict):
        problems.extend(counter_problems(status, manifest_rows=manifest_rows, journal=len(rows),
                                         final=final, total_shots=config.total_shots,
                                         expected_exit=expected_exit, expected=expected))
    missing = []
    for row in rows:
        if not isinstance(row, dict):
            problems.append("frames.jsonl row is not an object")
            continue
        for kind, path in referenced_artifacts(dataset, row, label):
            if not path.is_file():
                missing.append(f"{kind}: {path}")
    problems.extend(f"missing referenced artefact ({item})" for item in missing)
    diagnostics = checker_problems(label, dataset)
    problems.extend(f"checker: {item}" for item in diagnostics)
    facts.update({"checker_output": diagnostics,
                  "status": status.get("status") if isinstance(status, dict) else None,
                  "exit_code": status.get("exit_code") if isinstance(status, dict) else None,
                  "requested": status.get("requested") if isinstance(status, dict) else None,
                  "written": status.get("written_frames") if isinstance(status, dict) else None,
                  "saved": status.get("saved") if isinstance(status, dict) else None,
                  "skipped": status.get("skipped") if isinstance(status, dict) else None,
                  "journal_rows": len(rows), "manifest_rows": manifest_rows, "final": final,
                  "missing_artifacts": missing, "problems": problems})
    return problems, facts


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def source_hashes(job: Path) -> dict:
    job = Path(job)
    files = [*(job / "bproc_core").rglob("*.py"), *(job / "bproc_runtime").rglob("*.py"),
             job / "run_job.py", job / "render_scene.py", job / "scene.yaml"]
    files = sorted(set(files))
    hashes = {str(path.relative_to(job)): sha256(path) for path in files if path.is_file()}
    return {"files": hashes,
            "scene_py": hashes.get("bproc_core/scene.py"),
            "run_py": hashes.get("bproc_runtime/run.py")}


def build_jobs(work: Path, template: Path) -> list[Case]:
    base = load_project(template)
    for case in CASES:
        root = (Path(work) / case.key / "out").resolve()
        config = replace(base, output=replace(base.output, format=case.format, root=root,
                                              dataset_name="dataset"))
        export_job(config, Path(work) / case.key / "job")
    return list(CASES)


def launch(case: Case, work: Path, *, timeout_s: float = 900, warm_rows: int = 2) -> dict:
    """Run ``run_job.py`` and deliver ``case.signum`` once the journal has ``warm_rows`` rows."""
    work = Path(work)
    job = work / case.key / "job"
    dataset = dataset_of(work, case.key)
    log = (work / case.key / "run.log").open("w", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    process = subprocess.Popen([sys.executable, "run_job.py"], cwd=job, stdout=log,
                               stderr=subprocess.STDOUT, env=env, start_new_session=True)
    deadline = time.monotonic() + timeout_s
    at_kill = None
    try:
        while time.monotonic() < deadline:
            at_kill = journal_rows(dataset)
            if at_kill >= warm_rows or process.poll() is not None:
                break
            time.sleep(0.05)
        else:
            os.killpg(os.getpgid(process.pid), case.signum)
            code = process.wait(timeout=180)
            return {"case": case.key, "signal": case.signame, "exit": code, "journal_at_kill": at_kill,
                    "error": "timed out waiting for journal rows"}
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), case.signum)
            except ProcessLookupError:
                pass
        code = process.wait(timeout=180)
    finally:
        log.close()
    return {"case": case.key, "signal": case.signame, "signum": case.signum,
            "expected_exit": case.expected_exit, "exit": code, "journal_at_kill": at_kill}


def collect_case(case: Case, work: Path) -> tuple[list[str], dict]:
    work = Path(work)
    case_dir = work / case.key
    dataset = dataset_of(work, case.key)
    problems, facts = verify_case(case, dataset=dataset)
    checker_label = facts.get("config_format") or case.format
    diagnostics = facts.get("checker_output")
    if diagnostics is None:
        diagnostics = ["checker not run: dataset or format unavailable"]
    checker_lines = [f"dataset={dataset}", f"format={checker_label}", "require_complete=False",
                     *(diagnostics or ["PASS: no diagnostics"])]
    (case_dir / "checker.txt").write_text("\n".join(checker_lines) + "\n", encoding="utf-8")
    hashes = source_hashes(case_dir / "job")
    hashes["git_head"] = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                        capture_output=True, text=True, check=False).stdout.strip()
    write_json(case_dir / "source-hashes.json", hashes)
    for name in ("status.json", "frames.jsonl", "manifest.json"):
        source = dataset / "run_metadata" / name
        if source.is_file():
            (case_dir / "evidence").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, case_dir / "evidence" / name)
    facts["checker_problems"] = len(diagnostics)
    facts["source_hashes"] = {"scene_py": hashes.get("scene_py"), "run_py": hashes.get("run_py"),
                              "git_head": hashes["git_head"]}
    write_json(case_dir / "verify.json", facts)
    return problems, facts


def first_row(dataset: Path) -> dict:
    for line in (Path(dataset) / "run_metadata" / "frames.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            return json.loads(line)
    raise ValueError(f"no journal rows in {dataset}")


NEGATIVES = (
    ("missing-rgb", "signal-rgb-int", "rgb_masks", "rgb"),
    ("missing-depth", "signal-bop-int", "bop", "depth"),
    ("missing-mask", "signal-bop-int", "bop", "mask"),
    ("missing-mask-visib", "signal-bop-int", "bop", "mask_visib"),
)


def run_negatives(work: Path) -> list[str]:
    """Copy a successful dataset, delete one referenced artefact, and require a failure."""
    work = Path(work)
    problems = []
    cases = {case.key: case for case in CASES}
    for name, key, label, kind in NEGATIVES:
        source = dataset_of(work, key)
        target = work / "negatives" / name / "dataset"
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        row = first_row(source)
        candidates = [(candidate_kind, path) for candidate_kind, path in
                      referenced_artifacts(target, row, label) if candidate_kind == kind]
        if not candidates:
            problems.append(f"{name}: no referenced {kind} artefact in the journal")
            continue
        removed = candidates[0][1]
        removed.unlink()
        case = cases[key]
        diagnostics, facts = verify_case(case, dataset=target)
        facts["removed"] = str(removed.relative_to(target))
        facts["expected_result"] = "FAIL"
        facts["failed"] = bool(diagnostics)
        write_json(work / "negatives" / name / "result.json", facts)
        (work / "negatives" / name / "checker.txt").write_text(
            "\n".join(diagnostics or ["PASS: no diagnostics (negative fixture did not fail)"]) + "\n",
            encoding="utf-8")
        if not diagnostics:
            problems.append(f"{name}: deleting {facts['removed']} did not make the check fail")
        else:
            print(f"NEGATIVE {name}: FAIL as required after deleting {facts['removed']} "
                  f"({len(diagnostics)} problem(s))", flush=True)
        if not (source / Path(facts["removed"])).is_file():
            problems.append(f"{name}: original artefact {facts['removed']} is no longer present")
    return problems


def report(case: Case, exit_info: dict | None, facts: dict, problems: list[str]) -> None:
    line = {"case": case.key, **{key: facts.get(key) for key in
            ("format", "config_format", "status", "exit_code", "requested", "written", "saved",
             "skipped", "journal_rows", "manifest_rows", "final", "checker_problems")}}
    if exit_info:
        line.update({key: exit_info.get(key) for key in ("signal", "exit", "expected_exit", "journal_at_kill")})
    line["ok"] = not problems
    print(json.dumps(line, ensure_ascii=False), flush=True)
    for problem in problems:
        print(f"FAIL {case.key}: {problem}", flush=True)


def command_run(work: Path, template: Path) -> int:
    work = Path(work)
    if work.exists() and any(work.iterdir()):
        print(f"FAIL: {work} already contains evidence; use a fresh top-level directory", flush=True)
        return 1
    build_jobs(work, template)
    failures = []
    for case in CASES:
        exit_info = launch(case, work)
        write_json(work / case.key / "exit.json", exit_info)
        print(f"RAN {case.key}: signal={exit_info['signal']} exit={exit_info['exit']} "
              f"journal_at_kill={exit_info['journal_at_kill']}", flush=True)
        if exit_info.get("error"):
            failures.append(f"{case.key}: {exit_info['error']}")
        if exit_info["exit"] != case.expected_exit:
            failures.append(f"{case.key}: exit {exit_info['exit']} != expected {case.expected_exit}")
        problems, facts = collect_case(case, work)
        report(case, exit_info, facts, problems)
        failures.extend(f"{case.key}: {problem}" for problem in problems)
    failures.extend(f"negative: {problem}" for problem in run_negatives(work))
    write_json(work / "summary.json", {"ok": not failures, "failures": failures,
                                       "cases": [case.key for case in CASES]})
    if failures:
        print(f"\n{len(failures)} failure(s)", flush=True)
        return 1
    print("\nALL FOUR INTERRUPTED DATASETS PASS", flush=True)
    return 0


def command_verify(work: Path) -> int:
    work = Path(work)
    failures = []
    for case in CASES:
        exit_info = None
        path = work / case.key / "exit.json"
        if path.is_file():
            exit_info = json.loads(path.read_text(encoding="utf-8"))
        problems, facts = collect_case(case, work)
        if exit_info is None:
            problems.append("missing exit.json")
        else:
            if exit_info.get("exit") != case.expected_exit:
                problems.append(f"exit {exit_info.get('exit')} != expected {case.expected_exit}")
            if exit_info.get("signal") != case.signame:
                problems.append(f"signal {exit_info.get('signal')} != expected {case.signame}")
        report(case, exit_info, facts, problems)
        failures.extend(f"{case.key}: {problem}" for problem in problems)
    if failures:
        print(f"\n{len(failures)} failure(s)", flush=True)
        return 1
    print("\nALL FOUR INTERRUPTED DATASETS PASS", flush=True)
    return 0


def command_check(format_name: str, dataset: Path, expected_exit: int, expected: dict) -> int:
    label = {"rgb": "rgb_masks", "rgb_masks": "rgb_masks", "bop": "bop"}[format_name]
    case = Case("check", label, signal.SIGINT, "SIGINT", expected_exit)
    problems, facts = verify_case(case, dataset=Path(dataset), expected_exit=expected_exit,
                                  expected=expected)
    print(json.dumps({key: facts.get(key) for key in
                      ("dataset", "config_format", "status", "exit_code", "requested", "written",
                       "saved", "skipped", "journal_rows", "manifest_rows", "final")},
                     ensure_ascii=False), flush=True)
    for problem in problems:
        print(f"FAIL: {problem}", flush=True)
    print("PASS" if not problems else f"\n{len(problems)} problem(s)", flush=True)
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="export + interrupt + verify four fresh datasets")
    run.add_argument("--work", type=Path, default=WORK)
    run.add_argument("--template", type=Path, default=TEMPLATE)
    verify = sub.add_parser("verify", help="re-verify already collected evidence")
    verify.add_argument("--work", type=Path, default=WORK)
    negatives = sub.add_parser("negatives", help="prove deleted artefacts make the check fail")
    negatives.add_argument("--work", type=Path, default=WORK)
    check = sub.add_parser("check", help="verify one dataset directory")
    check.add_argument("--format", dest="format_name", choices=["rgb", "rgb_masks", "bop"], required=True)
    check.add_argument("--dataset", type=Path, required=True)
    check.add_argument("--expect-exit", type=int, default=130)
    for name in COUNTED:
        check.add_argument(f"--expect-{name}", type=int, default=EXPECTED[name])
    arguments = parser.parse_args(argv)
    if arguments.command == "run":
        return command_run(arguments.work, arguments.template)
    if arguments.command == "verify":
        return command_verify(arguments.work)
    if arguments.command == "negatives":
        problems = run_negatives(arguments.work)
        for problem in problems:
            print(f"FAIL: {problem}", flush=True)
        return 1 if problems else 0
    expected = {name: getattr(arguments, f"expect_{name}") for name in COUNTED}
    return command_check(arguments.format_name, arguments.dataset, arguments.expect_exit, expected)


if __name__ == "__main__":
    raise SystemExit(main())
