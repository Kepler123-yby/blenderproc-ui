"""A compact four-editor workspace, inspired by Blender's native layout."""

from pathlib import Path
from dataclasses import replace
import inspect

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QAction, QKeySequence, QColor
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                               QToolButton, QPushButton, QLabel, QTreeWidget, QTreeWidgetItem,
                               QTabWidget, QComboBox, QPlainTextEdit, QFileDialog, QMessageBox,
                               QAbstractItemView, QMenu)

from bproc_core.config import new_project
from bproc_ui.widgets import editor_header, scrollable, integer, SeedEditor
from bproc_ui.preview import PreviewController
from bproc_ui.views.scene_view import SceneView, target_preview_rgba
from bproc_ui.views.camera_view import CameraPreview
from bproc_ui.viewport_lod import ViewportLodController, original_display_mesh
from bproc_ui.panels.resources import ResourcesPanel
from bproc_ui.panels.sampling import SamplingPanel
from bproc_ui.panels.export import ExportPanel


class MainWindow(QMainWindow):
    def __init__(self, state, parent=None, scene_factory=SceneView, camera_factory=CameraPreview,
                 restore_settings=True):
        super().__init__(parent)
        self.state = state
        self._updating = False
        self._closing = False
        self.settings = QSettings("BlenderProc UI", "Dataset Workspace")
        self._persist_settings = restore_settings
        self.setMinimumSize(980, 700)
        self.resize(1320, 880)
        self._make_menu()
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(outer)
        workspace = QWidget()
        bar = QHBoxLayout(workspace)
        bar.setContentsMargins(8, 3, 8, 0)
        bar.setSpacing(2)
        brand = QLabel("◈  BlenderProc")
        brand.setObjectName("editorTitle")
        bar.addWidget(brand)
        bar.addSpacing(22)
        self.workspaces = []
        for index, title in enumerate(("场景", "采样", "脚本")):
            button = QToolButton()
            button.setText(title)
            button.setProperty("workspace", True)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._set_workspace(i))
            self.workspaces.append(button)
            bar.addWidget(button)
        bar.addStretch()
        self.project_label = QLabel("未保存项目")
        self.project_label.setObjectName("muted")
        bar.addWidget(self.project_label)
        layout.addWidget(workspace)

        self.horizontal = QSplitter(Qt.Horizontal)
        self.horizontal.setChildrenCollapsible(False)
        layout.addWidget(self.horizontal, 1)
        self.vertical = QSplitter(Qt.Vertical)
        self.vertical.setChildrenCollapsible(False)
        self.horizontal.addWidget(self.vertical)
        scene_editor = QWidget()
        scene_layout = QVBoxLayout(scene_editor)
        scene_layout.setContentsMargins(0, 0, 0, 0)
        scene_layout.setSpacing(0)
        self.anchor_badge = QLabel("◎ 未设置渲染中心")
        self.anchor_badge.setObjectName("anchorBadge")
        camera_button = QToolButton()
        camera_button.setText("相机视角")
        camera_button.clicked.connect(lambda: self.camera_preview.show_preview())
        fit = QToolButton()
        fit.setText("适应场景")
        self.lod_mode = QComboBox()
        self.lod_mode.addItem("自动 LOD", "auto")
        self.lod_mode.addItem("原始网格", "full")
        self.lod_mode.setToolTip("自动 LOD 只简化两个视口的显示网格；采样、碰撞、导出和真渲染始终使用原始几何。\n"
                                 "原始网格会把百万面模型完整上传，可能造成卡顿。")
        self.lod_mode.currentIndexChanged.connect(self._lod_mode_changed)
        scene_layout.addWidget(editor_header("▧  三维视口", self.lod_mode, self.anchor_badge, fit, camera_button))
        self.scene_view = scene_factory()
        fit.clicked.connect(self.scene_view.fit_scene)
        scene_layout.addWidget(self.scene_view, 1)
        self.viewport_caption = QLabel("  导入 PLY 目标模型以开始 · 左键旋转 / Shift + 左键平移 / 滚轮缩放")
        self.viewport_caption.setObjectName("muted")
        self.viewport_caption.setMinimumHeight(27)
        scene_layout.addWidget(self.viewport_caption)
        self.lod_status = QLabel("  视口 LOD：自动 · 等待模型")
        self.lod_status.setObjectName("muted")
        self.lod_status.setMinimumHeight(20)
        scene_layout.addWidget(self.lod_status)
        self.vertical.addWidget(scene_editor)
        self.bottom = QTabWidget()
        self.bottom.setMinimumHeight(135)
        self._build_sample_strip()
        self.vertical.addWidget(self.bottom)
        self.vertical.setSizes([650, 150])

        self.right = QSplitter(Qt.Vertical)
        self.right.setMinimumWidth(345)
        self.right.setChildrenCollapsible(False)
        self.horizontal.addWidget(self.right)
        outliner = QWidget()
        tree_layout = QVBoxLayout(outliner)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)
        tree_layout.addWidget(editor_header("▤  场景集合"))
        self.objects = QTreeWidget()
        self.objects.setHeaderLabels(["对象", "ID", "单位"])
        self.objects.setColumnWidth(0, 232)
        self.objects.setColumnWidth(1, 38)
        self.objects.setColumnWidth(2, 38)
        self.objects.setRootIsDecorated(True)
        self.objects.setAlternatingRowColors(True)
        self.objects.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.objects.itemSelectionChanged.connect(self._tree_selection)
        self.objects.itemChanged.connect(self._tree_item_changed)
        self.objects.setContextMenuPolicy(Qt.CustomContextMenu)
        self.objects.customContextMenuRequested.connect(self._tree_menu)
        tree_layout.addWidget(self.objects)
        self.right.addWidget(outliner)

        self.properties = QTabWidget()
        self.resources = ResourcesPanel(state)
        self.sampling = SamplingPanel(state)
        self.export_panel = ExportPanel(state)
        for title, panel in (("资源", self.resources), ("采样", self.sampling), ("脚本", self.export_panel)):
            self.properties.addTab(scrollable(panel), title)
        self.properties.currentChanged.connect(self._set_workspace)
        self.right.addWidget(self.properties)
        self.right.setSizes([230, 600])
        self.horizontal.setSizes([940, 380])
        self.horizontal.setStretchFactor(0, 1)
        self.horizontal.setStretchFactor(1, 0)

        self.camera_preview = camera_factory(self)
        self.preview = PreviewController(state, self)
        self.lod = ViewportLodController(self)
        self.lod.ready.connect(self._lod_ready)
        self.lod.failed.connect(self._lod_failed)
        self._display_meshes = {}
        self._pending = frozenset()
        self._lod_failures = {}
        self.preview.status_changed.connect(self._preview_status)
        self.sampling.region_changed.connect(self._region_changed)
        self.resources.error.connect(lambda message: self.statusBar().showMessage(message))
        self.export_panel.message.connect(lambda message: self.statusBar().showMessage(message, 12000))
        self.export_panel.exported.connect(self._exported)
        self.export_panel.stale.connect(self._export_stale)
        state.project_changed.connect(self.sync)
        state.selection_changed.connect(self._selection_changed)
        state.candidate_changed.connect(self._candidate_changed)
        # Meshes arrive after the project opens; panels must resync so sizes and camera
        # distances follow the geometry instead of the placeholder values.
        state.meshes_changed.connect(self._refresh_scene)
        state.meshes_changed.connect(self.sampling.sync)
        state.meshes_changed.connect(self.sync)
        state.dirty_changed.connect(self._update_title)
        self.sync()
        self._set_workspace(0)
        if restore_settings:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
            for key, splitter in (("horizontal", self.horizontal), ("vertical", self.vertical), ("right", self.right)):
                saved = self.settings.value(key)
                if saved:
                    splitter.restoreState(saved)
        QTimer.singleShot(200, self.camera_preview.show_preview)
        self.preview.schedule()

    def _make_menu(self):
        file_menu = self.menuBar().addMenu("文件")
        for label, shortcut, callback in (("新建项目", QKeySequence.New, self.new_project),
                                           ("打开项目…", QKeySequence.Open, self.open_project),
                                           ("保存", QKeySequence.Save, self.save_project),
                                           ("另存为…", QKeySequence.SaveAs, self.save_project_as)):
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.addAction(action)
        file_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        view_menu = self.menuBar().addMenu("视图")
        camera_action = QAction("相机视角", self)
        camera_action.setShortcut("F12")
        camera_action.triggered.connect(lambda: self.camera_preview.show_preview())
        view_menu.addAction(camera_action)
        fit_action = QAction("适应场景", self)
        fit_action.setShortcut("Home")
        fit_action.triggered.connect(lambda: self.scene_view.fit_scene())
        view_menu.addAction(fit_action)

    def _build_sample_strip(self):
        samples = QWidget()
        sample_layout = QVBoxLayout(samples)
        sample_layout.setContentsMargins(10, 8, 10, 8)
        sample_layout.setSpacing(8)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("机位来源"))
        self.preview_region = QComboBox()
        self.preview_region.setMinimumWidth(110)
        self.preview_region.currentIndexChanged.connect(self._bottom_region_changed)
        controls.addWidget(self.preview_region)
        controls.addWidget(QLabel("视角"))
        self.sample_index = integer(1, 1, 10000000)
        self.sample_index.setMaximumWidth(82)
        self.sample_index.valueChanged.connect(self._sample_changed)
        controls.addWidget(self.sample_index)
        controls.addWidget(QLabel("预览种子"))
        self.preview_seed = SeedEditor(self.state.config.seed)
        self.preview_seed.setMaximumWidth(100)
        self.preview_seed.valueChanged.connect(self._sample_changed)
        controls.addWidget(self.preview_seed)
        refresh = QPushButton("刷新候选场景")
        refresh.clicked.connect(self._refresh_clicked)
        controls.addWidget(refresh)
        controls.addStretch()
        sample_layout.addLayout(controls)
        self.preview_hint = QLabel()
        self.preview_hint.setObjectName("muted")
        self.preview_hint.setWordWrap(True)
        sample_layout.addWidget(self.preview_hint)
        self.preview_state = QLabel("导入目标后这里显示候选是否可用")
        self.preview_state.setObjectName("muted")
        self.preview_state.setWordWrap(True)
        sample_layout.addWidget(self.preview_state)
        self.bottom.addTab(samples, "▥  采样机位")
        command_widget = QWidget()
        command_layout = QHBoxLayout(command_widget)
        command_layout.setContentsMargins(8, 6, 8, 6)
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setPlaceholderText("在脚本工作区检查配置并导出，然后复制命令到终端运行。")
        command_layout.addWidget(self.command_preview, 1)
        copy = QPushButton("复制命令")
        copy.clicked.connect(lambda: self.export_panel._copy(self.command_preview.toPlainText()))
        command_layout.addWidget(copy)
        self.bottom.addTab(command_widget, ">_  终端命令")

    def _set_workspace(self, index):
        for i, button in enumerate(self.workspaces):
            button.setChecked(i == index)
        if self.properties.currentIndex() != index:
            self.properties.setCurrentIndex(index)
        if index == 2:
            self.bottom.setCurrentIndex(1)
        else:
            self.bottom.setCurrentIndex(0)

    def _tree_selection(self):
        if self._updating:
            return
        keys = [item.data(0, Qt.UserRole) for item in self.objects.selectedItems()]
        keys = [key for key in keys if key is not None]
        self.resources.selected_keys = keys
        item = self.objects.currentItem()
        key = item.data(0, Qt.UserRole) if item else None
        if key in {t.asset.key for t in self.state.config.targets}:
            self.state.select_target(key)

    def _tree_item_changed(self, item, column):
        if self._updating or column != 0:
            return
        key = item.data(0, Qt.UserRole)
        if key in {t.asset.key for t in self.state.config.targets}:
            try:
                self.resources.set_target_enabled(key, item.checkState(0) == Qt.Checked)
            except ValueError as error:
                self.statusBar().showMessage(str(error), 12000)
                self.sync()

    def _tree_menu(self, position):
        item = self.objects.itemAt(position)
        key = item.data(0, Qt.UserRole) if item else None
        if key not in {t.asset.key for t in self.state.config.targets}:
            return
        menu = QMenu(self)
        anchor = menu.addAction("设为渲染中心")
        anchor.setEnabled(any(t.asset.key == key and t.enabled for t in self.state.config.targets))
        chosen = menu.exec(self.objects.viewport().mapToGlobal(position))
        if chosen == anchor:
            self.state.set_anchor(key)

    def _selection_changed(self, key):
        self.scene_view.set_selection(key)
        if self._updating:
            return
        current = self.objects.currentItem()
        if current and current.data(0, Qt.UserRole) == key:
            return
        self._updating = True
        iterator = self.objects.invisibleRootItem().child(0)
        if iterator:
            for index in range(iterator.childCount()):
                item = iterator.child(index)
                if item.data(0, Qt.UserRole) == key:
                    self.objects.setCurrentItem(item)
                    break
        self._updating = False

    def _region_changed(self, key):
        self.preview.refresh(key, self.sample_index.value() - 1, self.preview_seed.value())
        self.scene_view.set_regions(self.state.config, key)
        self._updating = True
        self.preview_region.setCurrentIndex(self.preview_region.findData(key))
        self._updating = False

    def _bottom_region_changed(self, index):
        if not self._updating and index >= 0:
            self.sampling.select_region(self.preview_region.itemData(index))

    def _sample_changed(self):
        if not self._updating and hasattr(self, "preview"):
            self.preview.refresh(self.sampling.current_region_key, self.sample_index.value() - 1, self.preview_seed.value())

    def _refresh_clicked(self):
        self.preview_seed.setValue(self.preview_seed.value() + 1)

    def _refresh_scene(self):
        """Meshes can arrive after the first candidate; the viewport must resize and repaint."""
        self._lod_refresh()

    def _lod_mode_changed(self, index):
        if self._updating:
            return
        mode = self.lod_mode.itemData(index)
        if mode == self.state.config.viewport.lod_mode:
            return
        self.state.update(viewport=replace(self.state.config.viewport, lod_mode=mode))

    def _lod_refresh(self):
        """Request/refresh display meshes for the current context and redraw both viewports."""
        config = self.state.config
        viewport = config.viewport
        self.lod.set_context(self.state.context_revision, self.state.mesh_revision)
        display = {}
        if viewport.lod_mode == "full":
            for key, mesh in self.state.meshes.items():
                display[key] = original_display_mesh(mesh)
            pending = frozenset()
        else:
            self.lod.set_parameters(viewport.lod_target_faces, viewport.lod_threshold_faces)
            pending_keys = []
            for key, mesh in self.state.meshes.items():
                shown = self.lod.request(key, mesh)
                if shown is None:
                    pending_keys.append(key)
                else:
                    display[key] = shown
            pending = frozenset(pending_keys)
        self._display_meshes = display
        self._pending = pending
        self._lod_failures = {}
        self._update_lod_status()
        self._apply_scene_displays()

    def _lod_ready(self, key, display):
        if key not in self.state.meshes or display.source_faces != len(self.state.meshes[key].faces):
            return
        self._display_meshes[key] = display
        self._pending = frozenset(k for k in self._pending if k != key)
        self._lod_failures.pop(key, None)
        self._update_lod_status()
        self._apply_scene_displays()

    def _lod_failed(self, key, message):
        self._pending = frozenset(k for k in self._pending if k != key)
        self._lod_failures[key] = message
        self._update_lod_status()
        self._apply_scene_displays()

    def _apply_scene_displays(self):
        self._call_set_scene(self.scene_view, self.state.candidate, self.state.meshes)
        self._call_set_scene(self.camera_preview, self.state.candidate, self.state.meshes,
                             target_rgba=target_preview_rgba(self.state.config))

    def _call_set_scene(self, view, candidate, meshes, *, target_rgba=None):
        """Pass only the keywords the view supports, so old view doubles keep working."""
        parameters = inspect.signature(view.set_scene).parameters
        kwargs = {}
        if "target_rgba" in parameters:
            kwargs["target_rgba"] = target_rgba
        if "display_meshes" in parameters:
            kwargs["display_meshes"] = self._display_meshes
        if "pending" in parameters:
            kwargs["pending"] = self._pending
        view.set_scene(candidate, meshes, **kwargs)

    def _update_lod_status(self):
        viewport = self.state.config.viewport
        meshes = self.state.meshes
        if viewport.lod_mode == "full":
            self.lod_status.setText("  视口 LOD：原始网格 · 百万面模型可能明显卡顿（仅影响视口）")
            return
        if not meshes:
            self.lod_status.setText("  视口 LOD：自动 · 等待模型")
            return
        shown = len(self._display_meshes)
        total = len(meshes)
        if self._pending:
            names = "、".join(sorted(self._pending)[:2])
            self.lod_status.setText(
                f"  视口 LOD：自动 · 正在后台生成视口 LOD（{shown}/{total}，{names}）…")
            return
        if self._lod_failures:
            key, message = next(iter(self._lod_failures.items()))
            self.lod_status.setText(f"  视口 LOD：{key} 简化失败：{message}（保留包围盒代理）")
            return
        source = sum(len(m.faces) for m in meshes.values())
        drawn = sum(m.display_faces for m in self._display_meshes.values())
        simplified = sum(1 for m in self._display_meshes.values() if m.simplified)
        if simplified:
            self.lod_status.setText(
                f"  视口 LOD：自动 · {source:,} → {drawn:,} 面（{simplified} 个简化，{total} 个模型）")
        else:
            self.lod_status.setText(f"  视口 LOD：自动 · {source:,} 面未超过阈值")

    def _candidate_changed(self, candidate):
        self._apply_scene_displays()
        self.scene_view.set_selection(self.state.edited_target_key)
        self.scene_view.set_candidates([candidate] if candidate else [])
        if candidate:
            self.camera_preview.set_camera(self.state.config.camera, candidate.camera)
            anchor = next(t for t in self.state.config.targets if t.asset.key == candidate.anchor_key)
            fixed = next((c for c in self.state.config.fixed_cameras if c.key == candidate.region_key), None)
            region = None if fixed else next((r for r in self.state.config.regions if r.key == candidate.region_key), None)
            label = f"固定机位 · {fixed.name}" if fixed else (region.name if region else candidate.region_key)
            if hasattr(self.camera_preview, "caption"):
                self.camera_preview.caption.setText(f"  中心 {anchor.name}  |  {label}  |  场景 {candidate.scene_index + 1} · 视角 {candidate.view_index + 1}")
                self.camera_preview.setWindowTitle(f"相机视角 · {anchor.name} · {label} · 候选机位构图预览")
            pose = candidate.camera
            kind = "固定" if getattr(candidate, "camera_kind", "random") == "fixed" else "随机"
            caption = (f"  场景 {candidate.scene_index + 1} · 视角 {candidate.view_index + 1} · {kind}   "
                       f"r {pose.radius_m:.3f} m   φ {pose.azimuth_deg:.1f}°   α {pose.elevation_deg:.1f}°")
            diagnostics = getattr(candidate, "silhouette_diagnostics", ()) or ()
            if diagnostics:
                row = diagnostics[0]
                faces = row.get("faces")
                faces_text = f"{faces:,} 面" if isinstance(faces, int) else "面数未知"
                caption += (f"    |    轮廓 {row.get('method', '?')} {faces_text}"
                            f" · 网格 {row.get('grid_size', '?')}²")
                if row.get("estimate_only"):
                    caption += "（估计，需像素验收）"
            self.viewport_caption.setText(caption + "    |    左键旋转 / Shift + 左键平移 / 滚轮缩放")
        else:
            self.viewport_caption.setText("  用户透视 · 辅助网格不参与输出")

    def _preview_status(self, message):
        """Transient line only: the persistent guidance lives in ``preview_hint``."""
        self.preview_state.setText(message)

    def _update_hint(self):
        if self.state.config.camera_mode == "fixed":
            self.preview_hint.setText("固定机位：视角序号对应机位列表里的第几台；候选只会来自这些机位。")
        else:
            self.preview_hint.setText("随机区域：在右侧调整距离/方位/仰角范围；换视角序号即换一组采样。")

    def _export_stale(self):
        """A shown command must never look like it belongs to the current project."""
        self.command_preview.clear()
        self.command_preview.setPlaceholderText("上一个导出属于切换前的项目；重新导出后这里会显示当前命令。")

    def _exported(self, result):
        self.command_preview.setPlaceholderText("在脚本工作区检查配置并导出，然后复制命令到终端运行。")
        if isinstance(result, list):
            self.command_preview.setPlainText("\n\n".join(item.command for item in result))
            self.bottom.setCurrentIndex(1)
            self.statusBar().showMessage(f"已按尺寸拆分导出 {len(result)} 个任务", 15000)
            return
        self.command_preview.setPlainText(result.command)
        self.bottom.setCurrentIndex(1)
        self.statusBar().showMessage(f"已导出脚本与配置：{result.directory}", 15000)

    def _update_title(self, *_):
        filename = self.state.path.name if self.state.path else "未保存项目"
        marker = " *" if self.state.dirty else ""
        self.setWindowTitle(f"{filename}{marker} — BlenderProc 数据集工作区")
        self.project_label.setText(filename + marker)

    def sync(self):
        self._updating = True
        config = self.state.config
        self.objects.clear()
        target_root = QTreeWidgetItem(self.objects, ["▾  目标模型"])
        target_root.setExpanded(True)
        for target in config.targets:
            anchor = target.asset.key == config.anchor_key
            label = ("◎ " if anchor else "◇ ") + target.name
            item = QTreeWidgetItem(target_root, [label, str(target.obj_id), target.asset.unit])
            item.setData(0, Qt.UserRole, target.asset.key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if target.enabled else Qt.Unchecked)
            item.setToolTip(0, str(target.asset.path) + ("\n渲染中心" if anchor else ""))
            if anchor:
                item.setForeground(0, QColor("#f99c33"))
            if target.asset.key == self.state.edited_target_key:
                self.objects.setCurrentItem(item)
        if config.occluders:
            occ_root = QTreeWidgetItem(self.objects, [f"▾  遮挡物 ({len(config.occluders)})"])
            occ_root.setExpanded(False)
            for asset in config.occluders:
                QTreeWidgetItem(occ_root, ["◇ " + asset.path.name, "", asset.unit])
        QTreeWidgetItem(self.objects, ["▣  采样相机", "", f"{config.camera.width}×{config.camera.height}"])
        QTreeWidgetItem(self.objects, ["☼  点光源", "", "W"])
        QTreeWidgetItem(self.objects, [f"◈  World / HDRI ({len(config.hdri.files)})"])
        anchor = next((t for t in config.targets if t.asset.key == config.anchor_key), None)
        self.anchor_badge.setText("◎ 中心：" + (anchor.name if anchor else "未设置"))
        self.preview_region.clear()
        for camera in config.fixed_cameras:
            self.preview_region.addItem("▣ " + camera.name, camera.key)
        for region in config.regions:
            self.preview_region.addItem("◌ " + region.name, region.key)
        self.preview_region.setCurrentIndex(self.preview_region.findData(self.sampling.current_region_key))
        # A preview is always one scene, so its index walks the views of that scene, not the
        # whole dataset; a pinned camera has exactly one view.
        pinned = config.camera_mode == "fixed"
        self.sample_index.setMaximum(1 if pinned else max(1, config.views))
        self.sample_index.setValue(1 if pinned else self.sample_index.value())
        self.sample_index.setEnabled(not pinned)
        self.scene_view.set_regions(config, self.sampling.current_region_key)
        self.lod_mode.setCurrentIndex(self.lod_mode.findData(config.viewport.lod_mode))
        self._lod_refresh()
        if self.state.candidate is not None:
            # Both viewports read the colour from the config, so an appearance-only change has to
            # redraw them explicitly; geometry-only updates still arrive through candidate_changed.
            self._apply_scene_displays()
        cameras = (f"{len(config.fixed_cameras)} 个固定机位" if config.camera_mode == "fixed"
                   else f"{len(config.regions)} 个随机区域 · 每场景 {config.views} 视角")
        self.statusBar().showMessage(f"{sum(t.enabled for t in config.targets)} 个启用目标    |    "
                                     f"{cameras}"
                                     f"    |    {config.scene_count:,} 场景 × {config.views} 视角 = {config.total_shots:,} 张    |    "
                                     + ("RGB + mask + mask_visib" if config.output.format == "rgb_masks" else "BOP"))
        self._update_hint()
        self._update_title()
        self._updating = False

    def _confirm_discard(self):
        if not self.state.dirty:
            return True
        answer = QMessageBox.question(self, "未保存的项目", "保存当前项目的修改？",
                                       QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                       QMessageBox.Save)
        if answer == QMessageBox.Cancel:
            return False
        return self.save_project() if answer == QMessageBox.Save else True

    def new_project(self):
        if self._confirm_discard():
            self.state.path = None
            self.state.set_project(new_project(Path.cwd()))
            self._update_title()

    def open_project(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开项目", "", "项目 YAML (*.yaml *.yml)")
        if path:
            try:
                self.state.load(path)
                self._update_title()
            except (ValueError, OSError) as error:
                QMessageBox.critical(self, "无法打开项目", str(error))

    def save_project(self):
        if self.state.path is None:
            return self.save_project_as()
        try:
            self.state.save()
            self._update_title()
            self.statusBar().showMessage("项目已保存", 5000)
            return True
        except (ValueError, OSError) as error:
            QMessageBox.critical(self, "无法保存项目", str(error))
            return False

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存项目", str(self.state.path or Path.cwd() / "scene.yaml"),
                                             "项目 YAML (*.yaml *.yml)")
        if not path:
            return False
        try:
            self.state.save(path)
            self._update_title()
            self.statusBar().showMessage("项目已保存", 5000)
            return True
        except (ValueError, OSError) as error:
            QMessageBox.critical(self, "无法保存项目", str(error))
            return False

    def closeEvent(self, event):
        if self._closing or self._confirm_discard():
            self._closing = True
            if self._persist_settings:
                self.settings.setValue("geometry", self.saveGeometry())
                for key, splitter in (("horizontal", self.horizontal), ("vertical", self.vertical), ("right", self.right)):
                    self.settings.setValue(key, splitter.saveState())
            # A search still in flight must not write a candidate into state after the editor is gone.
            self.preview.close()
            self.lod.close()
            self.camera_preview.hide()
            event.accept()
        else:
            event.ignore()
