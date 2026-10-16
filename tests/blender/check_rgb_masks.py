"""Semantic, read-only validation of RGB and per-object dual masks."""
from pathlib import Path
import argparse
import sys
import numpy as np
from PIL import Image

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.blender._checks import verify_metadata


def verify_rgb_masks(root: Path) -> list[str]:
    errors, config, status, records = verify_metadata(root)
    root = Path(root)
    if config is None:
        return errors
    try:
        if config.output.format != "rgb_masks":
            errors.append("Project output format is not rgb_masks")
        if any((root / name).exists() for name in ("depth", "models", "train_pbr", "scene_gt.json")):
            errors.append("RGB masks format unexpectedly contains BOP/depth output")
        ids = sorted(t.obj_id for t in config.targets if t.enabled)
        anchor_id = next(t.obj_id for t in config.targets if t.asset.key == config.anchor_key)
        shape = (config.camera.height, config.camera.width)
        expected_rgb, expected_masks = set(), set()
        for row in records:
            index = row["frame_index"]
            relative = Path(row["relative_rgb"])
            if relative.is_absolute() or ".." in relative.parts or relative.parent != Path("rgb") or relative.stem != f"{index:06d}":
                errors.append(f"Frame {index}: unsafe or incorrectly named RGB path")
                continue
            expected_rgb.add(relative.name)
            try:
                with Image.open(root / relative) as image:
                    rgb = np.asarray(image)
                    if image.mode != "RGB" or rgb.shape != (*shape, 3) or rgb.dtype != np.uint8:
                        errors.append(f"Frame {index}: RGB dimensions/mode differ from project")
            except Exception as exc:
                errors.append(f"Frame {index}: cannot read RGB: {exc}")
            for obj_id in ids:
                name = f"{index:06d}_{obj_id:06d}.png"
                expected_masks.add(name)
                try:
                    masks = []
                    for kind in ("mask", "mask_visib"):
                        with Image.open(root / kind / name) as image:
                            pixels = np.asarray(image)
                            if image.mode != "L" or pixels.dtype != np.uint8 or pixels.shape != shape or not set(np.unique(pixels)) <= {0, 255}:
                                errors.append(f"Frame {index} object {obj_id}: {kind} must be 8-bit 0/255 at RGB resolution")
                            masks.append(pixels)
                    full, visible = masks
                    if full.shape != visible.shape:
                        continue
                    if not np.any(full) or not np.any(visible):
                        errors.append(f"Frame {index} object {obj_id}: empty silhouette or no visible pixels")
                    if np.any((visible > 0) & (full == 0)):
                        errors.append(f"Frame {index} object {obj_id}: visible mask is not a subset of full mask")
                    if obj_id == anchor_id and np.any(full):
                        ratio = 1 - np.count_nonzero(visible) / np.count_nonzero(full)
                        if not np.isclose(ratio, row["occlusion_ratio"], atol=1e-9):
                            errors.append(f"Frame {index}: actual mask occlusion differs from manifest")
                except Exception as exc:
                    errors.append(f"Frame {index} object {obj_id}: cannot read masks: {exc}")
        for name, expected in (("rgb", expected_rgb), ("mask", expected_masks), ("mask_visib", expected_masks)):
            actual = {p.name for p in (root / name).iterdir() if p.is_file()} if (root / name).is_dir() else set()
            if actual != expected:
                errors.append(f"{name}: unexpected/missing files: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    except Exception as exc:
        errors.append(f"Malformed RGB masks dataset: {type(exc).__name__}: {exc}")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    diagnostics = verify_rgb_masks(parser.parse_args().dataset)
    print("\n".join(diagnostics) if diagnostics else "RGB + mask + mask_visib: PASS")
    raise SystemExit(1 if diagnostics else 0)
