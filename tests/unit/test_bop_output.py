from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import os
import signal
import subprocess
import tempfile
import textwrap
import numpy as np
import pytest
from PIL import Image

from bproc_core.config import AssetSpec,CameraSpec,ProjectConfig,TargetSpec,OutputSpec
from bproc_runtime.contracts import RenderedFrame
from bproc_runtime.bop_output import BopWriter,bop_translation,depth_capacity_m
import bproc_runtime.bop_output as output
import bproc_runtime.bop_postprocess as postprocess


def test_units_and_capacity():
    assert bop_translation([.1,-.2,.3])==pytest.approx([100,-200,300])
    assert depth_capacity_m(.1)==pytest.approx(6.5535)


def test_sorted_targets_single_frame_and_gt_index(tmp_path,monkeypatch):
    calls=[]
    scene=SimpleNamespace(frame_start=3,frame_end=9,frame_set=lambda frame:None)
    root=tmp_path/"out";root.mkdir()
    def native(**kwargs):
        calls.append(kwargs)
        assert (scene.frame_start,scene.frame_end)==(0,1)
        assert kwargs["target_objects"]==["two","seven"]
        assert len(kwargs["colors"])==len(kwargs["depths"])==1
        assert kwargs["calc_mask_info_coco"] is False
        chunk=root/"train_pbr/000000"
        (chunk/"rgb").mkdir(parents=True,exist_ok=True);(chunk/"depth").mkdir(exist_ok=True)
        im_id=len(calls)-1
        (chunk/"scene_gt.json").write_text(json.dumps({str(im_id):[{"obj_id":2},{"obj_id":7}]}))
        Image.fromarray(kwargs["colors"][0]).save(chunk/"rgb"/f"{im_id:06d}.jpg")
        Image.fromarray(np.ones((1,2),np.uint16)).save(chunk/"depth"/f"{im_id:06d}.png")
    api=SimpleNamespace(writer=SimpleNamespace(write_bop=native))
    backend=SimpleNamespace(api=api,bpy=SimpleNamespace(context=SimpleNamespace(scene=scene)),targets={7:"seven",2:"two"})
    config=ProjectConfig(camera=CameraSpec(width=2,height=1),output=OutputSpec(format="bop"),targets=tuple(
        TargetSpec(AssetSpec(str(i),Path(f"{i}.ply")),str(i),i) for i in (7,2)))
    monkeypatch.setattr(output,"write_bop_models",lambda *args:None)
    writer=BopWriter(root,config,{},backend)
    candidate=SimpleNamespace(camera=SimpleNamespace(clip_far_m=10))
    frame=RenderedFrame(np.zeros((1,2,3),np.uint8),np.array([[.3,float("inf")]]),None,{})
    refs=[writer.write(i,candidate,frame) for i in range(2)]
    assert [r.bop_image_id for r in refs]==[0,1]
    assert all(r.gt_obj_ids==(2,7) for r in refs)
    assert calls[0]["depths"][0].tolist()==[[.3,0]]
    bad=replace(frame,depth_m=np.array([[7.,0]]))
    with pytest.raises(ValueError,match="capacity"):writer.write(2,candidate,bad)


def test_worker_overrides_hardcoded_near_and_far(monkeypatch):
    class Camera:
        def __init__(self,**kwargs):self.parameters=kwargs
    fake=SimpleNamespace(IntrinsicsCamera=Camera)
    monkeypatch.setitem(sys.modules,"pyrender",fake)
    monkeypatch.setattr(postprocess,"_utility",lambda:SimpleNamespace(_pyrender_init=lambda *args:None))
    monkeypatch.setattr(postprocess,"require_bop_hardware",lambda utility:None)
    postprocess.initialize_bop_worker(2,1,{},.00025,3.)
    camera=fake.IntrinsicsCamera(1,1,0,0,znear=.1,zfar=100000)
    assert camera.parameters["znear"]==.00025 and camera.parameters["zfar"]==3.


