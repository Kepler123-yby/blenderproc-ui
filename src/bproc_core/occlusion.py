"""Which occlusion ratio a frame must show, shared by rendering and dataset validation.

The renderer decides acceptance while a dataset is produced; ``tests/blender/_checks.py``
re-derives the same decision from the manifest afterwards. Keeping the rule in one place stops
the two from drifting apart, which is how frames used to be accepted during rendering and then
rejected on review (or the other way round).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from bproc_core.config import Range

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bproc_core.config import ProjectConfig

ZERO_OCCLUSION = Range(0.0, 0.0)


def enabled_target_count(config: ProjectConfig) -> int:
    return sum(1 for target in config.targets if target.enabled)


def has_dedicated_occluder(config: ProjectConfig) -> bool:
    """Occluder assets that can actually be instantiated.

    An empty asset list places nothing, and so does a maximum count of zero; both mean the
    configured ``occlusion.ratio`` is unreachable by dedicated occluders.
    """
    return bool(config.occluders) and config.occlusion.count[1] > 0


def effective_occlusion_range(config: ProjectConfig, wants_unoccluded: bool) -> Range:
    """Closed interval the measured centre occlusion of a frame must fall in.

    Only the targets themselves and the dedicated occluders can cover the anchor. When there is
    no dedicated occluder and no second target, requiring ``occlusion.ratio`` would reject every
    sample, so the category degrades to "no occlusion" instead of pretending it is satisfiable.
    """
    if wants_unoccluded:
        return ZERO_OCCLUSION
    if enabled_target_count(config) < 2 and not has_dedicated_occluder(config):
        return ZERO_OCCLUSION
    return config.occlusion.ratio


def occlusion_accepted(ratio: float, required: Range) -> bool:
    """Whether a measured ratio satisfies the category.

    The endpoints are accepted, but the zero category stays exact: a fraction computed from
    pixel counts is either 0.0 or at least one covered pixel, and a covered pixel is a covered
    pixel rather than a rounding artefact.
    """
    if required.minimum == 0.0 and required.maximum == 0.0:
        return ratio == 0.0
    return required.minimum <= ratio <= required.maximum
