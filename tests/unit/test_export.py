from dataclasses import replace
from pathlib import Path
import json
import shlex
import pytest

from bproc_core.config import *
from bproc_core.project_io import load_project
from bproc_export.bundle import export_job
from bproc_export.commands import build_command


@pytest.fixture
def project(tmp_path):
    mesh = tmp_path / "目标 ' $(literal).ply"
    mesh.write_text('ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n')
    hdri = tmp_path / 'studio.hdr'
    hdri.write_text('hdr')
    blender = tmp_path / 'blender'
    blender.mkdir()
    (blender / 'blender').touch()
    py = blender / '4.2/python/bin/python3.11'
    py.parent.mkdir(parents=True)
    py.touch()
    executable = tmp_path / 'env/bin/blenderproc'
    executable.parent.mkdir(parents=True)
    executable.touch()
    package = tmp_path / 'env/lib/python3.8/site-packages/blenderproc'
    package.mkdir(parents=True)
    return ProjectConfig(targets=(TargetSpec(AssetSpec('a', mesh), 'A', 7),), anchor_key='a',
                         hdri=HdriSpec(files=(hdri,)), output=OutputSpec(root=tmp_path / 'renders'),
                         runtime=RuntimeSpec(blender_dir=blender, blenderproc_executable=executable))


def test_bundle_without_an_hdri_exports_an_empty_resource_list(project, tmp_path):
    job = export_job(replace(project, hdri=HdriSpec(files=())), tmp_path / "job")
    assert load_project(job.directory / "scene.yaml").hdri.files == ()
    assert (job.directory / "render_scene.py").is_file()


def test_bundle_is_standalone_and_paths_literal(project, tmp_path):
    job = export_job(project, tmp_path / "中文 ' $(literal) job")
    assert shlex.split(job.command)[-1] == str(job.directory / 'scene.yaml')
    assert (job.directory / 'render_scene.py').read_text().splitlines()[0] == 'import blenderproc as bproc'
    assert (job.directory / 'bproc_core/camera.py').is_file()
    assert (job.directory / 'bproc_runtime/run.py').is_file()
    assert not (job.directory / 'bproc_ui').exists()
    assert json.loads((job.directory / 'launch.json').read_text())['ready'] is True
    assert load_project(job.directory / 'scene.yaml') == project
    command = build_command(job.directory, "bop ' $(false)", num_samples=4, output=tmp_path / '试 渲染')
    args = shlex.split(command)
    assert args[args.index('-n') + 1] == "bop ' $(false)"


@pytest.mark.parametrize('kind', ['directory', 'file', 'symlink', 'dangling'])
def test_existing_job_never_modified(project, tmp_path, kind):
    dest = tmp_path / 'existing'
    if kind == 'directory':
        dest.mkdir()
    elif kind == 'file':
        dest.write_text('keep')
    else:
        dest.symlink_to(tmp_path / ('missing' if kind == 'dangling' else 'studio.hdr'))
    with pytest.raises(ConfigError):
        export_job(project, dest)
    assert dest.exists() or dest.is_symlink()


def test_reject_output_overlap_and_incomplete_resources(project, tmp_path):
    with pytest.raises(ConfigError):
        export_job(project, project.output.root)
    with pytest.raises(ConfigError):
        export_job(replace(project, targets=()), tmp_path / 'bad')
    assert not (tmp_path / 'bad').exists()


def test_point_cloud_cannot_be_exported(project, tmp_path):
    project.targets[0].asset.path.write_text('ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 0\n')
    with pytest.raises(ConfigError, match='无法导出'):
        export_job(project, tmp_path / 'pointcloud-job')
    assert not (tmp_path / 'pointcloud-job').exists()
