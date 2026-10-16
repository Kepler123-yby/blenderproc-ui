from dataclasses import replace
import numpy as np
import pytest

from bproc_core.config import CameraSpec, RegionSpec
from bproc_core.camera import sample_camera, project_points
from bproc_ui.views.projection import preview_pixels, letterbox_rect


def test_vispy_transforms_match_asymmetric_core_projection():
    camera = CameraSpec(width=801, height=537, fx=772.3, fy=610.5, cx=295.7, cy=310.4)
    pose = sample_camera(RegionSpec(), np.array([.12, -.04, .07]), np.eye(3), camera, np.random.default_rng(72))
    points = np.array([[.12, -.04, .07], [.03, .05, .02], [.15, -.09, .04], [-.01, .03, .14]])
    expected, valid = project_points(points, pose, camera)
    assert valid.all()
    assert preview_pixels(points, pose, camera) == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize("window,image,expected", [
    ((800, 800), (640, 480), (0, 100, 800, 600)),
    ((1600, 1600), (640, 480), (0, 200, 1600, 1200)),
    ((800, 450), (640, 480), (100, 0, 600, 450)),
    ((0, 100), (640, 480), (0, 0, 0, 0)),
])
def test_letterbox_dimensions(window, image, expected):
    assert letterbox_rect(*window, *image) == expected
