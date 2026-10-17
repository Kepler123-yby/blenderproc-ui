#!/usr/bin/env python3
"""Open the project's current candidate scene in the Blender GUI."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bproc_core.assets import load_mesh
from bproc_core.project_io import load_project
from bproc_core.scene import Rejection, candidate_from_dict, propose_candidate
from bproc_export.blender_view import open_gui, prepare_scene


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, help="candidate.json；缺省时按 --request 采样一个")
    parser.add_argument("--request", type=int, default=0, help="采样序号，与 UI 的试渲染序号一致")
    parser.add_argument("--workspace", type=Path, help="快照与日志目录，默认为项目同级的 .blender-view/")
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("blender_view.py"))
    arguments = parser.parse_args(argv)

    config = load_project(arguments.project)
    if arguments.candidate:
        candidate = candidate_from_dict(json.loads(arguments.candidate.read_text(encoding="utf-8")))
    else:
        assets = [t.asset for t in config.targets if t.enabled] + list(config.occluders)
        meshes = {asset.key: load_mesh(asset) for asset in assets}
        candidate = None
        for attempt in range(60):
            proposal = propose_candidate(config, meshes, arguments.request, attempt)
            if not isinstance(proposal, Rejection):
                candidate = proposal
                break
        if candidate is None:
            print("没有可用候选机位；先调大距离或位置范围", file=sys.stderr)
            return 1
    workspace = arguments.workspace or Path(arguments.project).expanduser().absolute().parent / ".blender-view"
    blend = prepare_scene(config, candidate, workspace, arguments.script)
    print(f"场景已构建 {blend}")
    session = open_gui(config, blend, arguments.script, workspace)
    print(f"已在 Blender 中打开 · 日志 {session.log}")
    print(session.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
