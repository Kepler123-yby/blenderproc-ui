from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from PIL import Image
import pytest

from bproc_core.config import AssetSpec, CameraSpec, ProjectConfig, TargetSpec
from bproc_core.masks import decode_instances
from bproc_runtime.contracts import RenderedFrame
from bproc_runtime.output import reserve_dataset,OutputConflict
from bproc_runtime.rgb_masks import RgbMasksWriter


def png_header(path):
    data = Path(path).read_bytes()[:33]
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", path
    return data[24], data[25]


def test_per_object_masks_and_ids(tmp_path):
    config=ProjectConfig(camera=CameraSpec(width=3,height=2),targets=tuple(
        TargetSpec(AssetSpec(str(i),Path(f"{i}.ply")),str(i),i) for i in (7,2)))
    writer=RgbMasksWriter(reserve_dataset(tmp_path,"new"),config)
    full={i:np.ones((2,3),bool) for i in (2,7)}
    visible={i:np.array([[1,1,0],[0,0,0]],bool) for i in (2,7)}
    f=RenderedFrame(np.zeros((2,3,3),np.uint8),None,full,visible)
    ref=writer.write(0,None,f)
    assert ref.gt_obj_ids==(2,7)
    assert sorted(p.name for p in (writer.root/"mask").iterdir())==["000000_000002.png","000000_000007.png"]
    for kind in ("mask","mask_visib"):
        image=np.asarray(Image.open(writer.root/kind/"000000_000007.png"))
        assert image.dtype==np.uint8 and set(np.unique(image))<={0,255}
        assert png_header(writer.root/kind/"000000_000007.png")==(8,0)
    assert not list(writer.root.glob(".frame-*"))
    with pytest.raises(OutputConflict):writer.write(0,None,f)


def test_native_uint32_decoding_writes_obj_id_suffixes_as_8bit_png(tmp_path):
    config=ProjectConfig(camera=CameraSpec(width=3,height=2),targets=tuple(
        TargetSpec(AssetSpec(str(i),Path(f"{i}.ply")),str(i),i) for i in (7,2)))
    writer=RgbMasksWriter(reserve_dataset(tmp_path,"new"),config)
    segmap=np.array([[65536,7,0],[4294967295,16777217,0]],dtype=np.uint32)
    masks=decode_instances(segmap,[
        {"idx":65536,"ui_target_id":2},{"idx":7,"ui_target_id":7},
        {"idx":4294967295,"ui_target_id":2},{"idx":16777217,"ui_target_id":7}],
        [2,7,55])
    assert not masks[55].any()
    visible={2:masks[2].copy(),7:masks[7].copy()}
    visible[7][0,1]=False
    ref=writer.write(0,None,RenderedFrame(np.zeros((2,3,3),np.uint8),None,
                                          {2:masks[2],7:masks[7]},visible))
    assert ref.gt_obj_ids==(2,7)
    assert sorted(p.name for p in (writer.root/"mask").iterdir())==[
        "000000_000002.png","000000_000007.png"]
    expected={2:[[255,0,0],[255,0,0]],7:[[0,255,0],[0,255,0]]}
    expected_visible={2:[[255,0,0],[255,0,0]],7:[[0,0,0],[0,255,0]]}
    for obj_id in (2,7):
        for kind,expected_pixels in (("mask",expected),("mask_visib",expected_visible)):
            path=writer.root/kind/f"000000_{obj_id:06d}.png"
            with Image.open(path) as image:
                assert image.format=="PNG" and image.mode=="L"
                pixels=np.asarray(image)
            assert png_header(path)==(8,0)
            assert pixels.dtype==np.uint8 and pixels.shape==(2,3)
            assert set(np.unique(pixels))<={0,255}
            assert pixels.tolist()==expected_pixels[obj_id]
    full=(np.asarray(Image.open(writer.root/"mask/000000_000007.png"))>0)
    visible_pixels=(np.asarray(Image.open(writer.root/"mask_visib/000000_000007.png"))>0)
    assert not np.any(visible_pixels & ~full)


@pytest.mark.parametrize("case,message",[
    ("float","boolean"),
    ("missing","exactly every"),
    ("shape","boolean images"),
    ("superset","subset"),
])
def test_writer_rejects_wrong_shape_dtype_and_visible_outside_full(tmp_path,case,message):
    config=ProjectConfig(camera=CameraSpec(width=3,height=2),targets=tuple(
        TargetSpec(AssetSpec(str(i),Path(f"{i}.ply")),str(i),i) for i in (2,7)))
    writer=RgbMasksWriter(reserve_dataset(tmp_path,"new"),config)
    good={obj_id:np.ones((2,3),bool) for obj_id in (2,7)}
    full,visible=good,good
    if case=="float":
        full={2:good[2].astype(float),7:good[7]}
    elif case=="missing":
        full={2:good[2]}
    elif case=="shape":
        full={2:good[2][:1],7:good[7]}
    else:
        full={2:np.zeros((2,3),bool),7:good[7]}
    with pytest.raises(ValueError,match=message):
        writer.write(0,None,RenderedFrame(np.zeros((2,3,3),np.uint8),None,full,visible))


@pytest.mark.parametrize("kind",["empty","file","symlink","dangling"])
def test_existing_output_never_cleared(tmp_path,kind):
    path=tmp_path/"data"
    if kind=="empty":path.mkdir()
    elif kind=="file":path.write_text("user")
    else:path.symlink_to(tmp_path/("missing" if kind=="dangling" else "target"))
    with pytest.raises(OutputConflict):reserve_dataset(tmp_path,"data")
    assert path.exists() or path.is_symlink()


@pytest.mark.parametrize("name",["", ".", "..", "../outside", "a/b", "a\\b"])
def test_invalid_component(tmp_path,name):
    with pytest.raises(ValueError):reserve_dataset(tmp_path,name)
