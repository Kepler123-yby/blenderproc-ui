import blenderproc as bproc
"""Actual Blender oracle for off-center calibrated projection; run via BlenderProc."""
from pathlib import Path
import argparse
import json
import sys
import numpy as np
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bproc_core.camera import sample_camera, project_points, projection_matrix
from bproc_core.config import CameraSpec, RegionSpec, Azimuth, Range
from bproc_core.sampling import view_rng
from blenderproc.python.utility.SetupUtility import SetupUtility


def deny(*args, **kwargs):
    raise RuntimeError('Projection fixture may not install dependencies')


SetupUtility.setup_pip = staticmethod(deny)
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
bproc.init()
cam = CameraSpec(width=64, height=48, fx=81.3, fy=67.8, cx=25.3, cy=18.7)
region = RegionSpec(radius_m=Range(1, 1), azimuth=Azimuth(17, 17, False), elevation_deg=Range(28, 28))
pose = sample_camera(region, np.zeros(3), np.eye(3), cam, view_rng(42, 0, 0, 0))
K = np.array([[cam.fx, 0, cam.cx], [0, cam.fy, cam.cy], [0, 0, 1.]])
bproc.camera.set_intrinsics_from_K_matrix(K, cam.width, cam.height, clip_start=.001, clip_end=100)
bproc.camera.add_camera_pose(pose.world_from_camera, frame=0)
centers_local = np.array([[0., 0., -1.], [.15, .1, -1.], [-.13, -.08, -.8]])
centers_world = centers_local @ pose.world_from_camera[:3, :3].T + pose.position_m
for i, center in enumerate(centers_local):
    points = center + np.array([[-.022, -.022, 0], [.022, -.022, 0], [.022, .022, 0], [-.022, .022, 0]])
    world = points @ pose.world_from_camera[:3, :3].T + pose.position_m
    data = bpy.data.meshes.new(f'marker{i}')
    data.from_pydata(world.tolist(), [], [(0, 1, 2), (0, 2, 3)])
    obj = bproc.object.create_from_blender_mesh(data, object_name=f'marker{i}')
    material = bpy.data.materials.new(f'marker{i}')
    material.use_nodes = True
    material.node_tree.nodes.clear()
    emission = material.node_tree.nodes.new('ShaderNodeEmission')
    color = [0., 0., 0., 1.]
    color[i] = 1.
    emission.inputs['Color'].default_value = color
    out = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
    material.node_tree.links.new(emission.outputs[0], out.inputs['Surface'])
    data.materials.append(material)
bpy.context.scene.world.use_nodes = True
bpy.context.scene.world.node_tree.nodes.get('Background').inputs['Color'].default_value = (0, 0, 0, 1)
bpy.context.scene.view_settings.view_transform = 'Standard'
bproc.renderer.set_max_amount_of_samples(8)
bproc.renderer.set_output_format('PNG', 8)
data = bproc.renderer.render(output_dir=str(args.output), verbose=False)
from PIL import Image
Image.fromarray(data['colors'][0][..., :3]).save(args.output / 'markers.png')
scene = bpy.context.scene
bpy.context.view_layer.update()
P = np.array(scene.camera.calc_matrix_camera(bpy.context.evaluated_depsgraph_get(), x=cam.width,
    y=cam.height, scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y))
local_h = np.column_stack([centers_local, np.ones(3)])
clip = local_h @ P.T
ndc = clip[:, :2] / clip[:, 3, None]
blender_uv = np.column_stack([(ndc[:, 0] + 1) * cam.width / 2 - .5,
                             (1 - ndc[:, 1]) * cam.height / 2 - .5])
core_uv, _ = project_points(centers_world, pose, cam)
result = dict(camera=cam.__dict__, world_from_camera=pose.world_from_camera.tolist(),
              positions_world=centers_world.tolist(), core_pixels=core_uv.tolist(),
              blender_pixels=blender_uv.tolist(), blender_projection=P.tolist(),
              core_projection=projection_matrix(cam, .001, 100).tolist())
(args.output / 'projection.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print('Projection evidence written:', args.output)
