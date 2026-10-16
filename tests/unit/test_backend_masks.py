from dataclasses import replace
from types import SimpleNamespace
import tempfile
import numpy as np
import pytest

from bproc_core.config import CameraSpec, ProjectConfig
from bproc_core.masks import decode_instances, occlusion_ratio
from bproc_runtime.blender_backend import BlenderBackend
from bproc_runtime.contracts import RenderError, VisibilityEvidence


def test_decode_ids_are_not_segmentation_values():
    masks = decode_instances(np.array([[0,3,8]]), [
        {"idx":3,"ui_target_id":2}, {"idx":8,"ui_target_id":7}], [2,7,9])
    assert masks[2].tolist() == [[False,True,False]]
    assert masks[7].tolist() == [[False,False,True]]
    assert not masks[9].any()


class Object:
    def __init__(self, hidden=False): self.hidden = hidden
    def hide(self, hidden): self.hidden = hidden
    def is_hidden(self): return self.hidden


def make_backend(fail_second=False):
    backend = BlenderBackend()
    backend.targets = {2: Object(), 7: Object()}
    backend.objects = [*backend.targets.values(), Object(True)]
    scene = SimpleNamespace(frame_start=5, frame_end=10, frame_set=lambda index: None)
    backend.bpy = SimpleNamespace(context=SimpleNamespace(scene=scene))
    calls = []
    def segmentation(**kwargs):
        calls.append(tuple(obj.hidden for obj in backend.objects))
        if fail_second and len(calls) == 2:
            raise RuntimeError("fixture failure")
        return {"instance_segmaps": [np.array([[1,2]])], "instance_attribute_maps": [[
            {"idx":1,"ui_target_id":2}, {"idx":2,"ui_target_id":7}]]}
    backend.api = SimpleNamespace(renderer=SimpleNamespace(render_segmap=segmentation))
    backend._temporary = tempfile.TemporaryDirectory()
    return backend, calls


def test_isolation_restores_on_failure():
    backend, calls = make_backend(True)
    before = [obj.hidden for obj in backend.objects]
    with pytest.raises(RenderError, match="fixture failure"):
        backend.assess(2)
    assert [obj.hidden for obj in backend.objects] == before
    assert calls == [(False,False,True), (False,True,True)]
    assert (backend.bpy.context.scene.frame_start, backend.bpy.context.scene.frame_end) == (0,1)
    backend.close()


def test_successful_isolation_and_single_frame():
    backend, calls = make_backend()
    evidence = backend.assess(2)
    assert evidence.anchor_full.tolist() == [[True,False]]
    assert [o.hidden for o in backend.objects] == [False,False,True]
    backend.close()


def test_occlusion_compares_isolated_silhouette():
    e = VisibilityEvidence(np.ones((1,4),bool), {2:np.array([[1,1,1,0]],bool)})
    assert occlusion_ratio(e, 2) == .25
    with pytest.raises(ValueError, match="empty"):
        occlusion_ratio(VisibilityEvidence(np.zeros((1,4),bool), e.visible),2)
