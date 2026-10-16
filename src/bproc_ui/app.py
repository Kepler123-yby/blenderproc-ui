"""Native application entry point, installed separately from the bop environment."""

import argparse
import os
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from bproc_core.config import new_project
from bproc_core.project_io import load_project
from .state import ProjectState
from .theme import apply_theme


def create_app(argv=None):
    os.environ.setdefault("QT_API", "pyside6")
    existing = QApplication.instance()
    if existing:
        return existing
    fmt = QSurfaceFormat()
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    fmt.setVersion(2, 1)
    fmt.setProfile(QSurfaceFormat.NoProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(list(argv or []))
    app.setApplicationName("BlenderProc UI")
    app.setOrganizationName("BlenderProc UI")
    apply_theme(app)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="BlenderProc 本地数据集工作区")
    parser.add_argument("--project", type=Path, help="打开 YAML 项目配置")
    arguments = parser.parse_args(argv)
    app = create_app(sys.argv)
    try:
        config = load_project(arguments.project) if arguments.project else new_project(Path.cwd())
    except (OSError, ValueError) as error:
        parser.error(str(error))
    state = ProjectState(config)
    state.path = arguments.project.resolve() if arguments.project else None
    from .main_window import MainWindow
    window = MainWindow(state)
    window.show()
    return app.exec()
