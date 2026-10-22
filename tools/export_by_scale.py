#!/usr/bin/env python3
"""Export one BlenderProc job per target-size class.

This optional entry splits targets differing by more than 10x into separate jobs
(``<job>-size-1``, ``<job>-size-2``, ...) for independent framing and BOP depth-tolerance selection.
Mixed scales can also share one scene. Ordinary single-group projects are left untouched.

    .venv/bin/python tools/export_by_scale.py project.yaml .artifacts/render_job
    .venv/bin/python tools/export_by_scale.py project.yaml .artifacts/render_job --dry-run
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import sys
from pathlib import Path

from bproc_core.assets import load_mesh
from bproc_core.config import ScaleGroupingSpec
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
            "reframe": bool(group.grouping and group.grouping.get("reframe")),
            "scale_factor": None if not group.grouping else group.grouping.get("scale_factor"),
            "cameras_before_after": [] if not group.grouping else group.grouping.get("cameras", []),
            "light_change": None if not group.grouping else group.grouping.get("light"),
            "clamps": [] if not group.grouping else group.grouping.get("clamps", []),
        })
    return {"factor": MIXED_SCALE_FACTOR, "groups": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按目标尺寸拆分并导出 BlenderProc 任务")
    parser.add_argument("project", type=Path, help="项目 YAML")
    parser.add_argument("destination", type=Path, help="新任务目录；多组时自动加 -size-N 后缀")
    parser.add_argument("--dry-run", action="store_true", help="只打印分组，不写任务目录")
    reframe = parser.add_mutually_exclusive_group()
    reframe.add_argument("--reframe-groups", dest="reframe", action="store_true",
                         help="按组重新取景（仅在实际分成多组时生效）")
    reframe.add_argument("--no-reframe-groups", dest="reframe", action="store_false",
                         help="保持原绝对相机设置（默认沿用 YAML）")
    parser.set_defaults(reframe=None)
    parser.add_argument("--framing-margin", type=float,
                        help="重取景边距占画幅比例，0 到 0.45（不含）")
    args = parser.parse_args(argv)

    config = load_project(args.project)
    if args.reframe is not None or args.framing_margin is not None:
        grouping = config.scale_grouping
        if args.reframe is not None:
            grouping = replace(grouping, reframe=args.reframe)
        if args.framing_margin is not None:
            if not 0 <= args.framing_margin < 0.45:
                parser.error("--framing-margin 必须位于 0 到 0.45（不含）")
            grouping = replace(grouping, margin_fraction=args.framing_margin)
        config = replace(config, scale_grouping=grouping)
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
