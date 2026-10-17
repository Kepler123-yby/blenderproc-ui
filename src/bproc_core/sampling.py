"""Deterministic sampling; no process-global random state.

Every scene draws one placement/lighting state, and every view of that scene draws its own
camera, so a multi-view dataset shows the same arrangement from different viewpoints.
"""
from __future__ import annotations

from typing import Sequence
from functools import lru_cache
import math
import numpy as np


def allocate_regions(total: int, keys: Sequence[str]) -> dict[str, int]:
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or not keys or len(set(keys)) != len(keys):
        raise ValueError("A nonnegative count and unique, nonempty regions are required")
    quotient, remainder = divmod(total, len(keys))
    return {key: quotient + (i < remainder) for i, key in enumerate(keys)}


def scene_rng(seed: int, scene_index: int) -> np.random.Generator:
    """Stream shared by every view of a scene: placements, occluders, lighting."""
    if min(seed, scene_index) < 0:
        raise ValueError("Seed and scene indices must be nonnegative")
    return np.random.default_rng(np.random.SeedSequence([seed, scene_index, 0x5343454E]))


def view_rng(seed: int, scene_index: int, view_index: int, attempt_index: int) -> np.random.Generator:
    """Stream for one camera: a retry resamples only the pose, never the placement."""
    if min(seed, scene_index, view_index, attempt_index) < 0:
        raise ValueError("Seed, scene, view and attempt indices must be nonnegative")
    return np.random.default_rng(np.random.SeedSequence([seed, scene_index, view_index, attempt_index]))


def region_for_view(total: int, keys: Sequence[str], view_index: int) -> str:
    if not 0 <= view_index < total:
        raise ValueError("View index is outside the requested scene")
    remaining = view_index
    for key, count in allocate_regions(total, keys).items():
        if remaining < count:
            return key
        remaining -= count
    raise AssertionError("Unreachable region allocation")


def camera_for_view(mode: str, fixed_keys: Sequence[str], region_keys: Sequence[str],
                    views_per_scene: int, view_index: int) -> tuple[str, str]:
    """Map a view index to ``(kind, key)``: the pinned camera list, or a region of the scene."""
    if isinstance(view_index, bool) or not isinstance(view_index, int) or view_index < 0:
        raise ValueError("View index must be a nonnegative integer")
    if mode == "fixed":
        if not fixed_keys:
            raise ValueError("Fixed camera mode needs at least one camera")
        if view_index >= len(fixed_keys):
            raise ValueError("View index is outside the pinned camera list")
        return "fixed", fixed_keys[view_index]
    if mode != "random":
        raise ValueError("Camera mode must be 'fixed' or 'random'")
    if views_per_scene < 1 or not region_keys:
        raise ValueError("Random views need a positive view count and at least one region")
    if view_index >= views_per_scene:
        raise ValueError("View index is outside the requested scene")
    return "random", region_for_view(views_per_scene, region_keys, view_index)


@lru_cache(maxsize=8)
def unoccluded_scenes(total: int, fraction: float, seed: int) -> frozenset[int]:
    if not 0 <= fraction <= 1:
        raise ValueError("Unoccluded fraction must be between 0 and 1")
    count = math.floor(total * fraction + .5)
    # Separate stream: camera retries never change a scene's category.
    order = np.random.default_rng(np.random.SeedSequence([seed, 0x554E4F43])).permutation(total)
    return frozenset(int(i) for i in order[:count])
