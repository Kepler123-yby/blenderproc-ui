"""Immutable project schema and actionable readiness validation.

Constructors intentionally allow incomplete editing drafts. Call
``validate_project`` before exporting or rendering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
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
class FixedCameraSpec:
    """One fully determined camera in the anchor's local frame (radius / azimuth / elevation)."""

    key: str = "camera-1"
    name: str = "机位 1"
    radius_m: float = 0.7
    azimuth_deg: float = -90.0
    elevation_deg: float = 25.0

    def region(self) -> RegionSpec:
        """The degenerate sampling region that pins this camera."""
        return RegionSpec(key=self.key, name=self.name, radius_m=Range(self.radius_m, self.radius_m),
                          azimuth=Azimuth(self.azimuth_deg, self.azimuth_deg, False),
                          elevation_deg=Range(self.elevation_deg, self.elevation_deg))


@dataclass(frozen=True)
class CameraSpec:
    width: int = 640
    height: int = 480
    fx: float = 572.4114
    fy: float = 573.57043
    cx: float = 325.2611
    cy: float = 242.04899
    auto_scale: bool = True


# A project may start before its environment map is chosen: it then renders against this
# neutral world, the same grey Blender's default world uses.
DEFAULT_WORLD_COLOR = (0.05, 0.05, 0.05)


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


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    """sRGB hex to 0..1 floats; Blender wants linear, so callers convert once more."""
    return tuple(int(value[index:index + 2], 16) / 255.0 for index in (1, 3, 5))


def srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


@dataclass(frozen=True)
class MaterialState:
    """Concrete surface parameters: sampled per scene, or specified once by the user."""
    roughness: float = 0.65
    specular: float = 0.5
    metallic: float = 0.0


@dataclass(frozen=True)
class MaterialSpec:
    """Surface parameters for the target objects only.

    ``source: random`` samples a set inside the three ranges once per scene; ``source: fixed``
    renders every scene with ``fixed`` verbatim, which is what a material inspection needs.
    The model's own colour is kept unless ``color: solid``, matching the reference dataset
    (bproc_project/dataset_rendering/samplers/target.py).
    """
    color: Literal["original", "solid"] = "original"
    base_color: str = "#b3b7ba"
    source: Literal["random", "fixed"] = "random"
    fixed: MaterialState = MaterialState(0.4, 0.6, 0.0)
    roughness: Range = Range(0.05, 0.8)
    specular: Range = Range(0.0, 1.0)
    metallic: Range = Range(0.0, 0.0)


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
    device: Literal["CUDA", "OPTIX", "HIP", "ONEAPI", "METAL"] = "OPTIX"
    shading_mode: Literal["flat", "smooth", "auto"] = "auto"
    smooth_angle_deg: float = 30.0
    weighted_normals: bool = False
    denoiser: Literal["auto", "none", "OPTIX"] = "auto"
    noise_threshold: float = 0.01
    # Blender 4.2 Cycles native image tiling (`scene.cycles.use_auto_tile`). It caps the image
    # buffer, not the geometry: the whole scene is still resident and rendered in one call.
    use_tiling: bool = True
    tile_size: int = 2048


@dataclass(frozen=True)
class ViewportSpec:
    """Display-only simplification; never consumed by sampling, collision or export."""
    lod_mode: Literal["auto", "full"] = "auto"
    lod_threshold_faces: int = 100_000
    lod_target_faces: int = 50_000


