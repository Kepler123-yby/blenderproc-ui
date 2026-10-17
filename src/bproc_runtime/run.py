"""One bounded retry budget per requested dataset image."""
from __future__ import annotations

from collections import Counter
import signal
import threading
from typing import Protocol
import numpy as np

from bproc_core.masks import occlusion_ratio
from bproc_core.scene import propose_candidate, propose_scene, Rejection
from .records import FrameRef, RunSummary


class FrameWriter(Protocol):
    def write(self, index, candidate, frame) -> FrameRef: ...
    def finalize(self) -> None: ...


class RunTerminated(BaseException):
    pass


def run_project(config, meshes, backend, writer: FrameWriter, recorder) -> RunSummary:
    written = saved = 0
    failures = Counter()
    interrupted = fatal = False
    exit_code = 0
    began = False
    old_handlers = {}
    committing = False
    rendering = False
    finalizing = False
    pending_signal = None
    expected_ids = {t.obj_id for t in config.targets if t.enabled}
    anchor_id = next(t.obj_id for t in config.targets if t.asset.key == config.anchor_key)

    def reject(scene_index, view_index, attempt, reason):
        failures[reason] += 1
        recorder.record_rejection(scene_index, view_index, attempt, reason)

    def interrupt(signum, frame):
        nonlocal pending_signal
        if committing or rendering or finalizing:
            # Finish the image/manifest/counter transaction before honoring a
            # signal. Repeated Ctrl-C must not tear down BOP finalization or
            # replace an honest interrupted status with a stale running file.
            if pending_signal is None:
                pending_signal = signum
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt()
        raise RunTerminated()

    def native_call(method, *arguments):
        nonlocal rendering
        # Raising a Python exception while bpy's C renderer/compositor holds a
        # lock can deadlock Blender shutdown. Finish the current native pass and
        # honor cancellation as soon as it returns to our Python boundary.
        rendering = True
        try:
            result = method(*arguments)
        finally:
            rendering = False
        if pending_signal is not None:
            interrupt(pending_signal, None)
        return result

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, interrupt)
    try:
        recorder.begin(config)
        began = True
        native_call(backend.prepare, config, meshes)
        for scene_index in range(config.scene_count):
            scene = propose_scene(config, meshes, scene_index)
            if isinstance(scene, Rejection):
                reject(scene_index, 0, 0, scene.reason)
                continue
            for view_index in range(config.views):
                for attempt in range(config.occlusion.max_attempts):
                    candidate = propose_candidate(config, meshes, scene, view_index, attempt)
                    if isinstance(candidate, Rejection):
                        reject(scene_index, view_index, attempt, candidate.reason)
                        continue
                    native_call(backend.apply, candidate)
                    evidence = native_call(backend.assess, anchor_id)
                    if set(evidence.visible) != expected_ids:
                        raise ValueError("Backend mask IDs do not match enabled targets")
                    shape = (config.camera.height, config.camera.width)
                    if evidence.anchor_full.shape != shape or any(m.shape != shape for m in evidence.visible.values()):
                        raise ValueError("Backend masks have incorrect image dimensions")
                    if evidence.anchor_full.dtype != bool or any(m.dtype != bool for m in evidence.visible.values()):
                        raise ValueError("Backend masks must contain boolean pixels")
                    if not np.any(evidence.anchor_full):
                        reject(scene_index, view_index, attempt, "empty_baseline")
                        continue
                    if any(not np.any(mask) for mask in evidence.visible.values()):
                        reject(scene_index, view_index, attempt, "invisible_target")
                        continue
                    ratio = occlusion_ratio(evidence, anchor_id)
                    # Without occluders nothing can cover the target, so a ratio above zero is unreachable.
                    accepted = (ratio == 0.0 if candidate.wants_unoccluded or not config.occluders else
                                config.occlusion.ratio.minimum <= ratio <= config.occlusion.ratio.maximum)
                    if not accepted:
                        reject(scene_index, view_index, attempt, "occlusion")
                        continue
                    frame = native_call(backend.render, evidence)
                    committing = True
                    try:
                        ref = writer.write(written, candidate, frame)
                        written += 1
                        recorder.record_frame(ref, candidate, evidence)
                        if config.output.format != "bop":
                            saved = written
                    finally:
                        committing = False
                    if pending_signal is not None:
                        interrupt(pending_signal, None)
                    break
    except KeyboardInterrupt:
        interrupted, exit_code = True, 130
        failures["sigint"] += 1
    except RunTerminated:
        interrupted, exit_code = True, 143
        failures["sigterm"] += 1
    except Exception as exc:
        fatal, exit_code = True, 1
        failures["fatal:" + type(exc).__name__] += 1
        # A readable terminal diagnostic accompanies the machine-readable count.
        print(f"Render failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        finalizing = True
        try:
            writer.finalize()
            if config.output.format == "bop":
                saved = written
        except (Exception, KeyboardInterrupt) as exc:
            fatal, exit_code = True, 1
            failures["finalize:" + type(exc).__name__] += 1
            if config.output.format == "bop":
                saved = 0
            print(f"Output finalization failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            backend.close()
        except Exception as exc:
            fatal, exit_code = True, 1
            failures["close:" + type(exc).__name__] += 1
    if pending_signal is not None and not fatal:
        interrupted = True
        exit_code = 128 + pending_signal
        key = "sigint" if pending_signal == signal.SIGINT else "sigterm"
        if not failures[key]:
            failures[key] += 1
    if fatal:
        status = "failed"
    elif interrupted:
        status = "interrupted"
    elif saved == config.total_shots:
        status = "completed"
    else:
        status, exit_code = "partial", 2
    summary = RunSummary(status, config.total_shots, written, saved,
                         config.total_shots - saved, dict(failures), exit_code)
    try:
        if began:
            recorder.finish(summary)
    finally:
        for signum, previous in old_handlers.items():
            # Blender may have installed a C handler which Python represents as
            # None. It cannot be restored with signal.signal; use the OS default
            # as this standalone render process is about to exit.
            signal.signal(signum, signal.SIG_DFL if previous is None else previous)
    return summary
