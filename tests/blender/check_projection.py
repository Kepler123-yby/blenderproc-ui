"""Compare independent Blender projection, VisPy transforms and rendered markers."""
from pathlib import Path
import json
import numpy as np
from PIL import Image


def verify_projection(artifact_dir: Path) -> list[str]:
    try:
        root = Path(artifact_dir)
        info = json.loads((root / 'projection.json').read_text())
        errors = []
        core = np.asarray(info['core_pixels'])
        blender = np.asarray(info['blender_pixels'])
        if np.max(np.abs(core - blender)) > 1e-3:
            errors.append('Core pixel projection differs from bpy.calc_matrix_camera')
        from bproc_core.config import CameraSpec
        from bproc_core.camera import CameraPose
        from bproc_ui.views.projection import preview_pixels
        world = np.asarray(info['world_from_camera'])
        pose = CameraPose(world, world[:3, 3], 1., 17., 28., .001, 100.)
        preview = preview_pixels(info['positions_world'], pose, CameraSpec(**info['camera']))
        if np.max(np.abs(preview - blender)) > 1e-3:
            errors.append('VisPy transform differs from Blender calibrated camera')
        rgb = np.asarray(Image.open(root / 'markers.png').convert('RGB'))
        for channel, expected in enumerate(blender):
            other = np.max(np.delete(rgb, channel, axis=2), axis=2)
            mask = (rgb[..., channel] > 140) & (other < 60)
            y, x = np.nonzero(mask)
            if not len(x):
                errors.append(f'Marker {channel} is absent from the rendered image')
            elif np.max(np.abs(np.array([x.mean(), y.mean()]) - expected)) > 1.:
                errors.append(f'Marker {channel} is misaligned by more than one pixel')
        return errors
    except Exception as exc:
        return [f'Projection validation failed: {type(exc).__name__}: {exc}']


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('artifact_dir', type=Path)
    problems = verify_projection(parser.parse_args().artifact_dir)
    print('\n'.join(problems) if problems else 'Core / VisPy / Blender / rendered marker projection PASS')
    sys.exit(bool(problems))
