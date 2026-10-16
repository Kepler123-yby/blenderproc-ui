from types import SimpleNamespace
from PySide6.QtWidgets import QWidget

from bproc_ui.views.camera_view import CameraPreview


def fake_canvas(**kwargs):
    view = SimpleNamespace(scene=None, camera=None)
    return SimpleNamespace(native=QWidget(), central_widget=SimpleNamespace(add_view=lambda: view), update=lambda: None)


def test_camera_window_reopens_without_replacing_context(qtbot):
    preview = CameraPreview(canvas_factory=fake_canvas)
    qtbot.addWidget(preview)
    preview.show_preview()
    canvas = preview.canvas
    preview.close()
    assert not preview.isVisible()
    preview.show_preview()
    assert preview.isVisible()
    assert preview.canvas is canvas


def test_camera_letterbox_keeps_calibration_size_in_logical_coordinates(qtbot):
    preview = CameraPreview(canvas_factory=fake_canvas)
    qtbot.addWidget(preview)
    preview.letterbox.resize(800, 800)
    preview.letterbox.set_image_size(640, 480)
    assert preview.canvas.native.geometry().getRect() == (0, 100, 800, 600)
