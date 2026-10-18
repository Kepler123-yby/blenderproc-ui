from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
import os
import signal

from bproc_core.config import AssetSpec, CameraSpec, ProjectConfig, TargetSpec, OutputSpec
from bproc_core.scene import Rejection
from bproc_runtime.contracts import RenderedFrame, VisibilityEvidence
from bproc_runtime.records import FrameRef
import bproc_runtime.run as runner


def project(output_format="rgb_masks"):
    return ProjectConfig(targets=(TargetSpec(AssetSpec("a",Path("a.ply")),"a",2),),
                         anchor_key="a",camera=CameraSpec(width=2,height=1),scene_count=2,views_per_scene=1,
                         output=OutputSpec(format=output_format))


class Backend:
    def __init__(self, error=None): self.calls=[]; self.error=error; self.closed=False
    def prepare(self,*args): pass
    def apply(self,c): self.current=c; self.calls.append((c.scene_index,c.view_index,c.attempt_index))
    def assess(self,anchor):
        if self.error: raise self.error
        return VisibilityEvidence(np.ones((1,2),bool),{2:np.ones((1,2),bool)})
    def render(self,e):
        return RenderedFrame(np.zeros((1,2,3),np.uint8),None,{2:e.anchor_full},e.visible)
    def close(self): self.closed=True


class Writer:
    def __init__(self, fail=False): self.fail=fail; self.refs=[]
    def write(self,index,c,f):
        ref=FrameRef(index,f"rgb/{index:06d}.png",gt_obj_ids=(2,)); self.refs.append(ref); return ref
    def finalize(self):
        if self.fail: raise RuntimeError("postprocess failed")


class Recorder:
    def __init__(self): self.frames=[]; self.rejections=[]; self.summary=None
    def begin(self,c): pass
    def record_frame(self,ref,c,e): self.frames.append(ref)
    def record_rejection(self, scene_index, view_index, attempt_index, reason, *, phase="view"):
        self.rejections.append((scene_index, view_index, attempt_index, reason, phase))
    def finish(self,s): self.summary=s


def candidate(c,m,scene,view,attempt):
    return SimpleNamespace(scene_index=scene.scene_index,view_index=view,attempt_index=attempt,
                           wants_unoccluded=True)


def stub_scene(monkeypatch, rejected=None):
    """Every scene index maps to a usable scene state so camera proposals are reached.

    ``run_project`` now goes through ``sample_scene`` (which owns the layout retry budget), so the
    stub has to intercept that call site rather than the inner ``propose_scene``.
    """
    from bproc_core.scene import SceneState
    state = SceneState(0,(),None,1.,0.,None,True)

    def sample(config, meshes, scene_index, *, on_rejection=None):
        if rejected is not None and scene_index in rejected:
            return Rejection("missing_mesh", "gone")
        return SimpleNamespace(scene_index=scene_index, scene=state, placements=(), material=None,
                               layout_attempt_index=0)
    monkeypatch.setattr(runner, "sample_scene", sample)


