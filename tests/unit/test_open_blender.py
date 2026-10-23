"""`tools/open_blender.py` must refuse render configs the build would reject, in Chinese.

The shared `prepare_scene` entry stays real here: only `locate_runtime` and the Blender process
are replaced, so the guard itself is what the tool's error handling is exercised against.
"""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import pytest

from bproc_core.assets import load_mesh
from bproc_core.project_io import save_project
from bproc_core.scene import candidate_to_dict, propose_candidate, propose_scene
from bproc_export import blender_view
from tests.factories import make_project

ROOT = Path(__file__).parents[2]


def load_tool():
    spec = importlib.util.spec_from_file_location('open_blender_tool', ROOT / 'tools' / 'open_blender.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _write_inputs(tmp_path: Path, config) -> tuple[Path, Path]:
    project_path = tmp_path / "project.yaml"
    save_project(config, project_path)
    payload = tmp_path / "candidate.json"
    payload.write_text(json.dumps(candidate_to_dict(_candidate(config)), ensure_ascii=False),
                       encoding="utf-8")
    return project_path, payload


def _inject_runner(monkeypatch, module, runner):
    """Keep the real shared build entry; replace only the Blender process it would run."""
    real_prepare = blender_view.prepare_scene

    def prepare(config, candidate, workspace, script):
        return real_prepare(config, candidate, workspace, script, runner=runner)

    monkeypatch.setattr(module, "prepare_scene", prepare)


class _Done:
    def __init__(self, code):
        self.returncode = code


class _Session:
    def __init__(self, blend):
        self.blend = blend
        self.log = Path(blend).parent / "blender.log"
        self.command = "blender scene.blend"


@pytest.mark.parametrize("device", ("CUDA", "HIP", "ONEAPI", "METAL"))
def test_illegal_render_config_stops_before_blender_and_the_gui(tmp_path, monkeypatch, capsys, device):
    module = load_tool()
    config = make_project(tmp_path / "assets")
    invalid = replace(config, render=replace(config.render, device=device, denoiser="OPTIX"))
    project_path, payload = _write_inputs(tmp_path, invalid)

    monkeypatch.setattr(blender_view, "locate_runtime",
                        lambda project: pytest.fail("配置校验必须早于运行时定位"))
    _inject_runner(monkeypatch, module,
                   lambda *args, **kwargs: pytest.fail("Blender 不得启动"))
    monkeypatch.setattr(module, "open_gui", lambda *args, **kwargs: pytest.fail("不得打开 Blender GUI"))

    code = module.main(["--project", str(project_path), "--candidate", str(payload),
                        "--workspace", str(tmp_path / "view")])

    captured = capsys.readouterr()
    assert code == 1
    assert "配置错误" in captured.err
    assert "render.denoiser" in captured.err
    for hint in ("自动 GPU", "关闭", "OPTIX", "AMD/Intel", "macOS"):
        assert hint in captured.err
    assert "Traceback" not in captured.err
    assert "场景已构建" not in captured.out
    assert "已在 Blender 中打开" not in captured.out
    assert not (tmp_path / "view").exists()


def test_legal_render_config_builds_then_opens_the_gui(tmp_path, monkeypatch, capsys):
    module = load_tool()
    config = make_project(tmp_path / "assets")
    config = replace(config, render=replace(config.render, device="CUDA", denoiser="none"))
    project_path, payload = _write_inputs(tmp_path, config)
    monkeypatch.setattr(blender_view, "locate_runtime", lambda project: _locator(tmp_path))
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        kwargs["stdout"].write(b"BUILD LOG\n")
        Path(command[-1]).write_bytes(b"BLENDER")
        return _Done(0)

    _inject_runner(monkeypatch, module, runner)
    monkeypatch.setattr(module, "open_gui",
                        lambda config, blend, script, workspace: _Session(blend))

    code = module.main(["--project", str(project_path), "--candidate", str(payload),
                        "--workspace", str(tmp_path / "view")])

    captured = capsys.readouterr()
    assert code == 0
    assert "场景已构建" in captured.out
    assert "已在 Blender 中打开" in captured.out
    assert seen["command"][-1] == str(tmp_path / "view" / "scene.blend")
    assert (tmp_path / "view" / "scene.blend").read_bytes() == b"BLENDER"