@dataclass(frozen=True)
class ScaleGroupingSpec:
    """Opt-in per-group reframing for a genuinely multi-group export."""
    reframe: bool = False
    margin_fraction: float = 0.10


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
    camera_mode: Literal["fixed", "random"] = "random"
    scene_count: int = 250
    views_per_scene: int = 4
    layout_max_attempts: int = 30
    regions: tuple[RegionSpec, ...] = (RegionSpec(),)
    fixed_cameras: tuple[FixedCameraSpec, ...] = ()
    camera: CameraSpec = CameraSpec()
    occluders: tuple[AssetSpec, ...] = ()
    hdri: HdriSpec = HdriSpec()
    light: LightSpec = LightSpec()
    material: MaterialSpec = MaterialSpec()
    occlusion: OcclusionSpec = OcclusionSpec()
    seed: int = 42
    render: RenderSpec = RenderSpec()
    viewport: ViewportSpec = ViewportSpec()
    scale_grouping: ScaleGroupingSpec = ScaleGroupingSpec()
    output: OutputSpec = OutputSpec()
    runtime: RuntimeSpec = field(default_factory=RuntimeSpec)

    @property
    def views(self) -> int:
        """Camera views rendered for every scene: pinned cameras, or the random view count."""
        return len(self.fixed_cameras) if self.camera_mode == "fixed" else self.views_per_scene

    @property
    def total_shots(self) -> int:
        """Frames produced by one run; every scene contributes :attr:`views` frames."""
        return self.scene_count * self.views

    def with_total_shots(self, total: int) -> ProjectConfig:
        """Rescale the scene count to a new frame total (quick preflight runs)."""
        if total < 0:
            raise ValueError("Image count must be nonnegative")
        views = max(1, self.views)
        return replace(self, scene_count=0 if total == 0 else max(1, round(total / views)))


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
    if config.camera_mode not in ("fixed", "random"):
        issue("camera_mode", "机位模式只能是 fixed 或 random")
    integer("scene_count", config.scene_count, 0)
    if config.camera_mode == "random":
        integer("views_per_scene", config.views_per_scene, 1)
        if not config.regions:
            issue("regions", "随机机位模式至少需要一个随机区域")
    elif not config.fixed_cameras:
        issue("fixed_cameras", "固定机位模式至少需要一个机位")
    integer("seed", config.seed, 0)
    integer("layout_max_attempts", config.layout_max_attempts, 1)
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
    camera_keys = set(region_keys)
    for index, fixed in enumerate(config.fixed_cameras):
        name = f"fixed_cameras[{index}]"
        if not isinstance(fixed.key, str) or not fixed.key.strip() or fixed.key in camera_keys:
            issue(name + ".key", "机位 key 必须非空且与区域不重名")
        camera_keys.add(fixed.key)
        if not isinstance(fixed.name, str) or not fixed.name.strip():
            issue(name + ".name", "机位名称不能为空")
        finite(name + ".radius_m", fixed.radius_m, positive=True)
        if finite(name + ".azimuth_deg", fixed.azimuth_deg) and not -180 <= fixed.azimuth_deg <= 180:
            issue(name + ".azimuth_deg", "方位角必须位于 −180° 到 180°")
        if finite(name + ".elevation_deg", fixed.elevation_deg) and not -90 <= fixed.elevation_deg <= 90:
            issue(name + ".elevation_deg", "仰角必须位于 −90° 到 90°")
    if isinstance(config.total_shots, int) and config.total_shots < 1:
        issue("scene_count", "总张数至少为 1")
    cam = config.camera
    integer("camera.width", cam.width, 1)
    integer("camera.height", cam.height, 1)
    for field_name in ("fx", "fy", "cx", "cy"):
        finite("camera." + field_name, getattr(cam, field_name), positive=field_name in ("fx", "fy"))
    if not isinstance(cam.auto_scale, bool):
        issue("camera.auto_scale", "内参缩放选项必须是布尔值")
    # No HDRI yet is a supported state: the renderer falls back to DEFAULT_WORLD_COLOR.
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
    interval("material.roughness", config.material.roughness, low=0, high=1)
    interval("material.specular", config.material.specular, low=0, high=1)
    interval("material.metallic", config.material.metallic, low=0, high=1)
    if config.material.color not in ("original", "solid"):
        issue("material.color", "颜色来源只能为 original 或 solid")
    if config.material.source not in ("random", "fixed"):
        issue("material.source", "参数来源只能为 random 或 fixed")
    if not _HEX.match(config.material.base_color):
        issue("material.base_color", "基色必须是 #RRGGBB")
    for name in ("roughness", "specular", "metallic"):
        value = getattr(config.material.fixed, name)
        if not 0.0 <= value <= 1.0:
            issue(f"material.fixed.{name}", "固定材质参数必须在 0 到 1 之间")
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
    if occ.ratio.minimum > 0:
        from bproc_core.occlusion import effective_occlusion_range
        if effective_occlusion_range(config, False).maximum == 0:
            issue("occluders", "只有一个启用目标且没有可用的专门遮挡物；遮挡比例请求无法满足，帧只能按无遮挡接收", "warning")
    integer("render.samples", config.render.samples, 1)
    if not isinstance(config.render.use_tiling, bool):
        issue("render.use_tiling", "图像分块开关必须是布尔值")
    if (isinstance(config.render.tile_size, bool) or not isinstance(config.render.tile_size, int)
            or not 8 <= config.render.tile_size <= 8192):
        issue("render.tile_size", "图像分块尺寸必须是 8 到 8192 的整数")
    viewport = config.viewport
    if viewport.lod_mode not in ("auto", "full"):
        issue("viewport.lod_mode", "视口 LOD 模式只能是 auto 或 full")
    integer("viewport.lod_threshold_faces", viewport.lod_threshold_faces, 1)
    integer("viewport.lod_target_faces", viewport.lod_target_faces, 1)
    if (not isinstance(viewport.lod_threshold_faces, bool)
            and not isinstance(viewport.lod_target_faces, bool)
            and isinstance(viewport.lod_threshold_faces, int)
            and isinstance(viewport.lod_target_faces, int)
            and viewport.lod_target_faces > viewport.lod_threshold_faces):
        issue("viewport.lod_target_faces", "视口 LOD 目标面数不能大于触发面数")
    grouping = config.scale_grouping
    if not isinstance(grouping.reframe, bool):
        issue("scale_grouping.reframe", "分组重取景开关必须是布尔值")
    if finite("scale_grouping.margin_fraction", grouping.margin_fraction):
        if not 0 <= grouping.margin_fraction < 0.45:
            issue("scale_grouping.margin_fraction", "重取景边距必须位于 0 到 0.45（不含）")
    if config.render.device not in ("CUDA", "OPTIX", "HIP", "ONEAPI", "METAL"):
        issue("render.device", "仅支持 GPU 后端：OPTIX、CUDA、HIP、ONEAPI、METAL；禁止 CPU 渲染")
    if config.render.shading_mode not in ("flat", "smooth", "auto"):
        issue("render.shading_mode", "着色方式必须是 flat、smooth 或 auto")
    if finite("render.smooth_angle_deg", config.render.smooth_angle_deg) and not 0 <= config.render.smooth_angle_deg <= 180:
        issue("render.smooth_angle_deg", "平滑角度必须位于 0 到 180°")
    if not isinstance(config.render.weighted_normals, bool):
        issue("render.weighted_normals", "加权法线选项必须是布尔值")
    if config.render.denoiser not in ("auto", "none", "OPTIX"):
        issue("render.denoiser", "降噪仅支持 auto、none 或 GPU OPTIX")
    if config.render.denoiser == "OPTIX" and config.render.device != "OPTIX":
        issue("render.denoiser", "OptiX GPU 降噪需要 OPTIX 渲染后端；CUDA 等后端请使用自动或关闭")
    if finite("render.noise_threshold", config.render.noise_threshold) and not 0 <= config.render.noise_threshold <= 1:
        issue("render.noise_threshold", "自适应采样阈值必须位于 0 到 1，0 表示关闭")
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
    return tuple(issues)


def world_metadata(config: ProjectConfig) -> dict:
    """Record what lit the scene: the chosen HDRI, or the default neutral background."""
    if config.hdri.files:
        return {"kind": "hdri", "files": [str(path) for path in config.hdri.files]}
    return {"kind": "neutral", "color": list(DEFAULT_WORLD_COLOR)}
