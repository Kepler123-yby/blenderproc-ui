"""Object-ID masks, independent of Blender's transient segmentation IDs."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import numpy as np


def decode_instances(segmap, attributes, obj_ids: Iterable[int]) -> dict[int, np.ndarray]:
    segmap = np.asarray(segmap)
    if segmap.ndim != 2:
        raise ValueError("Instance segmentation must be a two-dimensional image")
    ids = tuple(obj_ids)
    result = {obj_id: np.zeros(segmap.shape, dtype=bool) for obj_id in ids}
    for entry in attributes:
        obj_id = entry.get("ui_target_id", entry.get("cp_ui_target_id", 0))
        if obj_id in result:
            result[obj_id] |= segmap == int(entry["idx"])
    return result


def occlusion_ratio(evidence, anchor_obj_id: int) -> float:
    full = np.asarray(evidence.anchor_full, dtype=bool)
    visible = np.asarray(evidence.visible[anchor_obj_id], dtype=bool)
    if full.ndim != 2 or full.shape != visible.shape:
        raise ValueError("Full and visible masks must have the same image shape")
    total = int(full.sum())
    if total == 0:
        raise ValueError("Center target has an empty isolated silhouette")
    # Segmentation is rasterized identically in both passes. Intersection also
    # makes the definition explicit in the presence of an erroneous stray pixel.
    return 1.0 - float(np.count_nonzero(full & visible)) / total
