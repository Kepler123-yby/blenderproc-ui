"""CPU mesh parsing stays off the GUI thread; uploads stay in the GUI thread."""

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from bproc_core.assets import load_mesh


class _Signals(QObject):
    loaded = Signal(str, int, object)
    failed = Signal(str, int, str)


class _LoadTask(QRunnable):
    def __init__(self, asset, generation):
        super().__init__()
        self.asset, self.generation = asset, generation
        self.signals = _Signals()

    @Slot()
    def run(self):
        try:
            mesh = load_mesh(self.asset)
        except Exception as error:
            self.signals.failed.emit(self.asset.key, self.generation,
                                     f"{self.asset.path}（{self.asset.unit}）：{error}")
        else:
            self.signals.loaded.emit(self.asset.key, self.generation, mesh)


class AssetLoader(QObject):
    loaded = Signal(str, int, object)
    failed = Signal(str, int, str)

    def __init__(self, parent=None, pool=None):
        super().__init__(parent)
        self.pool = pool or QThreadPool.globalInstance()

    def request(self, asset, generation):
        task = _LoadTask(asset, generation)
        task.signals.loaded.connect(self.loaded)
        task.signals.failed.connect(self.failed)
        self.pool.start(task)
