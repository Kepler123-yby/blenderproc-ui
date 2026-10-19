"""Native Cycles image tiling: application, re-application, evidence and restoration."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile

import numpy as np
import pytest

from bproc_core.config import ProjectConfig, RenderSpec, validate_project
from bproc_runtime.blender_backend import BlenderBackend
from bproc_runtime.contracts import VisibilityEvidence
from bproc_runtime.render_tiling import NativeTiling, TilingError


class FakeCycles:
    def __init__(self, use_auto_tile=False, tile_size=512):
        self.use_auto_tile = use_auto_tile
        self.tile_size = tile_size
        self.device = "CPU"


class FakeBpy:
    def __init__(self):
        self.scene = SimpleNamespace(cycles=FakeCycles(), frame_start=0, frame_end=1,
                                     frame_set=lambda index: None)
        self.handlers = SimpleNamespace(render_stats=[])
        self.context = SimpleNamespace(scene=self.scene, view_layer=None)
        self.app = SimpleNamespace(handlers=self.handlers)

    def undo_like_scene_replacement(self):
        """BlenderProc's segmap undo can recreate scene data under the same bpy module."""
        self.scene.cycles = FakeCycles(use_auto_tile=True, tile_size=1024)
        self.context.scene = self.scene


def spec(use_tiling=True, tile_size=256):
    return SimpleNamespace(use_tiling=use_tiling, tile_size=tile_size)


def test_apply_reads_back_native_values_and_restores_them(tmp_path):
    bpy = FakeBpy()
    tiling = NativeTiling(bpy, spec(True, 256), tmp_path)
    metadata = tiling.apply("prepare")
    assert bpy.scene.cycles.use_auto_tile is True
    assert bpy.scene.cycles.tile_size == 256
    assert metadata["requested_tile_size"] == 256
    assert tiling.metadata.original_use_tiling is False
    assert tiling.metadata.original_tile_size == 512
    tiling.restore()
    assert bpy.scene.cycles.use_auto_tile is False
    assert bpy.scene.cycles.tile_size == 512


def test_segmentation_undo_is_followed_by_a_fresh_application(tmp_path):
    bpy = FakeBpy()
    tiling = NativeTiling(bpy, spec(True, 128), tmp_path)
    tiling.apply("prepare")
    bpy.undo_like_scene_replacement()
    assert bpy.scene.cycles.tile_size == 1024          # undo restored unrelated defaults
    tiling.apply("segmentation")
    assert bpy.scene.cycles.use_auto_tile is True and bpy.scene.cycles.tile_size == 128
    assert [row["stage"] for row in tiling.metadata.stages] == ["prepare", "segmentation"]
    tiling.restore()
    assert bpy.scene.cycles.tile_size == 512 and bpy.scene.cycles.use_auto_tile is False


def test_requested_values_that_blender_rejects_are_an_error(tmp_path):
    bpy = FakeBpy()

    class Clamping(FakeCycles):
        def __setattr__(self, name, value):
            if name == "tile_size":
                value = 64
            super().__setattr__(name, value)

    bpy.scene.cycles = Clamping()
    tiling = NativeTiling(bpy, spec(True, 512), tmp_path)
    with pytest.raises(TilingError, match="未接受"):
        tiling.apply("prepare")


def test_missing_cycles_properties_fail_loudly(tmp_path):
    bpy = FakeBpy()
    bpy.scene.cycles = SimpleNamespace()
    with pytest.raises(TilingError, match="use_auto_tile"):
        NativeTiling(bpy, spec(), tmp_path).apply("prepare")


@pytest.mark.parametrize("bad", [True, 0, -8, 7, 9000, 12.5])
def test_invalid_tile_size_is_rejected_before_touching_blender(tmp_path, bad):
    bpy = FakeBpy()
    before = (bpy.scene.cycles.use_auto_tile, bpy.scene.cycles.tile_size)
    with pytest.raises(TilingError):
        NativeTiling(bpy, spec(True, bad), tmp_path)
    assert (bpy.scene.cycles.use_auto_tile, bpy.scene.cycles.tile_size) == before


