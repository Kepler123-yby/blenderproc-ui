"""Build export jobs off the Qt event thread; the UI only touches results on the main thread."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from PySide6.QtCore import QObject, QRunnable, Signal


@dataclass(frozen=True)
class ExportRequest:
    """Everything an export needs, captured before the worker starts."""

    request_id: str
    revision: int
    config: object
    path: Path
    project_path: Path | None = None


def new_request(state, path) -> ExportRequest:
    return ExportRequest(uuid.uuid4().hex, int(getattr(state, "context_revision", 0)),
                         state.config, Path(path), getattr(state, "path", None))


class _Signals(QObject):
    finished = Signal(object, object)
    failed = Signal(object, str)


class ExportTask(QRunnable):
    """Runs one `export_function(config, path)` call away from the UI thread."""

    def __init__(self, request: ExportRequest, export_function):
        super().__init__()
        self.request = request
        self.export_function = export_function
        self.signals = _Signals()

    def run(self):
        try:
            result = self.export_function(self.request.config, self.request.path)
        except Exception as error:          # noqa: BLE001 - reported in the panel, with its message
            self.signals.failed.emit(self.request, f"{type(error).__name__}: {error}")
            return
        self.signals.finished.emit(self.request, result)
