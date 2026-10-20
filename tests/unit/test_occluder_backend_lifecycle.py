"""Backend visibility, pool sizing and failure cleanup for occluder candidates.

These use the real ``BlenderBackend.apply/_isolate/_segmentation`` code with fake native objects,
so no Blender process is started. The contracts locked here are the ones that decide the rendered
picture: exactly the candidate's placements are visible, a physical asset appears once per frame,
an exception leaves nothing visible, and isolation returns to the state it entered.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from bproc_core.config import (AssetSpec, CameraSpec, HdriSpec, OcclusionSpec, ProjectConfig,
                               Range, TargetSpec)
from bproc_core.scene import Placement
from bproc_runtime.blender_backend import BlenderBackend, occluder_pool_capacity


class FakeObject:
    def __init__(self, name, hidden=False):
        self.name, self.hidden = name, hidden
        self.matrices = []
        self.attributes = {}

    def hide(self, hidden):
        self.hidden = hidden

    def is_hidden(self):
        return self.hidden

    def set_local2world_mat(self, matrix):
        self.matrices.append(np.asarray(matrix, dtype=float))

    def set_cp(self, key, value):
        self.attributes[key] = value

    def get_materials(self):
        return [SimpleNamespace(name=self.name + '_mat')]


class FakeLight:
    def __init__(self):
        self.location = None
        self.energy = None
        self.color = None

    def set_location(self, value):
        self.location = value

    def set_energy(self, value):
        self.energy = value

    def set_color(self, value):
        self.color = value


def make_config(strategy='physical_view', count=(1, 2), occluders=('o', 'p'), ratio=(.1, .5)):
    return ProjectConfig(
        targets=(TargetSpec(AssetSpec('a', __import__('pathlib').Path('a.ply')), 'A', 2),),
        anchor_key='a', hdri=HdriSpec(files=(__import__('pathlib').Path('env.hdr'),)),
        occluders=tuple(AssetSpec(key, __import__('pathlib').Path(key + '.ply'), 'mm') for key in occluders),
        occlusion=OcclusionSpec(none_fraction=0., ratio=Range(*ratio), count=count,
                                strategy=strategy, physical_scale=Range(1., 1.)),
        camera=CameraSpec(width=64, height=48))


def make_backend(config=None, *, pools=('o', 'p')):
    config = config or make_config()
    backend = BlenderBackend()
    backend.config = config
    backend.targets = {2: FakeObject('target-2')}
    backend._pools = {key: [FakeObject(f'{key}-{index}') for index in
                            range(occluder_pool_capacity(config))] for key in pools}
    backend.objects = [*backend.targets.values(), *(obj for pool in backend._pools.values() for obj in pool)]
    backend.light = FakeLight()
    backend.api = SimpleNamespace(camera=SimpleNamespace(
        set_intrinsics_from_K_matrix=lambda *args, **kwargs: None,
        add_camera_pose=lambda *args, **kwargs: None))
    backend.bpy = SimpleNamespace(context=SimpleNamespace(
        scene=SimpleNamespace(frame_start=0, frame_end=0, frame_set=lambda index: None)))
    backend._apply_material = lambda candidate: None
    backend._apply_world = lambda candidate: None
    return backend


def candidate(*placements, wants_unoccluded=False):
    return SimpleNamespace(
        placements=tuple(placements), wants_unoccluded=wants_unoccluded,
        camera=SimpleNamespace(clip_near_m=.01, clip_far_m=100., world_from_camera=np.eye(4)),
        light=SimpleNamespace(position_m=np.zeros(3), energy_w=1., color_rgb=(1., 1., 1.)),
        hdri_path=None, hdri_strength=1., hdri_rotation_deg=0., material=None)


def placement(key, role='occluder', obj_id=None):
    return Placement(key, None, obj_id, np.eye(4) * 2., role)


def visible(backend):
    return {obj.name for obj in backend.objects if not obj.hidden}


def test_pool_capacity_follows_the_strategy():
    assert occluder_pool_capacity(make_config('physical_view', count=(1, 4))) == 1
    assert occluder_pool_capacity(make_config('aimed_view', count=(1, 4))) == 4
    # A missing strategy field (old programmatic config) keeps the historical capacity.
    legacy = make_config()
    legacy = legacy.__class__(**{**legacy.__dict__,
                                 'occlusion': OcclusionSpec(none_fraction=0., count=(1, 3))})
    assert occluder_pool_capacity(legacy) == 3


def test_apply_shows_exactly_the_candidate_and_never_accumulates():
    backend = make_backend(make_config(count=(1, 2)))
    backend.apply(candidate(placement('a', 'target', 2), placement('o')))
    assert visible(backend) == {'target-2', 'o-0'}
    backend.apply(candidate(placement('a', 'target', 2), placement('p')))
    assert visible(backend) == {'target-2', 'p-0'}
    backend.apply(candidate(placement('a', 'target', 2)))
    assert visible(backend) == {'target-2'}
    backend.apply(candidate(placement('a', 'target', 2), placement('o'), placement('p')))
    assert visible(backend) == {'target-2', 'o-0', 'p-0'}
    # The matrix is applied once per visible object and equals the placement's matrix.
    assert len(backend._pools['o'][0].matrices) == 2
    np.testing.assert_array_equal(backend._pools['o'][0].matrices[-1], np.eye(4) * 2.)
    backend.apply(candidate(placement('a', 'target', 2)))
    assert len(backend._pools['o'][0].matrices) == 2, 'hidden objects must not be moved'


def test_aimed_view_reuses_a_pool_for_repeated_assets():
    backend = make_backend(make_config('aimed_view', count=(2, 2)))
    backend.apply(candidate(placement('a', 'target', 2), placement('o'), placement('o')))
    assert visible(backend) == {'target-2', 'o-0', 'o-1'}
    backend.apply(candidate(placement('a', 'target', 2), placement('o')))
    assert visible(backend) == {'target-2', 'o-0'}


def test_physical_duplicate_and_unknown_assets_hide_everything():
    backend = make_backend(make_config(count=(1, 2)))
    backend.apply(candidate(placement('a', 'target', 2), placement('o')))
    with pytest.raises(ValueError, match='unique'):
        backend.apply(candidate(placement('a', 'target', 2), placement('o'), placement('o')))
    assert visible(backend) == set() and backend.candidate is None
    backend.apply(candidate(placement('a', 'target', 2), placement('o')))
    with pytest.raises(ValueError, match='unknown occluder'):
        backend.apply(candidate(placement('a', 'target', 2), placement('removed')))
    assert visible(backend) == set() and backend.candidate is None
    with pytest.raises(ValueError, match='unknown target'):
        backend.apply(candidate(placement('a', 'target', 99)))
    assert visible(backend) == set()


def test_apply_failure_during_material_hides_everything():
    backend = make_backend(make_config(count=(1, 2)))
    def broken(candidate):
        raise RuntimeError('fixture material failure')
    backend._apply_material = broken
    with pytest.raises(RuntimeError, match='fixture material failure'):
        backend.apply(candidate(placement('a', 'target', 2), placement('o')))
    assert visible(backend) == set() and backend.candidate is None


def test_isolation_restores_the_visible_candidate_not_just_hidden_objects():
    backend = make_backend(make_config(count=(1, 2)))
    backend.apply(candidate(placement('a', 'target', 2), placement('o'), placement('p')))
    before = [(obj.name, obj.hidden) for obj in backend.objects]
    backend._segmentation = lambda: {2: np.ones((2, 2), dtype=bool)}
    evidence = backend.assess(2)
    assert evidence.anchor_full.all()
    assert [(obj.name, obj.hidden) for obj in backend.objects] == before


def test_mask_ids_only_hold_targets_and_never_occluders():
    backend = make_backend(make_config())
    segmap = np.array([[3, 5], [3, 0]])
    attributes = [{'idx': 3, 'ui_target_id': 2}, {'idx': 5, 'ui_target_id': 0}]
    backend.api.renderer = SimpleNamespace(render_segmap=lambda **kwargs: {
        'instance_segmaps': [segmap], 'instance_attribute_maps': [attributes]})
    backend._temporary = __import__('tempfile').TemporaryDirectory()
    masks = backend._segmentation()
    assert set(masks) == {2}
    assert masks[2].tolist() == [[True, False], [True, False]]
    backend._temporary.cleanup()