def test_retry_budget_shared(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",lambda *args:Rejection("collision",""))
    backend,recorder=Backend(),Recorder()
    r=runner.run_project(project(),{},backend,Writer(),recorder)
    assert (r.status,r.saved,r.skipped,r.exit_code)==("partial",0,2,2)
    assert len(recorder.rejections)==60
    assert recorder.rejections[-1]==(1,0,29,"collision","view") and backend.closed


def test_partial_is_not_success(monkeypatch):
    def propose(c,m,scene,view,attempt): return candidate(c,m,scene,view,attempt) if scene.scene_index==0 else Rejection("collision","")
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",propose)
    recorder=Recorder(); writer=Writer()
    r=runner.run_project(project(),{},Backend(),writer,recorder)
    assert (r.status,r.saved,r.skipped,r.exit_code)==("partial",1,1,2)
    assert recorder.frames==writer.refs and len(recorder.frames)==1


def test_bop_postprocess_failure_is_not_saved(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    r=runner.run_project(project("bop"),{},Backend(),Writer(True),Recorder())
    assert (r.status,r.written_frames,r.saved,r.skipped,r.exit_code)==("failed",2,0,2,1)


@pytest.mark.parametrize("error,code",[(KeyboardInterrupt(),130),(runner.RunTerminated(),143)])
def test_interrupt_counts_unattempted_images(monkeypatch,error,code):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    b=Backend(error)
    r=runner.run_project(project(),{},b,Writer(),Recorder())
    assert (r.status,r.saved,r.skipped,r.exit_code)==("interrupted",0,2,code)
    assert b.closed


def test_unknown_target_is_fatal(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    b=Backend(); b.assess=lambda _:VisibilityEvidence(np.ones((1,2),bool),{7:np.ones((1,2),bool)})
    r=runner.run_project(project(),{},b,Writer(),Recorder())
    assert r.exit_code==1 and r.status=="failed"


def test_invisible_target_retries(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    b=Backend(); b.assess=lambda _:VisibilityEvidence(np.ones((1,2),bool),{2:np.zeros((1,2),bool)})
    r=runner.run_project(project(),{},b,Writer(),Recorder())
    assert r.failures_by_reason=={"invisible_target":60}


def test_empty_baseline_retries(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    b=Backend(); b.assess=lambda _:VisibilityEvidence(np.zeros((1,2),bool),{2:np.ones((1,2),bool)})
    r=runner.run_project(project(),{},b,Writer(),Recorder())
    assert r.failures_by_reason=={"empty_baseline":60}


def test_failed_write_is_never_committed(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    writer=Writer()
    def fail(*args):raise OSError("disk full")
    writer.write=fail
    recorder=Recorder()
    r=runner.run_project(project(),{},Backend(),writer,recorder)
    assert r.status=="failed" and r.saved==r.written_frames==0
    assert not recorder.frames


@pytest.mark.parametrize("signum",[signal.SIGINT,signal.SIGTERM])
def test_signal_during_commit_finishes_manifest_and_counters(monkeypatch,signum):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    recorder=Recorder()
    original=recorder.record_frame
    def commit_then_signal(ref,c,e):
        os.kill(os.getpid(),signum)
        original(ref,c,e)
    recorder.record_frame=commit_then_signal
    r=runner.run_project(project(),{},Backend(),Writer(),recorder)
    assert (r.status,r.saved,r.written_frames,r.skipped,r.exit_code)==("interrupted",1,1,1,128+signum)
    assert len(recorder.frames)==1


def test_blender_c_signal_handler_can_be_represented_as_none(monkeypatch):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    restored=[]
    def fake_signal(signum,handler):
        assert handler is not None
        if handler == signal.SIG_DFL:
            restored.append(signum)
        return None
    monkeypatch.setattr(runner.signal,"signal",fake_signal)
    r=runner.run_project(project(),{},Backend(),Writer(),Recorder())
    assert r.status=="completed" and set(restored)=={signal.SIGINT,signal.SIGTERM}


@pytest.mark.parametrize("signum",[signal.SIGINT,signal.SIGTERM])
def test_native_pass_finishes_before_signal_is_honored(monkeypatch,signum):
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",candidate)
    backend=Backend(); finished=[]
    original=backend.assess
    def native_assess(anchor):
        os.kill(os.getpid(),signum)
        finished.append(True)
        return original(anchor)
    backend.assess=native_assess
    r=runner.run_project(project(),{},backend,Writer(),Recorder())
    assert finished==[True] and r.status=="interrupted" and r.saved==0 and r.exit_code==128+signum


def test_missing_occluders_accepts_every_target_visibility(monkeypatch):
    def propose(c,m,scene,view,attempt): return SimpleNamespace(scene_index=scene.scene_index,view_index=view,attempt_index=attempt,wants_unoccluded=False)
    stub_scene(monkeypatch)
    monkeypatch.setattr(runner,"propose_candidate",propose)
    recorder=Recorder()
    run=runner.run_project(project(),{},Backend(),Writer(),recorder)
    assert (run.status,run.saved,run.skipped)==("completed",2,0)
    assert recorder.rejections==[]
