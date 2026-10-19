"""Apply, observe and restore Blender 4.2's native Cycles image tiling.

The only tiling control Cycles exposes is ``scene.cycles.use_auto_tile`` / ``tile_size``; the
renderer still receives the same complete scene in one ``bpy.ops.render.render`` call. This
module therefore reduces the image buffer only — it is not geometry out-of-core. Proof that the
renderer actually scheduled tiles comes from Cycles' own ``render_stats`` lines
("Rendered 7/20 Tiles"), not from reading the property back.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile

TILE_MIN = 8
TILE_MAX = 8192
DEFAULT_TILE_SIZE = 2048


class TilingError(RuntimeError):
    """Cycles tiling cannot be applied or its real value cannot be confirmed."""


@dataclass
class TilingMetadata:
    requested_use_tiling: bool
    requested_tile_size: int
    original_use_tiling: bool | None = None
    original_tile_size: int | None = None
    effective_use_tiling: bool | None = None
    effective_tile_size: int | None = None
    stages: list = field(default_factory=list)
    log_path: str = ""
    capability: str = "image_buffer_only: the scene and its geometry stay fully resident"


class NativeTiling:
    """Own the Cycles tile attributes for one backend lifetime."""

    def __init__(self, bpy, spec, log_dir: Path):
        if isinstance(spec.tile_size, bool) or not isinstance(spec.tile_size, int):
            raise TilingError("图像分块尺寸必须是整数")
        if not TILE_MIN <= spec.tile_size <= TILE_MAX:
            raise TilingError(f"图像分块尺寸必须位于 {TILE_MIN} 到 {TILE_MAX}")
        self.bpy = bpy
        self.use_tiling = bool(spec.use_tiling)
        self.tile_size = int(spec.tile_size)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "cycles_tiles.log"
        self._original: tuple[bool, int] | None = None
        self._installed = False
        self._restored = False
        self._handler = None
        self.metadata = TilingMetadata(self.use_tiling, self.tile_size, log_path=str(self.log_path))

    # -- native attributes -------------------------------------------------------------------
    def _cycles(self):
        scene = getattr(getattr(self.bpy, "context", None), "scene", None)
        cycles = getattr(scene, "cycles", None) if scene is not None else None
        if cycles is None or not hasattr(cycles, "use_auto_tile") or not hasattr(cycles, "tile_size"):
            raise TilingError("当前 Blender 场景没有 Cycles 的 use_auto_tile / tile_size 属性；"
                              "无法启用图像分块（本适配器只使用真实存在的原生属性）")
        return cycles

    def apply(self, stage: str) -> dict:
        """Set and read back the native values; raises when Blender did not accept them."""
        cycles = self._cycles()
        if self._original is None:
            original = (bool(cycles.use_auto_tile), int(cycles.tile_size))
            self._original = original
            self.metadata.original_use_tiling, self.metadata.original_tile_size = original
        cycles.use_auto_tile = self.use_tiling
        cycles.tile_size = self.tile_size
        effective_use = bool(self._cycles().use_auto_tile)
        effective_size = int(self._cycles().tile_size)
        if effective_use != self.use_tiling or effective_size != self.tile_size:
            raise TilingError(
                f"Cycles 未接受图像分块设置：请求 {self.use_tiling}/{self.tile_size}，"
                f"实际 {effective_use}/{effective_size}")
        self.metadata.effective_use_tiling = effective_use
        self.metadata.effective_tile_size = effective_size
        self.metadata.stages.append({"stage": stage,
                                     "use_tiling": effective_use, "tile_size": effective_size,
                                     "at": datetime.now(timezone.utc).isoformat()})
        self._append_log(f"# stage {stage}: use_auto_tile={effective_use} tile_size={effective_size}")
        return asdict(self.metadata)

    # -- native execution evidence -----------------------------------------------------------
    def install_observer(self) -> None:
        handlers = getattr(self.bpy.app, "handlers", None)
        if handlers is None or not hasattr(handlers, "render_stats"):
            return
        def observe(stats, *_rest):
            text = stats if isinstance(stats, str) else str(stats)
            if "Tiles" in text or "Tile" in text:
                self._append_log(text)
        self._handler = observe
        handlers.render_stats.append(observe)
        self._installed = True

    def remove_observer(self) -> None:
        if not self._installed or self._handler is None:
            return
        try:
            handlers = getattr(self.bpy.app, "handlers", None)
            if handlers is not None:
                handlers.render_stats.remove(self._handler)
        except (AttributeError, ValueError, ReferenceError):
            pass
        self._installed = False
        self._handler = None

    def tile_lines(self) -> list[str]:
        try:
            text = self.log_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in text.splitlines() if "Tiles" in line]

    def _append_log(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line.rstrip("\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    # -- lifecycle ---------------------------------------------------------------------------
    def restore(self) -> None:
        self.remove_observer()
        if self._original is None or self._restored:
            return
        try:
            cycles = self._cycles()
        except TilingError:
            self._restored = True
            return
        use_tiling, tile_size = self._original
        cycles.use_auto_tile = use_tiling
        cycles.tile_size = tile_size
        self._restored = True
        self._append_log(f"# restored: use_auto_tile={bool(cycles.use_auto_tile)} "
                         f"tile_size={int(cycles.tile_size)}")

    def write_metadata(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self.metadata)
        payload["tile_schedule_lines"] = self.tile_lines()
        payload["scheduled_tiles_proven"] = bool(payload["tile_schedule_lines"])
        fd, temporary = tempfile.mkstemp(prefix=".tiling-", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, destination)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        return destination
