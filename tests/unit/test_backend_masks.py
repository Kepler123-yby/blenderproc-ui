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


UINT32_BOUNDARIES = np.array([
    [65535, 65536, 16777216],
    [16777217, 4294967294, 4294967295],
], dtype=np.uint32)


def test_decode_uint32_boundaries_keep_each_distinct_integer_id():
    attributes = [
        {"idx": 65535, "ui_target_id": 101},
        {"idx": 65536, "ui_target_id": 7},
        {"idx": 16777216, "ui_target_id": 300},
        {"idx": 16777217, "ui_target_id": 4},
        {"idx": 4294967294, "ui_target_id": 999},
        {"idx": 4294967295, "ui_target_id": 12},
    ]
    masks = decode_instances(UINT32_BOUNDARIES, attributes, [101, 7, 300, 4, 999, 12, 55])
    expected = {
        101: [[True, False, False], [False, False, False]],
        7: [[False, True, False], [False, False, False]],
        300: [[False, False, True], [False, False, False]],
        4: [[False, False, False], [True, False, False]],
        999: [[False, False, False], [False, True, False]],
        12: [[False, False, False], [False, False, True]],
        55: [[False, False, False], [False, False, False]],
    }
    for obj_id, pixels in expected.items():
        assert masks[obj_id].dtype == bool
        assert masks[obj_id].tolist() == pixels


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.uint32])
def test_decode_accepts_each_native_segmap_dtype(dtype):
    segmap = np.array([[0, 3, 8], [8, 3, 0]], dtype=dtype)
    masks = decode_instances(segmap, [
        {"idx": 3, "ui_target_id": 2}, {"idx": 8, "ui_target_id": 9}], [2, 9])
    assert masks[2].tolist() == [[False, True, False], [False, True, False]]
    assert masks[9].tolist() == [[False, False, True], [True, False, False]]
    assert masks[2].dtype == masks[9].dtype == bool


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.uint32])
def test_backend_accepts_native_dtypes_and_ignores_background_and_unknown_objects(dtype):
    segmap = np.array([[0, 3, 8, 9, 10]], dtype=dtype)
    attributes = [
        {"idx": 0, "ui_target_id": 0},
        {"idx": 3, "ui_target_id": 2},
        {"idx": 8, "cp_ui_target_id": 7},
        {"idx": 9, "ui_target_id": 0},
        {"idx": 10, "ui_target_id": 999},
    ]
    backend, _ = make_backend(segmap=segmap, attributes=attributes)
    try:
        masks = backend._segmentation()
        assert masks[2].tolist() == [[False, True, False, False, False]]
        assert masks[7].tolist() == [[False, False, True, False, False]]
        assert set(masks) == {2, 7}
    finally:
        backend.close()


@pytest.mark.parametrize("shape", [(3,), (2, 3, 1)])
def test_decode_rejects_non_image_segmap_dimensions(shape):
    with pytest.raises(ValueError, match="two-dimensional"):
        decode_instances(np.zeros(shape, dtype=np.uint32), [], [2])


def test_decode_merges_repeated_mappings_idempotently_and_order_independently():
    segmap = np.array([[0, 3], [8, 3]], dtype=np.uint16)
    attributes = [
        {"idx": 3, "ui_target_id": 2},
        {"idx": 8, "ui_target_id": 2},
        {"idx": 3, "ui_target_id": 2},
        {"idx": 8, "ui_target_id": 7},
    ]
    first = decode_instances(segmap, attributes, [2, 7])
    second = decode_instances(segmap, list(reversed(attributes)), [2, 7])
    assert first[2].tolist() == [[False, True], [True, True]]
    assert first[7].tolist() == [[False, False], [True, False]]
    assert np.array_equal(first[2], second[2]) and np.array_equal(first[7], second[7])


def test_backend_passes_native_uint32_ids_through_the_real_decoder():
    attributes = [
        {"idx": 65535, "ui_target_id": 2},
        {"idx": 65536, "ui_target_id": 7},
        {"idx": 16777216, "ui_target_id": 2},
        {"idx": 16777217, "ui_target_id": 7},
        {"idx": 4294967294, "ui_target_id": 2},
        {"idx": 4294967295, "ui_target_id": 7},
    ]
    backend, _ = make_backend(segmap=UINT32_BOUNDARIES, attributes=attributes)
    masks = backend._segmentation()
    assert masks[2].tolist() == [[True, False, True], [False, True, False]]
    assert masks[7].tolist() == [[False, True, False], [True, False, True]]
    assert masks[2].dtype == masks[7].dtype == bool
    kwargs = backend.segmentation_kwargs[0]
    assert kwargs["map_by"] == ["instance", "cp_ui_target_id"]
    assert kwargs["default_values"] == {"cp_ui_target_id": 0}
    backend.close()


class Object:
    def __init__(self, hidden=False): self.hidden = hidden
    def hide(self, hidden): self.hidden = hidden
    def is_hidden(self): return self.hidden


def make_backend(fail_second=False, segmap=None, attributes=None):
    backend = BlenderBackend()
    backend.targets = {2: Object(), 7: Object()}
    backend.objects = [*backend.targets.values(), Object(True)]
    scene = SimpleNamespace(frame_start=5, frame_end=10, frame_set=lambda index: None)
    backend.bpy = SimpleNamespace(context=SimpleNamespace(scene=scene))
    calls = []
    backend.segmentation_kwargs = []
    def segmentation(**kwargs):
        calls.append(tuple(obj.hidden for obj in backend.objects))
        backend.segmentation_kwargs.append(kwargs)
        if fail_second and len(calls) == 2:
            raise RuntimeError("fixture failure")
        return {"instance_segmaps": [np.array([[1, 2]]) if segmap is None else segmap],
                "instance_attribute_maps": [[
                    {"idx": 1, "ui_target_id": 2}, {"idx": 2, "ui_target_id": 7}]
                    if attributes is None else attributes]}
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
