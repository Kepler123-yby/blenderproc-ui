"""Lossless object-ID masks and RGB without BOP dependencies."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import shutil
import numpy as np
from PIL import Image

from .records import FrameRef
from .output import OutputConflict


class RgbMasksWriter:
    def __init__(self, dataset_dir: Path, config):
        self.root = Path(dataset_dir)
        self.config = config
        self.ids = tuple(sorted(t.obj_id for t in config.targets if t.enabled))
        self.extension = ".jpg" if config.output.rgb_encoding == "jpeg" else ".png"
        for name in ("rgb", "mask", "mask_visib"):
            (self.root / name).mkdir(exist_ok=False)

    def write(self, index, candidate, frame) -> FrameRef:
        shape = (self.config.camera.height, self.config.camera.width)
        if frame.rgb.shape != (*shape, 3) or frame.rgb.dtype != np.uint8:
            raise ValueError("RGB must be uint8 with the configured image dimensions")
        if frame.full_masks is None or set(frame.full_masks) != set(self.ids) or set(frame.visible_masks) != set(self.ids):
            raise ValueError("Masks must contain exactly every enabled target obj_id")
        for obj_id in self.ids:
            full, visible = np.asarray(frame.full_masks[obj_id]), np.asarray(frame.visible_masks[obj_id])
            if full.shape != shape or visible.shape != shape or full.dtype != bool or visible.dtype != bool:
                raise ValueError("Masks must be boolean images matching RGB dimensions")
            if np.any(visible & ~full):
                raise ValueError("Visible mask must be a subset of the full silhouette")
        stage = Path(tempfile.mkdtemp(prefix=".frame-", dir=self.root))
        files = []
        try:
            rgb_relative = Path("rgb") / f"{index:06d}{self.extension}"
            rgb_stage = stage / rgb_relative.name
            Image.fromarray(frame.rgb).save(rgb_stage, quality=95)
            files.append((rgb_stage, self.root / rgb_relative))
            for kind, masks in (("mask", frame.full_masks), ("mask_visib", frame.visible_masks)):
                for obj_id in self.ids:
                    name = f"{index:06d}_{obj_id:06d}.png"
                    source = stage / f"{kind}_{name}"
                    Image.fromarray(masks[obj_id].astype(np.uint8) * 255).save(source)
                    files.append((source, self.root / kind / name))
            # Hard-link publication is exclusive even if another process attempts
            # to create a same-named file after the run reserved its directory.
            for source, target in files:
                try:
                    os.link(source, target)
                except FileExistsError as exc:
                    raise OutputConflict(f"Refusing to overwrite image: {target}") from exc
            return FrameRef(index, rgb_relative.as_posix(), gt_obj_ids=self.ids)
        finally:
            shutil.rmtree(stage)

    def finalize(self) -> None:
        pass
