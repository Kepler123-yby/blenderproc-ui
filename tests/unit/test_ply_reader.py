from pathlib import Path
import struct

import numpy as np
import pytest

from bproc_core.assets import AssetSpec, load_mesh
from bproc_core.ply import PlyError, read_ply

HEADER = """ply
format {fmt} 1.0
comment exported by Blender
element vertex {verts}
property float x
property float y
property float z
property uchar red
property uchar green
property uchar blue
property uchar alpha
property float s
property float t
element face {faces}
property list uchar uint vertex_indices
element edge {edges}
property int vertex1
property int vertex2
end_header
"""


def _binary_polygons(tmp_path: Path, points, polygons, edges=0, alpha=255, name="mixed.ply") -> Path:
    lines = HEADER.format(fmt="binary_little_endian", verts=len(points),
                          faces=len(polygons), edges=edges).encode("ascii")
    body = bytearray()
    for point in points:
        body += struct.pack("<3f4B2f", *point, 210, 120, 60, alpha, 0.0, 0.0)
    for polygon in polygons:
        body += struct.pack("<B", len(polygon))
        body += struct.pack(f"<{len(polygon)}I", *polygon)
    for index in range(edges):
        body += struct.pack("<2i", index, index + 1)
    path = tmp_path / name
    path.write_bytes(lines + bytes(body))
    return path


def _quad_mesh():
    # Unit square as a single quad plus a triangle sharing one edge.
    points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0)]
    return points, [[0, 1, 2, 3], [1, 4, 2]]


def test_binary_mixed_polygons_triangulate_and_ignore_edge_element(tmp_path):
    points, polygons = _quad_mesh()
    path = _binary_polygons(tmp_path, points, polygons, edges=1)
    mesh = read_ply(path)
    np.testing.assert_allclose(mesh.vertices, points)
    assert mesh.faces.shape == (3, 3)
    np.testing.assert_array_equal(mesh.faces[:2], [[0, 1, 2], [0, 2, 3]])
    np.testing.assert_array_equal(mesh.faces[2], [1, 4, 2])
    np.testing.assert_array_equal(mesh.colors[0], [210, 120, 60, 255])


def test_ascii_polygon_mesh_is_accepted(tmp_path):
    path = tmp_path / "quad.ply"
    path.write_text("ply\nformat ascii 1.0\nelement vertex 4\n"
                    "property float x\nproperty float y\nproperty float z\n"
                    "element face 1\nproperty list uchar int vertex_indices\nend_header\n"
                    "0 0 0\n1 0 0\n1 1 0\n0 1 0\n4 0 1 2 3\n")
    mesh = read_ply(path)
    assert mesh.faces.shape == (2, 3)
    assert mesh.colors is None


def test_rejects_out_of_range_indices_and_truncated_bodies(tmp_path):
    points, polygons = _quad_mesh()
    broken = _binary_polygons(tmp_path, points, [[0, 1, 2, 9]], name="index.ply")
    with pytest.raises(PlyError):
        read_ply(broken)
    truncated = _binary_polygons(tmp_path, points, polygons, name="short.ply")
    truncated.write_bytes(truncated.read_bytes()[:-9])
    with pytest.raises(PlyError):
        read_ply(truncated)


def test_load_mesh_accepts_quad_ply_and_rejects_transparency(tmp_path):
    points, polygons = _quad_mesh()
    path = _binary_polygons(tmp_path, points, polygons)
    mesh = load_mesh(AssetSpec("cap", path, "mm"))
    assert mesh.faces.shape == (3, 3)
    np.testing.assert_allclose(mesh.bounds_m[1], [0.002, 0.001, 0])
    transparent = _binary_polygons(tmp_path, points, polygons, alpha=200, name="transparent.ply")
    with pytest.raises(Exception) as error:
        load_mesh(AssetSpec("cap", transparent, "mm"))
    assert "透明" in str(error.value)
