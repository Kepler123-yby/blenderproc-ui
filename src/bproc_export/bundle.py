from __future__ import annotations

from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
import json
import os
import shutil

from bproc_core.config import ConfigError, ProjectConfig, validate_project
from bproc_core.assets import load_mesh, AssetError
from bproc_core.project_io import save_project
from bproc_core.assets import content_bounds
from bproc_core.camera import envelope_radius
from bproc_core.project_io import project_to_dict
from bproc_core.scale_groups import depth_capacity, split_by_scale
from .commands import build_command
import hashlib
import tempfile


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    command: str
    preview_command: str


def _absolute_config(config: ProjectConfig) -> ProjectConfig:
    def asset(a):
        return replace(a, path=a.path.expanduser().resolve())
    runtime = config.runtime
    return replace(config, targets=tuple(replace(t, asset=asset(t.asset)) for t in config.targets),
                   occluders=tuple(asset(a) for a in config.occluders),
                   hdri=replace(config.hdri, files=tuple(p.expanduser().resolve() for p in config.hdri.files)),
                   output=replace(config.output, root=config.output.root.expanduser().resolve()),
                   runtime=replace(runtime, blender_dir=runtime.blender_dir.expanduser().resolve(),
                                   blenderproc_executable=runtime.blenderproc_executable.expanduser().resolve(),
                                   toolkit_path=runtime.toolkit_path.expanduser().resolve() if runtime.toolkit_path else None))


def locate_runtime(config: ProjectConfig) -> dict:
    runtime = config.runtime
    blender = runtime.blender_dir
    pythons = sorted(blender.glob('*/python/bin/python3.11'))
    if len(pythons) != 1:
        raise ConfigError(f'需要 Blender 4.2 的唯一 Python 3.11：{blender}')
    executable = runtime.blenderproc_executable
    packages = sorted(executable.parent.parent.glob('lib/python*/site-packages/blenderproc'))
    if len(packages) != 1:
        raise ConfigError(f'无法在 BlenderProc 可执行文件所属环境定位包：{executable}；请选择 bop/bin/blenderproc')
    return dict(schema_version=1, ready=True, blender_dir=str(blender),
                blender_python=str(pythons[0]),
                custom_packages=str(blender / 'custom-python-packages/lib/python3.11/site-packages'),
                blenderproc_executable=str(executable), blenderproc_package_dir=str(packages[0]))


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _export_problems(config: ProjectConfig) -> None:
    """Raise when a config cannot be packaged: invalid fields or an unreadable resource."""
    errors = [e for e in validate_project(config) if e.severity == 'error']
    if errors:
        raise ConfigError('\n'.join(f'{e.field}: {e.message}' for e in errors))
    for asset in [*(t.asset for t in config.targets if t.enabled), *config.occluders]:
        try:
            load_mesh(asset)
        except AssetError as exc:
            raise ConfigError(f'资源 {asset.key} 无法导出：{exc}') from exc


