"""Load canonical meshes once, converting units without changing their origins."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
import json
import math

import numpy as np
import trimesh

from .camera import fit_distance
from .config import AssetSpec, Box, CameraSpec, Range, TargetSpec
from .ply import PlyError, read_ply


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


def _ply_arrays(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    try:
        mesh = read_ply(path)
    except PlyError as exc:
        raise AssetError(f"无法读取 PLY 模型 {path}：{exc}") from exc
    if mesh.colors is not None and np.any(mesh.colors[:, 3] != 255):
        raise AssetError(f"首版不支持透明表面，请将模型设为不透明：{path}")
    return mesh.vertices, mesh.faces, mesh.colors


def _trimesh_arrays(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    loaded = trimesh.load(path, process=False)
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise AssetError(f"模型为空：{path}")
        # Applies only transforms recorded by the file; never re-centers CAD data.
        loaded = loaded.dump(concatenate=True)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise AssetError(f"模型没有可渲染的面；仅含点的模型不受支持：{path}")
    _reject_transparency(loaded, path)
    colors = None
    if getattr(loaded.visual, "kind", None) in ("vertex", "face") and getattr(loaded.visual, "defined", False):
        candidate = np.asarray(loaded.visual.vertex_colors, dtype=np.uint8)
        if candidate.shape == (len(loaded.vertices), 4):
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
            colors = np.tile(rgba, (len(loaded.vertices), 1))
    return np.asarray(loaded.vertices, dtype=np.float64), np.asarray(loaded.faces, dtype=np.int64), colors


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
        vertices, faces, colors = (_ply_arrays(path) if path.suffix.lower() == ".ply"
                                   else _trimesh_arrays(path))
        if len(faces) == 0:
            raise AssetError(f"模型没有可渲染的面；仅含点的模型不受支持：{path}")
        vertices = np.asarray(vertices, dtype=np.float64).copy() * factors[asset.unit]
        faces = np.asarray(faces, dtype=np.int64).copy()
        if vertices.ndim != 2 or vertices.shape[1] != 3 or len(vertices) == 0 or not np.isfinite(vertices).all():
            raise AssetError(f"模型顶点为空或包含非有限数值：{path}")
        if faces.ndim != 2 or faces.shape[1] != 3 or faces.min() < 0 or faces.max() >= len(vertices):
            raise AssetError(f"模型包含无效三角面索引：{path}")
        triangle_edges = vertices[faces[:, 1:]] - vertices[faces[:, :1]]
        if not np.any(np.linalg.norm(np.cross(triangle_edges[:, 0], triangle_edges[:, 1]), axis=1) > 0):
            raise AssetError(f"模型所有面均已退化：{path}")
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


LAYOUT_GAP_FRACTION = .1


def framing_band(bounds_m: np.ndarray, camera: CameraSpec) -> Range:
    """Distance window that keeps the whole box in frame while the camera sweeps its region."""
    minimum = fit_distance(bounds_m, camera, azimuths_deg=(-20., 0., 20.), fill=.85)
    return Range(minimum, minimum * 1.35)


def suggest_layout(meshes: Mapping[str, MeshData], targets: Sequence[TargetSpec], anchor_key: str,
                   camera: CameraSpec) -> tuple[dict[str, Box], Range]:
    """Per-target offset ranges that cannot overlap, plus the distance they need to stay framed.

    A single shared box for every target let two 10 cm cubes land on the same spot, so the sampler
    rejected whole scenes as collisions. The render centre now stays at the origin and each other
    target owns an X band on its own side of it, ordered by the target list: the nearest face of a
    band always keeps ``LAYOUT_GAP_FRACTION`` of the largest target clear of the previous one, so
    no sample can produce overlapping AABBs whatever the Y/Z jitter does.

    Returns a mapping for the enabled targets only; disabled targets keep their authored offsets.
    """
    enabled = [target for target in targets if target.enabled]
    if not enabled:
        raise ValueError("至少需要一个启用的目标")
    missing = [target.asset.key for target in enabled if target.asset.key not in meshes]
    if missing:
        raise ValueError("目标网格未载入：" + "、".join(missing))
    anchor_index = next((index for index, target in enumerate(enabled)
                         if target.asset.key == anchor_key), None)
    if anchor_index is None:
        raise ValueError("渲染中心必须是启用的目标")
    extents = {target.asset.key: np.ptp(meshes[target.asset.key].bounds_m, axis=0) for target in enabled}
    largest = max(float(extent.max()) for extent in extents.values())
    if largest <= 0:
        raise ValueError("目标的包围盒为空")
    gap = largest * LAYOUT_GAP_FRACTION
    jitter_y = largest * .15
    anchor_half = extents[enabled[anchor_index].asset.key][0] / 2
    # The render centre is the reference point every other target is placed around; a jittered
    # anchor would move the whole layout and make the offsets below meaningless.
    boxes = {enabled[anchor_index].asset.key: Box(Range(0., 0.), Range(0., 0.), Range(0., 0.))}
    left_edge, right_edge = -anchor_half - gap, anchor_half + gap
    for index, target in enumerate(enabled):
        if index == anchor_index:
            continue
        half = extents[target.asset.key][0] / 2
        width = max(largest * .5, 2 * half)
        jitter = Range(-jitter_y, jitter_y)
        depth = Range(-.35 * jitter_y, .35 * jitter_y)
        if index < anchor_index:
            near = left_edge - half - width
            boxes[target.asset.key] = Box(Range(near, near + width), jitter, depth)
            left_edge = near - half - gap
        else:
            near = right_edge + half
            boxes[target.asset.key] = Box(Range(near, near + width), jitter, depth)
            right_edge = near + width + half + gap
    low, high = np.full(3, np.inf), np.full(3, -np.inf)
    for target in enabled:
        box = boxes[target.asset.key]
        half = extents[target.asset.key] / 2
        low = np.minimum(low, np.array([box.x.minimum, box.y.minimum, box.z.minimum]) - half)
        high = np.maximum(high, np.array([box.x.maximum, box.y.maximum, box.z.maximum]) + half)
    return boxes, framing_band(np.stack((low, high)), camera)


def content_bounds(meshes: Mapping[str, MeshData], targets: Sequence[TargetSpec]) -> np.ndarray | None:
    """World box every enabled target can occupy, i.e. what the camera has to keep in frame.

    Each target is placed at the far corner of its own offset range, so the union covers any
    sample the sampler can draw. ``None`` when no enabled target has a loaded mesh.
    """
    low, high = np.full(3, np.inf), np.full(3, -np.inf)
    for target in targets:
        mesh = meshes.get(target.asset.key)
        if not target.enabled or mesh is None:
            continue
        reach = np.array([max(abs(target.offset.x.minimum), abs(target.offset.x.maximum)),
                          max(abs(target.offset.y.minimum), abs(target.offset.y.maximum)),
                          max(abs(target.offset.z.minimum), abs(target.offset.z.maximum))])
        half = np.ptp(mesh.bounds_m, axis=0) / 2
        low, high = np.minimum(low, -reach - half), np.maximum(high, reach + half)
    if not np.isfinite(low).all() or not (high > low).all():
        return None
    return np.stack((low, high))
