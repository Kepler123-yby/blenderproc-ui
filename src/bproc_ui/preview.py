"""Bounded geometry-only candidates using the production sampler."""

from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from bproc_core.scene import Rejection, propose_candidate


class PreviewController(QObject):
    status_changed = Signal(str)
    refreshed = Signal(object)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.region_key = state.config.regions[0].key if state.config.regions else None
        self.sample_index = 0
        self.preview_seed = state.config.seed
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        state.project_changed.connect(self.schedule)
        state.meshes_changed.connect(self.schedule)

    def schedule(self):
        self._timer.start()

    def refresh(self, region_key=None, sample_index=None, preview_seed=None):
        if region_key is not None:
            self.region_key = region_key
        if sample_index is not None:
            self.sample_index = sample_index
        if preview_seed is not None:
            self.preview_seed = preview_seed
        config = self.state.config
        region = next((r for r in config.regions if r.key == self.region_key), None)
        if region is None:
            region = config.regions[0] if config.regions else None
            self.region_key = region.key if region else None
        if region is None or not config.targets:
            self.state.set_candidate(None)
            self.status_changed.emit("导入目标并设置渲染中心，即可预览候选构图")
            return None
        request = min(max(0, self.sample_index), max(0, config.num_samples - 1))
        preview_config = replace(config, regions=(region,), seed=self.preview_seed)
        rejection = None
        try:
            for attempt in range(min(config.occlusion.max_attempts, 30)):
                candidate = propose_candidate(preview_config, self.state.meshes, request, attempt)
                if isinstance(candidate, Rejection):
                    rejection = candidate
                    continue
                self.state.set_candidate(candidate)
                self.status_changed.emit("候选构图已更新 · 尚未经过 Blender 像素遮挡验收")
                self.refreshed.emit(candidate)
                return candidate
        except (ValueError, KeyError, TypeError) as error:
            self.state.set_candidate(None)
            self.status_changed.emit(f"预览参数无效：{error}")
            return None
        self.state.set_candidate(None)
        reasons = {"missing_anchor": "请选择启用的渲染中心", "missing_mesh": "正在载入网格，或存在无法读取的资源",
                   "collision": "目标或遮挡物位置范围相交", "out_of_frame": "目标超出相机画幅，请增大距离或调整位置范围",
                   "camera_inside": "机位落在模型内部"}
        message = reasons.get(rejection.reason, rejection.detail) if rejection else "没有有效候选"
        self.status_changed.emit(f"{min(config.occlusion.max_attempts, 30)} 次内未获得候选：{message}")
        return None
