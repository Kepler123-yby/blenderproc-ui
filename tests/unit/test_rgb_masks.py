from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from PIL import Image
import pytest

from bproc_core.config import AssetSpec, CameraSpec, ProjectConfig, TargetSpec
from bproc_runtime.contracts import RenderedFrame
from bproc_runtime.output import reserve_dataset,OutputConflict
from bproc_runtime.rgb_masks import RgbMasksWriter


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
    assert not list(writer.root.glob(".frame-*"))
    with pytest.raises(OutputConflict):writer.write(0,None,f)


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
