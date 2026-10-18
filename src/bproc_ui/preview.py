"""Bounded geometry-only candidates using the production sampler."""

from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from bproc_core.scene import Rejection, propose_candidate, sample_scene


class PreviewController(QObject):
    status_changed = Signal(str)
    refreshed = Signal(object)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.region_key = (state.config.fixed_cameras[0].key if state.config.fixed_cameras else
                           state.config.regions[0].key if state.config.regions else None)
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
        fixed = next((c for c in config.fixed_cameras if c.key == self.region_key), None)
        region = None if fixed else next((r for r in config.regions if r.key == self.region_key), None)
        if fixed is None and region is None:
            fixed = config.fixed_cameras[0] if config.fixed_cameras else None
            region = None if fixed else (config.regions[0] if config.regions else None)
            self.region_key = fixed.key if fixed else region.key if region else None
        if fixed is None and region is None or not config.targets:
            self.state.set_candidate(None)
            self.status_changed.emit("导入目标并设置渲染中心，即可预览候选构图")
            return None
        # The preview is "scene 0 of one camera source". Random mode must keep the project's view
        # count: narrowing it to one made every 视角 after the first an out-of-range index.
        views = max(1, int(config.views))
        if fixed is not None:
            view_index = 0
            self.sample_index = 0
            preview_config = replace(config, camera_mode="fixed", regions=(), fixed_cameras=(fixed,),
                                     views_per_scene=1, scene_count=1, seed=self.preview_seed)
        else:
            view_index = min(max(0, int(self.sample_index)), views - 1)
            self.sample_index = view_index
            preview_config = replace(config, camera_mode="random", regions=(region,), fixed_cameras=(),
                                     scene_count=1, seed=self.preview_seed)
        rejection = None
        try:
            scene = sample_scene(preview_config, self.state.meshes, 0)
            if isinstance(scene, Rejection):
                rejection = scene
            else:
                for attempt in range(min(config.occlusion.max_attempts, 30)):
                    candidate = propose_candidate(preview_config, self.state.meshes, scene, view_index, attempt)
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
                   "camera_inside": "机位落在模型内部", "invalid_mesh": "遮挡物网格没有体积"}
        message = reasons.get(rejection.reason, rejection.detail) if rejection else "没有有效候选"
        self.status_changed.emit(f"{min(config.occlusion.max_attempts, 30)} 次内未获得候选：{message}")
        return None
