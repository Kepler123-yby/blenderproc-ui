from dataclasses import replace
import json
import numpy as np
import pytest
import trimesh

from bproc_core.assets import AssetError, load_mesh, mesh_diameter_m, suggest_ranges, write_bop_models
from bproc_core.config import AssetSpec, CameraSpec, TargetSpec
from tests.factories import make_mesh, write_mesh


@pytest.mark.parametrize("unit,factor", [("m", 1), ("cm", 100), ("mm", 1000)])
def test_units_preserve_cad_origin_and_original_resource_bytes(tmp_path, unit, factor):
    canonical = make_mesh("offset", center=(.1, -.05, .025), size=.1, color=(100, 120, 180, 255))
    path = write_mesh(tmp_path / "偏置.ply", canonical, factor=factor)
    original = path.read_bytes()
    mesh = load_mesh(AssetSpec("offset", path, unit))
    np.testing.assert_allclose(mesh.bounds_m.mean(axis=0), [.1, -.05, .025])
    np.testing.assert_allclose(mesh.vertices_m, canonical.vertices_m)
    np.testing.assert_array_equal(mesh.vertex_rgba, canonical.vertex_rgba)
    assert mesh_diameter_m(mesh) == pytest.approx(np.sqrt(3) * .1)
    target = TargetSpec(AssetSpec(mesh.key, path, unit), "offset", 7)
    models = tmp_path / "models"
    write_bop_models({mesh.key: mesh}, [target], models)
    exported = trimesh.load(models / "obj_000007.ply", process=False)
    np.testing.assert_allclose(exported.vertices.mean(axis=0), [100, -50, 25])
    info = json.loads((models / "models_info.json").read_text())
    assert set(info) == {"7"}
    assert info["7"]["diameter"] == pytest.approx(np.sqrt(3) * 100)
    assert info["7"]["min_x"] == pytest.approx(50)
    assert path.read_bytes() == original


def test_planar_diameter_and_uncontinuous_ids_without_recenter(tmp_path):
    mesh = make_mesh("flat", center=(1, 2, 3), size=(.2, .1, 0))
    assert mesh_diameter_m(mesh) == pytest.approx(np.sqrt(.2 ** 2 + .1 ** 2))
    path = write_mesh(tmp_path / "flat.ply", mesh)
    targets = [TargetSpec(AssetSpec("flat", path), "flat", i) for i in (7, 2)]
    write_bop_models({"flat": mesh}, targets, tmp_path / "models")
    assert sorted(p.name for p in (tmp_path / "models").glob("*.ply")) == ["obj_000002.ply", "obj_000007.ply"]
    with pytest.raises(FileExistsError):
        write_bop_models({"flat": mesh}, targets, tmp_path / "models")


@pytest.mark.parametrize("kind", ["pointcloud", "empty", "nonfinite", "degenerate", "transparent", "unit"])
def test_invalid_meshes_explain_failure(tmp_path, kind):
    path = tmp_path / "bad.ply"
    if kind in ("pointcloud", "empty"):
        vertices = "0 0 0\n" if kind == "pointcloud" else ""
        path.write_text(f"ply\nformat ascii 1.0\nelement vertex {int(kind == 'pointcloud')}\nproperty float x\nproperty float y\nproperty float z\nend_header\n{vertices}")
    else:
        mesh = make_mesh("bad", color=(10, 20, 30, 80) if kind == "transparent" else None)
        if kind == "nonfinite":
            mesh.vertices_m[0, 0] = float("nan")
        if kind == "degenerate":
            mesh.vertices_m[:] = 0
        write_mesh(path, mesh)
    asset = AssetSpec("bad", path, "inch" if kind == "unit" else "m")
    with pytest.raises(AssetError):
        load_mesh(asset)


def test_size_suggestions_account_for_all_targets_and_camera_fov(tmp_path):
    small = make_mesh("small", size=.02)
    large = make_mesh("large", size=.2)
    targets = [TargetSpec(AssetSpec(k, tmp_path / (k + ".ply")), k, i) for i, k in enumerate(("small", "large"), 1)]
    box, radius = suggest_ranges({"small": small, "large": large}, targets, CameraSpec())
    _, close_radius = suggest_ranges({"small": small}, targets[:1], CameraSpec())
    assert box.x.minimum < 0 < box.x.maximum
    assert radius.minimum > close_radius.minimum > 0
    assert radius.maximum > radius.minimum


def test_size_suggestions_stay_close_for_millimetre_models(tmp_path):
    tiny = make_mesh("tiny", size=.001)
    target = TargetSpec(AssetSpec("tiny", tmp_path / "tiny.ply"), "tiny", 1)
    _, radius = suggest_ranges({"tiny": tiny}, [target], CameraSpec())
    assert radius.minimum < .01
    assert radius.maximum / radius.minimum == pytest.approx(1.5)
