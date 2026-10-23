"""Dependency-free PLY reader for meshes with mixed polygon sizes.

Trimesh's PLY reader assumes every face element has one uniform list length, so
files exported by Blender with combined triangles and quads (and extra elements
such as ``edge``) fail to load at all. CAD exports do this routinely, so the
loader reads the elements directly and fan-triangulates polygons on the way in.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import numpy as np


class PlyError(ValueError):
    """Raised when a PLY file cannot be read as a polygon mesh."""


@dataclass(frozen=True)
class PlyMesh:
    vertices: np.ndarray
    faces: np.ndarray
    colors: np.ndarray | None


_SCALAR = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}
_COLOR_KEYS = ("red", "green", "blue", "alpha")


def _parse_header(data: bytes) -> tuple[int, str, list[dict]]:
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise PlyError("不是有效的 PLY 文件头")
    newline = data.find(b"\n", end)
    if newline < 0:
        raise PlyError("PLY 文件头缺少结尾换行")
    try:
        lines = data[:end].decode("ascii", "replace").splitlines()
    except Exception as exc:
        raise PlyError(f"PLY 文件头无法解码：{exc}") from exc
    fmt = None
    elements: list[dict] = []
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format" and len(parts) >= 2:
            fmt = parts[1]
        elif parts[0] == "element" and len(parts) >= 3:
            elements.append({"name": parts[1], "count": int(parts[2]), "properties": []})
        elif parts[0] == "property" and elements:
            if len(parts) >= 5 and parts[1] == "list":
                elements[-1]["properties"].append(
                    {"name": parts[4], "list": True, "count_type": parts[2], "value_type": parts[3]})
            elif len(parts) >= 3:
                elements[-1]["properties"].append(
                    {"name": parts[2], "list": False, "dtype": parts[1]})
    if fmt not in ("ascii", "binary_little_endian", "binary_big_endian"):
        raise PlyError(f"不支持的 PLY 编码：{fmt!r}")
    return newline + 1, fmt, elements


def _dtype(name: str, endian: str = "<") -> np.dtype:
    try:
        return np.dtype(endian + _SCALAR[name])
    except KeyError as exc:
        raise PlyError(f"不支持的 PLY 属性类型：{name}") from exc


def _struct(name: str, endian: str = "<") -> struct.Struct:
    try:
        return struct.Struct(endian + _SCALAR[name])
    except KeyError as exc:
        raise PlyError(f"不支持的 PLY 属性类型：{name}") from exc


def _read_ascii(body: bytes, elements: list[dict]) -> dict[str, dict]:
    tokens = body.split()
    cursor = 0
    out: dict[str, dict] = {}
    for element in elements:
        columns = {p["name"]: [] for p in element["properties"]}
        for _ in range(element["count"]):
            for prop in element["properties"]:
                if prop["list"]:
                    count = int(tokens[cursor])
                    cursor += 1
                    columns[prop["name"]].append(np.array(tokens[cursor:cursor + count], dtype=np.int64))
                    cursor += count
                else:
                    columns[prop["name"]].append(float(tokens[cursor]))
                    cursor += 1
        out[element["name"]] = columns
    return out


def _read_binary(body: bytes, elements: list[dict], endian: str) -> dict[str, dict]:
    offset = 0
    out: dict[str, dict] = {}
    for element in elements:
        props = element["properties"]
        count = element["count"]
        if all(not p["list"] for p in props):
            dtype = np.dtype([(p["name"], _dtype(p["dtype"], endian)) for p in props])
            nbytes = dtype.itemsize * count
            if offset + nbytes > len(body):
                raise PlyError(f"PLY 数据长度不足：{element['name']} 需要 {nbytes} 字节")
            block = np.frombuffer(body, dtype=dtype, count=count, offset=offset)
            offset += nbytes
            out[element["name"]] = {p["name"]: block[p["name"]] for p in props}
            continue
        columns: dict[str, list] = {p["name"]: [] for p in props}
        for _ in range(count):
            for prop in props:
                if prop["list"]:
                    count_spec = _struct(prop["count_type"], endian)
                    value_spec = _struct(prop["value_type"], endian)
                    if offset + count_spec.size > len(body):
                        raise PlyError(f"PLY 数据长度不足：{element['name']}.{prop['name']}")
                    length = int(count_spec.unpack_from(body, offset)[0])
                    offset += count_spec.size
                    needed = value_spec.size * length
                    if offset + needed > len(body):
                        raise PlyError(f"PLY 数据长度不足：{element['name']}.{prop['name']}")
                    values = np.frombuffer(body, dtype=_dtype(prop["value_type"], endian),
                                           count=length, offset=offset)
                    offset += needed
                    columns[prop["name"]].append(values.astype(np.int64))
                else:
                    spec = _struct(prop["dtype"], endian)
                    if offset + spec.size > len(body):
                        raise PlyError(f"PLY 数据长度不足：{element['name']}.{prop['name']}")
                    columns[prop["name"]].append(spec.unpack_from(body, offset)[0])
                    offset += spec.size
        out[element["name"]] = columns
    return out


def _triangulate(polygons: list[np.ndarray]) -> np.ndarray:
    triangles: list[tuple[int, int, int]] = []
    for polygon in polygons:
        indices = np.asarray(polygon, dtype=np.int64).reshape(-1)
        if len(indices) < 3:
            continue
        for i in range(1, len(indices) - 1):
            triangles.append((int(indices[0]), int(indices[i]), int(indices[i + 1])))
    return np.asarray(triangles, dtype=np.int64).reshape(-1, 3)


def read_ply(path: str | Path) -> PlyMesh:
    data = Path(path).read_bytes()
    offset, fmt, elements = _parse_header(data)
    body = data[offset:]
    columns = (_read_ascii(body, elements) if fmt == "ascii"
               else _read_binary(body, elements, "<" if fmt == "binary_little_endian" else ">"))
    vertex = columns.get("vertex")
    if vertex is None or "x" not in vertex:
        raise PlyError("PLY 缺少 vertex 元素")
    vertices = np.column_stack([np.asarray(vertex[axis], dtype=np.float64) for axis in ("x", "y", "z")])
    colors = None
    if all(key in vertex for key in _COLOR_KEYS[:3]):
        channels = [np.asarray(vertex[key]) for key in _COLOR_KEYS if key in vertex]
        colors = np.column_stack(channels).astype(np.float64)
        if max(float(np.max(c)) for c in channels) <= 1.0:
            colors = colors * 255.0
        colors = np.rint(np.clip(colors, 0, 255)).astype(np.uint8)
        if colors.shape[1] == 3:
            colors = np.column_stack([colors, np.full(len(colors), 255, dtype=np.uint8)])
    face = columns.get("face")
    polygons = list(face.get("vertex_indices", [])) if face else []
    faces = _triangulate(polygons)
    if len(faces) and (faces.min() < 0 or faces.max() >= len(vertices)):
        raise PlyError("PLY 面索引超出顶点范围")
    return PlyMesh(vertices, faces, colors)
