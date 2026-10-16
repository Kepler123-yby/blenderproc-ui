from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import os
import signal
import subprocess
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
