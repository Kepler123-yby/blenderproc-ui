"""The sole column-vector to VisPy row-vector boundary."""

import numpy as np
from vispy.visuals.transforms import MatrixTransform

from bproc_core.camera import projection_matrix


def letterbox_rect(window_width, window_height, image_width, image_height):
    if min(window_width, window_height, image_width, image_height) <= 0:
        return (0, 0, 0, 0)
    scale = min(window_width / image_width, window_height / image_height)
    width = min(window_width, round(image_width * scale))
    height = min(window_height, round(image_height * scale))
    return ((window_width - width) // 2, (window_height - height) // 2, width, height)


def preview_pixels(points, pose, camera):
    """Exercise VisPy transforms, independently of core.project_points."""
    view = MatrixTransform(np.linalg.inv(pose.world_from_camera).T)
    projection = MatrixTransform(projection_matrix(camera, pose.clip_near_m, pose.clip_far_m).T)
    camera_points = view.map(np.asarray(points, dtype=float))
    clip = projection.map(camera_points)
    ndc = clip[:, :2] / clip[:, 3, None]
    return np.column_stack(((ndc[:, 0] + 1) * camera.width / 2 - .5,
                            (1 - ndc[:, 1]) * camera.height / 2 - .5))
