"""On-demand geometry candidates, using the unmodified production configuration.

Budgets are checked between sampler operations, not by interrupting geometry work.
No scene/view grid is generated or cached here.
"""
from time import monotonic

from .scene import Rejection, propose_candidate, sample_scene


def candidate_for_preview(config, meshes, scene_index, view_index, *, max_attempts=30,
                          deadline_s=1.5, cancelled=None):
    if not 0 <= scene_index < config.scene_count:
        return Rejection("invalid_index", "场景编号超出范围")
    if not 0 <= view_index < config.views:
        return Rejection("invalid_index", "视角编号超出范围")
    started = monotonic()

    def stopped():
        if cancelled is not None and cancelled():
            return Rejection("cancelled", "候选查询已取消")
        if monotonic() - started >= deadline_s:
            return Rejection("timeout", "候选查询超时")
        return None

    class StopQuery(Exception):
        pass

    def layout_rejected(*_):
        if stopped() is not None:
            raise StopQuery

    stop = stopped()
    if stop is not None:
        return stop
    try:
        scene = sample_scene(config, meshes, scene_index, on_rejection=layout_rejected)
    except StopQuery:
        return stopped()
    stop = stopped()
    if stop is not None:
        return stop
    if isinstance(scene, Rejection):
        return scene
    result = Rejection("attempts", "候选查询次数已耗尽")
    for attempt in range(min(max_attempts, config.occlusion.max_attempts)):
        stop = stopped()
        if stop is not None:
            return stop
        result = propose_candidate(config, meshes, scene, view_index, attempt)
        stop = stopped()
        if stop is not None:
            return stop
        if not isinstance(result, Rejection):
            return result
    return result
