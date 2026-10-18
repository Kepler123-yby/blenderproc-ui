"""Export an inspectable terminal job, and open the current scene in Blender for a look; this editor never renders a dataset."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLineEdit, QComboBox, QPushButton,
                               QLabel, QPlainTextEdit, QFileDialog, QApplication, QCheckBox)

from bproc_core.config import validate_project
from bproc_export.bundle import export_job
from bproc_ui.export_worker import ExportTask, new_request
from bproc_ui.blender_view import BlenderViewer
from bproc_ui.widgets import number, integer, section, note, row


class ExportPanel(QWidget):
    exported = Signal(object)
    message = Signal(str)
    stale = Signal()

    def __init__(self, state, parent=None, export_function=None, pool=None):
        super().__init__(parent)
        self.state = state
        self.export_function = export_function or export_job
        self.pool = pool or QThreadPool.globalInstance()
        self._updating = False
        self._export_task = None
        self._last_request = None
        self.last_result = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 10)
        layout.setSpacing(6)
        form = section("渲染结果", layout)
        self.format = QComboBox()
        self.format.addItem("RGB + 双掩码", "rgb_masks")
        self.format.addItem("BOP", "bop")
        self.format.currentIndexChanged.connect(lambda: self._output_changed("format"))
        form.addRow("输出格式", self.format)
        self.root = QLineEdit()
        self.root.editingFinished.connect(lambda: self._output_changed("root"))
        choose_root = QPushButton("…")
        choose_root.setMaximumWidth(28)
        choose_root.clicked.connect(self._choose_root)
        form.addRow("输出父目录", row(self.root, choose_root))
        self.dataset_name = QLineEdit()
        self.dataset_name.editingFinished.connect(lambda: self._output_changed("dataset_name"))
        form.addRow("数据集名称", self.dataset_name)
        self.final_path = note("")
        form.addRow(self.final_path)
        self.rgb_encoding = QComboBox()
        for title, value in (("按格式默认", "default"), ("PNG", "png"), ("JPEG", "jpeg")):
            self.rgb_encoding.addItem(title, value)
        self.rgb_encoding.currentIndexChanged.connect(lambda: self._output_changed("rgb_encoding"))
        form.addRow("RGB 编码", self.rgb_encoding)
        self.depth_scale = number(.1, minimum=.00001, decimals=5, suffix=" mm")
        self.depth_scale.valueChanged.connect(lambda: self._output_changed("depth_scale_mm"))
        form.addRow("深度单位 / 数值", self.depth_scale)
        self.output_description = note("")
        form.addRow(self.output_description)
        form = section("Cycles 设置", layout)
        self.samples = integer(50, 1, 100000)
        self.samples.valueChanged.connect(lambda: self._render_changed("samples"))
        self.device = QComboBox()
        self.device.addItems(["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"])
        self.device.setToolTip("NVIDIA：OptiX / CUDA；AMD：HIP；Intel：oneAPI；Apple：Metal。")
        self.device.currentTextChanged.connect(lambda: self._render_changed("device"))
        form.addRow("采样数", self.samples)
        form.addRow("GPU 后端", self.device)
        self.denoiser = QComboBox()
        for title, value in (("自动 GPU", "auto"), ("关闭", "none"), ("OptiX", "OPTIX")):
            self.denoiser.addItem(title, value)
        self.denoiser.setToolTip("自动：当前 GPU 后端支持时使用 OptiX，否则关闭降噪。\n"
                                 "指定 OptiX 时须有受支持的 GPU；不使用 CPU 降噪。")
        self.denoiser.currentIndexChanged.connect(lambda: self._render_changed("denoiser"))
        form.addRow("降噪", self.denoiser)
        self.noise_threshold = number(.01, minimum=0, maximum=1, decimals=5)
        self.noise_threshold.setToolTip("自适应采样的噪声阈值；0 关闭自适应采样。")
        self.noise_threshold.valueChanged.connect(lambda: self._render_changed("noise_threshold"))
        form.addRow("噪声阈值 / 0 关闭", self.noise_threshold)
        form.addRow(note("终端仅使用 GPU 渲染；启动检查会核验所选 GPU 后端。"))
        form = section("表面处理", layout)
        self.shading_mode = QComboBox()
        for title, value in (("平直", "flat"), ("平滑", "smooth"), ("按角度平滑", "auto")):
            self.shading_mode.addItem(title, value)
        self.shading_mode.currentIndexChanged.connect(lambda: self._render_changed("shading_mode"))
        form.addRow("表面着色", self.shading_mode)
        self.smooth_angle = number(30, minimum=0, maximum=180, decimals=2, suffix="°")
        self.smooth_angle.valueChanged.connect(lambda: self._render_changed("smooth_angle_deg"))
        form.addRow("平滑角度", self.smooth_angle)
        self.weighted_normals = QCheckBox("加权法线")
        self.weighted_normals.setToolTip("按面面积和角度调整法线，改善平面与倒角的高光过渡。")
        self.weighted_normals.toggled.connect(lambda: self._render_changed("weighted_normals"))
        form.addRow("法线处理", self.weighted_normals)
        form.addRow(note("应用到目标和遮挡模型。几何预览仅用于构图，最终表面效果以 Blender 渲染为准。"))
        form = section("本机运行环境", layout)
        self.conda_env = QLineEdit()
        self.blender_dir = QLineEdit()
        self.blenderproc = QLineEdit()
        self.toolkit_path = QLineEdit()
        self.toolkit_path.setPlaceholderText("BOP 模式需要；可留空使用已安装包")
        for label, editor, directory, field in (("Conda 环境", self.conda_env, None, "conda_env"),
                                                 ("Blender 目录", self.blender_dir, True, "blender_dir"),
                                                 ("BlenderProc", self.blenderproc, False, "blenderproc_executable"),
                                                 ("BOP toolkit", self.toolkit_path, True, "toolkit_path")):
            editor.editingFinished.connect(lambda f=field: self._runtime_changed(f))
            if directory is None:
                form.addRow(label, editor)
            else:
                button = QPushButton("…")
                button.setMaximumWidth(28)
                button.clicked.connect(lambda checked=False, e=editor, d=directory, f=field: self._choose_runtime_path(e, d, f))
                form.addRow(label, row(editor, button))
        form.addRow(note("UI 使用独立环境；导出的任务在 bop 中运行，启动前离线检查实际 Blender Python。"))
        form = section("导出脚本与配置", layout)
        self.job_path = QLineEdit(str(state.config.output.root.parent / "jobs" / "render_job"))
        choose_job = QPushButton("…")
        choose_job.setMaximumWidth(28)
        choose_job.clicked.connect(self._choose_job)
        form.addRow("新任务目录", row(self.job_path, choose_job))
        check = QPushButton("检查配置")
        check.clicked.connect(self.validate)
        self.export_button = QPushButton("导出脚本与配置")
        self.export_button.setObjectName("primaryButton")
        self.export_button.clicked.connect(self._export_clicked)
        form.addRow(row(check, self.export_button))
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        self.validation.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self.validation)
        self.command = QPlainTextEdit()
        self.command.setReadOnly(True)
        self.command.setMaximumHeight(100)
        self.command.setPlaceholderText("导出成功后生成完整终端命令")
        form.addRow("完整渲染", self.command)
        copy = QPushButton("复制完整命令")
        copy.clicked.connect(lambda: self._copy(self.command.toPlainText()))
        form.addRow(copy)
        self.preview_command = QPlainTextEdit()
        self.preview_command.setReadOnly(True)
        self.preview_command.setMaximumHeight(100)
        self.preview_command.setPlaceholderText("4 张试渲染会使用独立输出目录")
        form.addRow("少量试渲染", self.preview_command)
        copy_preview = QPushButton("复制试渲染命令")
        copy_preview.clicked.connect(lambda: self._copy(self.preview_command.toPlainText()))
        form.addRow(copy_preview)
        form.addRow(note("复制命令后在终端执行。已有任务或数据集目录会拒绝覆盖；资源文件保留在原位置。"))
        viewer = section("在 Blender 中查看", layout)
        self.viewer = BlenderViewer(state, self)
        self.viewer.status.connect(self._blender_status)
        self.viewer.failed.connect(self._blender_failed)
        self.blender_open = QPushButton("在 Blender 中打开当前构图")
        self.blender_open.setObjectName("primaryButton")
        self.blender_open.clicked.connect(lambda: self.viewer.open())
        self.blender_state = QLabel("用当前候选构图重建场景后打开 Blender，可在里面自由转视角、试渲染参数。")
        self.blender_state.setWordWrap(True)
        self.blender_state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        viewer.addRow(self.blender_open)
        viewer.addRow(self.blender_state)
        layout.addStretch()
        state.project_changed.connect(self.sync)
        state.candidate_changed.connect(self._sync_blender)
        self.sync()

    def choose_format(self, value):
        index = self.format.findData(value)
        if index < 0:
            raise ValueError("仅支持 rgb_masks 或 bop")
        self.format.setCurrentIndex(index)

    def _output_changed(self, field):
        if not self._updating:
            values = {"format": self.format.currentData(), "root": Path(self.root.text()).expanduser().absolute(),
                      "dataset_name": self.dataset_name.text(), "rgb_encoding": self.rgb_encoding.currentData(),
                      "depth_scale_mm": self.depth_scale.value()}
            output = replace(self.state.config.output, **{field: values[field]})
            self.state.update(output=output)

    def _choose_root(self):
        path = QFileDialog.getExistingDirectory(self, "选择渲染输出父目录", self.root.text())
        if path:
            self.root.setText(path)
            self._output_changed("root")

    def _render_changed(self, field):
        if not self._updating:
            values = {"samples": self.samples.value(), "device": self.device.currentText(),
                      "shading_mode": self.shading_mode.currentData(),
                      "smooth_angle_deg": self.smooth_angle.value(),
                      "weighted_normals": self.weighted_normals.isChecked(),
                      "denoiser": self.denoiser.currentData(),
                      "noise_threshold": self.noise_threshold.value()}
            self.state.update(render=replace(self.state.config.render, **{field: values[field]}))

    def _runtime_changed(self, field):
        if not self._updating:
            values = {"conda_env": self.conda_env.text().strip(),
                      "blender_dir": Path(self.blender_dir.text()).expanduser().absolute(),
                      "blenderproc_executable": Path(self.blenderproc.text()).expanduser().absolute(),
                      "toolkit_path": Path(self.toolkit_path.text()).expanduser().absolute()
                      if self.toolkit_path.text().strip() else None}
            self.state.update(runtime=replace(self.state.config.runtime, **{field: values[field]}))

    def _choose_runtime_path(self, editor, directory, field):
        if directory:
            path = QFileDialog.getExistingDirectory(self, "选择运行环境目录", editor.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择 BlenderProc 可执行文件", editor.text())
        if path:
            editor.setText(path)
            self._runtime_changed(field)

    def _choose_job(self):
        path, _ = QFileDialog.getSaveFileName(self, "设置新的任务目录名", self.job_path.text(),
                                             options=QFileDialog.DontConfirmOverwrite)
        if path:
            self.job_path.setText(path)

    def _sync_blender(self, candidate=None):
        self.blender_open.setEnabled(self.state.candidate is not None)

    def _blender_status(self, message):
        self.blender_state.setText(message)

    def _blender_failed(self, message):
        self.blender_state.setText(message)
        self.message.emit(message)

    def validate(self):
        issues = validate_project(self.state.config)
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            self.validation.setObjectName("errorLabel")
            self.validation.setText("\n".join(f"{issue.field}：{issue.message}" for issue in errors))
        else:
            self.validation.setObjectName("successLabel")
            self.validation.setText("配置检查通过。导出后请先用 --check-only 核验渲染依赖。")
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)
        return issues

    def export_to(self, path):
        """Synchronous core API, kept for scripts and tests; the button uses `start_export`."""
        errors = [issue for issue in self.validate() if issue.severity == "error"]
        if errors:
            raise ValueError("请先修正配置检查中列出的项目")
        request = new_request(self.state, path)
        result = self.export_function(request.config, Path(path))
        self._show_result(result, request)
        return result

    def start_export(self, path) -> bool:
        """Run one export on a worker thread; returns False while another export is running."""
        if self._export_task is not None:
            return False
        errors = [issue for issue in self.validate() if issue.severity == "error"]
        if errors:
            self._show_error("请先修正配置检查中列出的项目")
            return False
        request = new_request(self.state, path)
        task = ExportTask(request, self.export_function)
        task.signals.finished.connect(self._export_finished)
        task.signals.failed.connect(self._export_failed)
        self._export_task = task
        self._busy(True)
        self.pool.start(task)
        return True

    def _busy(self, running):
        self.export_button.setEnabled(not running)
        self.export_button.setText("正在导出…" if running else "导出脚本与配置")

    def _matches(self, request) -> bool:
        """A result may only be shown as current if its whole context is still current."""
        return (request.revision == getattr(self.state, "context_revision", 0)
                and request.config == self.state.config
                and request.project_path == getattr(self.state, "path", None))

    def _export_finished(self, request, result):
        self._export_task = None
        self._busy(False)
        if not self._matches(request):
            # Only the snapshot's own context was exported; the disk copy stays valid.
            self.stale.emit()
            self._show_error(f"这次导出属于切换前的项目或旧设置，已过期；任务仍在 {result.directory}")
            return
        self._show_result(result, request)
        self.message.emit(f"导出完成：{result.directory}")

    def _export_failed(self, request, message):
        self._export_task = None
        self._busy(False)
        if not self._matches(request):
            self.stale.emit()
            self.message.emit("已忽略上一次导出任务的失败：" + message)
            return
        self._show_error(message)

    def _show_result(self, result, request):
        self._last_request = request
        self.last_result = result
        self.job_path.setText(str(result.directory))
        self.command.setPlainText(result.command)
        self.preview_command.setPlainText(result.preview_command)
        self.validation.setObjectName("successLabel")
        self.validation.setText(f"已导出：{result.directory}\n可关闭 UI，在终端执行以下命令。")
        self._restyle()
        self.exported.emit(result)

    def _show_error(self, text):
        self.validation.setObjectName("errorLabel")
        self.validation.setText(text)
        self._restyle()
        self.message.emit(text)

    def _restyle(self):
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)

    def _export_clicked(self):
        if not self.start_export(self.job_path.text()) and self._export_task is not None:
            self.message.emit("上一次导出还没结束")

    def _copy(self, value):
        if value:
            QApplication.clipboard().setText(value)
            self.message.emit("终端命令已复制")

    def sync(self):
        self._updating = True
        config = self.state.config
        self.format.setCurrentIndex(self.format.findData(config.output.format))
        self.root.setText(str(config.output.root))
        self.dataset_name.setText(config.output.dataset_name)
        self.final_path.setText("最终目录：" + str(config.output.root / config.output.dataset_name))
        self.rgb_encoding.setCurrentIndex(self.rgb_encoding.findData(config.output.rgb_encoding))
        self.depth_scale.setValue(config.output.depth_scale_mm)
        self.depth_scale.setEnabled(config.output.format == "bop")
        if config.output.format == "rgb_masks":
            self.output_description.setText("RGB + mask + mask_visib\nrgb/000000.png\nmask/000000_000002.png\nmask_visib/000000_000002.png\n后缀为 obj_id；每个启用目标分别输出 0/255 掩码。")
        else:
            self.output_description.setText("BOP：RGB、深度、相机与位姿、模型、双掩码和可见性。\nmask/000000_000000.png\n掩码后缀为 GT 列表索引，模型和位姿平移采用毫米。")
        self.samples.setValue(config.render.samples)
        self.device.setCurrentText(config.render.device)
        self.shading_mode.setCurrentIndex(self.shading_mode.findData(config.render.shading_mode))
        self.smooth_angle.setValue(config.render.smooth_angle_deg)
        self.smooth_angle.setEnabled(config.render.shading_mode == "auto")
        self.weighted_normals.setChecked(config.render.weighted_normals)
        self.denoiser.setCurrentIndex(self.denoiser.findData(config.render.denoiser))
        self.noise_threshold.setValue(config.render.noise_threshold)
        self.conda_env.setText(config.runtime.conda_env)
        self.blender_dir.setText(str(config.runtime.blender_dir))
        self.blenderproc.setText(str(config.runtime.blenderproc_executable))
        self.toolkit_path.setText(str(config.runtime.toolkit_path) if config.runtime.toolkit_path else "")
        self.toolkit_path.setEnabled(config.output.format == "bop")
        if self.last_result is not None and self._last_request is not None \
                and not self._matches(self._last_request):
            # The shown command belongs to a project or settings that are no longer current.
            self.last_result = None
            self._last_request = None
            self.command.clear()
            self.preview_command.clear()
            self.validation.setText("上一个导出属于切换前的项目或旧设置，命令已隐藏；磁盘上的任务仍可运行。")
            self.stale.emit()
        elif self.last_result:
            self.validation.setText("项目设置已更改；请导出到新的任务目录以更新快照。")
        self._updating = False
        self.blender_open.setEnabled(self.state.candidate is not None)
