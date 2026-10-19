#!/usr/bin/env python3
"""Bounded, read-only audit of native instance-mask compatibility evidence.

``reference`` recomputes the hashes and PNG facts of the files named by the
manifest.  It never globs the reference tree and never runs the reference
renderer.  ``dataset`` checks a freshly exported dataset together with its job
snapshot: dual mask/depth encoding, suffix -> obj_id mapping, run metadata
consistency, and a per-file hash comparison of the packaged source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BINARY_VALUES = {0, 255}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_facts(path: Path) -> dict:
    """Read the PNG signature and IHDR directly; PIL is not asked to guess the format."""
    with Path(path).open("rb") as stream:
        header = stream.read(33)
    signature = header[:8] == PNG_SIGNATURE
    facts = {"png_signature": signature}
    if not signature or len(header) < 33 or header[12:16] != b"IHDR":
        facts["ihdr"] = False
        return facts
    facts.update({
        "ihdr": True,
        "width": int.from_bytes(header[16:20], "big"),
        "height": int.from_bytes(header[20:24], "big"),
        "bit_depth": header[24],
        "color_type": header[25],
    })
    return facts


def image_facts(path: Path) -> dict:
    path = Path(path)
    facts = {"path": str(path), "sha256": sha256_file(path)}
    facts.update(png_facts(path))
    with Image.open(path) as image:
        facts.update({"pil_format": image.format, "pil_mode": image.mode,
                      "size": list(image.size)})
        pixels = np.asarray(image)
    facts.update({
        "numpy_dtype": str(pixels.dtype),
        "numpy_shape": list(pixels.shape),
        "unique_values": [int(value) for value in np.unique(pixels)] if pixels.size else [],
    })
    return facts


def _expected_dtype(bit_depth: int) -> str:
    return {8: "uint8", 16: "uint16"}.get(bit_depth, "unknown")


def _check_png(sample: dict, expected: dict, *, label: str) -> tuple[list[str], dict]:
    """Recompute one manifest sample and compare every field the manifest promises."""
    problems = []
    try:
        facts = image_facts(Path(sample["path"]))
    except Exception as exc:  # missing, unreadable, or not an image at all
        return [f"{label}: cannot read {sample['path']}: {type(exc).__name__}: {exc}"], {}
    if facts["sha256"] != expected.get("sha256"):
        problems.append(f"{label}: SHA256 {facts['sha256']} differs from manifest {expected.get('sha256')}")
    if facts["pil_format"] != "PNG":
        problems.append(f"{label}: PIL format is {facts['pil_format']}, not PNG")
    if not facts.get("ihdr"):
        problems.append(f"{label}: no PNG IHDR")
    else:
        if facts["bit_depth"] != expected.get("png_bit_depth"):
            problems.append(f"{label}: IHDR bit_depth {facts['bit_depth']} differs from {expected.get('png_bit_depth')}")
        if facts["color_type"] != expected.get("png_color_type"):
            problems.append(f"{label}: IHDR color_type {facts['color_type']} differs from {expected.get('png_color_type')}")
        dtype = _expected_dtype(facts["bit_depth"])
        if facts["numpy_dtype"] != dtype:
            problems.append(f"{label}: NumPy dtype {facts['numpy_dtype']} does not match bit depth {facts['bit_depth']} ({dtype})")
    if facts["pil_mode"] != expected.get("mode"):
        problems.append(f"{label}: PIL mode {facts['pil_mode']} differs from {expected.get('mode')}")
    if facts["size"] != expected.get("size"):
        problems.append(f"{label}: size {facts['size']} differs from {expected.get('size')}")
    if facts["unique_values"] != expected.get("unique_values"):
        problems.append(f"{label}: unique values {facts['unique_values']} differ from {expected.get('unique_values')}")
    return problems, facts


def audit_reference(manifest_path: Path) -> dict:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("reference manifest must be a JSON object")
    for key in ("sources", "samples"):
        if not isinstance(manifest.get(key), list) or not manifest[key]:
            raise ValueError(f"reference manifest must list nonempty {key}")
    problems = []
    source_facts = []
    for position, item in enumerate(manifest.get("sources", [])):
        path = Path(item["path"])
        facts = {"path": str(path), "expected_sha256": item.get("sha256")}
        if not path.is_file():
            problems.append(f"source[{position}]: missing {path}")
        else:
            facts["sha256"] = sha256_file(path)
            if facts["sha256"] != item.get("sha256"):
                problems.append(f"source[{position}]: SHA256 {facts['sha256']} differs from {item.get('sha256')}")
        source_facts.append(facts)
    sample_facts = []
    for position, item in enumerate(manifest.get("samples", [])):
        local_problems, facts = _check_png(item, item, label=f"sample[{position}]")
        problems.extend(local_problems)
        if facts:
            sample_facts.append(facts)
    paths = [item.get("path") for item in manifest.get("sources", []) + manifest.get("samples", [])]
    if len(paths) != len(set(paths)):
        problems.append("manifest lists a duplicate path")
    return {
        "mode": "reference",
        "scope": "exactly the manifest-listed sources and samples; no directory glob or recursive scan",
        "manifest": str(manifest_path),
        "counts": {"sources": len(source_facts), "samples": len(sample_facts)},
        "facts": {"sources": source_facts, "samples": sample_facts},
        "problems": problems,
        "passed": not problems,
    }


def _snapshot_files(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def audit_snapshot(job: Path) -> tuple[list[str], dict]:
    """Compare the packaged job source byte-for-byte with the current checkout."""
    pairs = {
        "bproc_core": (job / "bproc_core", REPO_ROOT / "src" / "bproc_core"),
        "bproc_runtime": (job / "bproc_runtime", REPO_ROOT / "src" / "bproc_runtime"),
        "run_job.py": (job / "run_job.py", REPO_ROOT / "src" / "bproc_export" / "templates" / "run_job.py"),
        "render_scene.py": (job / "render_scene.py", REPO_ROOT / "src" / "bproc_export" / "templates" / "render_scene.py"),
    }
    problems, facts = [], {}
    for name, (snapshot, source) in pairs.items():
        if snapshot.is_dir():
            packaged = _snapshot_files(snapshot)
            current = _snapshot_files(source)
            missing = sorted(set(current) - set(packaged))
            extra = sorted(set(packaged) - set(current))
            changed = sorted(relative for relative in set(current) & set(packaged)
                             if sha256_file(packaged[relative]) != sha256_file(current[relative]))
            if missing:
                problems.append(f"{name}: snapshot is missing {missing}")
            if extra:
                problems.append(f"{name}: snapshot has unexpected files {extra}")
            for relative in changed:
                problems.append(f"{name}: {relative} differs from the current source")
            facts[name] = {"files": len(packaged), "missing": missing, "extra": extra, "changed": changed}
        elif snapshot.is_file() and source.is_file():
            same = sha256_file(snapshot) == sha256_file(source)
            if not same:
                problems.append(f"{name}: snapshot differs from the current template")
            facts[name] = {"files": 1, "sha256_match": same}
        else:
            problems.append(f"{name}: snapshot entry is missing")
            facts[name] = {"files": 0, "missing": True}
    return problems, facts


def _load_json(path: Path, label: str, problems: list[str]):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except Exception as exc:
        problems.append(f"{label}: cannot read {path}: {type(exc).__name__}: {exc}")
        return None


def _metadata_facts(dataset: Path, problems: list[str]) -> tuple[list[dict], dict]:
    folder = dataset / "run_metadata"
    status = _load_json(folder / "status.json", "status", problems)
    manifest = _load_json(folder / "manifest.json", "manifest", problems)
    journal_path = folder / "frames.jsonl"
    rows = []
    if not journal_path.is_file():
        problems.append("frames.jsonl is missing")
    else:
        for number, line in enumerate(journal_path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                problems.append(f"journal line {number}: {exc}")
                continue
            if not isinstance(row, dict):
                problems.append(f"journal line {number}: expected a JSON object")
                continue
            row.pop("schema_version", None)
            rows.append(row)
    facts = {"journal_rows": len(rows)}
    if isinstance(status, dict):
        facts["status"] = {name: status.get(name) for name in
                           ("status", "requested", "written_frames", "saved", "skipped", "exit_code")}
        for name in ("requested", "written_frames", "saved", "skipped", "exit_code"):
            if isinstance(status.get(name), bool) or not isinstance(status.get(name), int):
                problems.append(f"status.{name} is not an integer")
        if status.get("status") != "completed":
            problems.append(f"status is {status.get('status')!r}, not completed")
        if status.get("exit_code") != 0:
            problems.append(f"status exit_code is {status.get('exit_code')}, not 0")
        if not (status.get("requested") == status.get("written_frames") == status.get("saved")):
            problems.append("requested, written_frames and saved are not equal")
        if status.get("skipped") != 0:
            problems.append(f"skipped is {status.get('skipped')}, not 0")
        if len(rows) != status.get("saved"):
            problems.append(f"journal has {len(rows)} rows but saved={status.get('saved')}")
    if isinstance(manifest, dict):
        facts["manifest"] = {"schema_version": manifest.get("schema_version"),
                             "final": manifest.get("final"), "frames": len(manifest.get("frames", []))}
        if manifest.get("schema_version") != 2 or manifest.get("final") is not True:
            problems.append("manifest is not the final schema 2 snapshot")
        if manifest.get("frames") != rows:
            problems.append("final manifest frames differ from the journal")
    for index, row in enumerate(rows):
        if row.get("frame_index") != index:
            problems.append(f"journal row {index}: frame_index is {row.get('frame_index')}")
    return rows, facts


def _mask_facts(path: Path, shape, label: str, problems: list[str]) -> tuple[np.ndarray | None, dict]:
    try:
        facts = image_facts(path)
        with Image.open(path) as image:
            pixels = np.asarray(image)
    except Exception as exc:
        problems.append(f"{label}: cannot read {path}: {type(exc).__name__}: {exc}")
        return None, {}
    if facts["pil_format"] != "PNG":
        problems.append(f"{label}: not PNG ({facts['pil_format']})")
    if not facts.get("ihdr") or facts.get("bit_depth") != 8 or facts.get("color_type") != 0:
        problems.append(f"{label}: IHDR is not 8/0: {facts.get('bit_depth')}/{facts.get('color_type')}")
    if facts["pil_mode"] != "L" or facts["numpy_dtype"] != "uint8":
        problems.append(f"{label}: expected L/uint8, got {facts['pil_mode']}/{facts['numpy_dtype']}")
    if tuple(facts["size"][::-1]) != tuple(shape):
        problems.append(f"{label}: size {facts['size']} differs from camera {(shape[1], shape[0])}")
    if not set(facts["unique_values"]) <= BINARY_VALUES:
        problems.append(f"{label}: values are not 0/255: {facts['unique_values']}")
    return pixels, facts


def audit_dataset(dataset_dir: Path, job_dir: Path) -> dict:
    from bproc_core.project_io import load_project

    dataset, job = Path(dataset_dir), Path(job_dir)
    problems: list[str] = []
    try:
        config = load_project(dataset / "run_metadata" / "project.yaml")
    except Exception as exc:
        problems.append(f"project.yaml: {type(exc).__name__}: {exc}")
        config = None
    rows, metadata = _metadata_facts(dataset, problems)
    ids = tuple(sorted(t.obj_id for t in config.targets if t.enabled)) if config else ()
    shape = (config.camera.height, config.camera.width) if config else (0, 0)
    masks = []
    mapping = []
    depth = {"frames": [], "recovery": "pixel * depth_scale_mm / 1000 metres"}
    if config is not None:
        if metadata.get("status", {}).get("requested") != config.total_shots:
            problems.append("status requested differs from the configured frame count")
        expected_mask_names = set()
        for row in rows:
            index = row.get("frame_index")
            if tuple(row.get("gt_obj_ids", ())) != ids:
                problems.append(f"frame {index}: recorded gt_obj_ids differ from the enabled objects")
            if config.output.format == "rgb_masks":
                frame_mapping = {}
                for obj_id in ids:
                    name = f"{index:06d}_{obj_id:06d}.png"
                    expected_mask_names.add(name)
                    frame_mapping[name.split("_", 1)[1][:-4]] = obj_id
                    full, full_facts = _mask_facts(dataset / "mask" / name, shape,
                                                   f"frame {index} mask {obj_id}", problems)
                    visible, visible_facts = _mask_facts(dataset / "mask_visib" / name, shape,
                                                         f"frame {index} mask_visib {obj_id}", problems)
                    if (full is not None and visible is not None and full.shape == visible.shape
                            and np.any((visible > 0) & (full == 0))):
                        problems.append(f"frame {index} object {obj_id}: visible is not a subset of full")
                    if full_facts and visible_facts:
                        masks.append({"frame_index": index, "obj_id": obj_id,
                                      "mask": full_facts, "mask_visib": visible_facts})
                mapping.append({"frame_index": index, "suffix_to_obj_id": frame_mapping})
                if not all((dataset / kind).is_dir() for kind in ("mask", "mask_visib")):
                    problems.append(f"frame {index}: mask directories are missing")
            elif config.output.format == "bop":
                scene_id, image_id = row.get("bop_scene_id"), row.get("bop_image_id")
                if not isinstance(scene_id, int) or not isinstance(image_id, int):
                    problems.append(f"frame {index}: missing integer bop_scene_id/bop_image_id")
                    continue
                chunk = dataset / "train_pbr" / f"{scene_id:06d}"
                gt = _load_json(chunk / "scene_gt.json", f"frame {index} scene_gt", problems)
                camera = _load_json(chunk / "scene_camera.json", f"frame {index} scene_camera", problems)
                if not isinstance(gt, dict) or not isinstance(camera, dict):
                    continue
                poses = gt.get(str(image_id), [])
                pose_ids = [pose.get("obj_id") for pose in poses]
                if tuple(pose_ids) != ids:
                    problems.append(f"frame {index}: scene_gt objects {pose_ids} differ from {list(ids)}")
                depth_path = chunk / "depth" / f"{image_id:06d}.png"
                try:
                    depth_facts = image_facts(depth_path)
                except Exception as exc:
                    problems.append(f"frame {index}: cannot read depth: {exc}")
                    depth_facts = {}
                if depth_facts:
                    if not (depth_facts.get("ihdr") and depth_facts.get("bit_depth") == 16
                            and depth_facts.get("color_type") == 0):
                        problems.append(f"frame {index}: depth IHDR is not 16/0")
                    if depth_facts["numpy_dtype"] != "uint16":
                        problems.append(f"frame {index}: depth dtype is {depth_facts['numpy_dtype']}")
                    if tuple(depth_facts["size"][::-1]) != tuple(shape):
                        problems.append(f"frame {index}: depth size differs from camera")
                scale = camera.get(str(image_id), {}).get("depth_scale")
                if not isinstance(scale, (int, float)) or scale != config.output.depth_scale_mm:
                    problems.append(f"frame {index}: scene_camera depth_scale {scale} differs from {config.output.depth_scale_mm}")
                depth["frames"].append({"frame_index": index,
                                        "bit_depth": depth_facts.get("bit_depth"),
                                        "color_type": depth_facts.get("color_type"),
                                        "depth_scale_mm": scale})
                frame_mapping = {}
                for position, obj_id in enumerate(pose_ids):
                    suffix = f"{position:06d}"
                    frame_mapping[suffix] = obj_id
                    name = f"{image_id:06d}_{suffix}.png"
                    full, full_facts = _mask_facts(chunk / "mask" / name, shape,
                                                   f"frame {index} mask index {position}", problems)
                    visible, visible_facts = _mask_facts(chunk / "mask_visib" / name, shape,
                                                         f"frame {index} mask_visib index {position}", problems)
                    if (full is not None and visible is not None and full.shape == visible.shape
                            and np.any((visible > 0) & (full == 0))):
                        problems.append(f"frame {index} gt index {position}: visible is not a subset of full")
                    if full_facts and visible_facts:
                        masks.append({"frame_index": index, "gt_index": position, "obj_id": obj_id,
                                      "mask": full_facts, "mask_visib": visible_facts})
                mapping.append({"frame_index": index, "bop_scene_id": scene_id,
                                "bop_image_id": image_id, "gt_index_to_obj_id": frame_mapping})
            else:
                problems.append(f"unsupported output format {config.output.format!r}")
        if config.output.format == "rgb_masks" and all((dataset / kind).is_dir() for kind in ("mask", "mask_visib")):
            for kind in ("mask", "mask_visib"):
                actual = {path.name for path in (dataset / kind).iterdir() if path.is_file()}
                if actual != expected_mask_names:
                    problems.append(f"{kind}: mask directory differs from the expected suffixes: "
                                    f"missing={sorted(expected_mask_names - actual)}, extra={sorted(actual - expected_mask_names)}")
    snapshot_problems, snapshot = audit_snapshot(job)
    problems.extend(snapshot_problems)
    return {
        "mode": "dataset",
        "scope": "manifest-listed frames, both mask directories, BOP depth/GT, and packaged source snapshot",
        "dataset": str(dataset),
        "job": str(job),
        "format": config.output.format if config else None,
        "enabled_obj_ids": list(ids),
        "metadata": metadata,
        "mapping": mapping,
        "masks": masks,
        "depth": depth,
        "snapshot": snapshot,
        "problems": problems,
        "passed": not problems,
    }


def write_report(path: Path, report: dict) -> None:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite an existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    # Exclusive creation also rejects dangling symlinks and races with another audit.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    reference = subparsers.add_parser("reference", help="audit the manifest-listed reference files")
    reference.add_argument("--manifest", type=Path, required=True)
    reference.add_argument("--output", type=Path, required=True)
    dataset = subparsers.add_parser("dataset", help="audit a freshly exported dataset and its job")
    dataset.add_argument("--dataset", type=Path, required=True)
    dataset.add_argument("--job", type=Path, required=True)
    dataset.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.output.exists():
        print(f"refusing to overwrite an existing report: {arguments.output}", file=sys.stderr)
        return 2
    try:
        report = (audit_reference(arguments.manifest) if arguments.command == "reference"
                  else audit_dataset(arguments.dataset, arguments.job))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = {"mode": arguments.command, "passed": False,
                  "problems": [f"invalid evidence: {type(exc).__name__}: {exc}"], "counts": {}}
    report["inputs"] = ({key: str(getattr(arguments, key)) for key in ("manifest", "output")}
                        if arguments.command == "reference"
                        else {key: str(getattr(arguments, key)) for key in ("dataset", "job", "output")})
    try:
        write_report(arguments.output, report)
    except OSError as exc:
        print(f"cannot write report: {exc}", file=sys.stderr)
        return 2
    for problem in report["problems"]:
        print(problem, file=sys.stderr)
    print(json.dumps({"mode": report["mode"], "passed": report["passed"],
                      "counts": report["counts"] if "counts" in report else
                      {"masks": len(report.get("masks", [])), "frames": len(report.get("mapping", []))},
                      "output": str(arguments.output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