def test_tile_schedule_evidence_comes_from_native_render_stats(tmp_path):
    bpy = FakeBpy()
    tiling = NativeTiling(bpy, spec(True, 256), tmp_path)
    tiling.apply("rgb")
    tiling.install_observer()
    handler = bpy.handlers.render_stats[0]
    handler("Fra:0 Mem:1M | Scene, ViewLayer | Rendered 7/20 Tiles, Sample 1/1", None)
    handler("Fra:0 Mem:1M | Scene, ViewLayer | Rendered 20/20 Tiles, Sample 1/1", None)
    handler("unrelated progress line", None)
    assert len(tiling.tile_lines()) == 2
    destination = tiling.write_metadata(tmp_path / "run_metadata" / "render_tiling.json")
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["scheduled_tiles_proven"] is True
    assert payload["tile_schedule_lines"][0].endswith("Rendered 7/20 Tiles, Sample 1/1")
    assert "image_buffer_only" in payload["capability"]
    tiling.restore()
    assert bpy.handlers.render_stats == []
    assert "restored" in (tmp_path / "cycles_tiles.log").read_text(encoding="utf-8")


def test_config_validation_accepts_only_real_tile_values():
    def fields(render):
        return {issue.field for issue in validate_project(ProjectConfig(render=render), check_files=False)
                if issue.severity == "error" and issue.field.startswith("render.")}

    assert not fields(RenderSpec(use_tiling=True, tile_size=256))
    assert "render.tile_size" in fields(RenderSpec(tile_size=7))
    assert "render.tile_size" in fields(RenderSpec(tile_size=9000))
    assert "render.tile_size" in fields(RenderSpec(tile_size=True))
    assert "render.tile_size" in fields(RenderSpec(tile_size=256.0))   # type: ignore[arg-type]
    assert "render.use_tiling" in fields(RenderSpec(use_tiling="yes"))  # type: ignore[arg-type]


class RecordingTiling:
    def __init__(self):
        self.stages = []

    def apply(self, stage):
        self.stages.append(stage)
        return {}


def test_backend_reapplies_at_every_native_boundary(tmp_path):
    backend = BlenderBackend()
    backend.targets = {2: SimpleNamespace(hidden=False, hide=lambda value: None,
                                         is_hidden=lambda: False)}
    backend.objects = []
    backend._temporary = tempfile.TemporaryDirectory()
    backend.tiling = RecordingTiling()
    backend.segmentation_kwargs = []
    scene = SimpleNamespace(frame_start=5, frame_end=10, frame_set=lambda index: None)

    def segmentation(**kwargs):
        backend.segmentation_kwargs.append(kwargs)
        return {"instance_segmaps": [np.array([[1]])],
                "instance_attribute_maps": [[{"idx": 1, "ui_target_id": 2}]]}

    backend.bpy = SimpleNamespace(context=SimpleNamespace(scene=scene))
    backend.api = SimpleNamespace(renderer=SimpleNamespace(render_segmap=segmentation))
    try:
        backend._segmentation()
        assert backend.tiling.stages == ["segmentation"]
    finally:
        backend.tiling = None
        backend.close()


def test_backend_rgb_pass_reapplies_tiling_and_records_metadata(tmp_path):
    backend = BlenderBackend()
    backend.config = ProjectConfig()
    backend.tiling = NativeTiling(FakeBpy(), spec(True, 256), tmp_path)
    backend.tiling.apply("prepare")
    preferences = SimpleNamespace(devices=[SimpleNamespace(type="OPTIX", use=True)],
                                  compute_device_type="OPTIX",
                                  has_optixdenoiser_gpu_devices=lambda: False)
    backend.tiling.bpy.scene.cycles.device = "GPU"
    backend.bpy = SimpleNamespace(context=SimpleNamespace(
        scene=backend.tiling.bpy.scene,
        view_layer=SimpleNamespace(cycles=SimpleNamespace(use_denoising=True)),
        preferences=SimpleNamespace(addons={"cycles": SimpleNamespace(preferences=preferences)})))
    calls = []

    def render(**kwargs):
        calls.append("render")
        return {"colors": [np.zeros((2, 2, 4), dtype=np.uint8)], "depth": []}

    backend.api = SimpleNamespace(renderer=SimpleNamespace(
        render=render, set_denoiser=lambda value: None, set_noise_threshold=lambda value: None))
    backend.targets = {}
    backend._temporary = tempfile.TemporaryDirectory()
    backend._anchor_id = None
    backend.render(VisibilityEvidence(np.ones((2, 2), bool), {}))
    assert calls == ["render"]
    assert backend.tiling.metadata.stages[-1]["stage"] == "rgb"
    payload = backend.tiling_metadata()
    assert payload["requested_tile_size"] == 256 and payload["effective_tile_size"] == 256
    assert payload["scheduled_tiles_proven"] is False       # no native stats line was captured
    backend.tiling.restore()
    backend.tiling = None
    backend.close()