def export_job(config: ProjectConfig, destination: Path) -> ExportResult:
    config = _absolute_config(config)
    _export_problems(config)
    # Keep the final component lexical until mkdir, so a dangling symlink remains a conflict.
    destination = Path(destination).expanduser().absolute()
    destination = destination.parent.resolve() / destination.name
    output = (config.output.root / config.output.dataset_name).resolve()
    inputs = [t.asset.path for t in config.targets] + [a.path for a in config.occluders] + list(config.hdri.files)
    if _contains(destination, output) or _contains(output, destination):
        raise ConfigError('导出任务目录与数据集目录不能互相包含')
    for resource in inputs:
        if _contains(destination, resource) or _contains(output, resource):
            raise ConfigError(f'任务或数据集目录不能包含输入资源：{resource}')
    locator = locate_runtime(config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()  # Atomic reservation, including empty directories and dangling links.
    except FileExistsError as exc:
        raise ConfigError(f'导出目录已存在，请选择新目录：{destination}') from exc
    stage = destination / '.building'
    stage.mkdir()
    try:
        source = Path(__file__).resolve().parents[1]
        for package in ('bproc_core', 'bproc_runtime'):
            shutil.copytree(source / package, stage / package,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('run_job.py', 'render_scene.py'):
            shutil.copyfile(Path(__file__).parent / 'templates' / name, stage / name)
        save_project(config, stage / 'scene.yaml')
        (stage / 'README.txt').write_text(
            '终端运行 run_job.py；资源路径是绝对路径，请保留原资源。\n'
            '先运行 --check-only 检查离线环境；--num-scenes 1 --output <新父目录> 可试渲染一个场景。\n'
            f'本任务共 {config.scene_count} 个场景 × {config.views_per_scene} 视角 = {config.total_shots} 张。\n'
            '科学参数可直接修改 scene.yaml；变更 Blender/BlenderProc 路径后请重新导出。\n', encoding='utf-8')
        for child in stage.iterdir():
            os.replace(child, destination / child.name)
        stage.rmdir()
        # Published last: an interrupted export cannot accidentally launch.
        (destination / '.launch.tmp').write_text(json.dumps(locator, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(destination / '.launch.tmp', destination / 'launch.json')
    except BaseException:
        (destination / 'EXPORT_INCOMPLETE.txt').write_text('导出未完成，请使用新的任务目录重新导出。\n', encoding='utf-8')
        raise
    return ExportResult(destination, build_command(destination, config.runtime.conda_env),
                        build_command(destination, config.runtime.conda_env, num_scenes=1,
                                      output=config.output.root / 'preview'))


def _source_hash(config: ProjectConfig) -> str:
    payload = json.dumps(project_to_dict(config), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _grouping_document(source: ProjectConfig, groups, group, meshes) -> dict:
    """Record the partition, envelopes, and every camera/light change for one group."""
    group_config = group.config
    reference = content_bounds(meshes, source.targets, source.anchor_key)
    bounds = content_bounds(meshes, group_config.targets, group_config.anchor_key)
    report = group.grouping or {}
    if group_config.camera_mode == "fixed":
        cameras = [{"key": spec.key, "azimuth_deg": spec.azimuth_deg,
                    "elevation_deg": spec.elevation_deg, "radius_m": spec.radius_m}
                   for spec in group_config.fixed_cameras]
    else:
        cameras = [{"key": region.key,
                    "radius_m": [region.radius_m.minimum, region.radius_m.maximum],
                    "azimuth": [region.azimuth.start_deg, region.azimuth.end_deg,
                                bool(region.azimuth.full_circle)],
                    "elevation_deg": [region.elevation_deg.minimum, region.elevation_deg.maximum]}
                   for region in group_config.regions]
    light = {"radius_m": [group_config.light.radius_m.minimum, group_config.light.radius_m.maximum],
             "energy_w": [group_config.light.energy_w.minimum, group_config.light.energy_w.maximum],
             "elevation_deg": [group_config.light.elevation_deg.minimum,
                               group_config.light.elevation_deg.maximum],
             "temperature_k": [group_config.light.temperature_k.minimum,
                               group_config.light.temperature_k.maximum]}
    return {
        "schema_version": 1,
        "source_project_sha256": _source_hash(source),
        "group_index": group.index + 1,
        "group_count": len(groups),
        "label": group.label,
        "dataset_name": group_config.output.dataset_name,
        "targets": [{"key": target.asset.key, "obj_id": target.obj_id,
                     "extent_m": extent} for target, extent in zip(group.targets, group.extents_m)],
        "partition_exact_once": True,
        "reference_bounds_m": None if reference is None else reference.tolist(),
        "group_bounds_m": None if bounds is None else bounds.tolist(),
        "reference_scale_m": None if reference is None else envelope_radius(reference),
        "group_scale_m": None if bounds is None else envelope_radius(bounds),
        "scale_factor": (None if report else
                         (envelope_radius(bounds) / envelope_radius(reference)
                          if bounds is not None and reference is not None and envelope_radius(reference) else None)),
        "reframe": bool(report.get("reframe", False)),
        "margin_fraction": group_config.scale_grouping.margin_fraction,
        "cameras": cameras,
        "cameras_before_after": report.get("cameras", []),
        "light": light,
        "light_change": report.get("light"),
        "clamps": report.get("clamps", []),
        "unchanged": report.get("unchanged", {
            "camera": {"width": group_config.camera.width, "height": group_config.camera.height,
                       "fx": group_config.camera.fx, "fy": group_config.camera.fy,
                       "cx": group_config.camera.cx, "cy": group_config.camera.cy},
            "pixel_size": [group_config.camera.width, group_config.camera.height],
            "obj_ids": sorted(target.obj_id for target in group_config.targets if target.enabled),
            "occlusion_ratio": [group_config.occlusion.ratio.minimum,
                                group_config.occlusion.ratio.maximum],
            "depth_scale_mm": group_config.output.depth_scale_mm,
        }),
    }


def _write_grouping(job: Path, document: dict) -> None:
    temporary = Path(job) / ".grouping.tmp"
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, Path(job) / "grouping.json")


def export_grouped_jobs(config, meshes, destination) -> list[ExportResult]:
    """Export one job per target-size class; ordinary projects still produce exactly one job.

    Jobs are written next to the requested directory (``<name>-size-1``, ``<name>-size-2``, ...)
    so the caller's "new job directory" convention is preserved. A multi-group export is built in
    private staging directories and published only after every group succeeded; a failure leaves
    no runnable ``launch.json`` behind.
    """
    destination = Path(destination).expanduser()
    # Grouping raises when an enabled target has not been loaded. Every resource, depth capacity
    # and destination conflict is checked before a single job directory is created.
    groups = split_by_scale(config, meshes)
    final_dirs = [destination if len(groups) == 1
                  else destination.with_name(f"{destination.name}-size-{group.index + 1}")
                  for group in groups]
    for group in groups:
        _export_problems(_absolute_config(group.config))
        if group.config.output.format == "bop":
            required, capacity = depth_capacity(group.config, meshes)
            if required >= capacity:
                raise ConfigError(
                    f"组 {group.label} 的完整深度包络需要 {required:.6f} m，"
                    f"当前 depth_scale_mm={group.config.output.depth_scale_mm:g} 只能编码 "
                    f"{capacity:.6f} m；请显式调整 depth_scale_mm，导出不会自动修改编码")
    for target in final_dirs:
        if target.exists() or target.is_symlink():
            raise ConfigError(f"导出目录已存在，请选择新目录：{target}")
    if len(groups) == 1:
        result = export_job(groups[0].config, final_dirs[0])
        _write_grouping(result.directory, _grouping_document(config, groups, groups[0], meshes))
        return [result]
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.building-", dir=destination.parent))
    published: list[Path] = []
    try:
        staged: list[tuple[Path, Path, ExportResult]] = []
        for group, final in zip(groups, final_dirs):
            stage = staging / final.name
            result = export_job(group.config, stage)
            _write_grouping(stage, _grouping_document(config, groups, group, meshes))
            staged.append((stage, final, result))
        for stage, final, _result in staged:
            os.replace(stage, final)
            published.append(final)
    except BaseException:
        # A half-published set must not look runnable: remove the entry point from every job
        # that already moved, then return it to staging for removal.
        for final in published:
            (final / "launch.json").unlink(missing_ok=True)
            (final / "EXPORT_INCOMPLETE.txt").write_text(
                "整套分组导出未完成；请使用新的任务目录重新导出。\n", encoding="utf-8")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    results = []
    for stage, final, result in staged:
        # The command was built while the job lived in staging; repoint it at the published path.
        command = result.command.replace(str(stage), str(final))
        preview = getattr(result, "preview_command", result.command).replace(str(stage), str(final))
        if is_dataclass(result):
            results.append(replace(result, directory=final, command=command, preview_command=preview))
        else:                                    # exporter test double
            result.directory = final
            result.command = command
            result.preview_command = preview
            results.append(result)
    return results
