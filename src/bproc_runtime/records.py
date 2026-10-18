"""Stable run counters and recorder interface."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FrameRef:
    frame_index: int
    relative_rgb: str
    bop_scene_id: int | None = None
    bop_image_id: int | None = None
    gt_obj_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RunSummary:
    status: str
    requested: int
    written_frames: int
    saved: int
    skipped: int
    failures_by_reason: dict[str, int]
    exit_code: int


class RunRecorder(Protocol):
    def begin(self, config) -> None: ...
    def record_frame(self, ref, candidate, evidence) -> None: ...
    def record_rejection(self, scene_index, view_index, attempt_index, reason, *, phase="view") -> None: ...
    def finish(self, summary: RunSummary) -> None: ...
