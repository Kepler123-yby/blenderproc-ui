"""Hand the exact render scene to the Blender GUI for inspection.

The scene is rebuilt by ``tools/blender_view.py`` inside Blender's own Python with the
same ``BlenderBackend`` the renderer uses, so nothing here re-implements scene building.
Building happens in a background Blender process that saves a ``.blend``; the GUI then
opens that file, which also skips the splash screen covering the viewport.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shlex
import subprocess

from bproc_core.config import ConfigError, validate_project
from bproc_core.project_io import save_project
from bproc_core.scene import candidate_to_dict

from .bundle import locate_runtime

BUILD_TIMEOUT_S = 600


@dataclass(frozen=True)
class BlenderSession:
    process: subprocess.Popen
    log: Path
    blend: Path

    @property
    def command(self) -> str:
        return shlex.join(str(part) for part in self.process.args)


def write_snapshot(config, candidate, workspace: Path) -> tuple[Path, Path]:
    """Persist the scene and the exact candidate so Blender rebuilds them verbatim."""
    workspace = Path(workspace).expanduser().absolute()
    workspace.mkdir(parents=True, exist_ok=True)
    scene = workspace / "scene.yaml"
    save_project(config, scene)
    payload = workspace / "candidate.json"
    payload.write_text(json.dumps(candidate_to_dict(candidate), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return scene, payload


def build_environment(locator: dict) -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(locator["blenderproc_package_dir"]).parent)
    environment["INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def gui_environment() -> dict:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT", None)
    return environment


def build_command(locator: dict, script: Path, scene: Path, candidate: Path, blend: Path,
                  scratch: Path) -> list[str]:
    # ``import blenderproc`` reads its temp directory from ``argv[argv.index('--') + 2]``
    # while importing, mirroring the arguments the exported job's launcher supplies.
    return [str(Path(locator["blender_dir"]) / "blender"), "--background",
            "--python-use-system-env", "--python", str(script), "--", str(script),
            str(scratch), "--config", str(scene), "--candidate", str(candidate),
            "--save-blend", str(blend)]


def gui_command(locator: dict, script: Path, blend: Path) -> list[str]:
    return [str(Path(locator["blender_dir"]) / "blender"), str(blend),
            "--python", str(script), "--", str(script), str(blend.parent / "scratch"),
            "--view-only", "--blenderproc-package", locator["blenderproc_package_dir"]]


def _tail(path: Path, lines: int = 6) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])


def render_problems(config) -> tuple[str, ...]:
    """Render-setting errors that must stop a scene build before Blender starts.

    Only ``render.*`` is guarded: viewing one scene has no business requiring the dataset
    export conditions (output conflicts, shot counts) to be settled first.
    """
    return tuple(f"{issue.field}: {issue.message}"
                 for issue in validate_project(config, check_files=False)
                 if issue.severity == "error" and issue.field.startswith("render."))


def prepare_scene(config, candidate, workspace: Path, script: Path,
                  runner=subprocess.run, *, geometry_only=False) -> Path:
    """Rebuild the scene in a background Blender process and return the saved ``.blend``."""
    # Reject invalid render settings here, before the runtime lookup or any build artefact, so
    # every caller (GUI viewer, tools/open_blender.py) gets the same Chinese advice instead of
    # Blender's exit code and traceback.
    problems = () if geometry_only else render_problems(config)
    if problems:
        raise ConfigError("\n".join(problems))
    locator = locate_runtime(config)
    workspace = Path(workspace).expanduser().absolute()
    scene, payload = write_snapshot(config, candidate, workspace)
    scratch = workspace / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    blend = workspace / "scene.blend"
    log = workspace / "build.log"
    command = build_command(locator, Path(script).expanduser().absolute(), scene, payload, blend, scratch)
    if geometry_only:
        command.insert(command.index("--save-blend"), "--geometry-only")
    with log.open("wb") as handle:
        completed = runner(command, env=build_environment(locator), stdout=handle,
                           stderr=subprocess.STDOUT, timeout=BUILD_TIMEOUT_S)
    if completed.returncode != 0 or not blend.exists():
        raise RuntimeError(f"Blender 构建场景失败（退出码 {completed.returncode}）：\n{_tail(log)}")
    return blend


def open_gui(config, blend: Path, script: Path, workspace: Path,
             popen=subprocess.Popen) -> BlenderSession:
    """Launch the Blender GUI in its own session so the UI is never blocked by it."""
    locator = locate_runtime(config)
    workspace = Path(workspace).expanduser().absolute()
    workspace.mkdir(parents=True, exist_ok=True)
    log = workspace / "blender.log"
    handle = log.open("wb")
    try:
        process = popen(gui_command(locator, Path(script).expanduser().absolute(), Path(blend)),
                        env=gui_environment(), stdout=handle, stderr=subprocess.STDOUT,
                        start_new_session=True)
    finally:
        handle.close()
    return BlenderSession(process, log, Path(blend))
