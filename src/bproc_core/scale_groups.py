"""Split a project into datasets that each describe one target-size class.

A dataset carries one BOP GT depth tolerance and one occlusion-ratio window. When enabled
targets differ by more than :data:`MIXED_SCALE_FACTOR`, those format-level values cannot serve
both the smallest and the largest target, so the honest output is several datasets. The split
keeps the physical scene unchanged: cameras, lighting and depth encoding stay as configured, and
a group that does not contain the original render centre gets a new centre with the other target
offsets shifted by that centre's nominal offset. Callers that want a closer framing per group can
edit the generated projects afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping
import numpy as np

from .assets import MeshData
from .config import Box, ConfigError, ProjectConfig, Range, TargetSpec

#: Target size ratio above which one dataset cannot honestly describe every object.
MIXED_SCALE_FACTOR = 10.0


@dataclass(frozen=True)
class ScaleGroup:
    """One target-size class plus the project that renders only its targets."""

    index: int
    targets: tuple[TargetSpec, ...]
    extents_m: tuple[float, ...]
    minimum_m: float
    maximum_m: float
    config: ProjectConfig

    @property
    def ratio(self) -> float:
        return self.maximum_m / self.minimum_m if self.minimum_m > 0 else 1.0

    @property
    def label(self) -> str:
        """Short, filesystem-friendly size label (millimetres unless the part is smaller)."""
        def value(metres: float) -> str:
            millimetres = metres * 1000
            return f"{millimetres:g}" if millimetres >= 0.01 else f"{metres * 1_000_000:g}um"
        if np.isclose(self.minimum_m, self.maximum_m):
            return value(self.maximum_m) + "mm"
        return f"{value(self.minimum_m)}-{value(self.maximum_m)}mm"


def _missing_mesh_keys(config: ProjectConfig, meshes: Mapping[str, MeshData]) -> list[str]:
    """Enabled targets whose mesh was never loaded; grouping must not drop them."""
    missing = []
    for target in config.targets:
        if target.enabled and target.asset.key not in meshes:
            missing.append(target.asset.key)
    return missing


def _require_complete_meshes(config: ProjectConfig, meshes: Mapping[str, MeshData]) -> None:
    """Refuse to group a project whose enabled targets are not all loaded.

    Dropping an unloaded target would export a dataset that silently lacks an object the user
    enabled, and the generated ``scene.yaml`` could not reveal the loss any more.
    """
    missing = _missing_mesh_keys(config, meshes)
    if missing:
        raise ConfigError(
            "按尺寸分组导出缺少启用目标的网格：" + "、".join(missing)
            + "。请等待资源载入完成或修正资源后重试。")


def _require_exact_partition(config: ProjectConfig, groups) -> None:
    """Every enabled target appears in exactly one group, with its own obj_id."""
    enabled = [target for target in config.targets if target.enabled]
    seen_keys: list[str] = []
    seen_ids: list[int] = []
    for group in groups:
        for target in group.config.targets:
            if not target.enabled:
                continue
            seen_keys.append(target.asset.key)
            seen_ids.append(target.obj_id)
    if len(seen_keys) != len(set(seen_keys)):
        raise ConfigError("按尺寸分组结果出现重复目标：" + "、".join(sorted(seen_keys)))
    if len(seen_ids) != len(set(seen_ids)):
        raise ConfigError("按尺寸分组结果出现重复 obj_id：" + "、".join(str(x) for x in sorted(seen_ids)))
    if set(seen_keys) != {target.asset.key for target in enabled}:
        lost = sorted({target.asset.key for target in enabled} - set(seen_keys))
        extra = sorted(set(seen_keys) - {target.asset.key for target in enabled})
        raise ConfigError("按尺寸分组的启用目标并集不完整：缺少 " + "、".join(lost)
                          + ("；多出 " + "、".join(extra) if extra else ""))
    if set(seen_ids) != {target.obj_id for target in enabled}:
        raise ConfigError("按尺寸分组的 obj_id 并集与启用目标不一致")


def target_extents(config: ProjectConfig, meshes: Mapping[str, MeshData]) -> list[float]:
    """Largest bounding-box side per enabled target, in metres."""
    extents = []
    for target in config.targets:
        if not target.enabled or target.asset.key not in meshes:
            continue
        bounds = np.asarray(meshes[target.asset.key].bounds_m, dtype=float)
        extents.append(float((bounds[1] - bounds[0]).max()))
    return extents


def _clusters(entries: list[tuple[TargetSpec, float]], factor: float) -> list[list[tuple[TargetSpec, float]]]:
    ordered = sorted(entries, key=lambda item: (item[1], item[0].obj_id, item[0].asset.key))
    groups: list[list[tuple[TargetSpec, float]]] = []
    for item in ordered:
        if groups and item[1] / groups[-1][0][1] > factor:
            groups.append([item])
        elif groups:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _midpoint(box: Box) -> np.ndarray:
    # Python floats, not ``np.float64``: generated projects are written as plain YAML and a numpy
    # scalar is not representable by the safe dumper.
    return np.array([(axis.minimum + axis.maximum) / 2 for axis in (box.x, box.y, box.z)],
                    dtype=float)


def _shifted(box: Box, shift: np.ndarray) -> Box:
    ranges = [Range(float(axis.minimum - delta), float(axis.maximum - delta))
              for axis, delta in zip((box.x, box.y, box.z), shift)]
    return Box(*ranges)


def _config_for_group(config: ProjectConfig, group: list[tuple[TargetSpec, float]],
                      original_anchor: TargetSpec | None, *, suffix: str | None) -> ProjectConfig:
    members = {target.asset.key for target, _ in group}
    targets = tuple(target for target in config.targets
                    if target.enabled and target.asset.key in members)
    if original_anchor is not None and original_anchor.asset.key in members:
        anchor = next(target for target in targets if target.asset.key == original_anchor.asset.key)
    else:
        # Keep the largest part as the new render centre; a single-part group has only one choice.
        anchor = max(targets, key=lambda target: next(extent for item, extent in group
                                                      if item.asset.key == target.asset.key))
    if original_anchor is not None and anchor.asset.key == original_anchor.asset.key:
        new_targets = targets
    else:
        # The anchor is always rendered at the origin. Shift every other offset by the anchor's
        # nominal offset so the group keeps the relative layout it had in the combined scene.
        shift = _midpoint(anchor.offset)
        new_targets = tuple(
            replace(target, offset=Box(Range(0., 0.), Range(0., 0.), Range(0., 0.)))
            if target.asset.key == anchor.asset.key
            else replace(target, offset=_shifted(target.offset, shift))
            for target in targets)
    output = config.output
    if suffix is not None:
        output = replace(output, dataset_name=f"{config.output.dataset_name}-{suffix}")
    return replace(config, targets=new_targets, anchor_key=anchor.asset.key, output=output)


def split_by_scale(config: ProjectConfig, meshes: Mapping[str, MeshData], *,
                   factor: float = MIXED_SCALE_FACTOR) -> tuple[ScaleGroup, ...]:
    """Return one :class:`ScaleGroup` per target-size class.

    A project whose enabled targets already lie within ``factor`` is returned as one unchanged
    group, so this function is safe to call on ordinary projects. Projects with fewer than two
    sized targets are also returned unchanged.
    """
    if factor <= 1:
        raise ValueError("Scale factor must be greater than one")
    _require_complete_meshes(config, meshes)
    entries = []
    for target in config.targets:
        if not target.enabled:
            continue
        bounds = np.asarray(meshes[target.asset.key].bounds_m, dtype=float)
        extent = float((bounds[1] - bounds[0]).max())
        entries.append((target, extent))
    sized = [(target, extent) for target, extent in entries if extent > 0]
    if len(sized) < 2 or max(extent for _, extent in sized) / min(extent for _, extent in sized) <= factor:
        # Return the caller's object untouched: rebuilding it would drop disabled targets and any
        # fields added to the schema later.
        targets = tuple(target for target in config.targets if target.enabled)
        extents = [extent for _, extent in sized] or [0.]
        by_key = {target.asset.key: extent for target, extent in entries}
        groups = (ScaleGroup(0, targets, tuple(by_key[target.asset.key] for target in targets),
                             min(extents), max(extents), config),)
        _require_exact_partition(config, groups)
        return groups

    original_anchor = next((target for target in config.targets
                            if target.enabled and target.asset.key == config.anchor_key), None)
    clusters = _clusters(sized, factor)
    multiple = len(clusters) > 1
    groups = []
    for index, cluster in enumerate(clusters):
        suffix = f"size-{index + 1}" if multiple else None
        group_config = _config_for_group(config, cluster, original_anchor, suffix=suffix)
        extents = [extent for _, extent in cluster]
        groups.append(ScaleGroup(index, tuple(target for target, _ in cluster), tuple(extents),
                                 min(extents), max(extents), group_config))
    _require_exact_partition(config, groups)
    return tuple(groups)
