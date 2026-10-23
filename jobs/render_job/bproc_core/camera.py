"""Column-vector, OpenGL camera coordinates and calibrated pixel projection."""
from __future__ import annotations

from dataclasses import dataclass, replace
import numpy as np

from .config import Azimuth, CameraSpec, Range, RegionSpec


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


def fit_distance(bounds_m, camera: CameraSpec, azimuths_deg=(0.,), elevation_deg: float = 20.,
                 fill: float = .8) -> float:
    """Smallest orbit distance that keeps the whole box inside ``fill`` of the frame.

    ``fit_radius_m`` circumscribes the bounding sphere, which over-estimates the distance for a
    spread of small targets and under-estimates it for a flat one: both ends produced regions
    whose cameras missed the frame. Projecting the eight corners is exact.
    """
    bounds = np.asarray(bounds_m, dtype=float)
    if bounds.shape != (2, 3) or (bounds[1] <= bounds[0]).any():
        raise ValueError("Bounds must be a (2, 3) minimum/maximum pair")
    if not 0 < fill <= 1:
        raise ValueError("Fill must be in (0, 1]")
    corners = np.array([[x, y, z] for x in bounds[:, 0] for y in bounds[:, 1] for z in bounds[:, 2]])
    limit_x, limit_y = fill * camera.width / 2, fill * camera.height / 2

    def fits(distance: float) -> bool:
        for azimuth in azimuths_deg:
            region = RegionSpec("fit", radius_m=Range(distance, distance),
                                azimuth=Azimuth(float(azimuth), float(azimuth), False),
                                elevation_deg=Range(elevation_deg, elevation_deg))
            pose = sample_camera(region, np.zeros(3), np.eye(3), camera, np.random.default_rng(0))
            pixels, valid = project_points(corners, pose, camera)
            if not valid.all():
                return False
            if np.any(np.abs(pixels[:, 0] - camera.width / 2) > limit_x):
                return False
            if np.any(np.abs(pixels[:, 1] - camera.height / 2) > limit_y):
                return False
        return True

    span = float(np.linalg.norm(bounds[1] - bounds[0])) or 1.
    low, high = 1e-5, max(1., span * 50)
    if not fits(high):
        return high
    for _ in range(45):
        middle = (low + high) / 2
        low, high = (low, middle) if fits(middle) else (middle, high)
    return high


@dataclass(frozen=True)
class FramingWindow:
    """Allowed pixel rectangle with the same half-pixel convention as ``project_points``."""

    left: float
    right: float
    top: float
    bottom: float

    def contains(self, pixels: np.ndarray) -> bool:
        pixels = np.asarray(pixels, dtype=float)
        return bool(np.all(pixels[:, 0] >= self.left) and np.all(pixels[:, 0] <= self.right)
                    and np.all(pixels[:, 1] >= self.top) and np.all(pixels[:, 1] <= self.bottom))


def framing_window(camera: CameraSpec, margin_fraction: float) -> FramingWindow:
    """Pixel rectangle used by opt-in group reframing.

    ``L/U/T/B`` follow the documented contract; the principal point must lie strictly inside,
    otherwise no camera change can make the window usable and the caller has to reject it.
    """
    margin = float(margin_fraction)
    if not 0.0 <= margin < 0.45:
        raise ValueError("取景边距必须位于 0 到 0.45（不含）")
    width, height = float(camera.width), float(camera.height)
    window = FramingWindow(left=-0.5 + margin * width, right=width - 0.5 - margin * width,
                           top=-0.5 + margin * height, bottom=height - 0.5 - margin * height)
    if not (window.left < camera.cx < window.right and window.top < camera.cy < window.bottom):
        raise ValueError("主点位于允许的取景矩形之外：无法在不修改内参的前提下重取景")
    return window


def bounds_corners(bounds_m) -> np.ndarray:
    bounds = np.asarray(bounds_m, dtype=float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or (bounds[1] <= bounds[0]).any():
        raise ValueError("包络必须是有限的 (2, 3) 最小/最大值")
    return np.array([[x, y, z] for x in bounds[:, 0] for y in bounds[:, 1] for z in bounds[:, 2]])


def _pose_at(radius: float, azimuth_deg: float, elevation_deg: float, camera: CameraSpec) -> CameraPose:
    region = RegionSpec(key="reframe", radius_m=Range(radius, radius),
                        azimuth=Azimuth(float(azimuth_deg), float(azimuth_deg), False),
                        elevation_deg=Range(float(elevation_deg), float(elevation_deg)))
    return sample_camera(region, np.zeros(3), np.eye(3), camera, np.random.default_rng(0))


def bounds_fit_window(bounds_m, camera: CameraSpec, azimuth_deg: float, elevation_deg: float,
                      window: FramingWindow, radius_m: float) -> bool:
    """Every corner enters the window and passes the existing near/far clip rules."""
    pose = _pose_at(float(radius_m), azimuth_deg, elevation_deg, camera)
    pixels, valid = project_points(bounds_corners(bounds_m), pose, camera)
    return bool(valid.all()) and window.contains(pixels)


def fit_bounds_radius(bounds_m, camera: CameraSpec, azimuth_deg: float, elevation_deg: float,
                      margin_fraction: float, *, iterations: int = 60) -> float:
    """Smallest orbit radius that keeps the whole box inside the margin window."""
    window = framing_window(camera, margin_fraction)
    span = float(np.linalg.norm(np.ptp(np.asarray(bounds_m, dtype=float), axis=0))) or 1.0
    low = max(1e-9, span * 1e-9)
    high = max(1.0, span * 50.0)
    if not bounds_fit_window(bounds_m, camera, azimuth_deg, elevation_deg, window, high):
        # The far clip grows with the radius, so doubling eventually fits every finite box.
        for _ in range(80):
            high *= 2.0
            if bounds_fit_window(bounds_m, camera, azimuth_deg, elevation_deg, window, high):
                break
        else:
            raise ValueError("无法将目标包络放入取景窗口")
    for _ in range(iterations):
        middle = (low + high) / 2.0
        if bounds_fit_window(bounds_m, camera, azimuth_deg, elevation_deg, window, middle):
            high = middle
        else:
            low = middle
    return high


def envelope_radius(bounds_m) -> float:
    """Largest corner norm around the anchor origin, used by the random-camera contract."""
    corners = bounds_corners(bounds_m)
    return float(np.linalg.norm(corners, axis=1).max())


def random_fit_radius(bounds_m, camera: CameraSpec, margin_fraction: float) -> float:
    """Closed-form radius that keeps every corner in frame from any direction.

    ``h`` converts the four pixel gaps into the tangent of the allowed half-angle;
    ``R / sin(atan(h))`` is the distance at which a sphere of radius ``R`` fills exactly that
    angle. It is conservative for the box and never depends on the sampled direction.
    """
    window = framing_window(camera, margin_fraction)
    radius = envelope_radius(bounds_m)
    h = min((camera.cx - window.left) / camera.fx, (window.right - camera.cx) / camera.fx,
            (camera.cy - window.top) / camera.fy, (window.bottom - camera.cy) / camera.fy)
    if not np.isfinite(h) or h <= 0:
        raise ValueError("取景窗口没有可用的半角")
    return radius / float(np.sin(np.arctan(h)))
