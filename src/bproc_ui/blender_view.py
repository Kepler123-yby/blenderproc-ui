"""Open the current candidate scene in the Blender GUI without blocking the editor.

Each request carries a snapshot of the project it was built from. A result is only handed to
Blender when that snapshot is still what the editor shows, so switching projects (or refreshing
the candidate) can never open a scene that belongs to the previous one.
"""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from bproc_export.blender_view import open_gui, prepare_scene


@dataclass(frozen=True)
class ViewRequest:
    """Everything a single "open in Blender" click is allowed to depend on."""

    request_id: int
    context_revision: int
    config: object
    candidate: object
    workspace: Path
    script: Path

    @property
    def signature(self):
        return f"请求 {self.request_id} · 上下文 {self.context_revision}"


class _Signals(QObject):
    prepared = Signal(object, object)
    failed = Signal(object, str)


class _Prepare(QRunnable):
    """Rebuilding the scene takes a few seconds, so it stays off the GUI thread."""

    def __init__(self, request):
        super().__init__()
        self.request = request
        self.signals = _Signals()

    @Slot()
    def run(self):
        request = self.request
        try:
            blend = prepare_scene(request.config, request.candidate, request.workspace, request.script,
                                  geometry_only=True)
        except Exception as error:
            self.signals.failed.emit(request, str(error))
        else:
            self.signals.prepared.emit(request, blend)


class BlenderViewer(QObject):
    status = Signal(str)
    opened = Signal(object)
    failed = Signal(str)

    def __init__(self, state, parent=None, pool=None, script=None):
        super().__init__(parent)
        self.state = state
        self.pool = pool or QThreadPool.globalInstance()
        self.script = Path(script) if script else None
        self.session = None
        self._request = None
        self._next_request = 0
        self._closed = False

    @property
    def script(self) -> Path:
        return self._script

    @script.setter
    def script(self, value) -> None:
        if value is None:
            root = Path(__file__).resolve().parents[2]
            value = root / "tools" / "blender_view.py"
        self._script = Path(value)

    @property
    def workspace(self) -> Path:
        base = Path(self.state.path).expanduser().parent if self.state.path else Path.cwd()
        return base / ".blender-view"

    @property
    def busy(self) -> bool:
        return self._request is not None

    def close(self):
        """A finishing background build must not open a window after the editor closes."""
        self._closed = True
        self._request = None

    def open(self) -> bool:
        """Rebuild the candidate scene in the background, then open the Blender GUI."""
        if self._closed:
            return False
        candidate = self.state.candidate
        if candidate is None:
            self.failed.emit("先调整机位得到候选构图，才能在 Blender 中查看")
            return False
        if self._request is not None:
            self.status.emit("场景正在重建，请稍候")
            return False
        self._next_request += 1
        config = self.state.candidate_config or self.state.config
        request = ViewRequest(self._next_request, self.state.context_revision, config,
                              candidate, self.workspace, self.script)
        task = _Prepare(request)
        task.signals.prepared.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        self._request = request
        self.status.emit("正在用当前候选构图重建 Blender 场景…")
        self.pool.start(task)
        return True

    def _current(self, request) -> bool:
        return (self._request is request
                and self.state.context_revision == request.context_revision
                and self.state.candidate is request.candidate)

    def _expire(self, request, reason) -> None:
        self._request = None
        self.status.emit(f"{reason}，这次在 Blender 中查看已取消（{request.signature}）")

    def _stale_reason(self, request) -> str:
        if self.state.context_revision != request.context_revision:
            return "项目已切换"
        return "候选构图已刷新"

    @Slot(object, object)
    def _prepared(self, request, blend) -> None:
        if not self._current(request):
            if self._request is request:
                self._expire(request, self._stale_reason(request))
            return
        self._request = None
        try:
            self.session = open_gui(request.config, blend, request.script, request.workspace)
        except Exception as error:
            self.failed.emit(str(error))
            return
        self.status.emit(f"已在 Blender 中打开当前构图 · 日志 {self.session.log}" + self._hdri_hint(request.config))
        self.opened.emit(self.session)

    def _hdri_hint(self, config) -> str:
        if config.hdri.files:
            return ""
        return "（还没选环境图，预览用中性灰背景）"

    @Slot(object, str)
    def _failed(self, request, message) -> None:
        if self._request is not request:
            return                             # a late failure must never clear a newer task
        if self.state.context_revision != request.context_revision or self.state.candidate is not request.candidate:
            self._expire(request, self._stale_reason(request))
            return
        self._request = None
        self.failed.emit(message)
