"""The Blender bridge only builds commands, environments and snapshots; no Blender here."""

from pathlib import Path
import json

import pytest

from bproc_core.assets import load_mesh
from bproc_core.project_io import load_project
from bproc_core.scene import candidate_from_dict, candidate_to_dict, propose_candidate, propose_scene
from bproc_export import blender_view

from tests.factories import make_project


def _locator(tmp_path: Path) -> dict:
    package = tmp_path / "env/lib/python3.8/site-packages/blenderproc"
    package.mkdir(parents=True)
    blender = tmp_path / "blender"
    blender.mkdir(exist_ok=True)
    return dict(blender_dir=str(blender), blenderproc_package_dir=str(package),
                blender_python=str(tmp_path / "python"))


def _candidate(config):
    meshes = {asset.key: load_mesh(asset)
              for asset in [*(t.asset for t in config.targets if t.enabled), *config.occluders]}
    return propose_candidate(config, meshes, propose_scene(config, meshes, 0), 0, 0)


def test_snapshot_round_trips_the_exact_candidate(tmp_path):
    config = make_project(tmp_path)
    candidate = _candidate(config)
    scene, payload = blender_view.write_snapshot(config, candidate, tmp_path / "view")
    assert load_project(scene).targets == config.targets
    assert candidate_to_dict(candidate_from_dict(json.loads(payload.read_text(encoding="utf-8")))) \
        == candidate_to_dict(candidate)


def test_build_environment_uses_the_conda_site_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/wrong")
    locator = _locator(tmp_path)
    environment = blender_view.build_environment(locator)
    assert environment["PYTHONPATH"] == str(Path(locator["blenderproc_package_dir"]).parent)
    assert environment["INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_gui_environment_drops_the_blenderproc_switch(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/conda/site-packages")
    monkeypatch.setenv("INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT", "1")
    monkeypatch.setenv("DISPLAY", ":1")
    environment = blender_view.gui_environment()
    assert "PYTHONPATH" not in environment
    assert "INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT" not in environment
    assert environment["DISPLAY"] == ":1"


def test_build_command_feeds_blenderproc_its_scratch_after_the_separator(tmp_path):
    locator = _locator(tmp_path)
    command = blender_view.build_command(locator, tmp_path / "view.py", tmp_path / "scene.yaml",
                                         tmp_path / "candidate.json", tmp_path / "scene.blend",
                                         tmp_path / "scratch")
    separator = command.index("--")
    assert command[separator + 1] == str(tmp_path / "view.py")
    assert command[separator + 2] == str(tmp_path / "scratch")
    assert "--background" in command
    assert command[command.index("--save-blend") + 1] == str(tmp_path / "scene.blend")


def test_gui_command_opens_the_saved_file_and_only_configures_the_viewport(tmp_path):
    locator = _locator(tmp_path)
    command = blender_view.gui_command(locator, tmp_path / "view.py", tmp_path / "scene.blend")
    assert command[1] == str(tmp_path / "scene.blend")
    assert "--view-only" in command
    assert "--background" not in command


class _Done:
    def __init__(self, code):
        self.returncode = code


def test_prepare_scene_runs_blender_in_the_background_and_returns_the_blend(tmp_path, monkeypatch):
    config = make_project(tmp_path)
    locator = _locator(tmp_path)
    monkeypatch.setattr(blender_view, "locate_runtime", lambda project: locator)
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        kwargs["stdout"].write(b"BUILD LOG\n")
        Path(command[-1]).write_bytes(b"BLENDER")
        return _Done(0)

    blend = blender_view.prepare_scene(config, _candidate(config), tmp_path / "view",
                                       tmp_path / "view.py", runner=runner)
    assert blend == tmp_path / "view/scene.blend"
    assert blend.read_bytes() == b"BLENDER"
    assert (tmp_path / "view/scene.yaml").exists()
    assert (tmp_path / "view/candidate.json").exists()
    assert (tmp_path / "view/build.log").read_text(encoding="utf-8") == "BUILD LOG\n"


def test_prepare_scene_reports_the_log_tail_when_blender_fails(tmp_path, monkeypatch):
    config = make_project(tmp_path)
    monkeypatch.setattr(blender_view, "locate_runtime", lambda project: _locator(tmp_path))

    def runner(command, **kwargs):
        kwargs["stdout"].write(b"Traceback: no EGL\n")
        return _Done(2)

    with pytest.raises(RuntimeError) as error:
        blender_view.prepare_scene(config, _candidate(config), tmp_path / "view",
                                   tmp_path / "view.py", runner=runner)
    assert "退出码 2" in str(error.value)
    assert "no EGL" in str(error.value)


class _Process:
    def __init__(self, command):
        self.args = command


def test_open_gui_starts_a_detached_session_and_keeps_its_log(tmp_path, monkeypatch):
    config = make_project(tmp_path)
    monkeypatch.setattr(blender_view, "locate_runtime", lambda project: _locator(tmp_path))
    seen = {}

    def popen(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        return _Process(command)

    session = blender_view.open_gui(config, tmp_path / "scene.blend", tmp_path / "view.py",
                                    tmp_path / "view", popen=popen)
    assert seen["kwargs"]["start_new_session"] is True
    assert "PYTHONPATH" not in seen["kwargs"]["env"]
    assert session.log == tmp_path / "view/blender.log"
    assert session.blend == tmp_path / "scene.blend"
    assert "--view-only" in session.command
