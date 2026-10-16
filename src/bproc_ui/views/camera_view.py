"""Persistent floating composition window with exact pinhole intrinsics."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QDialog, QVBoxLayout, QLabel
from vispy import scene
from vispy.scene.cameras.perspective import PerspectiveCamera
from vispy.visuals.transforms import MatrixTransform

from bproc_core.camera import projection_matrix
from .projection import letterbox_rect
from .scene_view import create_canvas, draw_placements, _clear


class CalibratedCamera(PerspectiveCamera):
    def __init__(self, camera, pose, **kwargs):
        self.calibration, self.pose = camera, pose
        super().__init__(fov=45, scale_factor=1, interactive=False, **kwargs)
        self.transform = MatrixTransform(pose.world_from_camera.T)

    def set_calibration(self, camera, pose):
        self.calibration, self.pose = camera, pose
        self.transform = MatrixTransform(pose.world_from_camera.T)
        self.view_changed()

    def _update_projection_transform(self, fx, fy):
        self._projection.matrix = projection_matrix(self.calibration, self.pose.clip_near_m,
                                                      self.pose.clip_far_m).T

    def viewbox_mouse_event(self, event):
        return  # Composition is controlled by the sampled pose, never by a drag.


class Letterbox(QWidget):
    def __init__(self, native, parent=None):
        super().__init__(parent)
        self.native = native
        native.setParent(self)
        self.image_size = (640, 480)
        self.setStyleSheet("background:#202020;")
        self.setMinimumSize(160, 120)

    def set_image_size(self, width, height):
        self.image_size = (width, height)
        self._place()

    def _place(self):
        # Qt logical coordinates. VisPy alone handles the framebuffer DPR.
        rect = letterbox_rect(self.width(), self.height(), *self.image_size)
        self.native.setGeometry(*rect)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()


class CameraPreview(QDialog):
    def __init__(self, parent=None, canvas_factory=None):
        super().__init__(parent, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setWindowTitle("相机视角 · 候选机位构图预览")
        self.resize(440, 380)
        self.canvas = (canvas_factory or create_canvas)(bgcolor="#3b3b3b")
        self.view = self.canvas.central_widget.add_view()
        self.camera = None
        self._objects = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.caption = QLabel("  等待有效候选机位")
        self.caption.setMinimumHeight(30)
        self.caption.setObjectName("anchorBadge")
        layout.addWidget(self.caption)
        self.letterbox = Letterbox(self.canvas.native)
        layout.addWidget(self.letterbox, 1)
        footer = QLabel("  几何构图预览 · 光影与遮挡率以终端渲染验收为准")
        footer.setObjectName("muted")
        footer.setWordWrap(True)
        footer.setMinimumHeight(31)
        layout.addWidget(footer)

    def set_camera(self, camera, pose):
        self.letterbox.set_image_size(camera.width, camera.height)
        if self.camera is None:
            self.camera = CalibratedCamera(camera, pose)
            self.view.camera = self.camera
        else:
            self.camera.set_calibration(camera, pose)
        self.canvas.update()

    def set_scene(self, candidate, meshes):
        _clear(self._objects)
        self._objects = draw_placements(self.view.scene, candidate, meshes)
        if candidate is None:
            self.caption.setText("  等待有效候选机位")
        else:
            self.caption.setText(f"  中心 {candidate.anchor_key}  |  {candidate.region_key}  |  机位 {candidate.request_index + 1}")
        self.canvas.update()

    def show_preview(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        self.hide()
        event.ignore()
