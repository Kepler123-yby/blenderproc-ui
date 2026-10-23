#!/usr/bin/env python3
"""Open the project's current candidate scene in the Blender GUI."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bproc_core.assets import load_mesh
from bproc_core.config import ConfigError
from bproc_core.project_io import load_project
from bproc_core.scene import Rejection, candidate_from_dict
from bproc_core.preview import candidate_for_preview
from bproc_export.blender_view import open_gui, prepare_scene


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, help="candidate.json；缺省时按 --scene/--view 采样一个")
    parser.add_argument("--scene", type=int, default=0, help="场景序号，与 UI 的试渲染序号一致")
    parser.add_argument("--view", type=int, default=0, help="该场景内的视角序号")
    parser.add_argument("--workspace", type=Path, help="快照与日志目录，默认为项目同级的 .blender-view/")
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("blender_view.py"))
    parser.add_argument("--geometry-only", action="store_true", help="只看几何；渲染按钮仍严格要求 GPU")
    arguments = parser.parse_args(argv)

    config = load_project(arguments.project)
    if arguments.candidate:
        candidate = candidate_from_dict(json.loads(arguments.candidate.read_text(encoding="utf-8")))
    else:
        assets = [t.asset for t in config.targets if t.enabled] + list(config.occluders)
        meshes = {asset.key: load_mesh(asset) for asset in assets}
        candidate = candidate_for_preview(config, meshes, arguments.scene, arguments.view)
        if isinstance(candidate, Rejection):
            print(f"没有可用候选：{candidate.reason}: {candidate.detail}", file=sys.stderr)
            return 1
    workspace = arguments.workspace or Path(arguments.project).expanduser().absolute().parent / ".blender-view"
    try:
        options = {"geometry_only": True} if arguments.geometry_only else {}
        blend = prepare_scene(config, candidate, workspace, arguments.script, **options)
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 1
    print(f"场景已构建 {blend}")
    session = open_gui(config, blend, arguments.script, workspace)
    print(f"已在 Blender 中打开 · 日志 {session.log}")
    print(session.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
