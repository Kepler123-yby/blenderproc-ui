"""Blender-side command dispatch. Importing this module does not import bpy."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from bproc_core.assets import load_mesh
from bproc_core.config import ConfigError, validate_project
from bproc_core.project_io import load_project
from .preflight import apply_overrides


def _terminal_status(exc: BaseException):
    """Map an early failure onto the status and exit code the run would have reported."""
    from .run import RunTerminated
    if isinstance(exc, KeyboardInterrupt):
        return 'interrupted', 130
    if isinstance(exc, RunTerminated):
        return 'interrupted', 143
    return 'failed', 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='生成同场景多目标数据集')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--format', choices=['rgb_masks', 'bop'])
    counting = parser.add_mutually_exclusive_group()
    counting.add_argument('--num-samples', type=int, help='只渲染前 N 张（按视角截断）')
    counting.add_argument('--num-scenes', type=int, help='只渲染前 N 个场景（每场景视角数不变）')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        config = apply_overrides(load_project(args.config), args)
        errors = [e for e in validate_project(config) if e.severity == 'error']
        if errors:
            raise ConfigError('\n'.join(f'{e.field}: {e.message}' for e in errors))
        # Load every resource before reserving output, so invalid meshes do not leave a run.
        assets = [t.asset for t in config.targets if t.enabled] + list(config.occluders)
        meshes = {asset.key: load_mesh(asset) for asset in assets}
        from .blender_backend import BlenderBackend
        from .output import reserve_dataset, JsonRunRecorder
        from .rgb_masks import RgbMasksWriter
        from .bop_output import BopWriter
        from .run import run_project, RunTerminated
        from .output import write_terminal_status
        dataset = reserve_dataset(config.output.root, config.output.dataset_name)
        recorder = None
        try:
            backend = BlenderBackend()
            writer = (BopWriter(dataset, config, meshes, backend) if config.output.format == 'bop'
                      else RgbMasksWriter(dataset, config))
            recorder = JsonRunRecorder(dataset / 'run_metadata')
            summary = run_project(config, meshes, backend, writer, recorder)
        except BaseException as exc:
            # Nobody else can finalize a dataset whose backend/writer never got built.
            if recorder is None or not recorder.begun or not recorder.finished:
                status, code = _terminal_status(exc)
                try:
                    write_terminal_status(dataset, status, exit_code=code, detail=f'{type(exc).__name__}: {exc}')
                except Exception as write_error:
                    print(f'写入最终状态失败：{type(write_error).__name__}: {write_error}', file=sys.stderr, flush=True)
            raise
        print(json.dumps(asdict(summary), ensure_ascii=False), flush=True)
        return summary.exit_code
    except BaseException as exc:
        print(f'任务失败：{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        if isinstance(exc, KeyboardInterrupt):
            return 130
        from .run import RunTerminated
        if isinstance(exc, RunTerminated):
            return 143
        return 1


if __name__ == '__main__':
    sys.exit(main())
