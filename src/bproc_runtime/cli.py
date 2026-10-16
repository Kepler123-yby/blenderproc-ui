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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='生成同场景多目标数据集')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--format', choices=['rgb_masks', 'bop'])
    parser.add_argument('--num-samples', type=int)
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
        from .run import run_project
        dataset = reserve_dataset(config.output.root, config.output.dataset_name)
        backend = BlenderBackend()
        writer = (BopWriter(dataset, config, meshes, backend) if config.output.format == 'bop'
                  else RgbMasksWriter(dataset, config))
        recorder = JsonRunRecorder(dataset / 'run_metadata')
        summary = run_project(config, meshes, backend, writer, recorder)
        print(json.dumps(asdict(summary), ensure_ascii=False), flush=True)
        return summary.exit_code
    except Exception as exc:
        print(f'任务失败：{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
