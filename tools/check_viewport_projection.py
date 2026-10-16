#!/usr/bin/env python3
"""Compare the actual calibrated Qt/OpenGL viewport against rendered Blender markers."""

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def centroids(rgb, target_width, target_height):
    result = []
    values = rgb[..., :3].astype(float)
    height, width = values.shape[:2]
    for channel in range(3):
        others = np.max(np.delete(values, channel, axis=2), axis=2)
        mask = (values[..., channel] > 60) & (values[..., channel] - others > 25)
        y, x = np.nonzero(mask)
        if not len(x):
            raise ValueError(f"Missing marker in channel {channel}")
        result.append([(x.mean() + .5) * target_width / width - .5,
                       (y.mean() + .5) * target_height / height - .5])
    return np.array(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
        parser.error("This integration check requires a desktop OpenGL context")
    from PySide6.QtCore import QTimer
    from bproc_core.assets import MeshData
    from bproc_core.camera import CameraPose
    from bproc_core.config import CameraSpec
    from bproc_core.scene import Placement
    from bproc_ui.app import create_app
    from bproc_ui.views.camera_view import CameraPreview

    reference = arguments.reference.resolve()
    info = json.loads((reference / "projection.json").read_text(encoding="utf-8"))
    camera = CameraSpec(**info["camera"])
    world = np.array(info["world_from_camera"])
    pose = CameraPose(world, world[:3, 3], 1, 17, 28, .001, 100)
    meshes, placements = {}, []
    local_corners = np.array([[-.022, -.022, 0], [.022, -.022, 0], [.022, .022, 0], [-.022, .022, 0]])
    for channel, center in enumerate(info["positions_world"]):
        vertices = local_corners @ world[:3, :3].T + np.asarray(center)
        faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
        color = [0, 0, 0, 255]
        color[channel] = 255
        key = f"marker-{channel}"
        meshes[key] = MeshData(key, vertices, faces, np.tile(color, (4, 1)),
                               np.array([vertices.min(axis=0), vertices.max(axis=0)]))
        placements.append(Placement(key, key, channel + 1, np.eye(4), "target"))
    candidate = SimpleNamespace(placements=placements, camera=pose, anchor_key="marker-0", region_key="projection", request_index=0)
    app = create_app([])
    preview = CameraPreview()
    preview.resize(camera.width * 10, camera.height * 10 + 61)
    preview.set_scene(candidate, meshes)
    preview.set_camera(camera, pose)
    preview.show_preview()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = [1]

    def check():
        evidence = {"camera": info["camera"], "world_from_camera": info["world_from_camera"]}
        try:
            image = preview.canvas.render()
            Image.fromarray(image).save(output / "viewport-markers.png")
            preview.grab().save(str(output / "viewport-window.png"))
            measured = centroids(image, camera.width, camera.height)
            blender_image = np.asarray(Image.open(reference / "markers.png").convert("RGB"))
            measured_blender = centroids(blender_image, camera.width, camera.height)
            expected = np.asarray(info["blender_pixels"])
            maximum_error = float(np.abs(measured - expected).max())
            blender_image_error = float(np.abs(measured - measured_blender).max())
            evidence.update(framebuffer_size=[image.shape[1], image.shape[0]],
                            viewport_pixels=measured.tolist(), blender_pixels=expected.tolist(),
                            blender_rendered_pixels=measured_blender.tolist(),
                            max_projection_error_pixels=maximum_error,
                            max_rendered_image_error_pixels=blender_image_error)
            if maximum_error > 1 or blender_image_error > 1:
                raise ValueError(f"Actual GL projection mismatch: matrix {maximum_error:.3f}px, image {blender_image_error:.3f}px")
            evidence["status"] = "passed"
            result[0] = 0
            print(f"Actual Qt/VisPy OpenGL projection PASS: matrix error {maximum_error:.4f}px, Blender image error {blender_image_error:.4f}px")
        except Exception as error:
            evidence.update(status="failed", error=repr(error))
            print(str(error), file=sys.stderr)
        (output / "viewport-projection.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        preview.hide()
        app.quit()

    QTimer.singleShot(500, check)
    app.exec()
    return result[0]


if __name__ == "__main__":
    raise SystemExit(main())
