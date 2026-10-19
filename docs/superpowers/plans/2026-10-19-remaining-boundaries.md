# Remaining Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the five independent remaining sub-projects with real code, tests and evidence: viewport LOD, Cycles native image tiling, opt-in group reframing, streaming true silhouettes, and production-resource acceptance.

**Architecture:** One small module per boundary (`viewport_lod.py`, `render_tiling.py`, `silhouette.py`, `validate_production_resources.py`) plus narrow integration edits in the existing config, UI, export and runtime paths. Each sub-project is committed separately so it can be reviewed and reverted alone.

**Tech Stack:** Python 3.10 UI/unit environment, PySide6 + VisPy, NumPy, trimesh, PyYAML, Blender 4.2.1 + BlenderProc 2.8 in the `bop` environment, pytest/pytest-qt.

**Spec:** `docs/superpowers/specs/2026-10-19-remaining-boundaries-design.md`

## Global Constraints

- Project root `~/rm_2027/blenderproc-ui`; absolute paths for resources and evidence.
- No new dependencies, no network, no conda/Blender/native changes, no reference-tree writes.
- Do not change BOP/mask/GT semantics, stable IDs, the 1e-4 window, existing caps or checkers.
- UI/unit tests: `env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_API=pyside6 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p pytestqt.plugin ...`
- Real renders only on the RTX 3060 Ti through OPTIX/EGL; CPU is geometry/unit-test only.
- Evidence in a fresh `.artifacts/review13-remaining/<run-id>/`, reports appended only.
- Preserve local commits `26a824b` and `c2433b5`; no push, amend or rebase.

## Review Focus

- A million-face mesh must never block the GUI thread or upload full geometry before its LOD.
- A late LOD result after a project/parameter change must not overwrite the new display state.
- Tiling evidence must show the renderer scheduled tiles, not merely that an attribute was set.
- Group reframing must be a no-op for single-group/normal projects and must not touch K, IDs, windows or depth encoding.
- Silhouette union must not turn overlapping components into XOR holes, and invalid meshes must say why.

---

### Task 1: Viewport LOD (S1)

**Files:** Create `src/bproc_ui/viewport_lod.py`, `tests/unit/test_viewport_lod.py`,
`tests/ui/test_viewport_lod.py`; modify `src/bproc_core/config.py`,
`src/bproc_ui/views/scene_view.py`, `views/camera_view.py`, `main_window.py`,
`tools/benchmark_remaining_geometry.py`.
**Interfaces:** `simplify_display_mesh(mesh, target_faces, *, version=...) -> DisplayMesh`;
`ViewportLodController.request(key, mesh, target_faces) -> DisplayMesh|None`;
signals `ready(key, display)`, `failed(key, reason)`, `status(key, text)`.

- [ ] RED: tests for defaults/invalid `ViewportSpec`, small-mesh identity, target bound, colour preservation, cache identity and eviction, stale-generation drop, non-GUI worker, both viewports receiving display meshes.
- [ ] GREEN: implement simplification + controller + config + view plumbing + Chinese control.
- [ ] Verify: `pytest tests/unit/test_viewport_lod.py tests/ui/test_viewport_lod.py -q`; real desktop benchmark (>=1M faces, heartbeat, two screenshots).
- [ ] Commit: `feat: add viewport LOD for large meshes`.

### Task 2: Cycles image tiling (S2)

**Files:** Create `src/bproc_runtime/render_tiling.py`, `tests/unit/test_render_tiling.py`; modify `config.py`, `blender_backend.py`, `run.py` (metadata), `output.py`, `panels/export.py`, `tools/render_demo.py`, `tools/gui_smoke.py`.
**Interfaces:** `NativeTiling(bpy, spec, temp_dir)` with `apply(stage) -> dict`, `restore()`, `metadata`.

- [ ] RED: config range/type validation, apply/read-back, re-apply after segmentation undo, restore on close and exception, metadata values, UI/CLI wiring, fake bpy without Cycles attributes fails loudly.
- [ ] GREEN: implement module and wiring; run real GPU characterization for `<tile`, exact multiple, non-multiple (`1025x769/256`), compare tiled/untiled masks/depth/RGB.
- [ ] Verify: backend/runtime tests + `check_bop.py` per new dataset + tile scheduling log.
- [ ] Commit: `feat: drive native Cycles image tiling`.

### Task 3: Group reframing (S3)

**Files:** Create `tests/unit/test_group_reframe.py`; modify `config.py`, `camera.py`, `scale_groups.py`, `bundle.py`, `tools/export_by_scale.py`, `panels/export.py`, `tests/unit/test_scale_groups.py`, `tests/ui/test_grouped_export.py`.
**Interfaces:** `framing_window(camera, margin)`, `fit_bounds_in_window(bounds, camera, az, el, margin)`,
`random_fit_radius(bounds, camera, margin)`; `ScaleGroup` unchanged externally; grouping.json writer.

