"""Load canonical meshes once, converting units without changing their origins."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
import json
import math

import numpy as np
import trimesh

from .config import AssetSpec, Box, CameraSpec, Range, TargetSpec


class AssetError(ValueError):
    """A resource cannot be used as an opaque surface mesh."""


@dataclass(frozen=True)
class MeshData:
    key: str
    vertices_m: np.ndarray
    faces: np.ndarray
    vertex_rgba: np.ndarray | None
    bounds_m: np.ndarray


def _reject_transparency(mesh: trimesh.Trimesh, path: Path) -> None:
    visual = mesh.visual
    if getattr(visual, "kind", None) in ("vertex", "face") and getattr(visual, "defined", False):
        colors = np.asarray(visual.vertex_colors)
        if colors.ndim == 2 and colors.shape[1] == 4 and np.any(colors[:, 3] != 255):
            raise AssetError(f"首版不支持透明表面，请将模型设为不透明：{path}")
    material = getattr(visual, "material", None)
    if material is not None:
        if getattr(material, "alphaMode", "OPAQUE") not in (None, "OPAQUE"):
            raise AssetError(f"首版不支持透明材质：{path}")
        color = getattr(material, "baseColorFactor", None)
        if color is None:
            color = getattr(material, "diffuse", None)
        if color is not None and len(color) == 4:
            alpha = float(color[3])
            maximum = 1.0 if np.asarray(color).dtype.kind == "f" and np.max(color) <= 1 else 255.0
            if alpha < maximum:
                raise AssetError(f"首版不支持透明材质：{path}")
        image = getattr(material, "image", None)
        if image is not None and getattr(image, "mode", None) in ("RGBA", "LA"):
            if np.asarray(image.getchannel("A")).min() < 255:
                raise AssetError(f"首版不支持包含透明像素的材质：{path}")


def load_mesh(asset: AssetSpec) -> MeshData:
    factors = {"m": 1.0, "cm": 0.01, "mm": 0.001}
    if asset.unit not in factors:
        raise AssetError(f"未知模型单位 {asset.unit!r}，请选择 m、cm 或 mm")
    path = Path(asset.path).expanduser()
    if path.suffix.lower() not in (".ply", ".obj"):
        raise AssetError(f"仅支持有面的 PLY / OBJ 网格：{path}")
    if not path.is_file():
        raise AssetError(f"找不到模型文件：{path}")
    try:
        loaded = trimesh.load(path, process=False)
        if isinstance(loaded, trimesh.Scene):
            if not loaded.geometry:
                raise AssetError(f"模型为空：{path}")
            # Applies only transforms recorded by the file; never re-centers CAD data.
            loaded = loaded.dump(concatenate=True)
        if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
            raise AssetError(f"模型没有可渲染的面；仅含点的 PLY 不受支持：{path}")
        _reject_transparency(loaded, path)
        vertices = np.asarray(loaded.vertices, dtype=np.float64).copy() * factors[asset.unit]
        faces = np.asarray(loaded.faces, dtype=np.int64).copy()
        if vertices.ndim != 2 or vertices.shape[1] != 3 or len(vertices) == 0 or not np.isfinite(vertices).all():
            raise AssetError(f"模型顶点为空或包含非有限数值：{path}")
        if faces.ndim != 2 or faces.shape[1] != 3 or faces.min() < 0 or faces.max() >= len(vertices):
            raise AssetError(f"模型包含无效三角面索引：{path}")
        triangle_edges = vertices[faces[:, 1:]] - vertices[faces[:, :1]]
        if not np.any(np.linalg.norm(np.cross(triangle_edges[:, 0], triangle_edges[:, 1]), axis=1) > 0):
            raise AssetError(f"模型所有面均已退化：{path}")
        colors = None
        if getattr(loaded.visual, "kind", None) in ("vertex", "face") and getattr(loaded.visual, "defined", False):
            candidate = np.asarray(loaded.visual.vertex_colors, dtype=np.uint8)
            if candidate.shape == (len(vertices), 4):
                colors = candidate.copy()
        else:
            material = getattr(loaded.visual, "material", None)
            color = getattr(material, "baseColorFactor", None)
            if color is None:
                color = getattr(material, "diffuse", None)
            if color is not None and len(color) >= 3:
                color = np.asarray(color)
                if color.dtype.kind == "f" and color.max() <= 1:
                    color = np.rint(color * 255)
                rgba = np.array([*color[:3], 255], dtype=np.uint8)
                colors = np.tile(rgba, (len(vertices), 1))
        bounds = np.array([vertices.min(axis=0), vertices.max(axis=0)])
        return MeshData(asset.key, vertices, faces, colors, bounds)
    except AssetError:
        raise
    except Exception as exc:
        raise AssetError(f"无法读取模型 {path}：{exc}") from exc


def mesh_diameter_m(mesh: MeshData) -> float:
    """Exact maximum vertex distance, bounded memory even for planar meshes."""
    points = np.asarray(mesh.vertices_m, dtype=np.float64)
    if len(points) < 2:
        return 0.0
    # Centering only this scratch calculation improves precision; canonical data stays intact.
    points = points - points.mean(axis=0)
    _, singular, basis = np.linalg.svd(points, full_matrices=False)
    tolerance = max(points.shape) * np.finfo(float).eps * singular[0] if singular[0] else 0
    rank = int(np.count_nonzero(singular > tolerance))
    if rank <= 1:
        axis = points @ basis[0]
        return float(axis.max() - axis.min())
    try:
        from scipy.spatial import ConvexHull
        hull = ConvexHull(points @ basis[:rank].T)
        points = points[hull.vertices]
    except (ImportError, ValueError, RuntimeError):
        pass
    except Exception as exc:
        # QHull may reject coplanar/near-degenerate CAD meshes. Exact blocked fallback.
        if exc.__class__.__name__ != "QhullError":
            raise
    maximum_squared = 0.0
    block = 512
    for i in range(0, len(points), block):
        a = points[i:i + block]
        for j in range(i, len(points), block):
            b = points[j:j + block]
            delta = a[:, None, :] - b[None, :, :]
            maximum_squared = max(maximum_squared, float(np.einsum("ijk,ijk->ij", delta, delta).max()))
    return math.sqrt(maximum_squared)


def _write_ply(mesh: MeshData, path: Path) -> None:
    colors = mesh.vertex_rgba
    with path.open("x", encoding="ascii", newline="\n") as stream:
        stream.write("ply\nformat ascii 1.0\ncomment canonical model coordinates, millimeters\n")
        stream.write(f"element vertex {len(mesh.vertices_m)}\nproperty double x\nproperty double y\nproperty double z\n")
        if colors is not None:
            stream.write("property uchar red\nproperty uchar green\nproperty uchar blue\nproperty uchar alpha\n")
        stream.write(f"element face {len(mesh.faces)}\nproperty list uchar int vertex_indices\nend_header\n")
        for index, vertex in enumerate(mesh.vertices_m * 1000.0):
            line = " ".join(format(float(v), ".17g") for v in vertex)
            if colors is not None:
                line += " " + " ".join(str(int(v)) for v in colors[index])
            stream.write(line + "\n")
        for face in mesh.faces:
            stream.write("3 " + " ".join(str(int(v)) for v in face) + "\n")


def write_bop_models(meshes: Mapping[str, MeshData], targets: Sequence[TargetSpec], models_dir: Path) -> None:
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    enabled = sorted((target for target in targets if target.enabled), key=lambda item: item.obj_id)
    ids = [target.obj_id for target in enabled]
    if len(set(ids)) != len(ids) or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
        raise AssetError("BOP 对象 ID 必须是唯一正整数")
    planned = [models_dir / f"obj_{target.obj_id:06d}.ply" for target in enabled] + [models_dir / "models_info.json"]
    if any(path.exists() or path.is_symlink() for path in planned):
        raise FileExistsError(f"拒绝覆盖已有 BOP 模型：{models_dir}")
    info = {}
    for target in enabled:
        mesh = meshes[target.asset.key]
        bounds_mm = mesh.bounds_m * 1000.0
        size_mm = bounds_mm[1] - bounds_mm[0]
        info[str(target.obj_id)] = {**{f"min_{axis}": float(bounds_mm[0, i]) for i, axis in enumerate("xyz")}, **{f"size_{axis}": float(size_mm[i]) for i, axis in enumerate("xyz")}, "diameter": mesh_diameter_m(mesh) * 1000.0}
        _write_ply(mesh, models_dir / f"obj_{target.obj_id:06d}.ply")
    with (models_dir / "models_info.json").open("x", encoding="utf-8") as stream:
        json.dump(info, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def suggest_ranges(meshes: Mapping[str, MeshData], targets: Sequence[TargetSpec], camera: CameraSpec) -> tuple[Box, Range]:
    """Conservative editable placement and distance suggestions for all targets."""
    extents = [meshes[t.asset.key].bounds_m[1] - meshes[t.asset.key].bounds_m[0] for t in targets if t.enabled and t.asset.key in meshes]
    if not extents:
        return Box(), Range(0.5, 0.8)
    largest = np.max(extents, axis=0)
    spread = float(max(largest.max() * max(1.5, math.sqrt(len(extents))), 0.02))
    box = Box(Range(-spread, spread), Range(-spread, spread), Range(-0.35 * spread, 0.35 * spread))
    all_radius = np.linalg.norm([spread + largest[0] / 2, spread + largest[1] / 2, 0.35 * spread + largest[2] / 2])
    # Principal-point offsets reduce the narrower half-frustum. Half-pixel image boundaries.
    tangent = min((camera.cx + 0.5) / camera.fx, (camera.width - 0.5 - camera.cx) / camera.fx,
                  (camera.cy + 0.5) / camera.fy, (camera.height - 0.5 - camera.cy) / camera.fy)
    if tangent <= 0 or not math.isfinite(tangent):
        return box, Range(0.5, 0.8)
    minimum = max(0.05, float(all_radius) * math.sqrt(1 + tangent * tangent) / tangent * 1.1)
    return box, Range(minimum, minimum * 1.5)
