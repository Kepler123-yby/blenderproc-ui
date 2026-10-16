"""Immutable project schema and actionable readiness validation.

Constructors intentionally allow incomplete editing drafts. Call
``validate_project`` before exporting or rendering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import math


class ConfigError(ValueError):
    """An unsupported or malformed project document."""


@dataclass(frozen=True)
class Range:
    minimum: float
    maximum: float


@dataclass(frozen=True)
class Azimuth:
    start_deg: float = -180.0
    end_deg: float = 180.0
    full_circle: bool = True


@dataclass(frozen=True)
class Box:
    x: Range = Range(-0.15, 0.15)
    y: Range = Range(-0.15, 0.15)
    z: Range = Range(-0.05, 0.05)


@dataclass(frozen=True)
class AssetSpec:
    key: str
    path: Path
    unit: Literal["m", "cm", "mm"] = "m"


@dataclass(frozen=True)
class TargetSpec:
    asset: AssetSpec
    name: str
    obj_id: int
    enabled: bool = True
    offset: Box = Box()


@dataclass(frozen=True)
class RegionSpec:
    key: str = "region-1"
    name: str = "全部方向"
    radius_m: Range = Range(0.5, 0.8)
    azimuth: Azimuth = Azimuth()
    elevation_deg: Range = Range(10.0, 65.0)


@dataclass(frozen=True)
class CameraSpec:
    width: int = 640
    height: int = 480
    fx: float = 572.4114
    fy: float = 573.57043
    cx: float = 325.2611
    cy: float = 242.04899
    auto_scale: bool = True


@dataclass(frozen=True)
class HdriSpec:
    files: tuple[Path, ...] = ()
    randomize: bool = True
    strength: Range = Range(0.6, 1.6)
    rotation_deg: Range = Range(0.0, 360.0)


@dataclass(frozen=True)
class LightSpec:
    energy_w: Range = Range(80.0, 100.0)
    radius_m: Range = Range(0.5, 1.0)
    elevation_deg: Range = Range(30.0, 90.0)
    temperature_k: Range = Range(4500.0, 6500.0)


@dataclass(frozen=True)
class OcclusionSpec:
    none_fraction: float = 0.2
    ratio: Range = Range(0.1, 0.5)
    max_attempts: int = 30
    count: tuple[int, int] = (1, 3)
    scale: Range = Range(0.25, 0.8)


@dataclass(frozen=True)
class RenderSpec:
    samples: int = 50
    device: Literal["CPU", "CUDA", "OPTIX", "HIP", "ONEAPI", "METAL"] = "CPU"


@dataclass(frozen=True)
class OutputSpec:
    format: Literal["rgb_masks", "bop"] = "rgb_masks"
    root: Path = Path("renders")
    dataset_name: str = "dataset"
    rgb_encoding: Literal["default", "png", "jpeg"] = "default"
    depth_scale_mm: float = 0.1


@dataclass(frozen=True)
class RuntimeSpec:
    conda_env: str = "bop"
    blender_dir: Path = field(default_factory=lambda: Path.home() / "blender/blender-4.2.1-linux-x64")
    blenderproc_executable: Path = field(default_factory=lambda: Path.home() / "miniconda3/envs/bop/bin/blenderproc")
    toolkit_path: Path | None = None


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int = 1
    targets: tuple[TargetSpec, ...] = ()
    anchor_key: str | None = None
    regions: tuple[RegionSpec, ...] = (RegionSpec(),)
    camera: CameraSpec = CameraSpec()
    occluders: tuple[AssetSpec, ...] = ()
    hdri: HdriSpec = HdriSpec()
    light: LightSpec = LightSpec()
    occlusion: OcclusionSpec = OcclusionSpec()
    num_samples: int = 1000
    seed: int = 42
    render: RenderSpec = RenderSpec()
    output: OutputSpec = OutputSpec()
    runtime: RuntimeSpec = field(default_factory=RuntimeSpec)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    severity: Literal["error", "warning"] = "error"


def new_project(base_dir: Path) -> ProjectConfig:
    """Create a saveable draft; resources are deliberately not invented."""
    return ProjectConfig(output=OutputSpec(root=Path(base_dir).expanduser().resolve() / "renders"))


def validate_project(config: ProjectConfig, *, check_files: bool = True) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []

    def issue(name, message, severity="error"):
        issues.append(ValidationIssue(name, message, severity))

    def finite(name, value, *, positive=False, nonnegative=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            issue(name, "必须是有限数值")
            return False
        if positive and value <= 0:
            issue(name, "必须大于 0")
        if nonnegative and value < 0:
            issue(name, "不能小于 0")
        return True

    def integer(name, value, minimum=0):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            issue(name, f"必须是大于等于 {minimum} 的整数")

    def interval(name, value, *, low=None, high=None, positive=False, nonnegative=False):
        ok = finite(name + ".minimum", value.minimum, positive=positive, nonnegative=nonnegative)
        ok = finite(name + ".maximum", value.maximum, positive=positive, nonnegative=nonnegative) and ok
        if ok:
            if value.minimum > value.maximum:
                issue(name, "下限不能大于上限")
            if low is not None and value.minimum < low or high is not None and value.maximum > high:
                issue(name, f"范围必须位于 {low if low is not None else '−∞'} 到 {high if high is not None else '∞'}")

    def asset_check(asset, name, suffixes):
        if not isinstance(asset.key, str) or not asset.key.strip():
            issue(name + ".key", "资源 key 不能为空")
        if asset.unit not in ("m", "cm", "mm"):
            issue(name + ".unit", "模型单位必须是 m、cm 或 mm")
        if not isinstance(asset.path, Path):
            issue(name + ".path", "资源路径必须是 Path")
        elif check_files:
            if not asset.path.is_file():
                issue(name + ".path", f"找不到模型文件：{asset.path}")
            elif asset.path.suffix.lower() not in suffixes:
                issue(name + ".path", f"模型格式应为 {' / '.join(suffixes)}")

    integer("schema_version", config.schema_version, 1)
    if config.schema_version != 1:
        issue("schema_version", "仅支持 schema_version: 1")
    integer("num_samples", config.num_samples, 1)
    integer("seed", config.seed, 0)
    enabled = [t for t in config.targets if t.enabled]
    if not enabled:
        issue("targets", "请添加至少一个启用的目标模型")
    keys: set[str] = set()
    obj_ids: set[int] = set()
    for index, target in enumerate(config.targets):
        name = f"targets[{index}]"
        asset_check(target.asset, name + ".asset", (".ply",))
        if target.asset.key in keys:
            issue(name + ".asset.key", "资源 key 不能重复")
        keys.add(target.asset.key)
        integer(name + ".obj_id", target.obj_id, 1)
        if target.obj_id in obj_ids:
            issue(name + ".obj_id", "BOP 对象 ID 不能重复")
        obj_ids.add(target.obj_id)
        if not isinstance(target.enabled, bool):
            issue(name + ".enabled", "启用状态必须是布尔值")
        if not isinstance(target.name, str) or not target.name.strip():
            issue(name + ".name", "目标名称不能为空")
        for axis in "xyz":
            interval(name + ".offset." + axis, getattr(target.offset, axis))
    if config.anchor_key not in {t.asset.key for t in enabled}:
        issue("anchor_key", "请选择一个启用目标作为渲染中心")
    for index, asset in enumerate(config.occluders):
        name = f"occluders[{index}]"
        asset_check(asset, name, (".ply", ".obj"))
        if asset.key in keys:
            issue(name + ".key", "目标和遮挡物的资源 key 不能重复")
        keys.add(asset.key)
    if not config.regions:
        issue("regions", "至少保留一个相机采样区域")
    region_keys: set[str] = set()
    for index, region in enumerate(config.regions):
        name = f"regions[{index}]"
        if not isinstance(region.key, str) or not region.key.strip() or region.key in region_keys:
            issue(name + ".key", "区域 key 必须非空且唯一")
        region_keys.add(region.key)
        interval(name + ".radius_m", region.radius_m, positive=True)
        interval(name + ".elevation_deg", region.elevation_deg, low=-90, high=90)
        for endpoint in ("start_deg", "end_deg"):
            value = getattr(region.azimuth, endpoint)
            if finite(name + ".azimuth." + endpoint, value) and not -180 <= value <= 180:
                issue(name + ".azimuth." + endpoint, "方位角必须位于 −180° 到 180°")
        if not isinstance(region.azimuth.full_circle, bool):
            issue(name + ".azimuth.full_circle", "全方位必须是布尔值")
    cam = config.camera
    integer("camera.width", cam.width, 1)
    integer("camera.height", cam.height, 1)
    for field_name in ("fx", "fy", "cx", "cy"):
        finite("camera." + field_name, getattr(cam, field_name), positive=field_name in ("fx", "fy"))
    if not isinstance(cam.auto_scale, bool):
        issue("camera.auto_scale", "内参缩放选项必须是布尔值")
    if not config.hdri.files:
        issue("hdri.files", "请选择至少一张 HDRI 环境图（.hdr / .exr）")
    for index, path in enumerate(config.hdri.files):
        if not isinstance(path, Path):
            issue(f"hdri.files[{index}]", "环境图路径必须是 Path")
        elif path.suffix.lower() not in (".hdr", ".exr") or check_files and not path.is_file():
            issue(f"hdri.files[{index}]", f"无效或找不到 HDRI 环境图：{path}")
    if not isinstance(config.hdri.randomize, bool):
        issue("hdri.randomize", "随机开关必须是布尔值")
    interval("hdri.strength", config.hdri.strength, nonnegative=True)
    interval("hdri.rotation_deg", config.hdri.rotation_deg)
    interval("light.energy_w", config.light.energy_w, nonnegative=True)
    interval("light.radius_m", config.light.radius_m, positive=True)
    interval("light.elevation_deg", config.light.elevation_deg, low=0, high=90)
    interval("light.temperature_k", config.light.temperature_k, low=1000, high=40000)
    occ = config.occlusion
    if finite("occlusion.none_fraction", occ.none_fraction) and not 0 <= occ.none_fraction <= 1:
        issue("occlusion.none_fraction", "无遮挡样本比例必须位于 0 到 1")
    interval("occlusion.ratio", occ.ratio, low=0, high=1)
    integer("occlusion.max_attempts", occ.max_attempts, 1)
    if len(occ.count) != 2:
        issue("occlusion.count", "遮挡物数量需要上下限两个整数")
    else:
        for i, value in enumerate(occ.count):
            integer(f"occlusion.count[{i}]", value, 0)
        if all(isinstance(v, int) and not isinstance(v, bool) for v in occ.count) and occ.count[0] > occ.count[1]:
            issue("occlusion.count", "遮挡物数量下限不能大于上限")
    interval("occlusion.scale", occ.scale, positive=True)
    if not config.occluders and occ.none_fraction < 1 and occ.ratio.minimum > 0:
        issue("occluders", "未选择专门遮挡物；只能依靠其他目标满足遮挡率", "warning")
    integer("render.samples", config.render.samples, 1)
    if config.render.device not in ("CPU", "CUDA", "OPTIX", "HIP", "ONEAPI", "METAL"):
        issue("render.device", "不支持的 Cycles 设备类型")
    output = config.output
    if output.format not in ("rgb_masks", "bop"):
        issue("output.format", "输出格式只能是 rgb_masks 或 bop")
    if output.rgb_encoding not in ("default", "png", "jpeg"):
        issue("output.rgb_encoding", "RGB 编码只能是 default、png 或 jpeg")
    if not isinstance(output.root, Path):
        issue("output.root", "输出父目录必须是 Path")
    if not isinstance(output.dataset_name, str) or output.dataset_name in ("", ".", "..") or any(c in output.dataset_name for c in ("/", "\\", "\x00")):
        issue("output.dataset_name", "数据集名称必须是一个非空目录名")
    finite("output.depth_scale_mm", output.depth_scale_mm, positive=True)
    if isinstance(output.root, Path) and isinstance(output.dataset_name, str) and output.dataset_name:
        final = output.root / output.dataset_name
        if check_files and (final.exists() or final.is_symlink()):
            issue("output.root", f"输出目录已存在，不能覆盖：{final}")
    runtime = config.runtime
    if not isinstance(runtime.conda_env, str) or not runtime.conda_env.strip():
        issue("runtime.conda_env", "conda 环境名不能为空")
    for name, path in (("blender_dir", runtime.blender_dir), ("blenderproc_executable", runtime.blenderproc_executable)):
        if not isinstance(path, Path):
            issue("runtime." + name, "运行时路径必须是 Path")
        elif check_files:
            expected = path / "blender" if name == "blender_dir" else path
            if not expected.is_file():
                issue("runtime." + name, f"找不到运行时文件：{expected}")
    if output.format == "bop" and runtime.toolkit_path is not None and check_files and not runtime.toolkit_path.is_dir():
        issue("runtime.toolkit_path", f"找不到 BOP toolkit 目录：{runtime.toolkit_path}")
    if isinstance(config.num_samples, int) and config.num_samples < len(config.regions):
        issue("regions", "样本数少于区域数，部分区域不会生成样本", "warning")
    return tuple(issues)
