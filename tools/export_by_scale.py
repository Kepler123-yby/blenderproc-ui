#!/usr/bin/env python3
"""Export one BlenderProc job per target-size class.

A dataset has one BOP GT depth tolerance and one occlusion-ratio window. Targets that differ by
more than 10x cannot share those format-level values, so this entry splits the project into
separate jobs (``<job>-size-1``, ``<job>-size-2``, ...) and leaves ordinary projects untouched.

    .venv/bin/python tools/export_by_scale.py project.yaml .artifacts/render_job
    .venv/bin/python tools/export_by_scale.py project.yaml .artifacts/render_job --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bproc_core.assets import load_mesh
from bproc_core.project_io import load_project
from bproc_core.scale_groups import MIXED_SCALE_FACTOR, split_by_scale
from bproc_export.bundle import export_grouped_jobs


def _summary(groups) -> dict:
    rows = []
    for group in groups:
        rows.append({
            "group": group.index + 1,
            "label": group.label,
            "ratio": round(group.ratio, 3),
            "dataset": group.config.output.dataset_name,
            "anchor": group.config.anchor_key,
            "targets": [
                {
                    "key": target.asset.key,
                    "name": target.name,
                    "obj_id": target.obj_id,
                    "extent_mm": round(extent * 1000, 3),
                }
                for target, extent in zip(group.targets, group.extents_m)
            ],
        })
    return {"factor": MIXED_SCALE_FACTOR, "groups": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按目标尺寸拆分并导出 BlenderProc 任务")
    parser.add_argument("project", type=Path, help="项目 YAML")
    parser.add_argument("destination", type=Path, help="新任务目录；多组时自动加 -size-N 后缀")
    parser.add_argument("--dry-run", action="store_true", help="只打印分组，不写任务目录")
    args = parser.parse_args(argv)

    config = load_project(args.project)
    meshes = {target.asset.key: load_mesh(target.asset)
              for target in config.targets if target.enabled}
    groups = split_by_scale(config, meshes)
    if args.dry_run:
        print(json.dumps(_summary(groups), ensure_ascii=False, indent=2))
        return 0

    results = export_grouped_jobs(config, meshes, args.destination)
    payload = _summary(groups)
    for row, result in zip(payload["groups"], results):
        row["job"] = str(result.directory)
        row["command"] = result.command
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
