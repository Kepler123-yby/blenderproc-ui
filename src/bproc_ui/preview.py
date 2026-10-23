"""Bounded geometry-only candidates using the production sampler.

Proposing a candidate is geometry work that can take seconds (a hundred occluders, each aimed
against a sampled silhouette). It therefore runs on a worker thread: ``refresh`` only snapshots
the config and bumps a token, and a result is applied only when its token is still the newest one.
A cancelled or superseded search is dropped instead of overwriting newer state, and the total
budget (attempts *and* wall clock) stays bounded so a bad configuration cannot hang the search.
"""

from dataclasses import replace

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from bproc_core.scene import Rejection
from bproc_core.preview import candidate_for_preview
from bproc_core.sampling import allocate_regions

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

    def __init__(self, token, config, meshes, scene_index, view_index, attempts, deadline_s):
        super().__init__()
        self.signals = _Search.Signals()
        self.token = token
        self._config = config
        self._meshes = meshes
        self._scene_index = scene_index
        self._view_index = view_index
        self._attempts = attempts
        self._deadline_s = deadline_s
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        rejection, result, timed_out, error = None, None, False, None
        try:
            candidate = candidate_for_preview(
                self._config, self._meshes, self._scene_index, self._view_index,
                max_attempts=self._attempts, deadline_s=self._deadline_s,
                cancelled=lambda: self._cancelled)
            if isinstance(candidate, Rejection):
                rejection = candidate
                timed_out = candidate.reason == "timeout"
            else:
                result = candidate
        except Exception as exception:
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
        self.scene_index = 0
        self._candidate_config = None
        self.preview_seed = state.config.seed
        self._token = 0
        self._search = None
        self._live = {}
        self._request_context = None
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        state.project_changed.connect(self.schedule)
        state.meshes_changed.connect(self.schedule)

    def schedule(self):
        """Debounce the *new* search, but invalidate the old result immediately.

        A debounce that only delays the next computation would leave the previous request's token
        current, so a worker finishing inside the debounce window could still write a candidate
        for the project the user just left.
        """
        if self._closed:
            return  # a destroyed editor must not schedule work for project state it no longer shows
        self._invalidate()
        self._timer.start()

    def waiting(self):
        """True while a search is in flight; the panels keep the previous candidate meanwhile."""
        return self._search is not None

    def close(self):
        """Stop accepting results; a destroyed editor must never write into project state.

        Closing also has to stop the *future* work, not just the current result: the controller
        stays connected to the state object, so a later edit would otherwise debounce into a new
        search and let its empty-project path write ``set_candidate(None)``.
        """
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._invalidate()

    def _invalidate(self):
        """Bump the token and cancel the in-flight search; older results are dropped on arrival."""
        self._token += 1
        if self._search is not None:
            self._search.cancel()
            self._search = None
        self._request_context = None

    def refresh(self, region_key=None, sample_index=None, preview_seed=None, scene_index=None):
        if self._closed:
            return None  # closed: no search, and no status or candidate write-back
        if region_key is not None:
            self.region_key = region_key
        if sample_index is not None:
            self.sample_index = sample_index
        if preview_seed is not None:
            self.preview_seed = preview_seed
        if scene_index is not None:
            self.scene_index = scene_index
        self._timer.stop()
        config = self.state.config
        sources = config.fixed_cameras if config.camera_mode == "fixed" else config.regions
        if not sources or not config.targets or config.views < 1 or config.scene_count < 1:
            self._invalidate()
            self.state.set_candidate(None)
            self.status_changed.emit("导入目标并设置渲染中心，即可预览候选构图")
            return None
        self.scene_index = min(max(0, int(self.scene_index)), config.scene_count - 1)
        self.sample_index = min(max(0, int(self.sample_index)), config.views - 1)
        # A source click navigates to its assigned first view; never narrow the sampler config.
        if region_key is not None and sample_index is None:
            counts = ({c.key: 1 for c in sources} if config.camera_mode == "fixed" else
                      allocate_regions(config.views, [r.key for r in sources]))
            if not counts.get(region_key, 0):
                self._invalidate()
                self.status_changed.emit("该来源没有分配视角，请选择有效视角编号")
                return None
            self.sample_index = sum(n for key, n in list(counts.items())[:list(counts).index(region_key)])
        view_index = self.sample_index
        preview_config = replace(config, seed=self.preview_seed)
        attempts = min(config.occlusion.max_attempts, 30)
        self._invalidate()
        token = self._token
        self._request_context = (self.state.context_revision, self.state.mesh_revision)
        self._candidate_config = preview_config
        search = _Search(token, preview_config, dict(self.state.meshes), self.scene_index,
                         view_index, attempts, SEARCH_DEADLINE_S)
        search.signals.done.connect(self._on_done)
        self._search = search
        self._live[token] = search
        self.status_changed.emit("正在计算候选构图…")
        QThreadPool.globalInstance().start(search)
        return None

    def _context_is_current(self):
        return self._request_context == (self.state.context_revision, self.state.mesh_revision)

    def _on_done(self, token, result, outcome, timed_out):
        self._live.pop(token, None)
        if self._closed or token != self._token:
            return  # closed, superseded or invalidated while it ran
        if not self._context_is_current():
            self._search = None
            return  # the project or its meshes changed without a new search yet
        self._search = None
        if isinstance(outcome, Exception):
            self.state.set_candidate(None)
            self.status_changed.emit(f"预览参数无效：{outcome}")
            return
        if result is not None:
            self.region_key = getattr(result, "region_key", self.region_key)
            self.state.set_candidate(result, config=self._candidate_config)
            self.status_changed.emit("候选构图已更新 · 候选预览 · 未经像素遮挡验收")
            self.refreshed.emit(result)
            return
        self.state.set_candidate(None)
        rejection = outcome
        message = _REASONS.get(rejection.reason, rejection.detail) if rejection else "没有有效候选"
        if timed_out:
            self.status_changed.emit(f"计算超过 {SEARCH_DEADLINE_S:g} 秒仍未找到候选（{message}）：遮挡物数量偏多或范围偏紧")
        else:
            self.status_changed.emit(f"未获得候选：{message}（次数与时间均有上限，可调小遮挡物数量或放宽范围）")