- [ ] RED: default off and single-group unchanged; 2 mm/40 mm two groups; fixed 30/30 pixel window; 100 deterministic random directions; non-anchor/offset/non-contiguous IDs; light s/s²; depth-capacity rejection; staging publish leaves no launch.json on late failure.
- [ ] GREEN: implement math + integration + CLI/UI + grouping.json + staging publish.
- [ ] Verify: unit/UI tests; real GPU two-group BOP sample + `check_bop.py` + audit.
- [ ] Commit: `feat: reframe scale groups on demand`.

### Task 4: Streaming silhouettes (S4)

**Files:** Create `src/bproc_core/silhouette.py`, `tests/unit/test_streaming_silhouette.py`; modify `scene.py`, `tests/unit/test_scene_candidates.py`, `tools/benchmark_remaining_geometry.py`, candidate serialization.
**Interfaces:** `stream_coverage(mesh, matrix, pose, camera, grid) -> (mask|None, SilhouetteDiagnostic)`;
`silhouette_report(...) -> dict`; `_TargetShape` gains method/diagnostic.

- [ ] RED: >2M-face hollow fixture keeps hole; repeated/separated/overlapping union; >1M crossings; invalid indices/non-finite/no faces/zero-area diagnostics; U/frame/1e-4 regressions; old caps unchanged.
- [ ] GREEN: implement block scanline union + cache + scene routing + public diagnostics.
- [ ] Verify: unit regressions; benchmark >2M faces (time/memory/hole image, 30 proposals vs high-res coverage).
- [ ] Commit: `feat: stream true silhouettes past legacy caps`.

### Task 5: Production acceptance tool (S5)

**Files:** Create `tools/validate_production_resources.py`, `tests/unit/test_production_validation.py`; modify docs.
**Interfaces:** `run(manifest, destination, num_scenes=None, case=None) -> summary dict`;
`verify(destination) -> summary`.

- [ ] RED: manifest/hash/coverage/duplicate/bad-count failures; destination refusal; success fixture path with fake runner; failure/non-zero; partial; two signals; rerun needs a new directory; fixture never production success.
- [ ] GREEN: implement orchestration reusing `load_project`, `export_grouped_jobs`, `run_job.py --check-only`, the real `check_bop.py` and the audit tool.
- [ ] Verify: fixture tests; bounded real-resource probe (currently blocked: `asserts/` empty, no Haven HDRI) recorded as unverified.
- [ ] Commit: `feat: add production resource acceptance runner`.

### Task 6: Evidence, docs and regression

**Files:** `tools/verify_remaining_evidence.py`, `tests/unit/test_remaining_evidence.py`, `docs/usage.md`, `docs/validation.md`, ignored `report.md` / `report-handoff.md`.
- [ ] RED/GREEN: verifier rejects missing evidence and accepts a complete synthetic index; docs list written/tested/real-scale/limit per sub-project.
- [ ] Run full `tests/unit tests/ui`, real desktop smoke, record new totals and exit codes, `git diff --check`, clean tracked tree.
- [ ] Commit: `docs: record remaining boundary evidence`.

### Status tracking

| Task | Code | Unit/UI | Real-scale evidence | Commit |
| --- | --- | --- | --- | --- |
| S1 LOD | done | 18 passed (14 unit + 4 Qt) | 1,000,000→39,798 faces, 0.226 s, heartbeat 18.26 ms, two GL screenshots | `7e0384d` |
| S2 tiling | done | 132 relevant passed; 14 dedicated | 20 tiles @1025×769/256, 4 @512/256; masks/depth exact, RGB RMSE .0104; 4 checkers PASS | `9ab5447` |
| S3 reframe | done | 80 relevant passed; 12 dedicated | 2mm/40mm two groups, 2 frames each, checkers/audits 0 | `4d1ba37` |
| S4 silhouette | done | 98 relevant passed; 12 dedicated | 2,002,000 faces, 0.96 s cold, 3.8 MiB working, 30/30 high-res | `68d86d3` |
| S5 production | done | 37 relevant passed; 14 dedicated | fixture end-to-end real run completed, verify correctly rejects; **production blocked (no Haven HDRI)** | `aec72cb`,`1f22ec2` |
| Evidence/docs | done | verifier 6 tests + full 656 passed | evidence gate fails only on blocked production | final docs commit |

Status is updated after the matching real command has produced its exit code; a pending row is
never reported as done.
