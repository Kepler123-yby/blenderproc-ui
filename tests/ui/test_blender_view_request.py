"""A Blender view request must never outlive the project snapshot it was built from."""

from dataclasses import replace
from pathlib import Path

import pytest

from bproc_ui import blender_view as bridge
from bproc_ui.blender_view import BlenderViewer, ViewRequest


class FakePool:
    """Collects runnables so a test can finish them in a controlled order."""

    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def finish(self, index=-1):
        self.tasks[index].run()


class Session:
    def __init__(self, blend):
        self.blend = blend
        self.log = str(blend) + ".log"


@pytest.fixture
def opened(monkeypatch):
    calls = []

    def prepare_scene(config, candidate, workspace, script, *, geometry_only):
        assert geometry_only is True
        return workspace / f"scene-{len(calls)}.blend"

    def open_gui(config, blend, script, workspace):
        calls.append((config, blend))
        return Session(blend)

    monkeypatch.setattr(bridge, "prepare_scene", prepare_scene)
    monkeypatch.setattr(bridge, "open_gui", open_gui)
    return calls


@pytest.fixture
def pool():
    return FakePool()


@pytest.fixture
def viewer(state, pool, opened):
    return BlenderViewer(state, pool=pool)


def statuses(viewer):
    seen = []
    viewer.status.connect(seen.append)
    return seen


def test_a_project_switch_cancels_the_pending_request(state, viewer, pool, opened):
    candidate = object()
    state.set_candidate(candidate)
    seen = statuses(viewer)
    assert viewer.open() is True
    state.set_project(replace(state.config, scene_count=7))   # revision changes, candidate cleared
    state.set_candidate(candidate)                            # same object identity as before
    pool.finish()
    assert opened == []
    assert any("已取消" in text for text in seen)
    assert viewer.busy is False
    assert viewer.open() is True                              # busy state was released
    pool.finish()
    assert len(opened) == 1


def test_a_to_b_to_a_still_rejects_the_first_request(state, viewer, pool, opened):
    first = object()
    state.set_candidate(first)
    viewer.open()
    state.set_project(replace(state.config, scene_count=7))
    state.set_project(replace(state.config, scene_count=9))
    state.set_candidate(first)
    pool.finish()
    assert opened == []


def test_refreshing_only_the_candidate_expires_the_request(state, viewer, pool, opened):
    state.set_candidate(object())
    revision = state.context_revision
    viewer.open()
    state.set_candidate(object())                             # same project, new candidate
    assert state.context_revision == revision
    pool.finish()
    assert opened == []


def test_save_as_invalidates_a_pending_request(state, viewer, pool, opened, tmp_path):
    state.set_candidate(object())
    viewer.open()
    state.save(tmp_path / "elsewhere.yaml")
    assert state.context_revision > 0
    pool.finish()
    assert opened == []


def test_a_late_failure_cannot_clear_a_newer_task(state, viewer, pool, opened):
    revisions = []
    viewer.status.connect(revisions.append)
    viewer.failed.connect(lambda message: revisions.append("FAILED " + message))
    state.set_candidate(object())
    viewer.open()
    pool.finish()                                             # A opened successfully
    old_request = ViewRequest(0, -1, state.config, object(), state.path, viewer.script)
    state.set_candidate(object())
    assert viewer.open() is True
    viewer._failed(old_request, "旧的失败")                    # late, unrelated failure
    assert viewer.busy is True
    assert not [text for text in revisions if text.startswith("FAILED")]
    pool.finish()
    assert len(opened) == 2


def test_a_stale_failure_is_reported_as_cancelled(state, viewer, pool, opened):
    failures = []
    viewer.failed.connect(failures.append)
    seen = statuses(viewer)
    state.set_candidate(object())
    viewer.open()
    state.set_project(replace(state.config, scene_count=7))
    task = pool.tasks[-1]
    task.signals.failed.emit(task.request, "渲染后端崩溃")
    assert failures == []
    assert any("已取消" in text for text in seen)
    assert viewer.busy is False


def test_opening_uses_the_request_snapshot_not_the_live_editor(state, viewer, pool, opened):
    from bproc_core.config import HdriSpec
    effective = replace(state.config, seed=789, scene_count=2, views_per_scene=3)
    state.set_candidate(object(), config=effective)
    viewer.open()
    request = pool.tasks[-1].request
    assert request.config is effective
    assert state.config.seed != request.config.seed
    pool.finish()
    assert opened[0][0] is request.config        # the snapshot that was built, not state.config
    assert "中性灰" in viewer._hdri_hint(replace(request.config, hdri=HdriSpec(files=())))
    assert "中性灰" not in viewer._hdri_hint(replace(request.config, hdri=HdriSpec(files=(Path("/x.hdr"),))))


def test_refreshing_the_candidate_reports_the_cancellation(state, viewer, pool, opened):
    seen = statuses(viewer)
    state.set_candidate(object())
    viewer.open()
    state.set_candidate(object())
    pool.finish()
    assert any("候选构图已刷新" in text for text in seen)
    assert opened == []


def test_a_second_click_while_building_is_ignored(state, viewer, pool, opened):
    state.set_candidate(object())
    assert viewer.open() is True
    assert viewer.open() is False
    assert len(pool.tasks) == 1


def test_closed_viewer_drops_finished_build_and_rejects_new_requests(state, viewer, pool, opened):
    state.set_candidate(object())
    assert viewer.open()
    viewer.close()
    pool.finish()
    assert opened == []
    assert not viewer.busy
    assert not viewer.open()
    assert len(pool.tasks) == 1
