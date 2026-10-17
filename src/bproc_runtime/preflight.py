"""Read-only probe, run by Blender's Python before BlenderProc can install anything."""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def default_requirements(package_dir: Path) -> list[str]:
    tree = ast.parse((package_dir / 'python/utility/DefaultConfig.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'default_pip_packages' for t in node.targets):
            result = ast.literal_eval(node.value)
            if isinstance(result, list) and all(isinstance(x, str) for x in result):
                return result
    raise ValueError('无法静态读取 BlenderProc 默认依赖；不兼容的版本')


def apply_overrides(config, args):
    from dataclasses import replace
    if args.format is not None:
        config = replace(config, output=replace(config.output, format=args.format))
    if args.output is not None:
        config = replace(config, output=replace(config.output, root=Path(args.output).expanduser().resolve()))
    if args.num_samples is not None:
        config = config.with_total_shots(args.num_samples)  # total frames, not scenes
    return config


def probe(locator: dict, config) -> list[str]:
    errors = []
    package = Path(locator['blenderproc_package_dir'])
    version_tree = ast.parse((package / 'version.py').read_text(encoding='utf-8'))
    versions = [ast.literal_eval(n.value) for n in version_tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets)]
    if versions != ['2.8.0']:
        errors.append(f'适配器需要 BlenderProc 2.8.0，实际为 {versions}')
    for key in ('blender_dir', 'blenderproc_executable'):
        if Path(getattr(config.runtime, key)).resolve() != Path(locator[key]).resolve():
            errors.append(f'runtime.{key} 已改变，请重新导出任务')
    if Path(sys.executable).resolve() != Path(locator['blender_python']).resolve():
        errors.append('预检必须使用任务指定的 Blender Python')
    cache_path = Path(locator['blender_dir']) / 'custom-python-packages/installed_packages_cache_v2.json'
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return errors + [f'缺少或无法读取离线缓存：{cache_path}；请人工准备 BlenderProc 环境，禁止自动修复']
    if not isinstance(cache, dict):
        return errors + ['BlenderProc 离线缓存必须是 JSON 对象']
    for requirement in default_requirements(package):
        name, _, required = requirement.partition('==')
        key = name.lower()
        if key not in cache or required and cache[key] != required:
            errors.append(f'离线缓存不匹配：{requirement}')
        try:
            actual = importlib.metadata.version(name)
            if required and actual != required:
                errors.append(f'Blender 依赖版本不匹配：{name} 需要 {required}，实际 {actual}')
        except importlib.metadata.PackageNotFoundError:
            errors.append(f'Blender 缺少依赖：{requirement}')
    modules = ['requests', 'numpy', 'yaml', 'PIL', 'imageio', 'git', 'skimage', 'png', 'scipy',
               'matplotlib', 'pytz', 'h5py', 'cv2', 'sklearn', 'dateutil', 'rich', 'trimesh', 'pyrender']
    if config.output.format == 'bop':
        if config.runtime.toolkit_path:
            sys.path.insert(0, str(config.runtime.toolkit_path))
        modules += ['bop_toolkit_lib', 'bop_toolkit_lib.misc', 'bop_toolkit_lib.visibility', 'OpenGL.EGL']
    for module in modules:
        try:
            loaded = importlib.import_module(module)
            location = getattr(loaded, '__file__', None)
            roots = [Path(locator['blender_dir']).resolve()]
            if module.startswith('bop_toolkit_lib') and config.runtime.toolkit_path:
                roots.append(Path(config.runtime.toolkit_path).resolve())
            if location and not any(Path(location).resolve().is_relative_to(root) for root in roots):
                errors.append(f'依赖来自 Blender 以外的环境：{module}: {location}')
        except Exception as exc:
            errors.append(f'无法导入 Blender 依赖 {module}：{exc}')
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description='BlenderProc 只读离线预检')
    parser.add_argument('--launch', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--format', choices=['rgb_masks', 'bop'])
    parser.add_argument('--num-samples', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        locator = json.loads(args.launch.read_text(encoding='utf-8'))
        if locator.get('schema_version') != 1 or locator.get('ready') is not True:
            raise ValueError('任务未完成导出')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        sys.path.append(locator['custom_packages'])
        os.environ.setdefault('PYOPENGL_PLATFORM', 'egl')
        from bproc_core.project_io import load_project
        from bproc_core.config import validate_project
        config = apply_overrides(load_project(args.config), args)
        errors = [f'{e.field}: {e.message}' for e in validate_project(config) if e.severity == 'error']
        errors.extend(probe(locator, config))
        if not errors:
            from bproc_core.assets import load_mesh, AssetError
            for asset in [*(t.asset for t in config.targets if t.enabled), *config.occluders]:
                try:
                    load_mesh(asset)
                except AssetError as exc:
                    errors.append(f'资源 {asset.key}：{exc}')
        if errors:
            print('离线预检未通过：\n' + '\n'.join('  • ' + error for error in errors), file=sys.stderr)
            print('请在 bop 环境中人工检查 blenderproc pip install；本任务不会安装或改写运行环境。', file=sys.stderr)
            return 1
        print(f'离线预检通过：BlenderProc 2.8.0 / {config.output.format} / {config.total_shots} 张')
        return 0
    except Exception as exc:
        print('离线预检失败：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
