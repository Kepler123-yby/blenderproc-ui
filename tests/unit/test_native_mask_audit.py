"""Native-mask audits use temporary evidence, never the developer's datasets."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from tools import audit_native_masks as audit
from tests.unit.test_output_checkers import valid_bop_output, valid_rgb_masks_output


@pytest.fixture
def reference_manifest(tmp_path):
    source = tmp_path / "render.py"
    source.write_text("# reference fixture\n")
    image = tmp_path / "mask.png"
    Image.fromarray(np.array([[0, 255], [255, 0]], dtype=np.uint8)).save(image)
    manifest = {
        "sources": [{"path": str(source), "sha256": audit.sha256_file(source)}],
        "samples": [{"path": str(image), "sha256": audit.sha256_file(image),
                     "mode": "L", "size": [2, 2], "png_bit_depth": 8,
                     "png_color_type": 0, "unique_values": [0, 255]}],
    }
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(manifest))
    return path


@pytest.fixture
def job_snapshot(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    for package in ("bproc_core", "bproc_runtime"):
        shutil.copytree(audit.REPO_ROOT / "src" / package, job / package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("run_job.py", "render_scene.py"):
        shutil.copyfile(audit.REPO_ROOT / "src/bproc_export/templates" / name, job / name)
    return job


def cli(*args):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, str(Path(audit.__file__)), *map(str, args)],
                          capture_output=True, text=True, env=env)


def test_reference_cli_reports_real_hashes_and_png_facts(reference_manifest, tmp_path):
    output = tmp_path / "report.json"
    result = cli("reference", "--manifest", reference_manifest, "--output", output)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["passed"] and report["counts"] == {"sources": 1, "samples": 1}
    facts = report["facts"]["samples"][0]
    assert (facts["bit_depth"], facts["color_type"], facts["pil_mode"],
            facts["numpy_dtype"], facts["unique_values"]) == (8, 0, "L", "uint8", [0, 255])


@pytest.mark.parametrize("corruption", ["hash", "format", "missing", "empty", "malformed"])
def test_reference_cli_fails_with_a_diagnostic_report(reference_manifest, tmp_path, corruption):
    manifest = json.loads(reference_manifest.read_text())
    if corruption == "hash":
        Path(manifest["sources"][0]["path"]).write_text("changed\n")
    elif corruption == "format":
        path = Path(manifest["samples"][0]["path"])
        Image.fromarray(np.array([[0, 255], [255, 0]], dtype=np.uint8)).save(path, format="BMP")
        manifest["samples"][0]["sha256"] = audit.sha256_file(path)
    elif corruption == "missing":
        Path(manifest["samples"][0]["path"]).unlink()
    elif corruption == "empty":
        manifest = {}
    else:
        reference_manifest.write_text("not json")
    if corruption != "malformed":
        reference_manifest.write_text(json.dumps(manifest))
    output = tmp_path / "failed.json"
    result = cli("reference", "--manifest", reference_manifest, "--output", output)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert not report["passed"] and report["problems"]


@pytest.mark.parametrize("dangling", [False, True])
def test_existing_report_or_symlink_is_never_overwritten(reference_manifest, tmp_path, dangling):
    output = tmp_path / "existing.json"
    if dangling:
        target = tmp_path / "not-created.json"
        output.symlink_to(target)
    else:
        output.write_text("preserve")
    result = cli("reference", "--manifest", reference_manifest, "--output", output)
    assert result.returncode == 2
    if dangling:
        assert output.is_symlink() and not target.exists()
    else:
        assert output.read_text() == "preserve"


@pytest.mark.parametrize("fmt", ["rgb", "bop"])
def test_dataset_cli_checks_suffix_mapping_and_snapshot(request, fmt, job_snapshot, tmp_path):
    root = request.getfixturevalue("valid_rgb_masks_output" if fmt == "rgb" else "valid_bop_output")
    output = tmp_path / "dataset-audit.json"
    result = cli("dataset", "--dataset", root, "--job", job_snapshot, "--output", output)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["passed"] and report["enabled_obj_ids"] == [2, 7]
    assert report["metadata"]["journal_rows"] == 1 and len(report["masks"]) == 2
    mapping = report["mapping"][0]
    key = "suffix_to_obj_id" if fmt == "rgb" else "gt_index_to_obj_id"
    assert mapping[key] == ({"000002": 2, "000007": 7} if fmt == "rgb" else {"000000": 2, "000001": 7})
    if fmt == "bop":
        assert report["depth"]["frames"][0]["bit_depth"] == 16


@pytest.mark.parametrize("corruption", ["changed", "missing", "extra"])
def test_dataset_cli_rejects_mismatched_source_snapshot(valid_rgb_masks_output, job_snapshot, tmp_path, corruption):
    source = job_snapshot / "bproc_core/masks.py"
    if corruption == "changed":
        source.write_text("# stale copy\n")
    elif corruption == "missing":
        source.unlink()
    else:
        (job_snapshot / "bproc_core/unexpected.py").write_text("# not exported\n")
    output = tmp_path / "snapshot-failed.json"
    result = cli("dataset", "--dataset", valid_rgb_masks_output, "--job", job_snapshot, "--output", output)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert not report["passed"] and any("bproc_core" in p for p in report["problems"])


@pytest.mark.parametrize("corruption", ["shape", "journal", "manifest", "status"])
def test_dataset_cli_reports_bad_evidence(valid_rgb_masks_output, job_snapshot, tmp_path, corruption):
    root = valid_rgb_masks_output
    if corruption == "shape":
        Image.fromarray(np.zeros((3, 5), dtype=np.uint8)).save(root / "mask/000000_000002.png")
    elif corruption == "journal":
        (root / "run_metadata/frames.jsonl").write_text("[]\n")
    elif corruption == "manifest":
        path = root / "run_metadata/manifest.json"
        data = json.loads(path.read_text())
        data["frames"] = []
        path.write_text(json.dumps(data))
    else:
        path = root / "run_metadata/status.json"
        data = json.loads(path.read_text())
        data["saved"] = 9
        path.write_text(json.dumps(data))
    output = tmp_path / "failed-dataset.json"
    result = cli("dataset", "--dataset", root, "--job", job_snapshot, "--output", output)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert not report["passed"] and report["problems"]
