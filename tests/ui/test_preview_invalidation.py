"""Deterministic interleavings for the asynchronous preview: no stale result may be written back.

Each test blocks a successful proposal, changes the project context (clear, switch, debounce,
mesh replacement, controller shutdown) and only then releases the old result. The old candidate
must be dropped, while an ordinary asynchronous response still reaches the viewports.
"""
from dataclasses import replace
from threading import Event
import time

from PySide6.QtCore import QCoreApplication, QEventLoop

from bproc_core.assets import load_mesh
from bproc_ui.preview import PreviewController
from tests.factories import make_project


def _pump(seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 10)
        time.sleep(.002)


def _pump_until(predicate, timeout=5.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 10)
        if predicate():
            return True
        time.sleep(.002)
    return predicate()


def _loaded(state):
    state.set_meshes({target.asset.key: load_mesh(target.asset) for target in state.config.targets})
    return state


class _BlockedProposal:
    """Hold the geometry proposal inside the worker until the test releases it."""

    def __init__(self, monkeypatch, result=None):
        self.entered = Event()
        self.release = Event()
        self.result = object() if result is None else result
        self.next_result = object()
        self.calls = 0
        monkeypatch.setattr("bproc_ui.preview.sample_scene", lambda *args, **kwargs: object())

        def proposal(*args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                return self.next_result      # searches started after the edit are not blocked
            self.entered.set()
            self.release.wait(5)
            return self.result

        monkeypatch.setattr("bproc_ui.preview.propose_candidate", proposal)

    def start(self, controller):
        controller.refresh()
        assert self.entered.wait(5), "the proposal worker never started"

    def release_result(self, controller):
        self.release.set()
        assert _pump_until(lambda: not controller.waiting()), "the worker result never arrived"
        _pump(.05)


def test_clearing_the_project_drops_a_running_search(state, monkeypatch):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    blocked.release.set()  # the search is now finished but its result is still queued

    state.update(targets=(), anchor_key=None)
    marker = len(statuses)
    _pump(.18)  # the debounced refresh for the now-empty project runs here
    assert state.candidate is None
    blocked.release_result(controller)

    assert controller._token > token_before, "clearing must invalidate the running request"
    assert state.candidate is None, "the old candidate must not reappear"
    assert not controller.waiting()
    assert statuses[-1].startswith("导入目标")
    assert "候选构图已更新" not in statuses[marker:]


def test_switching_the_project_drops_a_running_search(state, monkeypatch, tmp_path):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    blocked.release.set()

    other = make_project(tmp_path / "other", ids=(5,))
    other_meshes = {target.asset.key: load_mesh(target.asset) for target in other.targets}
    controller._timer.setInterval(5000)   # keep the new search inside the debounce for the test
    state.set_project(other)
    state.set_meshes(other_meshes)
    marker = len(statuses)
    _pump(.05)
    blocked.release_result(controller)

    assert controller._token > token_before
    assert state.candidate is not blocked.result
    assert state.candidate is None
    assert state.config.targets[0].asset.key == "target-5"
    assert "候选构图已更新" not in statuses[marker:]
    controller.close()


def test_reloading_the_same_project_drops_a_running_preview(state, monkeypatch, tmp_path):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    path = tmp_path / "reload.yaml"
    state.save(path)
    controller = PreviewController(state)
    controller._timer.setInterval(5000)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    try:
        state.load(path)
        assert state.candidate is None and state.meshes == {}
        assert controller._token > token_before
        blocked.release_result(controller)
        assert state.candidate is None
    finally:
        blocked.release.set()
        controller.close()


def test_a_result_arriving_during_the_debounce_is_dropped(state, monkeypatch):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    blocked.release.set()

    controller._timer.setInterval(5000)   # a long debounce: no replacement search can start
    state.update(seed=43)          # project change: schedules a debounced refresh
    assert not controller.waiting()
    assert state.candidate is None
    # Deliver the old result while the 100 ms debounce is still pending.
    assert _pump_until(lambda: controller._live == {}, timeout=1.0)
    _pump(.02)
    assert state.candidate is None, "a result from before the change must not be applied"
    assert controller._token > token_before
    assert "候选构图已更新" not in statuses
    controller.close()


def test_replacing_the_meshes_drops_a_running_search(state, monkeypatch):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    mesh_revision_before = state.mesh_revision
    blocked.release.set()

    controller._timer.setInterval(5000)
    state.set_meshes({target.asset.key: load_mesh(target.asset) for target in state.config.targets})
    assert state.mesh_revision > mesh_revision_before
    blocked.release_result(controller)

    assert controller._token > token_before
    assert state.candidate is None
    controller.close()


def test_a_context_bump_without_a_signal_also_drops_the_result(state, monkeypatch):
    """Belt and braces: even if a context change forgot to schedule, the write-back is refused."""
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    blocked.release.set()

    state.context_revision += 1     # emulate a change that did not fire project_changed
    assert _pump_until(lambda: controller._live == {}, timeout=2.0)

    assert state.candidate is None
    assert not controller.waiting()
    controller.close()


def test_a_mesh_revision_bump_without_a_signal_also_drops_the_result(state, monkeypatch):
    """The write-back compares geometry identity, not only the config revision."""
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    blocked.release.set()

    state.mesh_revision += 1     # geometry replaced without project_changed/meshes_changed
    assert _pump_until(lambda: controller._live == {}, timeout=2.0)

    assert controller._token == token_before
    assert state.candidate is None
    assert not controller.waiting()
    controller.close()


def test_the_empty_project_refresh_invalidates_on_its_own(state, monkeypatch):
    """Even with no scheduled refresh, the empty-project early return must invalidate."""
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    token_before = controller._token
    blocked.release.set()

    controller._timer.stop()                 # no debounced search may run in this test
    state.config = replace(state.config, targets=(), anchor_key=None)   # edit without any signal
    controller.refresh()                     # empty path: cancel + bump on its own
    assert controller._token > token_before
    assert "导入目标" in statuses[-1]
    blocked.release_result(controller)

    assert state.candidate is None
    assert not controller.waiting()
    assert "候选构图已更新" not in statuses
    controller.close()


def test_a_closed_controller_ignores_later_changes(state, monkeypatch):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    blocked.release.set()

    controller.close()
    marker = len(statuses)
    state.update(seed=43)        # the closed editor stays connected to the state object
    state.set_meshes({})        # an empty project edit must not reach the closed controller
    _pump(.25)
    assert not controller.waiting(), "a closed controller started a new search"
    assert state.candidate is None
    assert statuses[marker:] == [], "a closed controller wrote back a status"


def test_a_closed_controller_never_writes_back(state, monkeypatch):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    blocked = _BlockedProposal(monkeypatch)
    blocked.start(controller)
    blocked.release.set()

    controller.close()
    assert not controller.waiting()
    _pump(.2)
    blocked.release.set()

    assert state.candidate is None
    assert "候选构图已更新" not in statuses


def test_an_ordinary_asynchronous_result_is_still_applied(state, monkeypatch):
    _loaded(state)
    state.update(views_per_scene=1, scene_count=1)
    controller = PreviewController(state)
    statuses = []
    controller.status_changed.connect(statuses.append)
    sentinel = object()
    original_sample = __import__("bproc_ui.preview", fromlist=["sample_scene"]).sample_scene
    monkeypatch.setattr("bproc_ui.preview.sample_scene", lambda *args, **kwargs: object())
    monkeypatch.setattr("bproc_ui.preview.propose_candidate", lambda *args, **kwargs: sentinel)
    assert original_sample is not None

    token_before = controller._token
    controller.refresh()
    assert _pump_until(lambda: not controller.waiting())

    assert state.candidate is sentinel
    assert controller._token > token_before
    assert statuses[-1].startswith("候选构图已更新")
    controller.close()


def test_a_material_change_keeps_the_shown_candidate(state):
    _loaded(state)
    controller = PreviewController(state)
    candidate = object()
    state.set_candidate(candidate)
    state.update(material=replace(state.config.material, color="solid", base_color="#ff0000"))
    assert state.candidate is candidate
    assert not controller.waiting(), "only the debounced refresh may start a new search"
    controller.close()
