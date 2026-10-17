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
    parser.add_argument("--num-samples", type=int, help="覆盖场景数量")
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
    problems = validate_project(config)
    if problems:
        for problem in problems:
            print(f"配置错误：{problem}", file=sys.stderr)
        return 1
    result = export_job(config, destination)
    print(f"任务目录：{result.directory}")
    print(f"运行命令：{result.command}")
    if not arguments.run:
        return 0
    command = shlex.split(result.command)
    if arguments.num_samples is not None:
        command += ["--num-samples", str(arguments.num_samples)]
    print("开始渲染…", flush=True)
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
