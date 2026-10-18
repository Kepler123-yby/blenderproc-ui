"""`tools/render_demo.py` must block on errors, only warn otherwise, and export what it prints."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

import pytest

from bproc_core.config import ProjectConfig, ValidationIssue
from tests.factories import make_project

ROOT = Path(__file__).parents[2]


def load_tool():
    spec = importlib.util.spec_from_file_location('render_demo_tool', ROOT / 'tools' / 'render_demo.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Exported:
    def __init__(self, config, directory):
        self.config, self.directory = config, directory
        self.command, self.preview_command = 'true', 'true --preview'


@pytest.fixture
def tool(monkeypatch, tmp_path):
    module = load_tool()
    captured = {}

    def fake_export(config, destination):
        captured['config'], captured['destination'] = config, destination
        return Exported(config, destination)

    monkeypatch.setattr(module, 'export_job', fake_export)
    monkeypatch.setattr(module, 'load_project', lambda path: make_project(tmp_path / 'assets'))
    return module, captured


def run(tool_module, tmp_path, *extra, issues=()):
    module, captured = tool_module
    module.validate_project = lambda config: tuple(issues)
    return module.main(['--project', str(tmp_path / 'demo.yaml'), '--destination', str(tmp_path / 'job'),
                        '--format', 'rgb_masks', *extra]), captured


def test_scene_count_override_reaches_the_exported_snapshot(tool, tmp_path):
    code, captured = run(tool, tmp_path, '--num-scenes', '1')
    assert code == 0
    assert captured['config'].scene_count == 1
    assert captured['config'].total_shots == captured['config'].views_per_scene


def test_sample_count_override_rounds_up_to_a_whole_scene(tool, tmp_path):
    code, captured = run(tool, tmp_path, '--num-samples', '4')
    config = captured['config']
    assert code == 0 and config.total_shots >= 4
    assert config.total_shots == config.scene_count * config.views_per_scene


def test_warnings_do_not_block_the_export(tool, tmp_path, capsys):
    code, captured = run(tool, tmp_path, issues=[ValidationIssue('occluders', '只能按无遮挡接收', 'warning')])
    assert code == 0 and 'config' in captured
    assert '配置提醒' in capsys.readouterr().err


def test_errors_block_the_export(tool, tmp_path, capsys):
    code, captured = run(tool, tmp_path, issues=[ValidationIssue('targets', '至少需要一个启用目标')])
    assert code == 1 and 'config' not in captured
    assert '配置错误' in capsys.readouterr().err


def test_two_counting_options_are_rejected(tool, tmp_path):
    module, _ = tool
    with pytest.raises(SystemExit):
        module.main(['--project', str(tmp_path / 'd.yaml'), '--destination', str(tmp_path / 'j'),
                     '--format', 'rgb_masks', '--num-scenes', '1', '--num-samples', '4'])
