"""Column-vector, OpenGL camera coordinates and calibrated pixel projection."""
from __future__ import annotations

from dataclasses import dataclass, replace
import numpy as np

from .config import CameraSpec, RegionSpec


@dataclass(frozen=True)
class CameraPose:
    world_from_camera: np.ndarray
    position_m: np.ndarray
    radius_m: float
    azimuth_deg: float
    elevation_deg: float
    clip_near_m: float
    clip_far_m: float


def sample_camera(region: RegionSpec, center_m: np.ndarray, anchor_rotation: np.ndarray,
                  camera: CameraSpec, rng: np.random.Generator) -> CameraPose:
    radius = float(rng.uniform(region.radius_m.minimum, region.radius_m.maximum))
    azimuth = region.azimuth
    width = 360. if azimuth.full_circle else (azimuth.end_deg - azimuth.start_deg) % 360.
    phi = float(azimuth.start_deg + rng.uniform(0., width))
    phi = (phi + 180.) % 360. - 180.
    sin_alpha = rng.uniform(np.sin(np.deg2rad(region.elevation_deg.minimum)),
                            np.sin(np.deg2rad(region.elevation_deg.maximum)))
    alpha = float(np.rad2deg(np.arcsin(np.clip(sin_alpha, -1., 1.))))
    p, a = np.deg2rad([phi, alpha])
    offset = np.array([np.cos(a) * np.cos(p), np.cos(a) * np.sin(p), np.sin(a)]) * radius
    rotation = np.asarray(anchor_rotation, dtype=float)
    center = np.asarray(center_m, dtype=float)
    position = center + rotation @ offset
    forward = (center - position) / radius
    up = rotation @ np.array([0., 0., 1.])
    if abs(np.dot(forward, up)) > .999999:
        up = rotation @ np.array([0., 1., 0.])
    right = np.cross(forward, up)
    right /= np.linalg.norm(right)
    actual_up = np.cross(right, forward)
    world = np.eye(4)
    world[:3, :3] = np.column_stack([right, actual_up, -forward])
    world[:3, 3] = position
    return CameraPose(world, position, radius, phi, alpha,
                      max(1e-6, radius * .001), max(10., radius * 100.))


def project_points(points_m, pose: CameraPose, camera: CameraSpec) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(points_m, dtype=float).reshape(-1, 3)
    local = (points - pose.position_m) @ pose.world_from_camera[:3, :3]
    depth = -local[:, 2]
    with np.errstate(divide='ignore', invalid='ignore'):
        pixels = np.column_stack([camera.fx * local[:, 0] / depth + camera.cx,
                                  -camera.fy * local[:, 1] / depth + camera.cy])
    valid = (np.isfinite(pixels).all(axis=1) & (depth >= pose.clip_near_m)
             & (depth <= pose.clip_far_m))
    return pixels, valid


def projection_matrix(camera: CameraSpec, near: float, far: float) -> np.ndarray:
    if not 0 < near < far:
        raise ValueError("Projection requires 0 < near < far")
    w, h = camera.width, camera.height
    return np.array([[2 * camera.fx / w, 0., 1 - 2 * (camera.cx + .5) / w, 0.],
                     [0., 2 * camera.fy / h, 2 * (camera.cy + .5) / h - 1, 0.],
                     [0., 0., -(far + near) / (far - near), -2 * far * near / (far - near)],
                     [0., 0., -1., 0.]])


def resize_camera(camera: CameraSpec, width: int, height: int) -> CameraSpec:
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    if camera.auto_scale:
        sx, sy = width / camera.width, height / camera.height
        return replace(camera, width=width, height=height, fx=camera.fx * sx,
                       fy=camera.fy * sy, cx=camera.cx * sx, cy=camera.cy * sy)
    return replace(camera, width=width, height=height)


def fit_radius_m(bounds_m, camera: CameraSpec, fill: float = .8) -> float:
    """Distance at which the bbox covers ``fill`` of the frame height.

    Model sizes in this tool span millimetres to metres, so every default distance
    has to come from the asset itself instead of a fixed number.
    """
    if not 0 < fill <= 1:
        raise ValueError("Fill must be in (0, 1]")
    bounds = np.asarray(bounds_m, dtype=float)
    if bounds.shape != (2, 3) or (bounds[1] <= bounds[0]).any():
        raise ValueError("Bounds must be a (2, 3) minimum/maximum pair")
    # Bounding-sphere radius: the silhouette radius is pose dependent, so any smaller
    # surrogate clips the box corners when the orbit tilts.
    radius = float(np.linalg.norm(bounds[1] - bounds[0])) / 2
    return radius / (fill * (camera.height / 2) / camera.fy)
