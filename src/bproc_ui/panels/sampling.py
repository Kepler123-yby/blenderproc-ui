"""Camera regions, calibrated intrinsics and automatic illumination/occlusion."""

from dataclasses import replace
from uuid import uuid4

import numpy as np

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QStackedWidget, QComboBox, QLineEdit,
                               QPushButton, QCheckBox, QLabel, QRadioButton)

from bproc_core.config import Azimuth, FixedCameraSpec, Range, RegionSpec
from bproc_core.camera import fit_distance, resize_camera
from bproc_core.assets import content_bounds
from bproc_core.sampling import allocate_regions
from bproc_ui.widgets import RangeEditor, SeedEditor, integer, number, section, note, row

RANDOM_MATERIAL_HINT = ("每个场景在范围内取一组值，让同一批数据的表面参数有变化。\n"
                        "只影响目标物体，遮挡物保持原样。")
FIXED_MATERIAL_HINT = ("所有场景都用这一组值，适合检查某一档材质的成像效果。\n"
                       "只影响目标物体，遮挡物保持原样。")


class SamplingPanel(QWidget):
    region_changed = Signal(str)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._updating = False
        self._auto_fit_signature = None
        config = state.config
        self.current_region_key = (config.fixed_cameras[0].key if config.fixed_cameras else
                                   config.regions[0].key if config.regions else None)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 10)
        layout.setSpacing(6)
        form = section("采样中心", layout)
        self.center = QComboBox()
        self.center.currentIndexChanged.connect(self._center_changed)
        form.addRow("渲染中心", self.center)

        form = section("机位模式", layout)
        self.mode_fixed = QRadioButton("固定机位：列表里每个机位各出一张，张数自动跟着机位数")
        self.mode_random = QRadioButton("随机采样：每个场景在范围内抽 N 个机位")
        self.mode_fixed.toggled.connect(lambda on: on and self._mode_changed("fixed"))
        self.mode_random.toggled.connect(lambda on: on and self._mode_changed("random"))
        form.addRow(self.mode_fixed)
        form.addRow(self.mode_random)

        form = section("固定机位", layout)
        self.fixed_box = form.parentWidget()
        self.fixed = QComboBox()
        self.fixed.currentIndexChanged.connect(self._fixed_selected)
        form.addRow("当前机位", self.fixed)
        self.fixed_name = QLineEdit()
        self.fixed_name.editingFinished.connect(lambda: self._fixed_edited("name"))
        self.fixed_radius = number(minimum=.000001, maximum=1e6, decimals=3, suffix=" m")
        self.fixed_azimuth = number(minimum=-180, maximum=180, decimals=1, suffix="°")
        self.fixed_elevation = number(minimum=-90, maximum=90, decimals=1, suffix="°")
        for field, editor in (("radius_m", self.fixed_radius), ("azimuth_deg", self.fixed_azimuth),
                              ("elevation_deg", self.fixed_elevation)):
            editor.valueChanged.connect(lambda _value, name=field: self._fixed_edited(name))
        form.addRow("名称", self.fixed_name)
        self.fixed_radius_note = note("")
        form.addRow("距离 r", self.fixed_radius)
        form.addRow(self.fixed_radius_note)
        form.addRow("方位 φ", self.fixed_azimuth)
        form.addRow("仰角 α", self.fixed_elevation)
        add_fixed, capture = QPushButton("＋ 机位"), QPushButton("用当前视角添加")
        duplicate_fixed, remove_fixed = QPushButton("复制"), QPushButton("删除")
        fit_fixed = QPushButton("适配距离")
        add_fixed.clicked.connect(self.add_fixed_camera)
        capture.clicked.connect(self.capture_fixed_camera)
        duplicate_fixed.clicked.connect(self.duplicate_fixed_camera)
        remove_fixed.clicked.connect(self.remove_fixed_camera)
        fit_fixed.clicked.connect(self.fit_fixed_distance)
        form.addRow(row(add_fixed, capture))
        form.addRow(row(duplicate_fixed, remove_fixed, fit_fixed))
        form.addRow(note("每场景视角数 = 机位个数；加一个机位就多一张图，不用另填张数。「适配距离」按模型实际尺寸重算 r。"))
        self._fixed_widgets = [self.fixed_name, self.fixed_radius, self.fixed_azimuth, self.fixed_elevation,
                              add_fixed, capture, duplicate_fixed, remove_fixed, fit_fixed]

        form = section("随机区域", layout)
        self.random_box = form.parentWidget()
        self.region = QComboBox()
        self.region.currentIndexChanged.connect(self._region_selected)
        form.addRow("当前区域", self.region)
        self.region_name = QLineEdit()
        self.region_name.editingFinished.connect(lambda: self._region_edited("name"))
        form.addRow("名称", self.region_name)
        add, duplicate, remove = QPushButton("＋ 新区域"), QPushButton("复制"), QPushButton("删除")
        fit_region = QPushButton("适配距离")
        add.clicked.connect(lambda: self.add_region())
        duplicate.clicked.connect(self.duplicate_region)
        remove.clicked.connect(self.remove_region)
        fit_region.clicked.connect(self.fit_region_distance)
        form.addRow(row(add, duplicate, remove, fit_region))
        self.radius = RangeEditor(minimum=.000001, maximum=1e6, suffix=" m")
        self.radius.changed.connect(lambda: self._region_edited("radius_m"))
        self.azimuth_start = number(minimum=-180, maximum=180, decimals=1, suffix="°")
        self.azimuth_end = number(minimum=-180, maximum=180, decimals=1, suffix="°")
        self.azimuth_start.valueChanged.connect(lambda: self._region_edited("azimuth_start"))
        self.azimuth_end.valueChanged.connect(lambda: self._region_edited("azimuth_end"))
        self.full_circle = QCheckBox("全方位 360°")
        self.full_circle.toggled.connect(lambda: self._region_edited("full_circle"))
        self.elevation = RangeEditor(minimum=-90, maximum=90, decimals=1, suffix="°")
        self.elevation.changed.connect(lambda: self._region_edited("elevation_deg"))
        form.addRow("距离 r", self.radius)
        self.radius_note = note("")
        form.addRow(self.radius_note)
        form.addRow("方位 φ", row(self.azimuth_start, QLabel("→"), self.azimuth_end))
        form.addRow(self.full_circle)
        form.addRow("仰角 α", self.elevation)
        form.addRow(note("φ 0° 沿中心局部 +X，向 +Y 增大；α 0° 为水平，+90° 朝 +Z。\n相同上下限即退化为固定机位；170° → −170° 是跨界的 20°。"))
        self._region_widgets = [self.region, self.region_name, add, duplicate, remove, fit_region, self.radius,
                                self.azimuth_start, self.azimuth_end, self.full_circle, self.elevation]
        form = section("数据集采样", layout)
        self.scene_count = integer(250, 0, 1000000)
        self.views = integer(4, 1, 1000)
        self.layout_attempts = integer(30, 1, 1000)
        self.seed = SeedEditor(42)
        self.scene_count.valueChanged.connect(lambda: self._counts_changed("scene_count"))
        self.views.valueChanged.connect(lambda: self._counts_changed("views_per_scene"))
        self.layout_attempts.valueChanged.connect(lambda: self._counts_changed("layout_max_attempts"))
        self.seed.valueChanged.connect(lambda: self._counts_changed("seed"))
        form.addRow("场景数", self.scene_count)
        form.addRow("每场景视角数", self.views)
        form.addRow("场景布局尝试上限", self.layout_attempts)
        form.addRow("随机种子", self.seed)
        self.allocation = note("")
        form.addRow(self.allocation)
        form.addRow(note("同一场景共享目标布局、光照和材质；机位与遮挡物按视角生成，遮挡物会重新摆放。视角数按区域平均分配。\n多目标在同一张图中出现。"))

        form = section("光照 · 点光源", layout)
        self.energy = RangeEditor(minimum=0, suffix=" W", decimals=1)
        self.light_radius = RangeEditor(minimum=.000001, suffix=" m")
        self.light_elevation = RangeEditor(minimum=-90, maximum=90, decimals=1, suffix="°")
        self.temperature = RangeEditor(minimum=1000, maximum=40000, decimals=0, suffix=" K")
        for field, editor in (("energy_w", self.energy), ("radius_m", self.light_radius),
                              ("elevation_deg", self.light_elevation), ("temperature_k", self.temperature)):
            editor.changed.connect(lambda f=field: self._light_changed(f))
        form.addRow("功率", self.energy)
        form.addRow("距离", self.light_radius)
        form.addRow("仰角", self.light_elevation)
        form.addRow("色温", self.temperature)

        form = section("目标材质", layout)
        self.material_mode = QComboBox()
        self.material_mode.addItems(["跟随模型原色", "统一基色"])
        self.material_mode.currentIndexChanged.connect(self._material_mode_changed)
        self.base_color = QPushButton()
        self.base_color.clicked.connect(self._pick_base_color)
        form.addRow("颜色", row(self.material_mode, self.base_color))
        self.material_source = QComboBox()
        self.material_source.addItems(["范围内随机", "指定值"])
        self.material_source.currentIndexChanged.connect(self._material_source_changed)
        form.addRow("参数", self.material_source)

        self.roughness = RangeEditor(minimum=0, maximum=1, decimals=2)
        self.specular = RangeEditor(minimum=0, maximum=1, decimals=2)
        self.metallic = RangeEditor(minimum=0, maximum=1, decimals=2)
        for field in ("roughness", "specular", "metallic"):
            getattr(self, field).changed.connect(lambda f=field: self._material_changed(f))
        random_page = QWidget()
        random_form = QFormLayout(random_page)
        random_form.setContentsMargins(0, 0, 0, 0)
        random_form.addRow("粗糙度", self.roughness)
        random_form.addRow("金属度", self.metallic)
        random_form.addRow("高光", self.specular)

        self.fixed_roughness = number(.4, 0, 1, 3)
        self.fixed_specular = number(.6, 0, 1, 3)
        self.fixed_metallic = number(0., 0, 1, 3)
        for field in ("roughness", "specular", "metallic"):
            getattr(self, f"fixed_{field}").valueChanged.connect(
                lambda _value, f=field: self._material_fixed_changed(f))
        fixed_page = QWidget()
        fixed_form = QFormLayout(fixed_page)
        fixed_form.setContentsMargins(0, 0, 0, 0)
        fixed_form.addRow("粗糙度", self.fixed_roughness)
        fixed_form.addRow("金属度", self.fixed_metallic)
        fixed_form.addRow("高光", self.fixed_specular)

        self.material_pages = QStackedWidget()
        self.material_pages.addWidget(random_page)
        self.material_pages.addWidget(fixed_page)
        form.addRow(self.material_pages)
        self.material_hint = note(RANDOM_MATERIAL_HINT)
        form.addRow(self.material_hint)

        form = section("中心目标的遮挡约束", layout)
        self.occlusion_center = QLabel()
        self.occlusion_center.setObjectName("anchorBadge")
        self.occlusion_center.setWordWrap(True)
        form.addRow(self.occlusion_center)
        self.none_fraction = number(minimum=0, maximum=100, decimals=1, suffix=" %")
        self.occlusion_ratio = RangeEditor(minimum=0, maximum=100, decimals=1, suffix=" %")
        self.count_min, self.count_max = integer(1, 0, 100), integer(3, 0, 100)
        self.occlusion_strategy = QComboBox()
        self.occlusion_strategy.addItem("按视角表观尺寸", "aimed_view")
        self.occlusion_strategy.addItem("按物理尺寸", "physical_view")
        self.occluder_scale = RangeEditor(minimum=.000001, maximum=1000, decimals=3)
        self.physical_scale = RangeEditor(minimum=.000001, maximum=1000, decimals=3)
        self.attempts = integer(30, 1, 10000)
        self.none_fraction.valueChanged.connect(lambda: self._occlusion_changed("none_fraction"))
        self.occlusion_ratio.changed.connect(lambda: self._occlusion_changed("ratio"))
        self.count_min.valueChanged.connect(lambda: self._occlusion_changed("count_min"))
        self.count_max.valueChanged.connect(lambda: self._occlusion_changed("count_max"))
        self.occlusion_strategy.currentIndexChanged.connect(lambda: self._occlusion_changed("strategy"))
        self.occluder_scale.changed.connect(lambda: self._occlusion_changed("scale"))
        self.physical_scale.changed.connect(lambda: self._occlusion_changed("physical_scale"))
        self.attempts.valueChanged.connect(lambda: self._occlusion_changed("max_attempts"))
        form.addRow("无遮挡样本", self.none_fraction)
        form.addRow("其余遮挡率", self.occlusion_ratio)
        form.addRow("遮挡物个数", row(self.count_min, QLabel("—"), self.count_max))
        form.addRow("遮挡策略", self.occlusion_strategy)
        form.addRow("表观相对尺寸", self.occluder_scale)
        form.addRow("物理倍率", self.physical_scale)
        form.addRow("每张尝试上限", self.attempts)
        form.addRow(note("按实际像素验收中心遮挡，包含其他目标造成的遮挡。所有启用目标须完整入画且可见。"))
        form.addRow(note("按视角表观尺寸：数值是遮挡物相对目标的屏幕大小，物理米数随当前机位深度反算，"
                         "不随机旋转。按物理尺寸：数值乘模型米制尺寸后固定不变，每个视角重新采样均匀旋转与位置，"
                         "同一帧不重复使用同一资源；放不下或资源数不足会明确拒绝。两种策略各自保留自己的数值。"))

        form = section("相机内参 K", layout)
        self.width, self.height = integer(640, 1, 32768), integer(480, 1, 32768)
        self.width.editingFinished.connect(lambda: self._resolution_changed("width"))
        self.height.editingFinished.connect(lambda: self._resolution_changed("height"))
        form.addRow("分辨率", row(self.width, QLabel("×"), self.height))
        self.auto_scale = QCheckBox("分辨率变化时同比缩放内参")
        self.auto_scale.toggled.connect(lambda: self._intrinsics_changed("auto_scale"))
        form.addRow(self.auto_scale)
        self.intrinsics = {}
        for name in ("fx", "fy", "cx", "cy"):
            editor = number(minimum=.000001 if name in ("fx", "fy") else -1e6,
                            decimals=5, maximum=1e6)
            editor.editingFinished.connect(lambda n=name: self._intrinsics_changed(n))
            self.intrinsics[name] = editor
            form.addRow(name, editor)
        form.addRow(note("零 skew 针孔相机。独立相机窗口按 K 与画幅留边显示构图。"))
        layout.addStretch()
        state.project_changed.connect(self.sync)
        self.sync()

    def _selected_region(self):
        return next((r for r in self.state.config.regions if r.key == self.current_region_key), None)

    def _selected_fixed(self):
        return next((c for c in self.state.config.fixed_cameras if c.key == self.current_region_key), None)

    def _fixed_selected(self, index):
        if not self._updating and index >= 0:
            self.current_region_key = self.fixed.itemData(index)
            self.sync()
            self.region_changed.emit(self.current_region_key)

    def _distance_note(self, distance):
        """Say the distance in multiples of the object's XY radius: 0.31 m means nothing, 4.6× does."""
        mesh = self.state.meshes.get(self.state.config.anchor_key)
        if mesh is None:
            return ""
        size = np.asarray(mesh.bounds_m[1], dtype=float) - np.asarray(mesh.bounds_m[0], dtype=float)
        footprint = float(np.hypot(size[0], size[1])) / 2
        if footprint <= 0:
            return ""
        low, high = (distance.minimum, distance.maximum) if hasattr(distance, "minimum") else (distance, distance)
        if high <= low:
            return f"r = {low / footprint:.1f} × 物体 XY 半径（{footprint:.4f} m）"
        return (f"r = {low / footprint:.1f}–{high / footprint:.1f} × 物体 XY 半径"
                f"（{footprint:.4f} m）")

    def _fit_radius(self):
        """Framing distance for every enabled target, from the rule that seeds a new project.

        Fitting the render centre alone put the whole region inside the content, because other
        targets sit at random offsets around it, and every candidate then failed ``out_of_frame``.
        ``content_bounds`` already accounts for the offsets and the target sizes, so reuse the
        same rule instead of inventing a second distance rule that disagrees with the wizard.
        """
        bounds = content_bounds(self.state.meshes, self.state.config.targets,
                                self.state.config.anchor_key)
        if bounds is None:
            return None
        config = self.state.config
        region = config.regions[0] if config.regions else None
        azimuths, elevation = (0.,), 20.
        if region is not None:
            if region.azimuth.full_circle:
                azimuths = (-60., 0., 60.)
            else:
                span = abs(region.azimuth.end_deg - region.azimuth.start_deg) / 2
                centre = (region.azimuth.start_deg + region.azimuth.end_deg) / 2
                azimuths = (centre - span, centre, centre + span)
            elevation = (region.elevation_deg.minimum + region.elevation_deg.maximum) / 2
        try:
            return fit_distance(bounds, config.camera, azimuths_deg=azimuths,
                                elevation_deg=elevation, fill=.85)
        except ValueError:
            return None

    def _fit_band(self):
        """Region band for the loaded content: comfortable distance and 35% of headroom."""
        fitted = self._fit_radius()
        if fitted is None:
            return None
        return Range(round(fitted, 4), round(fitted * 1.35, 4))

    def add_fixed_camera(self, camera=None):
        cameras = self.state.config.fixed_cameras
        if camera is None:
            fitted = self._fit_radius()
            camera = FixedCameraSpec(key="camera-" + uuid4().hex[:8], name=f"机位 {len(cameras) + 1}",
                                     radius_m=round(fitted, 4) if fitted else 0.7)
        self.current_region_key = camera.key
        self.state.update(fixed_cameras=cameras + (camera,))
        self.region_changed.emit(camera.key)
        return camera

    def capture_fixed_camera(self):
        """Pin the camera the preview currently shows, so the user never types angles."""
        candidate = getattr(self.state, "candidate", None)
        if candidate is None:
            return None
        pose = candidate.camera
        return self.add_fixed_camera(FixedCameraSpec(
            key="camera-" + uuid4().hex[:8], name=f"机位 {len(self.state.config.fixed_cameras) + 1}",
            radius_m=round(float(pose.radius_m), 3), azimuth_deg=round(float(pose.azimuth_deg), 2),
            elevation_deg=round(float(pose.elevation_deg), 2)))

    def duplicate_fixed_camera(self):
        camera = self._selected_fixed()
        if camera:
            return self.add_fixed_camera(replace(camera, key="camera-" + uuid4().hex[:8],
                                                 name=camera.name + " 副本"))

    def remove_fixed_camera(self):
        camera = self._selected_fixed()
        if camera is None:
            return
        cameras = tuple(c for c in self.state.config.fixed_cameras if c.key != camera.key)
        keys = [c.key for c in cameras] + [r.key for r in self.state.config.regions]
        self.current_region_key = keys[0] if keys else None
        self.state.update(fixed_cameras=cameras)
        if self.current_region_key:
            self.region_changed.emit(self.current_region_key)

    def _fixed_edited(self, field):
        if self._updating:
            return
        camera = self._selected_fixed()
        if camera is None:
            return
        values = {"name": self.fixed_name.text().strip() or camera.name,
                  "radius_m": self.fixed_radius.value(), "azimuth_deg": self.fixed_azimuth.value(),
                  "elevation_deg": self.fixed_elevation.value()}
        updated = replace(camera, **{field: values[field]})
        self.state.update(fixed_cameras=tuple(updated if c.key == camera.key else c
                                              for c in self.state.config.fixed_cameras))

    def _mode_changed(self, mode):
        self._show_mode(mode)
        if not self._updating and self.state.config.camera_mode != mode:
            self.state.update(camera_mode=mode)

    def _show_mode(self, mode):
        """Only the editors of the active mode: both groups at once read as two settings."""
        self.fixed_box.setVisible(mode == "fixed")
        self.random_box.setVisible(mode == "random")

    def _auto_fit_scene(self):
        """Fit once per loaded model, and only after every enabled target has loaded.

        Meshes arrive one at a time, so fitting on the first arrival sized the region for half the
        scene and the next arrival shrank it again; each pass was persisted on save.
        """
        config = self.state.config
        needed = [t.asset.key for t in config.targets if t.enabled]
        if not needed or any(key not in self.state.meshes for key in needed):
            return
        signature = (config.anchor_key, tuple(sorted(needed)))
        if signature == self._auto_fit_signature:
            return
        self._auto_fit_signature = signature
        self.auto_fit_distances()

    def auto_fit_distances(self):
        """Pull camera distances to the model scale when a project was tuned for another size.

        Without this, opening a project whose radius was sized for a metre-scale asset shows a
        region sphere several times larger than the imported mesh. The replacement band comes
        from ``assets.framing_band``, so reopening a project converges instead of shrinking again.
        """
        targets = [t for t in self.state.config.targets if t.enabled and t.asset.key in self.state.meshes]
        if not targets:
            return False
        band = self._fit_band()
        if band is None:
            return False
        config, changed = self.state.config, False
        regions, cameras = [], []
        for region in config.regions:
            far = max(region.radius_m.minimum, region.radius_m.maximum) / band.maximum
            if far > 1.6 or far < 1 / 1.6:
                region, changed = replace(region, radius_m=Range(band.minimum, band.maximum)), True
            regions.append(region)
        for camera in config.fixed_cameras:
            far = camera.radius_m / band.minimum
            if far > 1.6 or far < 1 / 1.6:
                camera, changed = replace(camera, radius_m=round(band.minimum, 4)), True
            cameras.append(camera)
        if changed:
            self.state.update(regions=tuple(regions), fixed_cameras=tuple(cameras))
        return changed

    def fit_fixed_distance(self):
        """Refit pinned cameras to the loaded model size; the selection narrows it to one camera."""
        fitted = self._fit_radius()
        if fitted is None or not self.state.config.fixed_cameras:
            return None
        selected = self._selected_fixed()
        updated = tuple(replace(c, radius_m=round(fitted, 4)) if selected is None or c.key == selected.key else c
                        for c in self.state.config.fixed_cameras)
        self.state.update(fixed_cameras=updated)
        return updated

    def fit_region_distance(self):
        """Refit random regions to the loaded model size; the selection narrows it to one region."""
        band = self._fit_band()
        if band is None or not self.state.config.regions:
            return None
        selected = self._selected_region()
        updated = tuple(replace(r, radius_m=Range(band.minimum, band.maximum))
                        if selected is None or r.key == selected.key else r
                        for r in self.state.config.regions)
        self.state.update(regions=updated)
        return updated

    def _center_changed(self, index):
        if not self._updating and index >= 0:
            self.state.set_anchor(self.center.itemData(index))

    def _region_selected(self, index):
        if not self._updating and index >= 0:
            self.current_region_key = self.region.itemData(index)
            self.sync()
            self.region_changed.emit(self.current_region_key)

    def select_region(self, key):
        if any(r.key == key for r in self.state.config.regions) or any(c.key == key for c in self.state.config.fixed_cameras):
            self.current_region_key = key
            self.sync()
            self.region_changed.emit(key)

    def add_region(self, region=None):
        if region is None:
            fitted = self._fit_radius()
            region = RegionSpec(key="region-" + uuid4().hex[:8], name=f"区域 {len(self.state.config.regions) + 1}",
                                radius_m=Range(round(fitted * .9, 4), round(fitted * 1.4, 4)) if fitted else Range(.5, .8))
        self.current_region_key = region.key
        self.state.update(regions=self.state.config.regions + (region,))
        self.region_changed.emit(region.key)
        return region

    def duplicate_region(self):
        region = self._selected_region()
        if region:
            return self.add_region(replace(region, key="region-" + uuid4().hex[:8], name=region.name + " 副本"))

    def remove_region(self):
        if len(self.state.config.regions) > 1:
            regions = tuple(r for r in self.state.config.regions if r.key != self.current_region_key)
            self.current_region_key = regions[0].key
            self.state.update(regions=regions)
            self.region_changed.emit(self.current_region_key)

    def _region_edited(self, field):
        if self._updating:
            return
        selected = self._selected_region()
        if selected:
            values = {"name": self.region_name.text().strip() or selected.name,
                      "radius_m": self.radius.value(), "elevation_deg": self.elevation.value()}
            azimuth_fields = {"azimuth_start": ("start_deg", self.azimuth_start.value()),
                              "azimuth_end": ("end_deg", self.azimuth_end.value()),
                              "full_circle": ("full_circle", self.full_circle.isChecked())}
            if field in azimuth_fields:
                name, value = azimuth_fields[field]
                updated = replace(selected, azimuth=replace(selected.azimuth, **{name: value}))
            else:
                updated = replace(selected, **{field: values[field]})
            self.state.update(regions=tuple(updated if r.key == selected.key else r for r in self.state.config.regions))

    def _counts_changed(self, field):
        if self._updating:
            return
        if field == "scene_count":
            self.state.update(scene_count=self.scene_count.value())
        elif field == "views_per_scene":
            self.state.update(views_per_scene=self.views.value())
        elif field == "layout_max_attempts":
            self.state.update(layout_max_attempts=self.layout_attempts.value())
        else:
            self.state.update(seed=self.seed.value())

    def _light_changed(self, field):
        if not self._updating:
            editors = {"energy_w": self.energy, "radius_m": self.light_radius,
                       "elevation_deg": self.light_elevation, "temperature_k": self.temperature}
            self.state.update(light=replace(self.state.config.light, **{field: editors[field].value()}))

    def _material_mode_changed(self, index):
        if not self._updating:
            self.state.update(material=replace(self.state.config.material,
                                               color="solid" if index else "original"))
            self._paint_color_button()

    def _material_source_changed(self, index):
        """Switching between sampled ranges and the specified surface parameters."""
        self.material_pages.setCurrentIndex(index)
        self.material_hint.setText(FIXED_MATERIAL_HINT if index else RANDOM_MATERIAL_HINT)
        if not self._updating:
            self.state.update(material=replace(self.state.config.material,
                                               source="fixed" if index else "random"))

    def _material_fixed_changed(self, field):
        if not self._updating:
            editors = {"roughness": self.fixed_roughness, "specular": self.fixed_specular,
                       "metallic": self.fixed_metallic}
            fixed = replace(self.state.config.material.fixed, **{field: editors[field].value()})
            self.state.update(material=replace(self.state.config.material, fixed=fixed))

    def _pick_base_color(self):
        current = QColor(self.state.config.material.base_color)
        chosen = QColorDialog.getColor(current, self, "目标基色")
        if chosen.isValid():
            self.state.update(material=replace(self.state.config.material,
                                               color="solid", base_color=chosen.name()))
            self._paint_color_button()

    def _paint_color_button(self):
        value = self.state.config.material.base_color
        self.base_color.setText(value.upper())
        self.base_color.setStyleSheet(
            f"background: {value}; color: {'#1b1b1b' if QColor(value).lightness() > 140 else '#f2f2f2'};"
            "border: 1px solid #4a4a4a; border-radius: 3px; padding: 2px 10px;")

    def _material_changed(self, field):
        if not self._updating:
            editors = {"roughness": self.roughness, "specular": self.specular, "metallic": self.metallic}
            self.state.update(material=replace(self.state.config.material, **{field: editors[field].value()}))

    def _occlusion_changed(self, field):
        if not self._updating:
            ratio = self.occlusion_ratio.value()
            original = self.state.config.occlusion
            values = {"none_fraction": self.none_fraction.value() / 100,
                      "ratio": Range(ratio.minimum / 100, ratio.maximum / 100),
                      "max_attempts": self.attempts.value(), "scale": self.occluder_scale.value(),
                      "strategy": self.occlusion_strategy.currentData(),
                      "physical_scale": self.physical_scale.value()}
            if field == "count_min":
                changes = {"count": (self.count_min.value(), original.count[1])}
            elif field == "count_max":
                changes = {"count": (original.count[0], self.count_max.value())}
            else:
                changes = {field: values[field]}
            self.state.update(occlusion=replace(original, **changes))

    def resize_image(self, width, height):
        self.state.update(camera=resize_camera(self.state.config.camera, width, height))

    def _resolution_changed(self, field=None):
        if not self._updating:
            original = self.state.config.camera
            width = original.width if field == "height" else self.width.value()
            height = original.height if field == "width" else self.height.value()
            self.resize_image(width, height)

    def _intrinsics_changed(self, field):
        if not self._updating:
            value = self.auto_scale.isChecked() if field == "auto_scale" else self.intrinsics[field].value()
            self.state.update(camera=replace(self.state.config.camera, **{field: value}))

    def sync(self):
        self._auto_fit_scene()
        self._updating = True
        config = self.state.config
        self.center.clear()
        for target in config.targets:
            if target.enabled:
                self.center.addItem(target.name, target.asset.key)
        self.center.setCurrentIndex(self.center.findData(config.anchor_key))
        self.fixed.clear()
        for camera in config.fixed_cameras:
            self.fixed.addItem(camera.name, camera.key)
        keys = [c.key for c in config.fixed_cameras] + [r.key for r in config.regions]
        if self.current_region_key not in keys:
            self.current_region_key = keys[0] if keys else None
        self.fixed.setCurrentIndex(self.fixed.findData(self.current_region_key))
        fixed = self._selected_fixed()
        for widget in (self.fixed_name, self.fixed_radius, self.fixed_azimuth, self.fixed_elevation):
            widget.setEnabled(fixed is not None)
        self.fixed_radius_note.setText(self._distance_note(fixed.radius_m) if fixed else "")
        if fixed:
            self.fixed_name.setText(fixed.name)
            self.fixed_radius.setValue(fixed.radius_m)
            self.fixed_azimuth.setValue(fixed.azimuth_deg)
            self.fixed_elevation.setValue(fixed.elevation_deg)
        self.region.clear()
        for region in config.regions:
            self.region.addItem(region.name, region.key)
        self.region.setCurrentIndex(self.region.findData(self.current_region_key))
        region = self._selected_region()
        if region:
            self.region_name.setText(region.name)
            self.radius.set_value(region.radius_m)
            self.azimuth_start.setValue(region.azimuth.start_deg)
            self.azimuth_end.setValue(region.azimuth.end_deg)
            self.full_circle.setChecked(region.azimuth.full_circle)
            self.azimuth_start.setEnabled(not region.azimuth.full_circle)
            self.azimuth_end.setEnabled(not region.azimuth.full_circle)
            self.elevation.set_value(region.elevation_deg)
            self.radius_note.setText(self._distance_note(region.radius_m))
        else:
            self.radius_note.setText("")
        self.scene_count.setValue(config.scene_count)
        self.layout_attempts.setValue(config.layout_max_attempts)
        self.views.setValue(config.views)
        self.seed.setValue(config.seed)
        fixed_mode = config.camera_mode == "fixed"
        self.mode_fixed.setChecked(fixed_mode)
        self.mode_random.setChecked(not fixed_mode)
        self.views.setEnabled(not fixed_mode)
        self._show_mode(config.camera_mode)
        for widget in self._fixed_widgets:
            widget.setEnabled(fixed_mode)
        for widget in self._region_widgets:
            widget.setEnabled(not fixed_mode)
        if fixed_mode:
            views = len(config.fixed_cameras)
            detail = " / ".join(c.name for c in config.fixed_cameras) or "还没有机位"
            text = f"固定机位（每场景 {views} 个视角）：{detail}"
        else:
            allocations = allocate_regions(config.views, [r.key for r in config.regions])
            detail = " / ".join(f"{r.name} {allocations[r.key]}" for r in config.regions)
            text = f"随机区域（每场景 {config.views} 个视角）：{detail}"
        text += f"\n{config.scene_count} 场景 × {config.views} 视角 = {config.total_shots} 张"
        if config.camera_mode == "random" and any(value == 0 for value in allocations.values()):
            text += "\n部分区域分配为 0；请增加视角数或减少区域。"
        self.allocation.setText(text)
        self.energy.set_value(config.light.energy_w)
        self.light_radius.set_value(config.light.radius_m)
        self.light_elevation.set_value(config.light.elevation_deg)
        self.temperature.set_value(config.light.temperature_k)
        self.material_mode.setCurrentIndex(1 if config.material.color == "solid" else 0)
        self._paint_color_button()
        self.material_source.setCurrentIndex(1 if config.material.source == "fixed" else 0)
        self.material_pages.setCurrentIndex(self.material_source.currentIndex())
        self.material_hint.setText(FIXED_MATERIAL_HINT if config.material.source == "fixed"
                                   else RANDOM_MATERIAL_HINT)
        self.roughness.set_value(config.material.roughness)
        self.specular.set_value(config.material.specular)
        self.metallic.set_value(config.material.metallic)
        self.fixed_roughness.setValue(config.material.fixed.roughness)
        self.fixed_specular.setValue(config.material.fixed.specular)
        self.fixed_metallic.setValue(config.material.fixed.metallic)
        anchor = next((t for t in config.targets if t.asset.key == config.anchor_key), None)
        self.occlusion_center.setText("◎ 约束对象：" + (anchor.name if anchor else "未设置中心"))
        self.none_fraction.setValue(config.occlusion.none_fraction * 100)
        self.occlusion_ratio.set_value(Range(config.occlusion.ratio.minimum * 100, config.occlusion.ratio.maximum * 100))
        self.count_min.setValue(config.occlusion.count[0])
        self.count_max.setValue(config.occlusion.count[1])
        self.occluder_scale.set_value(config.occlusion.scale)
        self.occlusion_strategy.setCurrentIndex(
            max(0, self.occlusion_strategy.findData(getattr(config.occlusion, "strategy", "aimed_view"))))
        self.physical_scale.set_value(config.occlusion.physical_scale)
        self.occluder_scale.setEnabled(config.occlusion.strategy == "aimed_view")
        self.physical_scale.setEnabled(config.occlusion.strategy == "physical_view")
        self.attempts.setValue(config.occlusion.max_attempts)
        self.width.setValue(config.camera.width)
        self.height.setValue(config.camera.height)
        self.auto_scale.setChecked(config.camera.auto_scale)
        for name, widget in self.intrinsics.items():
            widget.setValue(getattr(config.camera, name))
        self._updating = False