def test_forked_initializer_timeout_worker_remains_terminable():
    # Run the pool in an isolated process group: the regression itself would
    # hang join() without the child-specific signal reset.
    program = textwrap.dedent('''
        import multiprocessing, queue, signal, time
        from bproc_runtime import bop_postprocess
        signal.signal(signal.SIGTERM, lambda *args: None)
        def blocked_initializer(*args):
            time.sleep(30)
        bop_postprocess.initialize_bop_worker = blocked_initializer
        context = multiprocessing.get_context('fork')
        ready = context.Queue()
        pool = context.Pool(1, initializer=bop_postprocess._initialize_report,
            initargs=(1, 1, {}, .001, 1., ready))
        try:
            ready.get(timeout=.2)
            raise AssertionError('blocked initializer unexpectedly returned')
        except queue.Empty:
            pass
        pool.terminate()
        pool.join()
        ready.close()
        print('clean shutdown', flush=True)
    ''')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2] / 'src')
    child = subprocess.Popen([sys.executable, '-c', program], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        stdout, stderr = child.communicate()
        pytest.fail('Pool worker ignored termination after initializer timeout: ' + stderr)
    assert child.returncode == 0 and 'clean shutdown' in stdout, stderr


def test_gt_delta_scales_with_the_target_so_millimetre_scenes_stay_visible():
    """A 15 mm default would make every millimetre pixel visible; the tolerance must follow size."""
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from bproc_core.assets import MeshData
    from bproc_runtime.bop_postprocess import gt_delta
    from tests.factories import make_project

    def mesh(extent):
        corners = np.array([[x, y, z] for x in (0, extent) for y in (0, extent) for z in (0, extent)])
        return MeshData("k", corners, np.zeros((0, 3), int), None, corners)

    config = make_project(Path(tempfile.mkdtemp()))
    target = config.targets[0]
    project = replace(config, targets=(target,), occluders=())
    assert gt_delta(project, {target.asset.key: mesh(.001)}) < .0005
    assert gt_delta(project, {target.asset.key: mesh(.3)}) == pytest.approx(.015)


def test_gt_delta_takes_the_smallest_target_in_a_mixed_scene():
    """One tolerance serves the whole dataset, so a large part must not vouch for a small one."""
    from dataclasses import replace
    from pathlib import Path

    import numpy as np

    from bproc_core.assets import MeshData
    from bproc_runtime.bop_postprocess import gt_delta, gt_delta_by_object
    from tests.factories import make_project

    def mesh(extent):
        corners = np.array([[x, y, z] for x in (0, extent) for y in (0, extent) for z in (0, extent)])
        return MeshData("k", corners, np.zeros((0, 3), int), None, corners)

    config = make_project(Path(tempfile.mkdtemp()))
    small, large = config.targets[0], replace(config.targets[1], asset=replace(
        config.targets[1].asset, key="large"))
    project = replace(config, targets=(small, large), occluders=())
    meshes = {small.asset.key: mesh(.001), large.asset.key: mesh(.1)}
    by_object = gt_delta_by_object(project, meshes)
    assert by_object[small.obj_id] < .0003
    assert by_object[large.obj_id] == pytest.approx(.005)
    # 5 % of the largest target (5 mm) would hide an occluder from a 1 mm part entirely.
    assert gt_delta(project, meshes) == pytest.approx(by_object[small.obj_id])


def test_finalize_delegates_to_native_helpers_with_configured_units(tmp_path, monkeypatch):
    """The adapter must call the two native helpers, never the package-installing entry point."""
    from tests.factories import make_box, make_project

    config = make_project(tmp_path / "inputs", output_format="bop")
    meshes = {target.asset.key: make_box(target.asset.key, [0, 0, 0], [.05, .05, .05])
              for target in config.targets}
    chunk = tmp_path / "dataset/train_pbr/000000"
    chunk.mkdir(parents=True)

    class FakeQueue:
        def get(self, timeout=None): return None
        def close(self): pass

    class FakePool:
        def close(self): pass
        def join(self): pass
        def terminate(self): raise AssertionError("a successful finalize must not terminate the pool")

    class FakeContext:
        def Queue(self): return FakeQueue()
        def Pool(self, processes, initializer, initargs):
            assert processes == 1 and initializer is postprocess._initialize_report
            assert initargs[:2] == (config.camera.width, config.camera.height)
            return FakePool()

    calls = []
    class Utility:
        @staticmethod
        def calc_gt_masks(**kwargs): calls.append(("masks", kwargs))
        @staticmethod
        def calc_gt_info(**kwargs): calls.append(("info", kwargs))

    import types
    toolkit = types.ModuleType("bop_toolkit_lib")
    for name in ("inout", "misc", "visibility"):
        module = types.ModuleType(f"bop_toolkit_lib.{name}")
        setattr(toolkit, name, module)
        monkeypatch.setitem(sys.modules, f"bop_toolkit_lib.{name}", module)
    monkeypatch.setitem(sys.modules, "bop_toolkit_lib", toolkit)
    monkeypatch.setattr(postprocess, "_utility", lambda: Utility)
    monkeypatch.setattr(postprocess.multiprocessing, "get_context", lambda method: FakeContext())

    postprocess.finalize_bop(tmp_path / "dataset", config, meshes, near_m=.001, far_m=5.)
    assert [name for name, _ in calls] == ["masks", "info"]
    for _, kwargs in calls:
        assert kwargs["chunk_dirs"] == [str(chunk)]
        assert kwargs["starting_frame_id"] == 0
        assert kwargs["annotation_scale"] == 1000.0
        assert kwargs["delta"] == pytest.approx(postprocess.gt_delta(config, meshes))
    assert "setup_pip" not in Path(postprocess.__file__).read_text(encoding="utf-8")
