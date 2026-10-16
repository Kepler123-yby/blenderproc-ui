"""Read-only integrity and offline preflight verification of an exported job."""
from pathlib import Path
import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bproc_core.project_io import load_project


def verify_runtime(job_dir: Path, *, execute_probe=True) -> list[str]:
    errors = []
    root = Path(job_dir).expanduser().absolute()
    try:
        required = ("run_job.py", "render_scene.py", "scene.yaml", "launch.json", "README.txt",
                    "bproc_core/config.py", "bproc_core/scene.py", "bproc_core/masks.py",
                    "bproc_runtime/preflight.py", "bproc_runtime/cli.py", "bproc_runtime/blender_backend.py")
        for name in required:
            if not (root / name).is_file():
                errors.append(f"Missing exported job file: {name}")
        if errors:
            return errors
        locator = json.loads((root / "launch.json").read_text(encoding="utf-8"))
        if not isinstance(locator, dict) or locator.get("schema_version") != 1 or locator.get("ready") is not True:
            errors.append("Export is incomplete or launch schema is unsupported")
            return errors
        config = load_project(root / "scene.yaml")
        for name in ("blender_dir", "blender_python", "custom_packages", "blenderproc_executable", "blenderproc_package_dir"):
            if not isinstance(locator.get(name), str) or not Path(locator[name]).is_absolute():
                errors.append(f"launch.{name} must be an absolute local path")
        if Path(locator["blender_dir"]) != config.runtime.blender_dir or Path(locator["blenderproc_executable"]) != config.runtime.blenderproc_executable:
            errors.append("Launch locator no longer matches configuration runtime paths; re-export the task")
        launcher = (root / "run_job.py").read_text(encoding="utf-8")
        tree = ast.parse(launcher, feature_version=(3, 8))
        allowed = {"argparse", "json", "os", "pathlib", "signal", "subprocess", "sys", "tempfile"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or "").split(".")[0]]
            else:
                continue
            if any(name not in allowed for name in modules):
                errors.append(f"Python 3.8 launcher imports nonstandard/preflight-unsafe module: {modules}")
        lines = [line.strip() for line in (root / "render_scene.py").read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines or lines[0] != "import blenderproc as bproc":
            errors.append("render_scene.py first effective line must be import blenderproc as bproc")
        for package in ("bproc_core", "bproc_runtime"):
            for source in (root / package).rglob("*.py"):
                parsed = ast.parse(source.read_text(encoding="utf-8"))
                for node in ast.walk(parsed):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                    if any(name.split(".")[0] in ("PySide6", "vispy", "bproc_ui") for name in names):
                        errors.append(f"Exported runtime imports desktop dependency: {source.relative_to(root)}")
        if errors or not execute_probe:
            return errors
        python = Path(locator["blenderproc_executable"]).with_name("python")
        if not python.is_file():
            return [f"Cannot locate conda launcher Python next to BlenderProc executable: {python}"]
        environment = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory(prefix="bproc-verify-") as temporary:
            # A fresh hypothetical output avoids treating an earlier completed render
            # as a runtime dependency error. --check-only must not create it.
            output = Path(temporary) / "unused-output"
            result = subprocess.run([str(python), str(root / "run_job.py"), "--config", str(root / "scene.yaml"), "--check-only", "--output", str(output)],
                                    cwd=temporary, env=environment, capture_output=True, text=True, timeout=45)
            if result.returncode:
                errors.append(f"Offline runtime preflight exited {result.returncode}: {(result.stdout + result.stderr).strip()}")
            if output.exists():
                errors.append("--check-only unexpectedly created render output")
    except Exception as exc:
        errors.append(f"Runtime verification: {type(exc).__name__}: {exc}")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("--static", action="store_true", help="Only inspect files; skip actual offline dependency probe")
    args = parser.parse_args()
    diagnostics = verify_runtime(args.job, execute_probe=not args.static)
    print("\n".join(diagnostics) if diagnostics else "Exported runtime: PASS")
    raise SystemExit(1 if diagnostics else 0)
