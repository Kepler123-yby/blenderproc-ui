"""Small canonical CAD fixtures; no downloaded assets or renderer required."""
from dataclasses import replace
from itertools import product
from pathlib import Path
import numpy as np

from bproc_core.assets import MeshData
from bproc_core.config import AssetSpec, Box, HdriSpec, OutputSpec, ProjectConfig, Range, RuntimeSpec, TargetSpec

# The 12 triangles of an axis-aligned box, indexed against the ``bounds_corners`` ordering used by
# the sampler. A one-triangle "box" is not a surface: it under-states a silhouette whenever the
# rasteriser is asked for real geometry, so fixtures must spell out every face.
BOX_FACES = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                      [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])


def box_surface(lo, hi):
    """Corners and all 12 triangles of an axis-aligned box, in ``bounds_corners`` order."""
    low, high = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
    return np.array(list(product(*zip(low, high))), dtype=float), BOX_FACES.copy()


def make_box(key, lo, hi, *, color=None) -> MeshData:
    """A box with its real closed surface, not a single triangle standing in for one."""
    vertices, faces = box_surface(lo, hi)
    colors = None if color is None else np.tile(np.asarray(color, dtype=np.uint8), (len(vertices), 1))
    return MeshData(key, vertices, faces, colors,
                    np.array([np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)]))


def make_mesh(key="mesh", *, center=(0.0, 0.0, 0.0), size=0.1, color=None) -> MeshData:
    vertices = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                         [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)
    vertices = vertices * np.asarray(size) / 2 + np.asarray(center)
    faces = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                      [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]], dtype=np.int64)
    colors = None if color is None else np.tile(np.asarray(color, dtype=np.uint8), (8, 1))
    return MeshData(key, vertices, faces, colors, np.array([vertices.min(axis=0), vertices.max(axis=0)]))


def write_mesh(path: Path, mesh: MeshData, *, factor=1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    has_color = mesh.vertex_rgba is not None
    with path.open("w", encoding="ascii") as stream:
        stream.write(f"ply\nformat ascii 1.0\nelement vertex {len(mesh.vertices_m)}\nproperty double x\nproperty double y\nproperty double z\n")
        if has_color:
            stream.write("property uchar red\nproperty uchar green\nproperty uchar blue\nproperty uchar alpha\n")
        stream.write(f"element face {len(mesh.faces)}\nproperty list uchar int vertex_indices\nend_header\n")
        for i, vertex in enumerate(mesh.vertices_m * factor):
            line = " ".join(map(str, vertex))
            if has_color:
                line += " " + " ".join(map(str, mesh.vertex_rgba[i]))
            stream.write(line + "\n")
        for face in mesh.faces:
            stream.write("3 " + " ".join(map(str, face)) + "\n")
    return path


def make_project(tmp_path: Path, ids=(2, 7), output_format="rgb_masks") -> ProjectConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    targets = []
    for index, obj_id in enumerate(ids):
        key = f"target-{obj_id}"
        path = write_mesh(tmp_path / f"目标 {obj_id}.ply", make_mesh(key, center=(0.02, -0.01, 0.03), size=0.05))
        asset = AssetSpec(key, path.resolve(), "m")
        offset = Box(Range(0.08 * index, 0.12 * index), Range(-0.03, 0.03), Range(-0.02, 0.02))
        targets.append(TargetSpec(asset, f"目标 {obj_id}", obj_id, True, offset))
    hdri = tmp_path / "环境.hdr"
    hdri.write_bytes(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 2 +X 2\n" + bytes([128, 128, 128, 129]) * 4)
    blender_dir = tmp_path / "blender"
    blender_dir.mkdir(exist_ok=True)
    (blender_dir / "blender").touch()
    executable = tmp_path / "blenderproc"
    executable.touch()
    return ProjectConfig(targets=tuple(targets), anchor_key=targets[0].asset.key if targets else None,
                         hdri=HdriSpec(files=(hdri.resolve(),)),
                         output=OutputSpec(format=output_format, root=(tmp_path / "output").resolve()),
                         runtime=RuntimeSpec(blender_dir=blender_dir.resolve(), blenderproc_executable=executable.resolve()))


def make_runtime(tmp_path: Path) -> RuntimeSpec:
    """A minimal on-disk Blender/BlenderProc layout that `locate_runtime` accepts."""
    blender = tmp_path / "blender"
    blender.mkdir(parents=True, exist_ok=True)
    (blender / "blender").touch()
    python = blender / "4.2/python/bin/python3.11"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    executable = tmp_path / "env/bin/blenderproc"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    (tmp_path / "env/lib/python3.8/site-packages/blenderproc").mkdir(parents=True, exist_ok=True)
    return RuntimeSpec(blender_dir=blender, blenderproc_executable=executable)
