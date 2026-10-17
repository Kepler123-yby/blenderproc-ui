from pathlib import Path
from dataclasses import replace
import importlib.util
import json
import subprocess
import sys
import pytest

from bproc_core.config import ProjectConfig, RuntimeSpec
from bproc_runtime.preflight import probe, default_requirements, apply_overrides


def test_default_list_parsed_without_executing(tmp_path):
    config = tmp_path / 'python/utility/DefaultConfig.py'
    config.parent.mkdir(parents=True)
    config.write_text("raise RuntimeError('must not execute')\nclass DefaultConfig:\n default_pip_packages=['wheel','pyyaml==6.0.1']\n")
    assert default_requirements(tmp_path) == ['wheel', 'pyyaml==6.0.1']


def test_missing_cache_stops_before_any_bproc_import(tmp_path, monkeypatch):
    package = tmp_path / 'blenderproc'
    package.mkdir()
    (package / 'version.py').write_text("__version__ = '2.8.0'\n")
    config = ProjectConfig(runtime=RuntimeSpec(blender_dir=tmp_path, blenderproc_executable=tmp_path / 'blenderproc-bin'))
    locator = dict(blenderproc_package_dir=str(package), blender_dir=str(tmp_path), blender_python=sys.executable,
                   blenderproc_executable=str(config.runtime.blenderproc_executable))
    def refuse(name):
        raise AssertionError('No module should be imported before cache check: ' + name)
    monkeypatch.setattr('bproc_runtime.preflight.importlib.import_module', refuse)
    assert any('离线缓存' in error for error in probe(locator, config))


def test_incomplete_job_launcher_fails_without_starting_child(tmp_path):
    source = Path(__file__).parents[2] / 'src/bproc_export/templates/run_job.py'
    launcher = tmp_path / 'run_job.py'
    launcher.write_text(source.read_text())
    (tmp_path / 'launch.json').write_text(json.dumps({'schema_version': 1, 'ready': False}))
    result = subprocess.run([sys.executable, str(launcher)], capture_output=True, text=True)
    assert result.returncode == 1 and '未完成' in result.stderr


def test_overrides_are_applied_before_validation(tmp_path):
    import argparse
    args = argparse.Namespace(format='rgb_masks', output=tmp_path, num_samples=3)
    config = apply_overrides(ProjectConfig(), args)
    assert config.output.format == 'rgb_masks' and config.total_shots == 4 and config.output.root == tmp_path
    assert config.scene_count == 1


def test_launcher_installs_handlers_before_spawn_and_isolates_process_group(monkeypatch):
    import signal
    source = Path(__file__).parents[2] / 'src/bproc_export/templates/run_job.py'
    spec = importlib.util.spec_from_file_location('portable_launcher_test', source)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    previous = signal.getsignal(signal.SIGINT)
    class Child:
        def __init__(self, command, env, start_new_session):
            assert start_new_session is True
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            # Exercise the startup window before Popen has returned its object.
            handler(signal.SIGINT, None)
            self.signals = []
        def send_signal(self, signum):
            self.signals.append(signum)
        def poll(self):
            return None
        def wait(self):
            assert self.signals == [signal.SIGINT]
            return 130
    monkeypatch.setattr(launcher.subprocess, 'Popen', Child)
    assert launcher._run_child(['unused'], {}) == 130
    assert signal.getsignal(signal.SIGINT) == previous


def test_preflight_rejects_resource_changed_to_point_cloud(tmp_path, monkeypatch, capsys):
    from tests.factories import make_project
    from bproc_core.project_io import save_project
    from bproc_runtime.preflight import main
    config = make_project(tmp_path / 'inputs')
    config.targets[0].asset.path.write_text('ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 0\n')
    project_path = tmp_path / 'scene.yaml'
    save_project(config, project_path)
    locator = tmp_path / 'launch.json'
    locator.write_text(json.dumps({'schema_version': 1, 'ready': True, 'custom_packages': str(tmp_path)}))
    monkeypatch.setattr('bproc_runtime.preflight.probe', lambda locator, config: [])
    assert main(['--launch', str(locator), '--config', str(project_path)]) == 1
    assert '没有可渲染的面' in capsys.readouterr().err
