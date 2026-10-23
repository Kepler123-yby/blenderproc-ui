"""Small, explicit adapter for BlenderProc 2.8's native BOP writer."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np

from bproc_core.assets import write_bop_models
from .records import FrameRef


def bop_translation(t_m) -> list[float]:
    return (np.asarray(t_m, dtype=float) * 1000.0).tolist()


def depth_capacity_m(scale_mm: float) -> float:
    if not np.isfinite(scale_mm) or scale_mm <= 0:
        raise ValueError("Depth scale must be finite and positive")
    return 65535.0 * scale_mm / 1000.0


class BopWriter:
    def __init__(self, dataset_dir: Path, config, meshes, backend):
        self.root = Path(dataset_dir)
        self.config, self.meshes, self.backend = config, meshes, backend
        self.ids = tuple(sorted(t.obj_id for t in config.targets if t.enabled))
        self._initialized = False
        self._minimum_depth = float("inf")
        self._far = 1.0

    def write(self, index, candidate, frame) -> FrameRef:
        if frame.depth_m is None:
            raise ValueError("BOP requires depth in meters")
        shape = (self.config.camera.height, self.config.camera.width)
        if frame.depth_m.shape != shape or frame.rgb.shape != (*shape, 3):
            raise ValueError("BOP RGB/depth dimensions do not match the configured camera")
        depth = np.asarray(frame.depth_m, dtype=np.float64).copy()
        valid = np.isfinite(depth) & (depth > 0) & (depth < candidate.camera.clip_far_m)
        depth[~valid] = 0
        if np.any(depth[valid] > depth_capacity_m(self.config.output.depth_scale_mm)):
            raise ValueError("Geometry depth exceeds uint16 capacity; increase depth_scale_mm")
        if np.any(valid):
            self._minimum_depth = min(self._minimum_depth, float(depth[valid].min()))
        self._far = max(self._far, candidate.camera.clip_far_m)
        if not self._initialized:
            write_bop_models(self.meshes, tuple(t for t in self.config.targets if t.enabled), self.root / "models")
            self._initialized = True
        scene = self.backend.bpy.context.scene
        scene.frame_start, scene.frame_end = 0, 1
        scene.frame_set(0)
        encoding = "PNG" if self.config.output.rgb_encoding == "png" else "JPEG"
        self.backend.api.writer.write_bop(output_dir=str(self.root), dataset="",
            target_objects=[self.backend.targets[obj_id] for obj_id in self.ids],
            colors=[frame.rgb], depths=[depth], color_file_format=encoding,
            annotation_unit="mm", depth_scale=self.config.output.depth_scale_mm,
            frames_per_chunk=1000, append_to_existing_output=True,
            calc_mask_info_coco=False, ignore_dist_thres=float("inf"))
        chunks = sorted((self.root / "train_pbr").glob("[0-9][0-9][0-9][0-9][0-9][0-9]"))
        if not chunks:
            raise RuntimeError("BOP writer produced no scene directory")
        chunk = chunks[-1]
        gt = json.loads((chunk / "scene_gt.json").read_text(encoding="utf-8"))
        image_id = max(map(int, gt))
        ids = tuple(row["obj_id"] for row in gt[str(image_id)])
        if ids != self.ids:
            raise RuntimeError("BOP writer omitted or reordered enabled target ground truth")
        extension = ".png" if encoding == "PNG" else ".jpg"
        relative = (chunk / "rgb" / f"{image_id:06d}{extension}").relative_to(self.root)
        if not (self.root / relative).is_file() or not (chunk / "depth" / f"{image_id:06d}.png").is_file():
            raise RuntimeError("BOP writer did not publish the RGB/depth pair")
        return FrameRef(index, relative.as_posix(), int(chunk.name), image_id, ids)

    def finalize(self) -> None:
        if self._initialized:
            from .bop_postprocess import finalize_bop
            near = max(1e-6, self._minimum_depth / 4) if np.isfinite(self._minimum_depth) else 1e-4
            finalize_bop(self.root, self.config, self.meshes, near_m=near, far_m=self._far)
