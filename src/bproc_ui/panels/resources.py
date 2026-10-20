"""Targets, independent render anchor, local placement boxes and Haven HDRIs."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLineEdit, QComboBox, QPushButton,
                               QCheckBox, QLabel, QListWidget, QListWidgetItem,
                               QAbstractItemView, QFileDialog, QMessageBox)

from bproc_core.config import AssetSpec, TargetSpec, Box
from bproc_core.assets import suggest_layout
from bproc_ui.asset_loader import AssetLoader
from bproc_ui.widgets import integer, RangeEditor, section, note, row


class ResourcesPanel(QWidget):
    error = Signal(str)

    def __init__(self, state, parent=None, loader=None):
        super().__init__(parent)
        self.state = state
        self.loader = loader or AssetLoader(self)
        self._updating = False
        self._generations, self._requested = {}, {}
        self._inflight, self._failures = {}, {}
        self._asset_load_epoch = state.asset_load_epoch
        self._suggest_after_load = False
        self.selected_keys = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 10)
        layout.setSpacing(6)
        form = section("目标模型", layout)
        self.import_unit = QComboBox()
        self.import_unit.addItems(["mm", "cm", "m"])
        add = QPushButton("＋ 导入 PLY")
        add.clicked.connect(self._choose_targets)
        form.addRow("导入单位", row(self.import_unit, add))
        self.target_choice = QComboBox()
        self.target_choice.currentIndexChanged.connect(self._choose_target)
        form.addRow("编辑目标", self.target_choice)
        self.name = QLineEdit()
        self.name.editingFinished.connect(lambda: self._edit_target(name=self.name.text().strip()))
        form.addRow("名称", self.name)
        self.obj_id = integer(1, 1, 999999)
        self.obj_id.valueChanged.connect(lambda value: self._edit_target(obj_id=value))
        form.addRow("对象 ID", self.obj_id)
        self.unit = QComboBox()
        self.unit.addItems(["m", "cm", "mm"])
        self.unit.currentTextChanged.connect(self._unit_changed)
        self.enabled = QCheckBox("加入每张场景图")
        self.enabled.toggled.connect(self._enabled_changed)
        form.addRow("单位 / 状态", row(self.unit, self.enabled))
        self.path_label = note("尚未导入目标 PLY 网格")
        form.addRow(self.path_label)
        self.size_label = note("保留模型原点，载入时仅换算单位")
        form.addRow(self.size_label)
        self.anchor_button = QPushButton("设为渲染中心")
        self.anchor_button.clicked.connect(self._anchor_clicked)
        remove = QPushButton("移除")
        remove.clicked.connect(self._remove_clicked)
        form.addRow(row(self.anchor_button, remove))
        reload_assets = QPushButton("重新载入资源")
        reload_assets.clicked.connect(self.reload_assets)
        form.addRow(reload_assets)
        self.anchor_label = QLabel()
        self.anchor_label.setObjectName("anchorBadge")
        self.anchor_label.setWordWrap(True)
        form.addRow(self.anchor_label)
        form = section("相对中心的位置范围", layout)
        self.offsets = {axis: RangeEditor(suffix=" m") for axis in "xyz"}
        for axis, editor in self.offsets.items():
            editor.changed.connect(self._offset_changed)
            form.addRow(axis.upper(), editor)
        self.offset_note = note("范围描述模型包围盒中心。坐标轴沿渲染中心的局部方向。")
        form.addRow(self.offset_note)
        bulk = QPushButton("应用到树中其他选中目标")
        bulk.clicked.connect(self._bulk_clicked)
        suggest = QPushButton("恢复建议范围")
        suggest.clicked.connect(self.restore_suggestions)
        form.addRow(bulk)
        form.addRow(suggest)

        form = section("遮挡物", layout)
        self.occluders = QListWidget()
        self.occluders.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.occluders.setMaximumHeight(92)
        self.occluders.itemSelectionChanged.connect(self._sync_occluder_unit)
        self.occluders.currentRowChanged.connect(self._sync_occluder_unit)
        form.addRow(self.occluders)
        self.occluder_import_unit = QComboBox()
        self.occluder_import_unit.addItems(["mm", "cm", "m"])
        form.addRow("新增导入单位", self.occluder_import_unit)
        add_occ = QPushButton("＋ 文件")
        add_occ.clicked.connect(self._choose_occluders)
        folder_occ = QPushButton("扫描目录")
        folder_occ.clicked.connect(self._scan_occluders)
        remove_occ = QPushButton("移除")
        remove_occ.clicked.connect(self._remove_occluders)
        form.addRow(row(add_occ, folder_occ, remove_occ))
        self.occluder_unit = QComboBox()
        self.occluder_unit.addItems(["m", "cm", "mm"])
        self.occluder_unit.currentTextChanged.connect(self._set_occluder_unit)
        form.addRow("选中资源单位", self.occluder_unit)
        self.occluder_size_label = note("选中遮挡物可查看换算后的米制尺寸；新增默认 mm，请按源文件确认")
        form.addRow(self.occluder_size_label)
        form.addRow(note("新增遮挡物使用独立单位（默认 mm）；已导入资源只由选中行编辑器修改。"))

        form = section("World · Haven HDRI", layout)
        self.hdris = QListWidget()
        self.hdris.setMaximumHeight(96)
        self.hdris.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.hdris.currentRowChanged.connect(self._fixed_hdri_changed)
        form.addRow(self.hdris)
        add_hdri = QPushButton("＋ HDRI")
        add_hdri.clicked.connect(self._choose_hdri)
        scan_hdri = QPushButton("扫描目录")
        scan_hdri.clicked.connect(self._scan_hdri)
        remove_hdri = QPushButton("移除")
        remove_hdri.clicked.connect(self._remove_hdri)
        form.addRow(row(add_hdri, scan_hdri, remove_hdri))
        self.random_hdri = QCheckBox("每张随机选择环境")
        self.random_hdri.toggled.connect(self._hdri_changed)
        form.addRow(self.random_hdri)
        self.strength = RangeEditor(minimum=0, suffix="")
        self.rotation = RangeEditor(minimum=-3600, maximum=3600, decimals=1, suffix="°")
        self.strength.changed.connect(self._hdri_changed)
        self.rotation.changed.connect(self._hdri_changed)
        form.addRow("环境强度", self.strength)
        form.addRow("Z 旋转", self.rotation)
        form.addRow(note("HDRI 提供背景与照明；未选时用中性灰 0.05 世界背景，视口网格仅为辅助线。"))
        self.error_label = QLabel()
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.error_label)
        layout.addStretch()
        self.loader.loaded.connect(self._loaded)
        self.loader.failed.connect(self._failed)
        state.project_changed.connect(self.sync)
        state.selection_changed.connect(self.sync)
        state.meshes_changed.connect(self._sync_sizes)
        self.sync()

    def _target(self):
        return next((t for t in self.state.config.targets if t.asset.key == self.state.edited_target_key), None)

    def _show_error(self, message):
        self.error_label.setText(str(message))
        self.error.emit(str(message))

    def add_target(self, path, unit=None):
        path = Path(path).expanduser().resolve()
        occupied = {t.obj_id for t in self.state.config.targets}
        obj_id = next(i for i in range(1, 1000000) if i not in occupied)
        asset = AssetSpec("target-" + uuid4().hex[:10], path, unit or self.import_unit.currentText())
        target = TargetSpec(asset=asset, name=path.stem, obj_id=obj_id)
        if not self.state.config.targets:
            self._suggest_after_load = True
        self.state.update(targets=self.state.config.targets + (target,),
                          anchor_key=self.state.config.anchor_key or asset.key)
        self.state.select_target(asset.key)
        return asset.key

    def _choose_targets(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "导入目标模型", "", "PLY 网格 (*.ply)")
        for path in paths:
            self.add_target(path)

    def _choose_target(self, index):
        if not self._updating and index >= 0:
            self.state.select_target(self.target_choice.itemData(index))

    def _edit_target(self, **changes):
        if self._updating:
            return
        target = self._target()
        if target is None:
            return
        if "name" in changes and not changes["name"]:
            self._show_error("目标名称不能为空")
            self.sync()
            return
        if "obj_id" in changes and any(t.asset.key != target.asset.key and t.obj_id == changes["obj_id"]
                                       for t in self.state.config.targets):
            self._show_error("该对象 ID 已被其他目标使用")
            self.sync()
            return
        updated = replace(target, **changes)
        self.state.update(targets=tuple(updated if t.asset.key == target.asset.key else t
                                        for t in self.state.config.targets))

    def _unit_changed(self, unit):
        if not self._updating and self._target():
            self._edit_target(asset=replace(self._target().asset, unit=unit))

    def set_target_enabled(self, key, enabled):
        if not enabled and key == self.state.config.anchor_key:
            raise ValueError("请先把另一个启用目标设为渲染中心，再停用当前中心")
        self.state.update(targets=tuple(replace(t, enabled=enabled) if t.asset.key == key else t
                                        for t in self.state.config.targets))

    def _enabled_changed(self, enabled):
        if not self._updating and self._target():
            try:
                self.set_target_enabled(self._target().asset.key, enabled)
            except ValueError as error:
                self._show_error(error)
                self.sync()

    def _anchor_clicked(self):
        if self._target():
            try:
                self.state.set_anchor(self._target().asset.key)
            except ValueError as error:
                self._show_error(error)

    def remove_target(self, key):
        remaining = tuple(t for t in self.state.config.targets if t.asset.key != key)
        if key == self.state.config.anchor_key and remaining:
            raise ValueError("请先把另一个启用目标设为渲染中心，再移除当前中心")
        self._generations[key] = self._generations.get(key, 0) + 1
        self._requested.pop(key, None)
        self.state.update(targets=remaining, anchor_key=None if not remaining else self.state.config.anchor_key)

    def _remove_clicked(self):
        if self._target():
            try:
                self.remove_target(self._target().asset.key)
            except ValueError as error:
                self._show_error(error)

    def _offset_changed(self):
        if not self._updating and self._target() and self._target().asset.key != self.state.config.anchor_key:
            self._edit_target(offset=Box(*(self.offsets[a].value() for a in "xyz")))

    def apply_offsets_to_selected(self, box, keys=None):
        selected = set(keys if keys is not None else self.selected_keys)
        self.state.update(targets=tuple(replace(t, offset=replace(box))
                                        if t.asset.key in selected and t.asset.key != self.state.config.anchor_key else t
                                        for t in self.state.config.targets))

    def _bulk_clicked(self):
        if self._target():
            self.apply_offsets_to_selected(self._target().offset)

    def restore_suggestions(self):
        """Give every enabled target its own feasible offsets and a distance that frames them all."""
        try:
            enabled = [t for t in self.state.config.targets if t.enabled]
            if not enabled or any(t.asset.key not in self.state.meshes for t in enabled):
                raise ValueError("请先等待所有启用目标载入完成")
            boxes, radius = suggest_layout(self.state.meshes, enabled, self.state.config.anchor_key,
                                           self.state.config.camera)
            self.state.update(targets=tuple(replace(t, offset=boxes[t.asset.key]) if t.asset.key in boxes else t
                                            for t in self.state.config.targets),
                              regions=tuple(replace(r, radius_m=radius) for r in self.state.config.regions))
        except (ValueError, KeyError) as error:
            self._show_error(error)

    def add_occluders(self, paths, unit=None):
        """Import occluders with their own unit, deduplicated by resolved path.

        The target import control never decides an occluder's unit: the source library is the
        only authority, and the independent control defaults to mm with a visible hint. A single
        batch also deduplicates against itself, so a repeated path (or a symlink pointing at an
        already imported file) cannot become a second physical asset.
        """
        default = unit or self.occluder_import_unit.currentText()
        seen = {asset.path.resolve() for asset in self.state.config.occluders}
        added = []
        for path in paths:
            resolved = Path(path).expanduser().resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            added.append(AssetSpec("occluder-" + uuid4().hex[:10], resolved, default))
        if added:
            self.state.update(occluders=self.state.config.occluders + tuple(added))
            added_keys = {asset.key for asset in added}
            self.occluders.clearSelection()
            for row in range(self.occluders.count()):
                item = self.occluders.item(row)
                if item.data(Qt.UserRole) in added_keys:
                    item.setSelected(True)
            self._sync_occluder_unit()
        return tuple(added)

    def _choose_occluders(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "导入遮挡物", "", "网格 (*.ply *.obj)")
        self.add_occluders(paths)

    def _scan_occluders(self):
        directory = QFileDialog.getExistingDirectory(self, "选择遮挡物目录")
        if directory:
            paths = sorted(p for p in Path(directory).rglob("*") if p.is_file() and p.suffix.lower() in (".ply", ".obj"))
            if not paths:
                self._show_error("目录内没有 PLY / OBJ 网格")
            self.add_occluders(paths)

    def _remove_occluders(self):
        keys = {item.data(Qt.UserRole) for item in self.occluders.selectedItems()}
        self.state.update(occluders=tuple(a for a in self.state.config.occluders if a.key not in keys))

    def _occluder_assets(self):
        """Selected rows, or the current row when a rebuild left nothing selected."""
        keys = {item.data(Qt.UserRole) for item in self.occluders.selectedItems()}
        if not keys:
            item = self.occluders.currentItem()
            keys = {item.data(Qt.UserRole)} if item is not None else set()
        return [asset for asset in self.state.config.occluders if asset.key in keys]

    def _sync_occluder_unit(self, *_):
        """Mirror the selected rows' unit, saying `mixed` instead of silently showing one."""
        assets = self._occluder_assets()
        blocked = self.occluder_unit.blockSignals(True)
        units = {asset.unit for asset in assets}
        if len(units) == 1:
            self.occluder_unit.setPlaceholderText("")
            self.occluder_unit.setCurrentText(units.pop())
        elif units:
            self.occluder_unit.setCurrentIndex(-1)
            self.occluder_unit.setPlaceholderText("混合单位（请选择）")
        self.occluder_unit.blockSignals(blocked)
        self._sync_occluder_size(assets)

    def _set_occluder_unit(self, unit):
        if self._updating:
            return
        keys = {item.data(Qt.UserRole) for item in self.occluders.selectedItems()}
        if not keys:
            return
        self.state.update(occluders=tuple(replace(a, unit=unit) if a.key in keys else a
                                        for a in self.state.config.occluders))

    def add_hdri(self, paths):
        files = list(self.state.config.hdri.files)
        for path in paths:
            path = Path(path).expanduser().resolve()
            if path.suffix.lower() in (".hdr", ".exr") and path not in files:
                files.append(path)
        self.state.update(hdri=replace(self.state.config.hdri, files=tuple(files)))

    def scan_hdri(self, directory):
        files = sorted(p for p in Path(directory).rglob("*") if p.is_file() and p.suffix.lower() in (".hdr", ".exr"))
        self.add_hdri(files)
        return files

    def _choose_hdri(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加 Haven HDRI", "", "HDRI (*.hdr *.exr)")
        self.add_hdri(paths)

    def _scan_hdri(self):
        directory = QFileDialog.getExistingDirectory(self, "选择 Haven 环境目录")
        if directory and not self.scan_hdri(directory):
            self._show_error("目录内没有 .hdr / .exr 环境图")

    def _remove_hdri(self):
        paths = {item.data(Qt.UserRole) for item in self.hdris.selectedItems()}
        self.state.update(hdri=replace(self.state.config.hdri,
                                      files=tuple(p for p in self.state.config.hdri.files if p not in paths)))

    def _fixed_hdri_changed(self, index):
        if not self._updating and not self.state.config.hdri.randomize and index >= 0:
            files = self.state.config.hdri.files
            selected = self.hdris.item(index).data(Qt.UserRole)
            self.state.update(hdri=replace(self.state.config.hdri, files=(selected,) + tuple(p for p in files if p != selected)))

    def _hdri_changed(self):
        if not self._updating:
            self.state.update(hdri=replace(self.state.config.hdri, randomize=self.random_hdri.isChecked(),
                                          strength=self.strength.value(), rotation_deg=self.rotation.value()))

    def _assets(self):
        return {a.key: a for a in [*(t.asset for t in self.state.config.targets), *self.state.config.occluders]}

    def _invalidate_asset_requests(self):
        for key in set(self._generations) | set(self._requested) | set(self._inflight):
            self._generations[key] = self._generations.get(key, 0) + 1
        # set_project may deliberately retain completed canonical meshes. Reuse those,
        # but never carry an in-flight request (or failure) into the new document.
        self._requested = {key: asset for key, asset in self._assets().items()
                           if key in self.state.meshes}
        self._inflight.clear()
        self._failures.clear()
        self._suggest_after_load = False
        self.error_label.clear()
        self._asset_load_epoch = self.state.asset_load_epoch

    def reload_assets(self):
        self._invalidate_asset_requests()
        self.state.set_meshes({})
        self._request_meshes()

    def _request_meshes(self):
        if self._asset_load_epoch != self.state.asset_load_epoch:
            self._invalidate_asset_requests()
        assets = self._assets()
        for key in tuple(self._requested):
            if key not in assets:
                self._requested.pop(key)
                self._inflight.pop(key, None)
                self._failures.pop(key, None)
                self._generations[key] = self._generations.get(key, 0) + 1
        for key, asset in assets.items():
            if (self._requested.get(key) != asset or
                    (key not in self.state.meshes and key not in self._inflight and self._failures.get(key) != asset)):
                self._requested[key] = asset
                self._generations[key] = self._generations.get(key, 0) + 1
                self._inflight[key] = self._generations[key]
                self.loader.request(asset, self._generations[key])

    def _loaded(self, key, generation, mesh):
        if (self._asset_load_epoch != self.state.asset_load_epoch
                or generation != self._generations.get(key)
                or self._assets().get(key) != self._requested.get(key)):
            return
        if key not in self._assets():
            return
        self._inflight.pop(key, None)
        self._failures.pop(key, None)
        self.state.set_meshes({**self.state.meshes, key: mesh})
        self.error_label.clear()
        enabled = [t for t in self.state.config.targets if t.enabled]
        if self._suggest_after_load and all(t.asset.key in self.state.meshes for t in enabled):
            self._suggest_after_load = False
            self.restore_suggestions()

    def _failed(self, key, generation, message):
        if (self._asset_load_epoch == self.state.asset_load_epoch
                and generation == self._generations.get(key) and key in self._assets()
                and self._assets().get(key) == self._requested.get(key)):
            self._inflight.pop(key, None)
            self._failures[key] = self._assets()[key]
            self._show_error(message)

    def _sync_size(self):
        target = self._target()
        mesh = self.state.meshes.get(target.asset.key) if target else None
        if mesh is None:
            self.size_label.setText("等待网格载入；请确认单位与源文件")
        else:
            dims = np.ptp(mesh.bounds_m, axis=0)
            self.size_label.setText("尺寸 " + " × ".join(f"{d:.4g}" for d in dims) + " m\n"
                                    + f"{len(mesh.vertices_m):,} 顶点  /  {len(mesh.faces):,} 三角面")

    def _sync_occluder_size(self, assets=None):
        assets = self._occluder_assets() if assets is None else assets
        if not assets:
            self.occluder_size_label.setText("选中遮挡物可查看换算后的米制尺寸；新增默认 mm，请按源文件确认")
            return
        asset, mesh = assets[0], self.state.meshes.get(assets[0].key)
        if mesh is None:
            self.occluder_size_label.setText(f"{asset.path.name}（{asset.unit}）：等待网格载入")
            return
        dims = np.ptp(mesh.bounds_m, axis=0)
        self.occluder_size_label.setText(
            f"{asset.path.name}：{asset.unit} → " + " × ".join(f"{d:.4g}" for d in dims) + " m"
            + f"（{len(mesh.vertices_m):,} 顶点 / {len(mesh.faces):,} 三角面）")

    def _sync_sizes(self):
        self._sync_size()
        self._sync_occluder_size()

    def sync(self, *_):
        self._updating = True
        target = self._target()
        self.target_choice.clear()
        for item in self.state.config.targets:
            prefix = "◎ " if item.asset.key == self.state.config.anchor_key else ""
            self.target_choice.addItem(prefix + item.name, item.asset.key)
        self.target_choice.setCurrentIndex(self.target_choice.findData(self.state.edited_target_key))
        for control in (self.name, self.obj_id, self.unit, self.enabled, self.anchor_button):
            control.setEnabled(target is not None)
        if target:
            self.name.setText(target.name)
            self.obj_id.setValue(target.obj_id)
            self.unit.setCurrentText(target.asset.unit)
            self.enabled.setChecked(target.enabled)
            self.path_label.setText(str(target.asset.path))
            is_anchor = target.asset.key == self.state.config.anchor_key
            self.anchor_button.setText("◎ 当前渲染中心" if is_anchor else "设为渲染中心")
            for axis, editor in self.offsets.items():
                editor.set_value(getattr(target.offset, axis))
                editor.setEnabled(not is_anchor)
            self.offset_note.setText("中心目标包围盒中心固定在 (0, 0, 0)，不改变模型原点。" if is_anchor else
                                     "范围描述模型包围盒中心。坐标轴沿渲染中心的局部方向。")
        else:
            self.name.clear()
            self.path_label.setText("尚未导入目标 PLY 网格")
            for editor in self.offsets.values():
                editor.setEnabled(False)
        anchor = next((t for t in self.state.config.targets if t.asset.key == self.state.config.anchor_key), None)
        self.anchor_label.setText("◎ 渲染中心：" + (anchor.name if anchor else "未设置"))
        current = self.occluders.currentItem()
        current_key = None if current is None else current.data(Qt.UserRole)
        selected_keys = {item.data(Qt.UserRole) for item in self.occluders.selectedItems()}
        self.occluders.clear()
        for asset in self.state.config.occluders:
            item = QListWidgetItem(f"◇ {asset.path.name}  [{asset.unit}]")
            item.setData(Qt.UserRole, asset.key)
            item.setToolTip(str(asset.path))
            self.occluders.addItem(item)
            if asset.key in selected_keys:
                item.setSelected(True)
            if asset.key == current_key:
                self.occluders.setCurrentItem(item)
        self.hdris.clear()
        for path in self.state.config.hdri.files:
            item = QListWidgetItem("◈ " + path.name)
            item.setData(Qt.UserRole, path)
            item.setToolTip(str(path))
            self.hdris.addItem(item)
        self.hdris.setCurrentRow(0 if self.hdris.count() else -1)
        self.random_hdri.setChecked(self.state.config.hdri.randomize)
        self.strength.set_value(self.state.config.hdri.strength)
        self.rotation.set_value(self.state.config.hdri.rotation_deg)
        self._sync_size()
        self._sync_occluder_size()
        self._updating = False
        self._request_meshes()
