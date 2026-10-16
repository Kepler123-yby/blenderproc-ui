#!/usr/bin/env python3
"""Create local, redistributable meshes and a tiny HDRI without downloads."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bproc_core.config import (AssetSpec, Azimuth, Box, CameraSpec, HdriSpec,
    OcclusionSpec, OutputSpec, ProjectConfig, Range, RegionSpec, RenderSpec, RuntimeSpec, TargetSpec)
from bproc_core.project_io import save_project


def _mesh(path, vertices, faces, color):
    with path.open("x", encoding="ascii") as stream:
        stream.write("ply\nformat ascii 1.0\n")
        stream.write(f"element vertex {len(vertices)}\nproperty float x\nproperty float y\nproperty float z\n")
        stream.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
        stream.write(f"element face {len(faces)}\nproperty list uchar int vertex_indices\nend_header\n")
        for point in vertices:
            stream.write(" ".join(map(str, (*point, *color))) + "\n")
        for face in faces:
            stream.write("3 " + " ".join(map(str, face)) + "\n")


def create_demo(root: Path, blender: Path) -> Path:
    root = Path(root).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "demo.yaml").exists():
        raise FileExistsError(f"Demo already exists: {root}")
    # Asymmetric wedge, in mm, with a deliberately displaced CAD origin.
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    vertices = [(60,-80,-15),(140,-80,-15),(140,-20,-15),(60,-20,-15),
                (60,-80,65),(140,-80,65),(120,-20,45),(60,-20,65)]
    _mesh(root / "target_mm.ply", vertices, faces, (230, 132, 48))
    # A triangular prism, in cm, with another displaced origin.
    vertices7 = [(-5,6,-2),(-1,6,-2),(-4,10,-2),(-5,6,4),(-1,6,4),(-4,10,4)]
    faces7 = [(0,2,1),(3,4,5),(0,1,4),(0,4,3),(1,2,5),(1,5,4),(2,0,3),(2,3,5)]
    _mesh(root / "target_cm.ply", vertices7, faces7, (65, 148, 215))
    _mesh(root / "occluder.ply", [(x/100, y/100, z/100) for x,y,z in
        [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]],
        faces, (110, 160, 103))
    script = root / "_create_hdri.py"
    script.write_text("""import bpy
import math
import sys
image = bpy.data.images.new('Demo environment', width=64, height=32, float_buffer=True)
pixels = []
for y in range(32):
    for x in range(64):
        t = y / 31
        bright = 3 if 8 < x < 18 and 18 < y < 27 else 0
        pixels.extend((0.12 + .25*t + bright, .16 + .28*t + bright, .22 + .35*t + bright, 1.0))
image.pixels = pixels
image.file_format = 'HDR'
image.filepath_raw = sys.argv[sys.argv.index('--') + 1]
image.save()
""", encoding="utf-8")
    executable = Path(blender)
    if executable.is_dir():
        executable /= "blender"
    try:
        subprocess.run([str(executable), "--background", "--factory-startup", "--python", str(script),
                        "--", str(root / "studio.hdr")], check=True, timeout=60)
    finally:
        script.unlink(missing_ok=True)
    target2 = TargetSpec(AssetSpec("target-2", root / "target_mm.ply", "mm"), "楔形目标", 2)
    target7 = TargetSpec(AssetSpec("target-7", root / "target_cm.ply", "cm"), "三角目标", 7,
        offset=Box(Range(-.02,.02), Range(.12,.16), Range(-.02,.02)))
    toolkit = Path.home() / "blenderproc/23AutoEngineer/Linemod-Render/bop_toolkit"
    config = ProjectConfig(targets=(target2, target7), anchor_key="target-2",
        occluders=(AssetSpec("occluder", root / "occluder.ply", "m"),),
        regions=(RegionSpec("front", "正面", Range(.55,.65), Azimuth(-25,25,False), Range(15,35)),),
        hdri=HdriSpec(files=(root / "studio.hdr",)), num_samples=4,
        render=RenderSpec(samples=8), output=OutputSpec(root=root / "renders", dataset_name="demo-rgb"),
        runtime=RuntimeSpec(blender_dir=executable.parent, toolkit_path=toolkit if toolkit.is_dir() else None))
    save_project(config, root / "demo.yaml")
    return root / "demo.yaml"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--blender", type=Path, default=RuntimeSpec().blender_dir)
    arguments = parser.parse_args()
    print(create_demo(arguments.root, arguments.blender))
