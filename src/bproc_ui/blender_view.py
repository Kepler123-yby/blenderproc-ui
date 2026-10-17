"""Open the current candidate scene in the Blender GUI without blocking the editor."""

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from bproc_export.blender_view import open_gui, prepare_scene


class _Signals(QObject):
    prepared = Signal(object)
    failed = Signal(str)


class _Prepare(QRunnable):
    """Rebuilding the scene takes a few seconds, so it stays off the GUI thread."""

    def __init__(self, config, candidate, workspace, script):
        super().__init__()
        self.config, self.candidate = config, candidate
        self.workspace, self.script = workspace, script
        self.signals = _Signals()

    @Slot()
    def run(self):
        try:
            blend = prepare_scene(self.config, self.candidate, self.workspace, self.script)
        except Exception as error:
            self.signals.failed.emit(str(error))
        else:
            self.signals.prepared.emit(blend)


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
        self._task = None

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

    def open(self) -> bool:
        """Rebuild the candidate scene in the background, then open the Blender GUI."""
        candidate = self.state.candidate
        if candidate is None:
            self.failed.emit("先调整机位得到候选构图，才能在 Blender 中查看")
            return False
        if self._task is not None:
            self.status.emit("场景正在重建，请稍候")
            return False
        task = _Prepare(self.state.config, candidate, self.workspace, self.script)
        task.signals.prepared.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        self._task = task
        self.status.emit("正在用当前候选构图重建 Blender 场景…")
        self.pool.start(task)
        return True

    @Slot(object)
    def _prepared(self, blend) -> None:
        self._task = None
        try:
            self.session = open_gui(self.state.config, blend, self.script, self.workspace)
        except Exception as error:
            self._failed(str(error))
            return
        self.status.emit(f"已在 Blender 中打开当前构图 · 日志 {self.session.log}")
        self.opened.emit(self.session)

    @Slot(str)
    def _failed(self, message) -> None:
        self._task = None
        self.failed.emit(message)
