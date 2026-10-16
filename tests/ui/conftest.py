import os
os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from bproc_ui.state import ProjectState
from tests.factories import make_project


@pytest.fixture
def state(tmp_path, qtbot):
    return ProjectState(make_project(tmp_path / "assets"))
