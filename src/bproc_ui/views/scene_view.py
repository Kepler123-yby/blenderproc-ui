"""Interactive geometry viewport. Its observer camera never edits sampling."""

from itertools import product

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget, QVBoxLayout
from vispy import app, scene
from vispy.visuals.transforms import MatrixTransform

from bproc_core.camera import sample_camera
from bproc_core.config import hex_to_rgb
from bproc_core.scene import bounds_corners, placement_bounds, transform_points



def create_canvas(**kwargs):
    app.use_app("pyside6")
    kwargs.setdefault("bgcolor", "#393939")  # Blender's solid-shading viewport grey
    return scene.SceneCanvas(keys=None, show=False, **kwargs)


def _clear(nodes):
    for node in nodes:
        node.parent = None
    nodes.clear()


def placed_content_bounds(candidate, meshes):
    """World bounds of every placed target and occluder, or None when nothing is placed.

    Sampling spheres, camera markers and helper grids are deliberately excluded: the viewport
    frames the geometry the renderer will actually see.
    """
    if candidate is None:
        return None
    bounds = []
    for placement in candidate.placements:
        if placement.asset_key not in meshes:
            continue
        box = np.asarray(placement_bounds(placement, meshes), dtype=float)
        if box.shape == (2, 3) and np.isfinite(box).all():
            bounds.append(box)
    if not bounds:
        return None
    return np.array([np.min([b[0] for b in bounds], axis=0), np.max([b[1] for b in bounds], axis=0)])


def target_preview_rgba(config):
    """Base colour shared by both viewports; None means keep the imported vertex colours."""
    material = getattr(config, "material", None)
    if material is None or getattr(material, "color", "original") != "solid":
        return None
    return (*hex_to_rgb(material.base_color), 1.)


def _box_lines(bounds):
    corners = np.array(list(product(*zip(bounds[0], bounds[1]))))
    segments = []
    for i, p in enumerate(corners):
        for j in range(i + 1, len(corners)):
            if np.count_nonzero(corners[j] != p) == 1:
                segments.extend((p, corners[j]))
    return np.asarray(segments)


def draw_placements(parent, candidate, meshes, selected_key=None, outlines=False, target_rgba=None):
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
        if placement.role == "target" and target_rgba is not None:
            rgba, color = None, target_rgba
        visual = scene.visuals.Mesh(vertices=mesh.vertices_m, faces=mesh.faces,
                                    vertex_colors=rgba, color=color,
                                    shading="smooth", parent=parent)
        visual.transform = MatrixTransform(placement.world_from_model.T)
        nodes.append(visual)
        if outlines and placement.target_key == selected_key:
            outline = scene.visuals.Line(_box_lines(mesh.bounds_m), color="#f99c33", width=2,
                                         connect="segments", method="gl", parent=parent)
            outline.transform = MatrixTransform(placement.world_from_model.T)
            nodes.append(outline)
    return nodes


def nice_step(value):
    """Round a cell size to 1/2/5 × 10^n so the grid reads in human units."""
    if not np.isfinite(value) or value <= 0:
        return 1e-3
    exponent = int(np.floor(np.log10(value)))
    base = value / 10 ** exponent
    return (1. if base < 1.5 else 2. if base < 3.5 else 5.) * 10 ** exponent


