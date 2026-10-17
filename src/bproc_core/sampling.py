"""Deterministic sampling; no process-global random state."""
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


def request_rng(seed: int, request_index: int, attempt_index: int) -> np.random.Generator:
    if min(seed, request_index, attempt_index) < 0:
        raise ValueError("Seed and sample indices must be nonnegative")
    return np.random.default_rng(np.random.SeedSequence([seed, request_index, attempt_index]))


def region_for_request(total: int, keys: Sequence[str], request_index: int) -> str:
    if not 0 <= request_index < total:
        raise ValueError("Sample index is outside the requested dataset")
    for key, count in allocate_regions(total, keys).items():
        if request_index < count:
            return key
        request_index -= count
    raise AssertionError("Unreachable region allocation")


def camera_for_request(fixed_shots: int, fixed_keys: Sequence[str], random_shots: int,
                       region_keys: Sequence[str], request_index: int) -> tuple[str, str]:
    """Map a request index to ``(kind, key)``; pinned cameras occupy the first frames."""
    if isinstance(request_index, bool) or not isinstance(request_index, int) or request_index < 0:
        raise ValueError("Request index must be a nonnegative integer")
    if fixed_shots < 0 or random_shots < 0:
        raise ValueError("Camera counts must be nonnegative")
    if fixed_shots and not fixed_keys:
        raise ValueError("Pinned camera frames need at least one camera")
    if random_shots and not region_keys:
        raise ValueError("Random frames need at least one region")
    if request_index < fixed_shots:
        for key, count in allocate_regions(fixed_shots, list(fixed_keys)).items():
            if request_index < count:
                return "fixed", key
            request_index -= count
    elif request_index < fixed_shots + random_shots:
        request_index -= fixed_shots
        for key, count in allocate_regions(random_shots, list(region_keys)).items():
            if request_index < count:
                return "random", key
            request_index -= count
    raise ValueError("Request index is outside the requested dataset")


@lru_cache(maxsize=8)
def unoccluded_requests(total: int, fraction: float, seed: int) -> frozenset[int]:
    if not 0 <= fraction <= 1:
        raise ValueError("Unoccluded fraction must be between 0 and 1")
    count = math.floor(total * fraction + .5)
    # Separate stream: retries never change a request's category.
    order = np.random.default_rng(np.random.SeedSequence([seed, 0x554E4F43])).permutation(total)
    return frozenset(int(i) for i in order[:count])
