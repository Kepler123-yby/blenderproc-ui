"""Native Blender 4.2 preview session. Import only inside Blender after BlenderProc."""
import json
from pathlib import Path

import bpy
from bpy.props import IntProperty, PointerProperty, StringProperty

from bproc_core.assets import load_mesh
from bproc_core.preview import candidate_for_preview
from bproc_core.project_io import load_project
from bproc_core.scene import Rejection, candidate_from_dict, candidate_to_dict
from .blender_backend import BlenderBackend

NOTICE = "候选预览 · 未经像素遮挡验收"
SESSION_KEY = "bproc_preview_snapshot"
_session = None


def viewport(mode):
    """Change real spaces, including inactive VIEW_3D spaces on the current screen."""
    touched = 0
    for area in bpy.context.screen.areas:
        if area.type != "VIEW_3D":
            continue
        space = area.spaces.active
        space.shading.type = "SOLID"
        space.overlay.show_extras = True  # camera body/frustum in the overview
        space.region_3d.view_perspective = "CAMERA" if mode == "camera" else "PERSP"
        if mode == "overview" and _session is not None:
            pose = _session.candidate.camera
            space.region_3d.view_location = (0, 0, 0)
            space.region_3d.view_distance = max(.1, pose.radius_m * 2.5)
        area.tag_redraw()
        touched += 1
    if not touched:
        raise RuntimeError("没有 VIEW_3D 区域，请先打开三维视口")
    return touched


def remove_preview_scene(scene):
    """Remove only data owned by this dedicated preview, never other user scenes."""
    if SESSION_KEY not in scene:
        return
    if len(bpy.data.scenes) == 1:
        bpy.data.scenes.new("Scene")  # Blender requires one local scene, including on close.
    objects = list(scene.objects)
    world = scene.world
    bpy.data.scenes.remove(scene)
    for obj in objects:
        data = obj.data
        materials = list(data.materials) if obj.type == "MESH" else []
        kind = obj.type
        if obj.users == 0:
            bpy.data.objects.remove(obj)
            if data is not None and data.users == 0:
                collection = {"MESH": bpy.data.meshes, "CAMERA": bpy.data.cameras,
                              "LIGHT": bpy.data.lights}.get(kind)
                if collection is not None:
                    collection.remove(data)
            for material in materials:
                material.use_fake_user = False
                if material.users == 0:
                    bpy.data.materials.remove(material)
    if world is not None and world.users == 0:
        bpy.data.worlds.remove(world)


class PreviewSession:
    def __init__(self, config, meshes, candidate, snapshot):
        self.config, self.meshes = config, meshes
        self.backend = BlenderBackend()
        self.backend.prepare(config, meshes, geometry_only=True)
        self.backend.bpy.context.scene[SESSION_KEY] = snapshot
        self.candidate = candidate
        # The handed-off candidate may come from a different NumPy build or a later attempt.
        # Preserve that one exact snapshot on revisits; all other indices are sampled on demand.
        self.initial_candidate = candidate
        self.backend.apply(candidate)
        self._sync()

    def _sync(self):
        props = bpy.context.scene.bproc_preview
        props.scene_number = self.candidate.scene_index + 1
        props.view_number = self.candidate.view_index + 1
        props.status = NOTICE
        # Save the *current* exact candidate, not just the first candidate file.
        bpy.context.scene["bproc_preview_candidate"] = json.dumps(candidate_to_dict(self.candidate))

    def select(self, scene_index, view_index):
        initial = self.initial_candidate
        result = (initial if (scene_index, view_index) == (initial.scene_index, initial.view_index)
                  else candidate_for_preview(self.config, self.meshes, scene_index, view_index))
        if isinstance(result, Rejection):
            self._sync()
            raise ValueError(f"{result.reason}: {result.detail}")
        previous = self.candidate
        try:
            self.backend.apply(result)
        except Exception:
            self.backend.apply(previous)
            self._sync()
            raise
        self.candidate = result
        self._sync()
        return result

    def render_preview(self):
        viewport("camera")  # remain SOLID on any validation/device failure
        self.backend.configure_render()
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.shading.type = "RENDERED"
                area.tag_redraw()

    def close(self):
        self.backend.close()
        remove_preview_scene(self.backend.geometry_scene)


def start_session(config_path, candidate_path=None, *, candidate=None):
    global _session
    register()
    config_path = Path(config_path).resolve()
    config = load_project(config_path)
    assets = [t.asset for t in config.targets if t.enabled] + list(config.occluders)
    meshes = {a.key: load_mesh(a) for a in assets}
    if candidate is None:
        candidate = candidate_from_dict(json.loads(Path(candidate_path).read_text(encoding="utf-8")))
    if _session is not None:
        _session.close()
        _session = None
    # A saved preview is rebuilt once when opening the GUI, then objects are reused.
    for scene in list(bpy.data.scenes):
        if SESSION_KEY in scene:
            remove_preview_scene(scene)
    snapshot = json.dumps({"config": str(config_path)})
    _session = PreviewSession(config, meshes, candidate, snapshot)
    viewport("camera")
    return _session


