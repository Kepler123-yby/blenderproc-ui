"""Bounded geometry-only candidates using the production sampler.

Proposing a candidate is geometry work that can take seconds (a hundred occluders, each aimed
against a sampled silhouette). It therefore runs on a worker thread: ``refresh`` only snapshots
the config and bumps a token, and a result is applied only when its token is still the newest one.
A cancelled or superseded search is dropped instead of overwriting newer state, and the total
budget (attempts *and* wall clock) stays bounded so a bad configuration cannot hang the search.
"""

from dataclasses import replace

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from bproc_core.scene import Rejection, propose_candidate, sample_scene

#: One refresh may spend at most this long proposing. A hundred-occluder scene needs ~5 s for the
#: full budget; the UI should report that instead of freezing the window.
SEARCH_DEADLINE_S = 1.5

_REASONS = {
    "missing_anchor": "请选择启用的渲染中心",
    "missing_mesh": "正在载入网格，或存在无法读取的资源",
    "collision": "目标或遮挡物位置范围相交",
    "out_of_frame": "目标超出相机画幅，请增大距离或调整位置范围",
    "camera_inside": "机位落在模型内部",
    "invalid_mesh": "遮挡物网格没有体积",
}


class _Search(QRunnable):
    """Runs one bounded search and reports back; never touches Qt widgets or project state."""

    class Signals(QObject):
        done = Signal(int, object, object, bool)

    def __init__(self, token, config, meshes, view_index, attempts, deadline_s):
        super().__init__()
        self.signals = _Search.Signals()
        self.token = token
        self._config = config
        self._meshes = meshes
        self._view_index = view_index
        self._attempts = attempts
        self._deadline_s = deadline_s
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from time import monotonic

        started = monotonic()
        rejection, result, timed_out, error = None, None, False, None
        try:
            scene = sample_scene(self._config, self._meshes, 0)
            if not isinstance(scene, Rejection):
                for attempt in range(self._attempts):
                    if self._cancelled:
                        break
                    candidate = propose_candidate(self._config, self._meshes, scene, self._view_index, attempt)
                    if not isinstance(candidate, Rejection):
                        result = candidate
                        break
                    rejection = candidate
                    if monotonic() - started > self._deadline_s:
                        timed_out = True
                        break
            else:
                rejection = scene
        except (ValueError, KeyError, TypeError) as exception:
            error = exception
        # Always report, even when cancelled: the controller needs the token back to drop the entry,
        # and a superseded search must not leave a dangling reference to its signal object.
        try:
            self.signals.done.emit(self.token, result, error or rejection, timed_out)
        except RuntimeError:
            pass  # the controller was destroyed while this search ran


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
        self._token = 0
        self._search = None
        self._live = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        state.project_changed.connect(self.schedule)
        state.meshes_changed.connect(self.schedule)

    def schedule(self):
        self._timer.start()

    def waiting(self):
        """True while a search is in flight; the panels keep the previous candidate meanwhile."""
        return self._search is not None

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
            self._cancel()
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
        attempts = min(config.occlusion.max_attempts, 30)
        self._cancel()
        self._token += 1
        search = _Search(self._token, preview_config, dict(self.state.meshes), view_index, attempts,
                         SEARCH_DEADLINE_S)
        search.signals.done.connect(self._on_done)
        self._search = search
        self._live[self._token] = search
        self.status_changed.emit("正在计算候选构图…")
        QThreadPool.globalInstance().start(search)
        return None

    def _cancel(self):
        if self._search is not None:
            self._search.cancel()
            self._search = None

    def _on_done(self, token, result, outcome, timed_out):
        self._live.pop(token, None)
        if token != self._token:
            return  # superseded while it ran
        self._search = None
        if isinstance(outcome, Exception):
            self.state.set_candidate(None)
            self.status_changed.emit(f"预览参数无效：{outcome}")
            return
        if result is not None:
            self.state.set_candidate(result)
            self.status_changed.emit("候选构图已更新 · 尚未经过 Blender 像素遮挡验收")
            self.refreshed.emit(result)
            return
        self.state.set_candidate(None)
        rejection = outcome
        message = _REASONS.get(rejection.reason, rejection.detail) if rejection else "没有有效候选"
        if timed_out:
            self.status_changed.emit(f"计算超过 {SEARCH_DEADLINE_S:g} 秒仍未找到候选（{message}）：遮挡物数量偏多或范围偏紧")
        else:
            self.status_changed.emit(f"未获得候选：{message}（次数与时间均有上限，可调小遮挡物数量或放宽范围）")