def floor_grid(scale, cells=30):
    """Blender-style floor patch: a 30×30 grid of human-sized cells that fades to zero.

    The patch is bounded but its alpha reaches zero inside the last cell, so from any camera
    the floor dissolves instead of ending — the grid still reads as borderless.
    """
    scale = max(float(scale), 1e-4)
    step = nice_step(scale / 4.)
    extent = cells / 2. * step
    minor, section = [], []
    for i in range(-cells // 2, cells // 2 + 1):
        (section if i % 5 == 0 else minor).append(i * step)

    def segments(values):
        pairs = []
        for at in values:
            pairs.extend(([-extent, at, 0], [extent, at, 0], [at, -extent, 0], [at, extent, 0]))
        return np.array(pairs, dtype=float).reshape(-1, 3)

    return {"extent": extent,
            "step": step,
            "minor": segments(minor),
            "section": segments(section),
            "axes": (np.array([[-extent, 0, 0], [extent, 0, 0]], dtype=float),
                     np.array([[0, -extent, 0], [0, extent, 0]], dtype=float),
                     np.array([[0, 0, 0], [0, 0, extent * .5]], dtype=float))}


class SceneView(QWidget):
    def __init__(self, parent=None, canvas_factory=None):
        super().__init__(parent)
        self.canvas = (canvas_factory or create_canvas)()
        self.view = self.canvas.central_widget.add_view()
        self.view.camera = scene.cameras.TurntableCamera(fov=45, elevation=32, azimuth=42,
                                                       up="+z", scale_factor=1.8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas.native)
        self.view_label = QLabel("用户透视", self)
        self.view_label.setObjectName("muted")
        self.view_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.view_label.move(10, 8)
        self._objects, self._regions, self._points = [], [], []
        self._candidate = None
        self._meshes = {}
        self._selected_key = None
        self._last_keys = ()
        self._config = None
        self._helper_radius = None
        self._fit_bounds = None
        self._hud, self._grid_scale = [], None
        self._grid(1)

    def _grid(self, scale):
        """Blender-style ground grid: faded minor cells, brighter section lines, coloured axes.

        Rebuilt only when the scale changes, and always with real data: a visual created
        empty and filled in later can keep stale bounds and silently vanish.
        """
        scale = max(float(scale), 1e-4)
        if self._grid_scale is not None and abs(scale - self._grid_scale) <= .02 * self._grid_scale:
            return
        self._grid_scale = scale
        _clear(self._hud)
        grid = floor_grid(scale)
        extent = grid["extent"]

        def add(positions, color, width, fade=True):
            """Distance bands rather than per-vertex alpha: some GL drivers drop vertex alpha."""
            pairs = positions.reshape(-1, 2, 3)
            if fade:
                radial = np.linalg.norm(pairs.mean(axis=1)[:, :2], axis=1) / extent
                bands = ((0., .55, 1.), (.55, .8, .35), (.8, 1.01, 0.))
            else:
                radial = np.zeros(len(pairs))
                bands = ((0., 1.01, 1.),)
            for low, high, alpha in bands:
                chosen = pairs[(radial >= low) & (radial < high)]
                if len(chosen):
                    self._hud.append(scene.visuals.Line(chosen.reshape(-1, 3), connect="segments",
                                                        color=(*color[:3], alpha * color[3]),
                                                        width=width, parent=self.view.scene))

        add(grid["minor"], (.44, .44, .44, 1.), 1)
        add(grid["section"], (.60, .60, .60, 1.), 1)
        add(grid["axes"][0], (.78, .36, .36, 1.), 2)
        add(grid["axes"][1], (.46, .72, .38, 1.), 2)
        add(grid["axes"][2], (.36, .52, .78, 1.), 2, fade=False)
        origin = scene.visuals.Markers(parent=self.view.scene)
        origin.set_data(np.zeros((1, 3)), face_color=(.95, .72, .38, .9), edge_color="#2b2b2b", size=6)
        self._hud.append(origin)

    def _target_rgba(self):
        return target_preview_rgba(getattr(self, "_config", None))

    def set_selection(self, key):
        self._selected_key = key
        self.set_scene(self._candidate, self._meshes)

    def set_scene(self, candidate, meshes):
        self._candidate, self._meshes = candidate, meshes
        _clear(self._objects)
        self._objects = draw_placements(self.view.scene, candidate, meshes, self._selected_key, True,
                                        self._target_rgba())
        keys = tuple((p.asset_key, p.role) for p in candidate.placements) if candidate is not None else ()
        bounds = placed_content_bounds(candidate, meshes)
        # Re-frame only when the geometry really changed: occluders move on every view, and
        # resetting the observer camera then would fight the user's own navigation.
        refit = keys != self._last_keys or self._helper_radius is None
        if bounds is not None and self._helper_radius:
            # Compare with the geometry that was framed, never with the observer camera: the user
            # is allowed to navigate away without the next refresh yanking the view back.
            center = (bounds[0] + bounds[1]) / 2
            scale = float(np.linalg.norm(bounds[1] - bounds[0])) / 2
            if self._fit_bounds is not None:
                moved = float(np.linalg.norm(center - self._fit_bounds[0]))
                refit = refit or moved > .25 * self._helper_radius
            refit = refit or abs(scale - self._helper_radius) > .25 * max(scale, self._helper_radius)
        self._last_keys = keys
        if refit:
            self.fit_scene()
        self.canvas.update()

    def fit_scene(self):
        # Frame the union of the placed geometry; helpers are sized from that same box so a
        # centimetre-scale model is not dwarfed by a metre-scale grid.
        bounds = placed_content_bounds(self._candidate, self._meshes)
        if bounds is None:
            center = np.zeros(3)
            radius = self._candidate.camera.radius_m if self._candidate is not None else 0.
            radius = radius if radius > 0 else 1.
        else:
            center = (bounds[0] + bounds[1]) / 2
            radius = float(np.linalg.norm(bounds[1] - bounds[0])) / 2
            radius = radius if radius > 0 else .5
        self.view.camera.center = tuple(float(v) for v in center)
        self.view.camera.scale_factor = max(radius * 2.5, .02)
        self._helper_radius = radius
        self._fit_bounds = (np.asarray(center, dtype=float), radius)
        self._grid(radius)

    def _content_radius(self):
        """Half diagonal of the placed geometry, in metres."""
        bounds = placed_content_bounds(self._candidate, self._meshes)
        if bounds is None:
            return 0.
        return float(np.linalg.norm(bounds[1] - bounds[0])) / 2

    def set_regions(self, config, selected_key=None):
        previous = self._config
        self._config = config
        _clear(self._regions)
        for region in config.regions:
            angle = region.azimuth
            span = 360. if angle.full_circle else (angle.end_deg - angle.start_deg) % 360.
            phis = np.deg2rad(angle.start_deg + np.linspace(0, span, 80))
            lo, hi = np.deg2rad([region.elevation_deg.minimum, region.elevation_deg.maximum])
            color = (.79, .62, .36, .45) if region.key == selected_key else (.47, .59, .69, .18)
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
        if self._candidate is not None and previous is not None and (
                previous.material.color != config.material.color
                or previous.material.base_color != config.material.base_color):
            self.set_scene(self._candidate, self._meshes)

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