def restore_session():
    scene = bpy.context.scene
    if SESSION_KEY not in scene:
        raise RuntimeError("缺少预览快照；请从应用的「在 Blender 中查看」重新打开")
    snapshot = json.loads(scene[SESSION_KEY])
    candidate = candidate_from_dict(json.loads(scene["bproc_preview_candidate"]))
    return start_session(snapshot["config"], candidate=candidate)


def current_session():
    if _session is None or bpy.context.scene != _session.backend.geometry_scene:
        raise RuntimeError("当前场景没有活动预览会话；请从应用重新打开")
    return _session


class BPROC_PreviewProperties(bpy.types.PropertyGroup):
    scene_number: IntProperty(name="场景", min=1, default=1)
    view_number: IntProperty(name="视角", min=1, default=1)
    status: StringProperty(default=NOTICE)


class BPROC_OT_preview_update(bpy.types.Operator):
    bl_idname = "bproc.preview_update"
    bl_label = "更新候选"

    def execute(self, context):
        props = context.scene.bproc_preview
        try:
            current_session().select(props.scene_number - 1, props.view_number - 1)
        except Exception as error:
            props.status = f"更新失败（保留上次候选）：{error}"
            self.report({'WARNING'}, props.status)
            return {'CANCELLED'}
        return {'FINISHED'}


class BPROC_OT_preview_camera(bpy.types.Operator):
    bl_idname = "bproc.preview_camera"
    bl_label = "相机视图"

    def execute(self, context):
        viewport("camera")
        return {'FINISHED'}


class BPROC_OT_preview_overview(bpy.types.Operator):
    bl_idname = "bproc.preview_overview"
    bl_label = "场景总览"

    def execute(self, context):
        viewport("overview")
        return {'FINISHED'}


class BPROC_OT_preview_render(bpy.types.Operator):
    bl_idname = "bproc.preview_render"
    bl_label = "渲染预览（严格 GPU）"

    def execute(self, context):
        try:
            current_session().render_preview()
        except Exception as error:
            context.scene.bproc_preview.status = f"渲染失败：{error}；请检查 GPU 后端/降噪设置，无 CPU 回退"
            self.report({'WARNING'}, context.scene.bproc_preview.status)
            return {'CANCELLED'}
        return {'FINISHED'}


class BPROC_PT_preview(bpy.types.Panel):
    bl_label = "场景 / 相机预览"
    bl_idname = "BPROC_PT_preview"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderProc"

    def draw(self, context):
        layout = self.layout
        layout.label(text=NOTICE)
        try:
            session = current_session()
        except RuntimeError as error:
            layout.label(text=str(error))
            return
        config, candidate = session.config, session.candidate
        props = context.scene.bproc_preview
        layout.label(text=f"场景 1–{config.scene_count} / 视角 1–{config.views}")
        layout.prop(props, "scene_number")
        layout.prop(props, "view_number")
        layout.operator("bproc.preview_update")
        row = layout.row()
        row.operator("bproc.preview_camera")
        row.operator("bproc.preview_overview")
        layout.operator("bproc.preview_render")
        layout.label(text=props.status)
        box = layout.box()
        box.label(text=f"当前：场景 {candidate.scene_index + 1} / 视角 {candidate.view_index + 1}")
        box.label(text=f"来源：{candidate.camera_kind} / {candidate.region_key}")
        box.label(text=f"有效种子：{config.seed} / attempt {candidate.attempt_index}")
        c, pose = config.camera, candidate.camera
        box.label(text=f"{c.width}×{c.height} · fx/fy {c.fx:g}/{c.fy:g}")
        box.label(text=f"cx/cy {c.cx:g}/{c.cy:g}")
        box.label(text=f"r {pose.radius_m:.4g}m · φ {pose.azimuth_deg:.2f}° · α {pose.elevation_deg:.2f}°")
        box.label(text="相机 XYZ：" + ", ".join(f"{v:.4g}" for v in pose.position_m))
        box.label(text=f"光源 {candidate.light.energy_w:.4g}W · XYZ " +
                       ", ".join(f"{v:.3g}" for v in candidate.light.position_m))
        box.label(text=f"HDRI：{candidate.hdri_path or '中性灰'}")
        box.label(text=f"环境强度 {candidate.hdri_strength:.3g} / 旋转 {candidate.hdri_rotation_deg:.2f}°")
        box.label(text=f"遮挡物 {sum(p.role != 'target' for p in candidate.placements)} / 无遮挡分类 {candidate.wants_unoccluded}")
        box.label(text=f"材质：粗糙度 {candidate.material.roughness:.3g} / 金属 {candidate.material.metallic:.3g}")
        box.label(text=f"渲染：{config.render.device} / 降噪 {config.render.denoiser}")
        box.label(text="只读参数；科学配置请在原应用编辑")


_CLASSES = (BPROC_PreviewProperties, BPROC_OT_preview_update, BPROC_OT_preview_camera,
            BPROC_OT_preview_overview, BPROC_OT_preview_render, BPROC_PT_preview)


def register():
    for cls in _CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    if not hasattr(bpy.types.Scene, "bproc_preview"):
        bpy.types.Scene.bproc_preview = PointerProperty(type=BPROC_PreviewProperties)


def unregister():
    global _session
    if _session is not None:
        _session.close()
        _session = None
    if hasattr(bpy.types.Scene, "bproc_preview"):
        del bpy.types.Scene.bproc_preview
    for cls in reversed(_CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
