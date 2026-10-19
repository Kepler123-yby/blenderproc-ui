#!/usr/bin/env python3
"""Offline, manifest-driven acceptance of real production resources.

``run`` derives one deterministic case per (project, HDRI), exports a fresh job, runs the
exported ``--check-only``, renders on the GPU, calls the repository's real BOP checker and the
native-mask audit, and writes a summary with commands, exit codes, counters, provenance and a
source snapshot. ``verify`` re-checks an existing evidence directory read-only (it may re-run
the checker and the audit, but never exports or renders).

Nothing here downloads resources, executes the reference renderer or scans historical datasets.
A summary whose resources are classified ``fixture`` can never be production-verified.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from bproc_core.config import ConfigError, validate_project  # noqa: E402
from bproc_core.project_io import load_project  # noqa: E402


class ManifestError(ValueError):
    """The manifest or one of its resources cannot be used safely."""


@dataclass(frozen=True)
class ManifestResource:
    path: Path
    role: str
    sha256: str
    source: str
    classification: str


@dataclass(frozen=True)
class ManifestCase:
    id: str
    project: Path
    hdris: tuple[Path, ...]
    scenes: int
    seed: int
    grouped: bool = False
    reframe: bool = False


@dataclass(frozen=True)
class Manifest:
    path: Path
    resources: tuple[ManifestResource, ...]
    cases: tuple[ManifestCase, ...]


@dataclass(frozen=True)
class JobPlan:
    job: Path
    dataset: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def load_manifest(path: Path) -> Manifest:
    path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"无法读取 manifest：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError("manifest 必须是 schema_version: 1 的对象")
    base = path.parent
    resources = []
    for index, item in enumerate(payload.get("resources") or []):
        if not isinstance(item, dict):
            raise ManifestError(f"resources[{index}] 必须是对象")
        try:
            raw_path = item["path"]
            role = item["role"]
            sha = item["sha256"]
            source = item["source"]
            classification = item["classification"]
        except KeyError as exc:
            raise ManifestError(f"resources[{index}] 缺少字段 {exc}") from exc
        if role not in ("model", "occluder", "hdri"):
            raise ManifestError(f"resources[{index}].role 必须是 model/occluder/hdri")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
            raise ManifestError(f"resources[{index}].sha256 必须是 64 位十六进制")
        if not isinstance(source, str) or not source.strip():
            raise ManifestError(f"resources[{index}].source 不能为空")
        if classification not in ("production", "fixture"):
            raise ManifestError(f"resources[{index}].classification 必须是 production 或 fixture")
        if not isinstance(raw_path, str) or not raw_path:
            raise ManifestError(f"resources[{index}].path 必须是非空字符串")
        resolved = (base / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).expanduser().resolve()
        resources.append(ManifestResource(resolved, role, sha.lower(), source, classification))
    cases = []
    seen_ids = set()
    for index, item in enumerate(payload.get("cases") or []):
        if not isinstance(item, dict):
            raise ManifestError(f"cases[{index}] 必须是对象")
        try:
            case_id = item["id"]
            project = item["project"]
            hdris = item["hdris"]
            scenes = item["scenes"]
            seed = item["seed"]
        except KeyError as exc:
            raise ManifestError(f"cases[{index}] 缺少字段 {exc}") from exc
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise ManifestError(f"cases[{index}].id 必须是非空且唯一的字符串")
        seen_ids.add(case_id)
        if isinstance(scenes, bool) or not isinstance(scenes, int) or scenes < 1:
            raise ManifestError(f"cases[{index}].scenes 必须是正整数")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ManifestError(f"cases[{index}].seed 必须是非负整数")
        if not isinstance(hdris, list) or not hdris:
            raise ManifestError(f"cases[{index}].hdris 必须是非空列表")
        resolved_hdris = []
        for raw in hdris:
            if not isinstance(raw, str) or not raw:
                raise ManifestError(f"cases[{index}].hdris 只能是路径字符串")
            resolved_hdris.append((base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).expanduser().resolve())
        for name in ("grouped", "reframe"):
            if name in item and not isinstance(item[name], bool):
                raise ManifestError(f"cases[{index}].{name} 必须是布尔值")
        project_path = (base / project).resolve() if not Path(project).is_absolute() else Path(project).expanduser().resolve()
        cases.append(ManifestCase(case_id, project_path, tuple(resolved_hdris), scenes, seed,
                                  bool(item.get("grouped", False)), bool(item.get("reframe", False))))
    if not resources:
        raise ManifestError("manifest 至少需要一个资源")
    if not cases:
        raise ManifestError("manifest 至少需要一个 case")
    return Manifest(path, tuple(resources), tuple(cases))


def _project_resources(config) -> list[tuple[Path, str]]:
    entries = []
    for target in config.targets:
        if target.enabled:
            entries.append((target.asset.path.expanduser().resolve(), "model"))
    for asset in config.occluders:
        entries.append((asset.path.expanduser().resolve(), "occluder"))
    entries.extend((path.expanduser().resolve(), "hdri") for path in config.hdri.files)
    return entries


def preflight(manifest: Manifest, *, num_scenes: int | None = None,
              only_case: str | None = None) -> list[tuple[ManifestCase, Path, object]]:
    """Validate every resource, hash and project reference before any task is created."""
    by_path = {resource.path: resource for resource in manifest.resources}
    for resource in manifest.resources:
        if not resource.path.is_file():
            raise ManifestError(f"资源不存在：{resource.path}")
        actual = _sha256(resource.path)
        if actual != resource.sha256:
            raise ManifestError(f"资源哈希不符：{resource.path}（manifest {resource.sha256}，实际 {actual}）")
    plans = []
    for case in manifest.cases:
        if only_case is not None and case.id != only_case:
            continue
        if not case.project.is_file():
            raise ManifestError(f"找不到项目 YAML：{case.project}")
        try:
            config = load_project(case.project)
        except (ConfigError, OSError, ValueError) as exc:
            raise ManifestError(f"无法读取项目 {case.project}：{exc}") from exc
        # The source project's own output directory may already exist; the run always derives a
        # fresh directory under the new destination, so validate against a unique placeholder.
        probe = replace(config, output=replace(
            config.output, root=Path(tempfile.gettempdir()) / f"bproc-production-preflight-{os.getpid()}",
            dataset_name="probe"))
        errors = [issue for issue in validate_project(probe, check_files=True) if issue.severity == "error"]
        if errors:
            raise ManifestError(f"项目 {case.project} 未通过检查：" + "；".join(
                f"{issue.field}: {issue.message}" for issue in errors))
        for path, role in _project_resources(config):
            entry = by_path.get(path)
            if entry is None:
                raise ManifestError(f"项目 {case.project} 引用的资源没有写进 manifest：{path}（{role}）")
            if entry.role != role:
                raise ManifestError(f"资源 {path} 的 role 应为 {role}，manifest 写的是 {entry.role}")
        for hdri in case.hdris:
            entry = by_path.get(hdri)
            if entry is None or entry.role != "hdri":
                raise ManifestError(f"case {case.id} 的 HDRI 未作为 hdri 资源写入 manifest：{hdri}")
            if hdri.suffix.lower() not in (".hdr", ".exr"):
                raise ManifestError(f"HDRI 必须是 .hdr 或 .exr：{hdri}")
        scenes = case.scenes if num_scenes is None else num_scenes
        if scenes < 1:
            raise ManifestError("--num-scenes 必须是正整数")
        # One derived case per HDRI, so every environment is actually used.
        for hdri in case.hdris:
            plans.append((case, hdri, scenes))
    if only_case is not None and not plans:
        raise ManifestError(f"manifest 中没有 case {only_case!r}")
    return plans


def derived_config(case: ManifestCase, hdri: Path, scenes: int, destination: Path):
    project = load_project(case.project)
    grouping = replace(project.scale_grouping, reframe=case.reframe)
    output = replace(project.output, format="bop", root=(destination / "datasets").resolve(),
                     dataset_name=f"{case.id}-{hdri.stem}")
    return replace(project, scene_count=scenes, seed=case.seed,
                   hdri=replace(project.hdri, files=(hdri,), randomize=False),
                   scale_grouping=grouping, output=output)


def _source_snapshot() -> dict:
    files = sorted((REPO_ROOT / "src/bproc_core").glob("*.py")) + \
        sorted((REPO_ROOT / "src/bproc_runtime").glob("*.py")) + \
        [REPO_ROOT / "tools/validate_production_resources.py",
         REPO_ROOT / "tests/blender/check_bop.py"]
    try:
        head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain",
                                 "--untracked-files=no"], capture_output=True, text=True,
                                check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        head, status = "unknown", "unknown"
    return {"git_head": head, "git_status": status,
            "files": {str(path.relative_to(REPO_ROOT)): _sha256(path) for path in files}}


def _device_snapshot() -> str:
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                 "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


class SignalForwarder:
    """Forward SIGINT/SIGTERM to the active child group and stop after its clean exit."""

    def __init__(self):
        self.signum = None
        self.child = None

    def __enter__(self):
        self.previous = {sig: signal.signal(sig, self._handle) for sig in (signal.SIGINT, signal.SIGTERM)}
        return self

    def __exit__(self, *exc):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
        return False

    def _handle(self, signum, frame):
        if self.signum is None:
            self.signum = signum
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(os.getpgid(self.child.pid), signum)
            except (ProcessLookupError, PermissionError):
                pass

    def attach(self, process):
        self.child = process

    def detach(self):
        self.child = None


class LocalExecutor:
    """The real export/check/render/checker/audit chain, reusing the repository tools."""

    def __init__(self, forwarder: SignalForwarder):
        self.forwarder = forwarder
        self.commands: list[dict] = []
        self.env = "bop"

    def _run(self, command: list[str], log: Path) -> int:
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, cwd=str(REPO_ROOT))
            self.forwarder.attach(process)
            try:
                code = process.wait()
            finally:
                self.forwarder.detach()
        self.commands.append({"command": command, "log": str(log), "exit_code": code,
                              "seconds": time.time() - started})
        return code

    def export(self, config, job_dir: Path, *, grouped: bool = False) -> list[JobPlan]:
        from bproc_core.assets import load_mesh
        from bproc_export.bundle import export_grouped_jobs, export_job
        self.env = config.runtime.conda_env
        meshes = {asset.key: load_mesh(asset) for asset in
                  [*(target.asset for target in config.targets if target.enabled), *config.occluders]}
        if grouped:
            results = export_grouped_jobs(config, meshes, job_dir)
        else:
            results = [export_job(config, job_dir)]
        jobs = []
        for result in results:
            snapshot = load_project(Path(result.directory) / "scene.yaml")
            dataset = (snapshot.output.root / snapshot.output.dataset_name).resolve()
            jobs.append(JobPlan(Path(result.directory), dataset))
        return jobs

    def check_only(self, job: JobPlan, log: Path) -> int:
        return self._run(["conda", "run", "--no-capture-output", "-n", self.env, "python",
                          str(job.job / "run_job.py"), "--config", str(job.job / "scene.yaml"),
                          "--check-only"], log)

    def render(self, job: JobPlan, log: Path) -> int:
        return self._run(["conda", "run", "--no-capture-output", "-n", self.env, "python",
                          str(job.job / "run_job.py"), "--config", str(job.job / "scene.yaml")], log)

    def checker(self, job: JobPlan, log: Path) -> int:
        return self._run([sys.executable, str(REPO_ROOT / "tests/blender/check_bop.py"),
                          str(job.dataset)], log)

    def audit(self, job: JobPlan, log: Path, report: Path) -> int:
        return self._run([sys.executable, str(REPO_ROOT / "tools/audit_native_masks.py"),
                          "dataset", "--dataset", str(job.dataset), "--job", str(job.job),
                          "--output", str(report)], log)


def _dataset_counters(dataset: Path) -> dict:
    status_path = Path(dataset) / "run_metadata" / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"数据集缺少可读的 status.json：{dataset}：{exc}") from exc
    frames_path = Path(dataset) / "run_metadata" / "frames.jsonl"
    rejections_path = Path(dataset) / "run_metadata" / "rejections.jsonl"
    frames = len([line for line in frames_path.read_text(encoding="utf-8").splitlines() if line.strip()]) \
        if frames_path.is_file() else int(status.get("written_frames", 0))
    view_rejections = 0
    if rejections_path.is_file():
        for line in rejections_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("phase") == "view":
                view_rejections += 1
    requested = int(status.get("requested", 0))
    saved = int(status.get("saved", 0))
    written = int(status.get("written_frames", frames))
    return {"status": status.get("status"), "requested": requested, "written": written,
            "saved": saved, "skipped": int(status.get("skipped", requested - saved)),
            "proposal_accept_rate": (frames / (frames + view_rejections)) if frames + view_rejections else 0.0,
            "frame_yield": (saved / requested) if requested else 0.0,
            "errors": status.get("failures_by_reason") or {},
            "view_rejections": view_rejections}


def run(manifest_path: Path, destination: Path, *, num_scenes: int | None = None,
        only_case: str | None = None, executor=None, forwarder=None,
        classification_hint: str | None = None) -> int:
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        print(f"destination already exists: {destination}", file=sys.stderr)
        return 2
    manifest = load_manifest(manifest_path)
    plans = preflight(manifest, num_scenes=num_scenes, only_case=only_case)
    classification = classification_hint or (
        "fixture" if any(resource.classification == "fixture" for resource in manifest.resources)
        else "production")
    destination.mkdir(parents=True)
    summary_path = destination / "summary.json"
    snapshot = _source_snapshot()
    summary = {"schema_version": 1, "status": "running", "manifest": str(manifest.path),
               "manifest_sha256": _sha256(manifest.path), "classification": classification,
               "production_verified": False, "device": _device_snapshot(),
               "resources": [{"path": str(resource.path), "role": resource.role,
                              "sha256": resource.sha256, "source": resource.source,
                              "classification": resource.classification}
                             for resource in manifest.resources],
               "source": snapshot, "cases": [], "started_at": datetime.now(timezone.utc).isoformat()}
    _atomic_json(summary_path, summary)
    forwarder = forwarder or SignalForwarder()
    executor = executor or None
    exit_code = 0
    with forwarder as active:
        if executor is None:
            executor = LocalExecutor(active)
        for case, hdri, scenes in plans:
            key = f"{case.id}-{hdri.stem}"
            entry = {"case": case.id, "hdri": str(hdri), "scenes": scenes, "status": "not_started",
                     "jobs": [], "seconds": 0.0}
            summary["cases"].append(entry)
            if active.signum is not None:
                _atomic_json(summary_path, summary)
                continue
            started = time.time()
            try:
                config = derived_config(case, hdri, scenes, destination)
                entry["config"] = {"seed": config.seed, "scene_count": config.scene_count,
                                   "views": config.views, "grouped": case.grouped,
                                   "reframe": case.reframe,
                                   "depth_scale_mm": config.output.depth_scale_mm}
                jobs = executor.export(config, destination / "jobs" / key, grouped=case.grouped)
                entry["jobs"] = [{"job": str(job.job), "dataset": str(job.dataset),
                                  "steps": []} for job in jobs]
                for job, job_entry in zip(jobs, entry["jobs"]):
                    steps = job_entry["steps"]
                    for name, call in (
                            ("check_only", lambda job=job: executor.check_only(
                                job, destination / "logs" / f"{key}-{job.job.name}-check.log")),
                            ("render", lambda job=job: executor.render(
                                job, destination / "logs" / f"{key}-{job.job.name}-render.log"))):
                        code = call()
                        steps.append({"step": name, "exit_code": code})
                        if code != 0:
                            raise ManifestError(f"{name} failed for {job.job} (exit {code})")
                        if active.signum is not None:
                            break
                    if active.signum is not None:
                        break
                    counters = _dataset_counters(job.dataset)
                    job_entry["counters"] = counters
                    code = executor.checker(job, destination / "logs" / f"{key}-{job.job.name}-checker.log")
                    steps.append({"step": "checker", "exit_code": code})
                    if code != 0:
                        raise ManifestError(f"checker failed for {job.dataset} (exit {code})")
                    report = destination / "audits" / f"{key}-{job.job.name}.json"
                    code = executor.audit(job, destination / "logs" / f"{key}-{job.job.name}-audit.log", report)
                    steps.append({"step": "audit", "exit_code": code})
                    if code != 0:
                        raise ManifestError(f"audit failed for {job.dataset} (exit {code})")
                    if counters["status"] != "completed" or counters["saved"] != counters["requested"]:
                        raise ManifestError(
                            f"dataset partial for {job.dataset}: "
                            f"saved {counters['saved']} / requested {counters['requested']}")
                entry["status"] = "interrupted" if active.signum is not None else "completed"
            except Exception as exc:          # noqa: BLE001 - reported in the summary, not hidden
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["seconds"] = time.time() - started
            if active.signum is not None:
                entry["status"] = "interrupted"
            _atomic_json(summary_path, summary)
        if active.signum is not None:
            exit_code = 128 + int(active.signum)
            summary["status"] = "interrupted"
        else:
            incomplete = [entry for entry in summary["cases"] if entry["status"] != "completed"]
            exit_code = 1 if incomplete else 0
            summary["status"] = "failed" if incomplete else "completed"
        _atomic_json(destination / "commands.json", list(getattr(executor, "commands", []) or []))
        all_production = classification == "production"
        summary["production_verified"] = bool(
            all_production and summary["status"] == "completed"
            and all(entry["status"] == "completed" for entry in summary["cases"]))
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["exit_code"] = exit_code
        _atomic_json(summary_path, summary)
    print(json.dumps({"destination": str(destination), "status": summary["status"],
                      "classification": classification,
                      "production_verified": summary["production_verified"],
                      "exit_code": exit_code}, ensure_ascii=False))
    return exit_code


def verify(destination: Path, *, checker=None) -> int:
    """Read-only re-check of an existing evidence directory; never exports or renders."""
    destination = Path(destination).expanduser().absolute()
    summary_path = destination / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read summary: {exc}", file=sys.stderr)
        return 2
    problems = []
    if summary.get("classification") != "production":
        problems.append("classification is not production (fixture evidence cannot pass)")
    if summary.get("status") != "completed":
        problems.append(f"summary status is {summary.get('status')!r}")
    if not summary.get("production_verified"):
        problems.append("summary is not production_verified")
    if summary.get("classification") == "production":
        recorded_device = summary.get("device")
        if not recorded_device or recorded_device == "unavailable":
            problems.append("device evidence is missing")
    resources = summary.get("resources") or []
    production_resources = [row for row in resources if row.get("classification") == "production"]
    models = {row.get("sha256") for row in production_resources if row.get("role") == "model"}
    hdris = {row.get("sha256") for row in production_resources if row.get("role") == "hdri"}
    if len(models) < 2:
        problems.append("production evidence needs at least two real target models")
    if len(hdris) < 2:
        problems.append("production evidence needs at least two real HDRI resources")
    for row in resources:
        path = Path(row.get("path", ""))
        if not path.is_file() or _sha256(path) != row.get("sha256"):
            problems.append(f"resource hash mismatch: {path}")
    for entry in summary.get("cases", []):
        config = entry.get("config") or {}
        if int(config.get("scene_count", 0)) * int(config.get("views", 0)) < 2:
            problems.append(f"case {entry.get('case')} has fewer than two views")
        for job_entry in entry.get("jobs", []):
            if (job_entry.get("counters") or {}).get("saved") != (job_entry.get("counters") or {}).get("requested"):
                problems.append(f"case {entry.get('case')} is not fully saved")
    current = _source_snapshot()
    recorded = summary.get("source") or {}
    if recorded.get("git_head") != current.get("git_head"):
        problems.append("source git HEAD differs from the recorded snapshot")
    for name, digest in (recorded.get("files") or {}).items():
        path = REPO_ROOT / name
        if not path.is_file() or _sha256(path) != digest:
            problems.append(f"source snapshot mismatch: {name}")
    checker = checker or (lambda job, log: subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests/blender/check_bop.py"), str(job.dataset)],
        capture_output=True, text=True).returncode)
    checked = []
    for entry in summary.get("cases", []):
        for job_entry in entry.get("jobs", []):
            job = JobPlan(Path(job_entry["job"]), Path(job_entry["dataset"]))
            started = time.time()
            code = checker(job, destination / "logs" / f"verify-{job.job.name}.log")
            checked.append({"dataset": str(job.dataset), "checker_exit_code": code,
                            "seconds": time.time() - started})
            if code != 0:
                problems.append(f"checker failed for {job.dataset} (exit {code})")
            if not (job.dataset / "run_metadata" / "status.json").is_file():
                problems.append(f"dataset has no status.json: {job.dataset}")
    payload = {"destination": str(destination), "checked": checked, "problems": problems,
               "passed": not problems, "classification": summary.get("classification")}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if not problems else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="export, render, check and audit a manifest")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--destination", type=Path, required=True)
    run_parser.add_argument("--num-scenes", type=int, help="override each case's scene count")
    run_parser.add_argument("--case", help="only this case id")
    verify_parser = sub.add_parser("verify", help="read-only re-check of a finished destination")
    verify_parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run(args.manifest, args.destination, num_scenes=args.num_scenes,
                       only_case=args.case)
        return verify(args.destination)
    except ManifestError as exc:
        print(f"manifest/preflight failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
