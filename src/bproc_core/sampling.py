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


@lru_cache(maxsize=8)
def unoccluded_requests(total: int, fraction: float, seed: int) -> frozenset[int]:
    if not 0 <= fraction <= 1:
        raise ValueError("Unoccluded fraction must be between 0 and 1")
    count = math.floor(total * fraction + .5)
    # Separate stream: retries never change a request's category.
    order = np.random.default_rng(np.random.SeedSequence([seed, 0x554E4F43])).permutation(total)
    return frozenset(int(i) for i in order[:count])
