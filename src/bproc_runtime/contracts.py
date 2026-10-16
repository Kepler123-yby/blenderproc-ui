"""The boundary between scene sampling and a rendering engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import numpy as np


class RenderError(RuntimeError):
    """A render pass failed; the run must stop rather than reuse stale output."""


@dataclass(frozen=True)
class VisibilityEvidence:
    anchor_full: np.ndarray
    visible: dict[int, np.ndarray]


@dataclass(frozen=True)
class RenderedFrame:
    rgb: np.ndarray
    depth_m: np.ndarray | None
    full_masks: dict[int, np.ndarray] | None
    visible_masks: dict[int, np.ndarray]


class Backend(Protocol):
    def prepare(self, config, meshes) -> None: ...
    def apply(self, candidate) -> None: ...
    def assess(self, anchor_obj_id: int) -> VisibilityEvidence: ...
    def render(self, evidence: VisibilityEvidence) -> RenderedFrame: ...
    def close(self) -> None: ...
