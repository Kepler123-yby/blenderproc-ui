"""Interactive geometry viewport. Its observer camera never edits sampling."""

from itertools import product

import numpy as np
from PySide6.QtWidgets import QWidget, QVBoxLayout
from vispy import app, scene
from vispy.visuals.transforms import MatrixTransform

from bproc_core.camera import sample_camera
from bproc_core.scene import bounds_corners, transform_points


def create_canvas(**kwargs):
    app.use_app("pyside6")
    kwargs.setdefault("bgcolor", "#3b3b3b")
    return scene.SceneCanvas(keys=None, show=False, **kwargs)


def _clear(nodes):
    for node in nodes:
        node.parent = None
    nodes.clear()


def _box_lines(bounds):
    corners = np.array(list(product(*zip(bounds[0], bounds[1]))))
    segments = []
    for i, p in enumerate(corners):
        for j in range(i + 1, len(corners)):
            if np.count_nonzero(corners[j] != p) == 1:
                segments.extend((p, corners[j]))
    return np.asarray(segments)


def draw_placements(parent, candidate, meshes, selected_key=None, outlines=False):
    nodes = []
    if candidate is None:
        return nodes
    for placement in candidate.placements:
        mesh = meshes.get(placement.asset_key)
        if mesh is None:
            continue
        rgba = mesh.vertex_rgba
        if rgba is not None:
            rgba = np.asarray(rgba, dtype=float)
            if rgba.max(initial=0) > 1:
                rgba = rgba / 255.
        color = (.53, .56, .59, 1) if placement.role == "target" else (.34, .39, .43, 1)
        visual = scene.visuals.Mesh(vertices=mesh.vertices_m, faces=mesh.faces,
                                    vertex_colors=rgba, color=color, shading="smooth", parent=parent)
        visual.transform = MatrixTransform(placement.world_from_model.T)
        nodes.append(visual)
        if outlines and placement.target_key == selected_key:
            outline = scene.visuals.Line(_box_lines(mesh.bounds_m), color="#f99c33", width=2,
                                         connect="segments", method="gl", parent=parent)
            outline.transform = MatrixTransform(placement.world_from_model.T)
            nodes.append(outline)
    return nodes


