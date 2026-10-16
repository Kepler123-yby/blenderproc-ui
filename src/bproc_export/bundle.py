from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import json
import os
import shutil

from bproc_core.config import ConfigError, ProjectConfig, validate_project
from bproc_core.assets import load_mesh, AssetError
from bproc_core.project_io import save_project
from .commands import build_command


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    command: str
    preview_command: str


def _absolute_config(config: ProjectConfig) -> ProjectConfig:
    def asset(a):
        return replace(a, path=a.path.expanduser().resolve())
    runtime = config.runtime
    return replace(config, targets=tuple(replace(t, asset=asset(t.asset)) for t in config.targets),
                   occluders=tuple(asset(a) for a in config.occluders),
                   hdri=replace(config.hdri, files=tuple(p.expanduser().resolve() for p in config.hdri.files)),
                   output=replace(config.output, root=config.output.root.expanduser().resolve()),
                   runtime=replace(runtime, blender_dir=runtime.blender_dir.expanduser().resolve(),
                                   blenderproc_executable=runtime.blenderproc_executable.expanduser().resolve(),
                                   toolkit_path=runtime.toolkit_path.expanduser().resolve() if runtime.toolkit_path else None))


def locate_runtime(config: ProjectConfig) -> dict:
    runtime = config.runtime
    blender = runtime.blender_dir
    pythons = sorted(blender.glob('*/python/bin/python3.11'))
    if len(pythons) != 1:
        raise ConfigError(f'需要 Blender 4.2 的唯一 Python 3.11：{blender}')
    executable = runtime.blenderproc_executable
    packages = sorted(executable.parent.parent.glob('lib/python*/site-packages/blenderproc'))
    if len(packages) != 1:
        raise ConfigError(f'无法在 BlenderProc 可执行文件所属环境定位包：{executable}；请选择 bop/bin/blenderproc')
    return dict(schema_version=1, ready=True, blender_dir=str(blender),
                blender_python=str(pythons[0]),
                custom_packages=str(blender / 'custom-python-packages/lib/python3.11/site-packages'),
                blenderproc_executable=str(executable), blenderproc_package_dir=str(packages[0]))


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def export_job(config: ProjectConfig, destination: Path) -> ExportResult:
    config = _absolute_config(config)
    errors = [e for e in validate_project(config) if e.severity == 'error']
    if errors:
        raise ConfigError('\n'.join(f'{e.field}: {e.message}' for e in errors))
    for asset in [*(t.asset for t in config.targets if t.enabled), *config.occluders]:
        try:
            load_mesh(asset)
        except AssetError as exc:
            raise ConfigError(f'资源 {asset.key} 无法导出：{exc}') from exc
    # Keep the final component lexical until mkdir, so a dangling symlink remains a conflict.
    destination = Path(destination).expanduser().absolute()
    destination = destination.parent.resolve() / destination.name
    output = (config.output.root / config.output.dataset_name).resolve()
    inputs = [t.asset.path for t in config.targets] + [a.path for a in config.occluders] + list(config.hdri.files)
    if _contains(destination, output) or _contains(output, destination):
        raise ConfigError('导出任务目录与数据集目录不能互相包含')
    for resource in inputs:
        if _contains(destination, resource) or _contains(output, resource):
            raise ConfigError(f'任务或数据集目录不能包含输入资源：{resource}')
    locator = locate_runtime(config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()  # Atomic reservation, including empty directories and dangling links.
    except FileExistsError as exc:
        raise ConfigError(f'导出目录已存在，请选择新目录：{destination}') from exc
    stage = destination / '.building'
    stage.mkdir()
    try:
        source = Path(__file__).resolve().parents[1]
        for package in ('bproc_core', 'bproc_runtime'):
            shutil.copytree(source / package, stage / package,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('run_job.py', 'render_scene.py'):
            shutil.copyfile(Path(__file__).parent / 'templates' / name, stage / name)
        save_project(config, stage / 'scene.yaml')
        (stage / 'README.txt').write_text(
            '终端运行 run_job.py；资源路径是绝对路径，请保留原资源。\n'
            '先运行 --check-only 检查离线环境；--num-samples 4 --output <新父目录> 可试渲染。\n'
            '科学参数可直接修改 scene.yaml；变更 Blender/BlenderProc 路径后请重新导出。\n', encoding='utf-8')
        for child in stage.iterdir():
            os.replace(child, destination / child.name)
        stage.rmdir()
        # Published last: an interrupted export cannot accidentally launch.
        (destination / '.launch.tmp').write_text(json.dumps(locator, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(destination / '.launch.tmp', destination / 'launch.json')
    except BaseException:
        (destination / 'EXPORT_INCOMPLETE.txt').write_text('导出未完成，请使用新的任务目录重新导出。\n', encoding='utf-8')
        raise
    return ExportResult(destination, build_command(destination, config.runtime.conda_env),
                        build_command(destination, config.runtime.conda_env, num_samples=4,
                                      output=config.output.root / 'preview'))
