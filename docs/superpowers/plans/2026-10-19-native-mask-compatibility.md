# Native Mask Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify native uint32 instance-ID compatibility without changing the binary PNG output contract.

**Architecture:** Preserve native segmentation, integer decoding and both existing writers. Add characterization tests, stricter standalone checkers and a bounded read-only audit CLI.

**Tech Stack:** Existing Python, NumPy, Pillow, pytest, BlenderProc 2.8, Blender 4.2.1 and OPTIX/NVIDIA EGL.

**Spec:** `docs/superpowers/specs/2026-10-19-native-mask-compatibility-design.md`

## Global Constraints

- Existing offline GPU-only environment; no installation or modification of native packages.
- Reference scripts/data and historical artifacts are read-only; never execute the destructive reference renderer.
- Binary masks remain 8-bit L PNG, 0/255; BOP depth remains 16-bit with existing millimetre scale.
- No new formats/UI options, sampling changes, push or history rewriting.

## Review Focus

- Adjacent uint32 IDs above float32 exact-integer range must remain distinct (Task 1).
- Multiple transient IDs for one stable object must OR together without renaming files (Task 1).
- A uint16 mask with values 0/255 must not be mistaken for an 8-bit mask (Task 2).
- A BMP named `.png` must fail both real CLI checkers (Task 2).
- Malformed evidence, stale job source or an existing report/symlink must not yield a false pass or overwrite (Task 2).

---

### Task 1: Characterize the existing native contracts

**Files:** `tests/unit/test_backend_masks.py`, `test_rgb_masks.py`, `test_bop_output.py`.

**Interfaces:** Existing `decode_instances`, `BlenderBackend._segmentation`, `RgbMasksWriter.write` and `finalize_bop`; no production API changes.

- [x] Add direct/backend dtype matrices, adjacent uint32 extremes, background/unknown objects and duplicate/order-independent mapping assertions.
- [x] Exercise actual decoding → writer and assert pixels, stable suffixes, IHDR 8/0 and existing rejection contracts.
- [x] Verify native BOP helper delegation and units without executing installation paths.
- [x] Run the tests. These are characterization tests: GREEN on existing production code is expected, not a fabricated RED.

### Task 2: Enforce and audit the established output contract

**Files:** `tests/blender/check_{bop,rgb_masks}.py`, `tests/unit/test_output_checkers.py`, `tools/audit_native_masks.py`, `tests/unit/test_native_mask_audit.py`.

**Interfaces:** Preserve existing checker functions. Add `reference --manifest --output` and `dataset --dataset --job --output` audit CLI modes; exit 0 on success, 1 on bad evidence, 2 on refused/unwritable output.

- [x] Write format-corruption matrices and observe real checker RED: 9 failed, 18 passed, 50 deselected.
- [x] Add only PNG/L/uint8 checks, preserving old geometry, depth and visibility assertions; observe GREEN.
- [x] Check every corruption via real CLI and add positive CLI cases.
- [x] Add audit tests before fixing incomplete-tool behavior: real RED 6 failed, 11 passed (including new per-frame depth-report schema).
- [x] Reject empty/malformed manifests, report bad metadata/shapes, audit all depth frames, create reports exclusively and reject existing/dangling symlinks.
- [x] Run combined tests: 134 passed, exit 0. No dependency on local reference datasets in unit tests.

### Task 3: New GPU evidence, documentation and delivery

**Files:** New ignored evidence under `.artifacts/review12-native-masks/completion-20261019/`; `docs/usage.md`, `docs/validation.md`; ignored local reports.

**Interfaces:** Existing `tools/render_demo.py`, `run_job.py --check-only`, real checkers and the new audit CLI.

- [x] Read the reference manifest and recompute all 13 file hashes and PNG facts; retain reports under fresh names.
- [x] Derive a two-view project from the existing demo, targets 2/7, 4 samples, OPTIX; export RGB/BOP separately to new directories.
- [x] Run both preflights, both GPU jobs and both real checkers; all 8 command exits are 0, each format saves 2/2 frames. Keep raw commands/logs in `commands.json` and `logs/`.
- [x] Audit native output format, object mapping, metadata and packaged-source hashes.
- [x] Correct bit-depth terminology and actual BOP tolerance; document finite evidence and absence of production rendering changes.
- [x] Run complete unit/Qt regression: 567 passed in 36.81 s, exit 0.
- [x] Commit only authorized files: `26a824b test: verify native instance mask compatibility`; no push.
- [x] Bounded independent arbiter: **VERDICT: PASS**, all AC-1–10; independently reran 567 tests (35.33 s, exit 0), both real checkers and in-memory audits. Original full-cycle REVISE remains historical. This checklist/sign-off update is documentation-only.

## Verification commands

```bash
cd /home/kepler/rm_2027/blenderproc-ui
E=.artifacts/review12-native-masks/completion-20261019
.venv/bin/python tools/audit_native_masks.py reference \
  --manifest .artifacts/review12-native-masks/reference-audit.json --output "$E/reference-NEW.json"
.venv/bin/python tools/audit_native_masks.py dataset \
  --dataset "$E/results/native-rgb" --job "$E/rgb-job" --output "$E/rgb-NEW.json"
.venv/bin/python tools/audit_native_masks.py dataset \
  --dataset "$E/results/native-bop" --job "$E/bop-job" --output "$E/bop-NEW.json"
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -p pytestqt.plugin tests/unit tests/ui -q
```

Replace `NEW` with a fresh suffix: audit reports are intentionally never overwritten. The full GPU export/preflight/render command list and exit codes are recorded in `$E/commands.json`; do not rerun its driver into the same directories.
