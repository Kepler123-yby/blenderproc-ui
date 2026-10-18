#!/usr/bin/env python3
"""Export the demo project to a fresh job directory and optionally render it."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bproc_core.config import validate_project
from bproc_core.project_io import load_project
from bproc_export.bundle import export_job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="源 demo.yaml")
    parser.add_argument("--destination", type=Path, required=True, help="新任务目录，必须不存在")
    parser.add_argument("--format", choices=["rgb_masks", "bop"], required=True)
    parser.add_argument("--output-root", type=Path, help="结果父目录，默认为任务目录旁的 results/")
    parser.add_argument("--dataset-name", help="数据集名称，默认为 demo-<格式>")
    counting = parser.add_mutually_exclusive_group()
    counting.add_argument("--num-samples", type=int,
                          help="只渲染前 N 张；按视角取整，例如 1/4/5 张在 3 视角时实际是 3/3/6 张")
    counting.add_argument("--num-scenes", type=int, help="只渲染前 N 个场景，每个场景的视角数不变")
    parser.add_argument("--device", help="覆盖 Cycles 后端，例如 OPTIX / CUDA")
    parser.add_argument("--run", action="store_true", help="导出后立即执行任务命令")
    arguments = parser.parse_args(argv)

    destination = arguments.destination.expanduser().absolute()
    if destination.exists():
        parser.error(f"destination already exists: {destination}")
    project = load_project(arguments.project)
    output_root = arguments.output_root
    if output_root is None:
        output_root = destination.parent / "results"
    output = replace(project.output, format=arguments.format,
                     dataset_name=arguments.dataset_name or f"demo-{arguments.format}",
                     root=output_root.expanduser().absolute())
    render = project.render
    if arguments.device:
        render = replace(render, device=arguments.device)
    config = replace(project, output=output, render=render)
    if arguments.num_scenes is not None:
        if arguments.num_scenes < 0:
            parser.error("--num-scenes 必须是非负整数")
        config = replace(config, scene_count=arguments.num_scenes)
    elif arguments.num_samples is not None:
        config = config.with_total_shots(arguments.num_samples)   # frames, rounded up to a scene
    issues = validate_project(config)
    errors = [issue for issue in issues if issue.severity == "error"]
    for issue in issues:
        if issue.severity != "error":
            print(f"配置提醒：{issue.field}：{issue.message}", file=sys.stderr)
    if errors:
        for issue in errors:
            print(f"配置错误：{issue.field}：{issue.message}", file=sys.stderr)
        return 1
    print(f"渲染规模：{config.scene_count} 个场景 × {config.views_per_scene} 视角 = {config.total_shots} 张", flush=True)
    result = export_job(config, destination)
    print(f"任务目录：{result.directory}")
    print(f"运行命令：{result.command}")
    if not arguments.run:
        return 0
    # The snapshot already carries the counting overrides, so --run equals the printed command.
    print("开始渲染…", flush=True)
    return subprocess.call(shlex.split(result.command))


if __name__ == "__main__":
    raise SystemExit(main())
