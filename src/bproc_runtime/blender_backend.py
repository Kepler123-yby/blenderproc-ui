"""BlenderProc 2.8 single-frame adapter; Blender imports are deliberately lazy."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import importlib
import math
import tempfile
import numpy as np

from bproc_core.config import DEFAULT_WORLD_COLOR
from bproc_core.masks import decode_instances
from .contracts import RenderError, VisibilityEvidence, RenderedFrame
from .render_quality import (apply_object_shading, assert_gpu_only, configure_gpu,
                             configure_rgb_quality, label_pass_without_denoising)


class BlenderBackend:
    def __init__(self, api=None):
        self.api = api
        self.bpy = getattr(api, "bpy", None)
        self.targets = {}
        self.objects = []
        self._pools = {}
        self._temporary = None
        self.config = None
        self.candidate = None
        self._anchor_id = None
        self._renderer_module = None
        self._original_render = None
        self.gpu_devices = None

    def prepare(self, config, meshes) -> None:
        if self.api is None:
            self.api = importlib.import_module("blenderproc")
        if getattr(self.api, "__name__", None) == "blenderproc":
            # BlenderProc 2.8's progress reader is a non-daemon thread. A signal
            # during thread.start can bypass its cleanup and hang Python exit.
            # Preserve native Cycles terminal output and avoid that thread/pipe
            # path, including the render() calls inside render_segmap().
            self._renderer_module = importlib.import_module("blenderproc.python.renderer.RendererUtility")
            self._original_render = self._renderer_module.render
            original = self._original_render
            def render_without_progress(*args, **kwargs):
                kwargs["verbose"] = True
                return original(*args, **kwargs)
            self._renderer_module.render = render_without_progress
        if self.bpy is None:
            self.bpy = importlib.import_module("bpy")
        self.config = config
        self._temporary = tempfile.TemporaryDirectory(prefix="bproc-ui-")
        (Path(self._temporary.name) / "seg").mkdir()
        (Path(self._temporary.name) / "rgb").mkdir()
        self.api.init()
        self.api.renderer.set_max_amount_of_samples(config.render.samples)
        self.gpu_devices = configure_gpu(self.api, self.bpy, config.render.device)
        configure_rgb_quality(self.api, self.bpy, config.render)
        self.api.renderer.set_output_format("PNG", 8)
        if config.output.format == "bop":
            self.api.renderer.enable_depth_output(activate_antialiasing=False)
        for target in config.targets:
            if target.enabled:
                obj = self._make_object(meshes[target.asset.key], f"target_{target.obj_id:06d}")
                obj.set_cp("category_id", target.obj_id)
                obj.set_cp("ui_target_id", target.obj_id)
                self.targets[target.obj_id] = obj
        for asset in config.occluders:
            self._pools[asset.key] = []
            for i in range(config.occlusion.count[1]):
                obj = self._make_object(meshes[asset.key], f"occluder_{asset.key}_{i}")
                obj.set_cp("ui_target_id", 0)
                obj.set_cp("category_id", 0)
                obj.hide(True)
                self._pools[asset.key].append(obj)
        self.light = self.api.types.Light()
        self.light.set_type("POINT")
        self._create_world()

    def _make_object(self, mesh, name):
        data = self.bpy.data.meshes.new(name)
        data.from_pydata(mesh.vertices_m.tolist(), [], mesh.faces.tolist())
        data.update()
        obj = self.api.object.create_from_blender_mesh(data, object_name=name)
        material = self.bpy.data.materials.new(name + "_material")
        material.use_nodes = True
        shader = material.node_tree.nodes.get("Principled BSDF")
        shader.inputs["Roughness"].default_value = 0.65
        shader.inputs["Base Color"].default_value = (0.55, 0.61, 0.68, 1.0)
        if mesh.vertex_rgba is not None:
            colors = np.asarray(mesh.vertex_rgba, dtype=float)
            if colors.max(initial=0) > 1:
                colors /= 255.0
            attribute = data.color_attributes.new(name="Color", type="FLOAT_COLOR", domain="POINT")
            attribute.data.foreach_set("color", colors.reshape(-1))
            vertex_node = material.node_tree.nodes.new("ShaderNodeVertexColor")
            vertex_node.layer_name = "Color"
            material.node_tree.links.new(vertex_node.outputs["Color"], shader.inputs["Base Color"])
        data.materials.append(material)
        apply_object_shading(obj, self.config.render)
        self.objects.append(obj)
        return obj

    def _create_world(self):
        world = self.bpy.context.scene.world
        world.use_nodes = True
        world.node_tree.nodes.clear()
        nodes = world.node_tree.nodes
        out = nodes.new("ShaderNodeOutputWorld")
        bg = nodes.new("ShaderNodeBackground")
        env = nodes.new("ShaderNodeTexEnvironment")
        mapping = nodes.new("ShaderNodeMapping")
        coords = nodes.new("ShaderNodeTexCoord")
        world.node_tree.links.new(coords.outputs["Generated"], mapping.inputs["Vector"])
        world.node_tree.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
        world.node_tree.links.new(env.outputs["Color"], bg.inputs["Color"])
        world.node_tree.links.new(bg.outputs["Background"], out.inputs["Surface"])

    def _single_frame(self):
        scene = self.bpy.context.scene
        scene.frame_start, scene.frame_end = 0, 1
        scene.frame_set(0)

    def apply(self, candidate) -> None:
        self.candidate = candidate
        self._single_frame()
        for obj in self.objects:
            obj.hide(True)
        used = {}
        for placement in candidate.placements:
            if placement.role == "target":
                obj = self.targets[placement.obj_id]
            else:
                index = used.get(placement.asset_key, 0)
                obj = self._pools[placement.asset_key][index]
                used[placement.asset_key] = index + 1
            obj.set_local2world_mat(placement.world_from_model)
            obj.hide(False)
        camera = self.config.camera
        K = np.array([[camera.fx, 0, camera.cx], [0, camera.fy, camera.cy], [0, 0, 1.]])
        self.api.camera.set_intrinsics_from_K_matrix(K, camera.width, camera.height,
            clip_start=candidate.camera.clip_near_m, clip_end=candidate.camera.clip_far_m)
        self.api.camera.add_camera_pose(candidate.camera.world_from_camera, frame=0)
        self.light.set_location(candidate.light.position_m)
        self.light.set_energy(candidate.light.energy_w)
        self.light.set_color(candidate.light.color_rgb)
        self._apply_world(candidate)

    def _apply_world(self, candidate) -> None:
        """Follow the candidate's environment map, or a neutral studio grey without one."""
        # Look nodes up afresh: segmentation's UndoAfterExecution invalidates raw
        # bpy references while BlenderProc wrappers recover objects by name.
        world = self.bpy.context.scene.world
        nodes = world.node_tree.nodes
        env = next(n for n in nodes if n.type == "TEX_ENVIRONMENT")
        background = next(n for n in nodes if n.type == "BACKGROUND")
        color = background.inputs["Color"]
        if candidate.hdri_path is None:
            # A project without an HDRI still has to preview: black would hide the model.
            for link in list(color.links):
                world.node_tree.links.remove(link)
            color.default_value = (*DEFAULT_WORLD_COLOR, 1.0)
        else:
            if not color.links:
                world.node_tree.links.new(env.outputs["Color"], color)
            env.image = self.bpy.data.images.load(str(candidate.hdri_path), check_existing=True)
        background.inputs["Strength"].default_value = candidate.hdri_strength
        next(n for n in nodes if n.type == "MAPPING").inputs["Rotation"].default_value = (
            0.0, 0.0, math.radians(candidate.hdri_rotation_deg))

    def _segmentation(self):
        self._single_frame()
        with tempfile.TemporaryDirectory(dir=self._temporary.name, prefix="seg-") as directory:
            try:
                if self.gpu_devices is not None:
                    assert_gpu_only(self.bpy, self.config.render.device)
                with label_pass_without_denoising(self.bpy):
                    data = self.api.renderer.render_segmap(output_dir=str(Path(self._temporary.name) / "seg"), temp_dir=directory,
                        map_by=["instance", "cp_ui_target_id"], default_values={"cp_ui_target_id": 0},
                        use_alpha_channel=False)
                segmaps = data["instance_segmaps"]
                attributes = data["instance_attribute_maps"]
                if len(segmaps) != 1 or len(attributes) != 1:
                    raise ValueError("Expected exactly one segmentation frame")
                return decode_instances(segmaps[0], attributes[0], self.targets)
            except Exception as exc:
                raise RenderError(f"Segmentation pass failed: {exc}") from exc

    @contextmanager
    def _isolate(self, obj_id):
        before = [(obj, obj.is_hidden()) for obj in self.objects]
        try:
            selected = self.targets[obj_id]
            for obj, _ in before:
                obj.hide(obj is not selected)
            yield
        finally:
            for obj, hidden in before:
                obj.hide(hidden)
            self._single_frame()

    def assess(self, anchor_obj_id: int) -> VisibilityEvidence:
        self._anchor_id = anchor_obj_id
        visible = self._segmentation()
        with self._isolate(anchor_obj_id):
            full = self._segmentation()[anchor_obj_id]
        return VisibilityEvidence(full, visible)

    def render(self, evidence: VisibilityEvidence) -> RenderedFrame:
        full_masks = None
        if self.config.output.format == "rgb_masks":
            full_masks = {self._anchor_id: evidence.anchor_full}
            for obj_id in self.targets:
                if obj_id != self._anchor_id:
                    with self._isolate(obj_id):
                        full_masks[obj_id] = self._segmentation()[obj_id]
        self._single_frame()
        with tempfile.TemporaryDirectory(dir=self._temporary.name, prefix="rgb-pass-") as directory:
            try:
                assert_gpu_only(self.bpy, self.config.render.device)
                configure_rgb_quality(self.api, self.bpy, self.config.render)
                # Utility.register_output rejects reusing a key with a different
                # path. Reuse our private RGB path across candidates, and avoid
                # loading the registered segmentation files from prior passes.
                data = self.api.renderer.render(output_dir=str(Path(self._temporary.name) / "rgb"),
                    load_keys={"colors", "depth"}, verbose=True)
                if len(data["colors"]) != 1:
                    raise ValueError("Expected exactly one RGB frame")
                rgb = np.asarray(data["colors"][0])[..., :3]
                if rgb.dtype != np.uint8:
                    raise ValueError("Renderer did not return uint8 RGB")
                depth = None
                if self.config.output.format == "bop":
                    if len(data["depth"]) != 1:
                        raise ValueError("Expected exactly one depth frame")
                    depth = np.asarray(data["depth"][0])
                return RenderedFrame(rgb, depth, full_masks, evidence.visible)
            except Exception as exc:
                raise RenderError(f"RGB/depth pass failed: {exc}") from exc

    def close(self) -> None:
        if self._renderer_module is not None and self._original_render is not None:
            self._renderer_module.render = self._original_render
            self._original_render = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
