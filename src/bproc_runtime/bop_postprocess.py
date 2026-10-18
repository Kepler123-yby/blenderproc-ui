"""BOP masks/info without invoking BlenderProc's package-installing path."""
from __future__ import annotations

import inspect
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import sys
import numpy as np

from .gpu_gl import require_bop_hardware


def _utility():
    from blenderproc.python.writer.BopWriterUtility import _BopWriterUtility
    for name in ("calc_gt_masks", "calc_gt_info"):
        parameters = inspect.signature(getattr(_BopWriterUtility, name)).parameters
        if not {"pool", "chunk_dirs", "starting_frame_id", "annotation_scale", "delta"} <= set(parameters):
            raise RuntimeError("Unsupported BlenderProc BOP helper API; expected version 2.8.0")
    return _BopWriterUtility


def initialize_bop_worker(width, height, trimesh_objects, near_m, far_m) -> None:
    os.environ["PYOPENGL_PLATFORM"] = "egl"
    utility = _utility()
    utility._pyrender_init(width, height, trimesh_objects)
    require_bop_hardware(utility)
    import pyrender
    original = pyrender.IntrinsicsCamera

    class CalibratedCamera(original):
        def __init__(self, fx, fy, cx, cy, znear=0.1, zfar=100000, **kwargs):
            super().__init__(fx=fx, fy=fy, cx=cx, cy=cy, znear=near_m, zfar=far_m, **kwargs)

    pyrender.IntrinsicsCamera = CalibratedCamera


def _initialize_report(width, height, meshes, near, far, ready):
    # fork inherits the coordinator's graceful-finalization signal deferrer.
    # Workers must remain terminable when EGL initialization times out; only the
    # parent owns Ctrl-C bookkeeping and final dataset status.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        initialize_bop_worker(width, height, meshes, near, far)
        ready.put(None)
    except BaseException as exc:
        # An initializer that raises makes multiprocessing continually respawn
        # failing workers. Report once and let the parent terminate the pool.
        ready.put(f"{type(exc).__name__}: {exc}")


def gt_delta(config, meshes) -> float:
    """Depth tolerance for GT visibility, in metres.

    BlenderProc defaults to 15 mm, which is fine for 10-30 cm objects but swallows whole
    millimetre scenes: everything within 15 mm of the model counts as the object, so occluders
    end up inside ``mask_visib``. Scale the tolerance with the largest enabled target and never
    let it fall below two depth steps.
    """
    extent = 0.0
    for target in config.targets:
        if not target.enabled or target.asset.key not in meshes:
            continue
        bounds = meshes[target.asset.key].bounds_m
        extent = max(extent, float((bounds[1] - bounds[0]).max()))
    step = float(getattr(config.output, "depth_scale_mm", .1)) / 1000
    return float(min(.015, max(2 * step, .05 * extent)))


def finalize_bop(dataset_dir: Path, config, meshes, *, near_m=1e-6, far_m=1000.0) -> None:
    import trimesh
    if config.runtime.toolkit_path is not None:
        toolkit = str(Path(config.runtime.toolkit_path).resolve())
        if toolkit not in sys.path:
            sys.path.insert(0, toolkit)
    # Fail before pool creation when the optional local toolkit is unavailable.
    from bop_toolkit_lib import inout, misc, visibility  # noqa: F401
    utility = _utility()
    chunks = sorted(str(p) for p in (Path(dataset_dir) / "train_pbr").glob("[0-9]*") if p.is_dir())
    objects = {t.obj_id: trimesh.Trimesh(vertices=meshes[t.asset.key].vertices_m,
                faces=meshes[t.asset.key].faces, process=False) for t in config.targets if t.enabled}
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    pool = context.Pool(1, initializer=_initialize_report,
        initargs=(config.camera.width, config.camera.height, objects, near_m, far_m, ready))
    try:
        try:
            error = ready.get(timeout=45)
        except queue.Empty as exc:
            raise RuntimeError("EGL BOP worker did not initialize within 45 seconds") from exc
        if error:
            raise RuntimeError("EGL BOP worker initialization failed: " + error)
        delta = gt_delta(config, meshes)
        utility.calc_gt_masks(pool=pool, chunk_dirs=chunks, starting_frame_id=0,
                              annotation_scale=1000.0, delta=delta)
        utility.calc_gt_info(pool=pool, chunk_dirs=chunks, starting_frame_id=0,
                             annotation_scale=1000.0, delta=delta)
        pool.close()
        pool.join()
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    finally:
        ready.close()