class SceneView(QWidget):
    def __init__(self, parent=None, canvas_factory=None):
        super().__init__(parent)
        self.canvas = (canvas_factory or create_canvas)()
        self.view = self.canvas.central_widget.add_view()
        self.view.camera = scene.cameras.TurntableCamera(fov=45, elevation=24, azimuth=42,
                                                       up="+z", scale_factor=1.8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas.native)
        self._objects, self._regions, self._helpers, self._points = [], [], [], []
        self._candidate = None
        self._meshes = {}
        self._selected_key = None
        self._last_keys = ()
        self._config = None
        self._grid(1)

    def _grid(self, scale):
        _clear(self._helpers)
        scale = max(float(scale), .01)
        length = scale * 1.2
        lines = []
        for a in np.linspace(-length, length, 21):
            lines.extend(([-length, a, 0], [length, a, 0], [a, -length, 0], [a, length, 0]))
        self._helpers.append(scene.visuals.Line(np.array(lines), connect="segments", color="#4a4a4a",
                                                width=1, parent=self.view.scene))
        for axis, color, name in ((0, "#a35858", "X"), (1, "#65a06d", "Y"), (2, "#6596c8", "Z")):
            end = np.zeros(3)
            end[axis] = length
            self._helpers.append(scene.visuals.Line(np.array([np.zeros(3), end]), color=color,
                                                    width=1.5, parent=self.view.scene))
            self._helpers.append(scene.visuals.Text(name, pos=end, color=color, font_size=9,
                                                    parent=self.view.scene))

    def set_selection(self, key):
        self._selected_key = key
        self.set_scene(self._candidate, self._meshes)

    def set_scene(self, candidate, meshes):
        self._candidate, self._meshes = candidate, meshes
        _clear(self._objects)
        self._objects = draw_placements(self.view.scene, candidate, meshes, self._selected_key, True)
        if candidate is not None:
            keys = tuple((p.asset_key, p.role) for p in candidate.placements)
            if keys != self._last_keys:
                self._last_keys = keys
                self.fit_scene()
        self.canvas.update()

    def fit_scene(self):
        # Frame the imported geometry; a helper grid sized by camera distance would dwarf a
        # millimetre-scale or centimetre-scale model.
        radius = self._content_radius()
        if radius <= 0 and self._candidate is not None:
            radius = self._candidate.camera.radius_m
        if radius <= 0:
            radius = 1.
        self.view.camera.center = (0, 0, 0)
        self.view.camera.scale_factor = max(radius * 2.5, .02)
        self._grid(radius)

    def _content_radius(self):
        """Half diagonal of the placed geometry, in metres."""
        if self._candidate is None:
            return 0.
        radius = 0.
        for placement in self._candidate.placements:
            mesh = self._meshes.get(placement.asset_key)
            bounds = None if mesh is None else np.asarray(mesh.bounds_m, dtype=float)
            if bounds is None or bounds.shape != (2, 3) or not np.isfinite(bounds).all():
                continue
            corners = transform_points(bounds_corners(bounds), np.asarray(placement.world_from_model))
            radius = max(radius, float(np.linalg.norm(np.ptp(corners, axis=0))) / 2)
        return radius

    def set_regions(self, config, selected_key=None):
        self._config = config
        _clear(self._regions)
        for region in config.regions:
            angle = region.azimuth
            span = 360. if angle.full_circle else (angle.end_deg - angle.start_deg) % 360.
            phis = np.deg2rad(angle.start_deg + np.linspace(0, span, 80))
            lo, hi = np.deg2rad([region.elevation_deg.minimum, region.elevation_deg.maximum])
            color = (.79, .62, .36, .55) if region.key == selected_key else (.47, .59, .69, .3)
            curves = []
            for r in {region.radius_m.minimum, region.radius_m.maximum}:
                for alpha in {lo, hi}:
                    curves.append(r * np.column_stack([np.cos(alpha) * np.cos(phis),
                                                       np.cos(alpha) * np.sin(phis), np.full(len(phis), np.sin(alpha))]))
                for phi in {phis[0], phis[-1]}:
                    alphas = np.linspace(lo, hi, 40)
                    curves.append(r * np.column_stack([np.cos(alphas) * np.cos(phi),
                                                       np.cos(alphas) * np.sin(phi), np.sin(alphas)]))
            for curve in curves:
                self._regions.append(scene.visuals.Line(curve, color=color, width=1, parent=self.view.scene))
        for camera in config.fixed_cameras:
            phi, alpha = np.deg2rad([camera.azimuth_deg, camera.elevation_deg])
            position = camera.radius_m * np.array([np.cos(alpha) * np.cos(phi),
                                                   np.cos(alpha) * np.sin(phi), np.sin(alpha)])
            color = (.95, .72, .38, .95) if camera.key == selected_key else (.62, .70, .78, .85)
            self._regions.append(scene.visuals.Line(np.vstack([position, np.zeros(3)]), color=color,
                                                    width=1, parent=self.view.scene))
            marker = scene.visuals.Markers(parent=self.view.scene)
            marker.set_data(np.atleast_2d(position), face_color=color, edge_color="#302a24", size=9)
            self._regions.append(marker)
        self.canvas.update()

    def set_candidates(self, candidates):
        _clear(self._points)
        if candidates:
            positions = np.array([c.camera.position_m for c in candidates])
            visual = scene.visuals.Markers(parent=self.view.scene)
            visual.set_data(positions, face_color="#e2b16b", edge_color="#302a24", size=7)
            self._points.append(visual)
            for candidate in candidates:
                pose = candidate.camera
                length = pose.radius_m * .08
                rotation, position = pose.world_from_camera[:3, :3], pose.position_m
                corners = np.array([[-.7, -.5, -1], [.7, -.5, -1], [.7, .5, -1], [-.7, .5, -1]])
                corners = corners * length @ rotation.T + position
                segments = []
                for i in range(4):
                    segments.extend((position, corners[i], corners[i], corners[(i + 1) % 4]))
                self._points.append(scene.visuals.Line(np.array(segments), connect="segments",
                                                       color="#d6ad70", width=1, parent=self.view.scene))
        self.canvas.update()
