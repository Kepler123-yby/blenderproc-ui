# Remaining Boundaries: Viewport LOD, Image Tiling, Group Reframing, Streaming Silhouettes, Production Acceptance – Design

Status: implementation design, written before product code (2026-10-19). Evidence run root:
`.artifacts/review13-remaining/20261019-remaining-builder/`. Baseline `c2433b5` (567 tests).

This design fixes the five independent sub-projects requested after the previous rounds. It
records facts verified in the working tree and in the installed Blender 4.2.1 / BlenderProc 2.8
sources; it does not restate the historical reports as current behaviour.

## S1 – Viewport LOD

**Goal.** Both real VisPy viewports (`views/scene_view.py:67-97` `draw_placements`, used by
`SceneView` and by `views/camera_view.py:111-120`) draw a simplified representation of meshes
above a face threshold. Canonical `MeshData` stays untouched: placements, collisions, sampling,
BOP model writing (`bproc_core/assets.py:178`), export and native rendering keep the full mesh.

**Non-goals.** No production geometry decimation, no collision proxy replacement, no material
redesign, no screen-space continuous LOD.

**Decisions.**
- New pure-NumPy vertex-cluster simplification in `src/bproc_ui/viewport_lod.py`: cluster
  vertices on a lattice derived from the mesh extent, keep the mean of each cluster, remap
  faces, drop degenerate and duplicate triangles, average vertex colours (alpha = min). If the
  first lattice still yields more than the target face count, double the cell size and retry,
  bounded (12 rounds). Deterministic and dependency-free.
- Display data lives in `DisplayMesh` (`vertices_m`, `faces`, `vertex_rgba` for the display,
  canonical `bounds_m`), never in `ProjectState.meshes`.
- New `ViewportSpec` on `ProjectConfig`: `lod_mode: auto|full = auto`,
  `lod_threshold_faces: int = 100000`, `lod_target_faces: int = 50000` (both strictly positive
  integers, target <= threshold). Validated in `validate_project`; serialized by the existing
  dataclass codec, so old YAML omitting `viewport` loads the defaults and export snapshots
  contain the block.
- `ViewportLodController` (QObject) owns a QThreadPool task, one in-flight task, latest-pending
  coalescing, an 8-entry / 128 MiB LRU keyed by mesh identity (weak reference), resource
  generation, mesh revision, simplification parameters and an algorithm version. Context
  changes, resource replacement, clearing, parameter changes and window close bump a generation
  and drop late results.
- On `auto` a heavy mesh is drawn as a wireframe box of its canonical bounds while its display
  mesh is computed; the full arrays are never uploaded first. Small meshes (<= threshold) keep
  their original arrays object-for-object.
- Main window owns the controller; `SceneView.set_scene` / `CameraPreview.set_scene` accept a
  display-mesh mapping plus a pending-key set. Framing still uses `placed_content_bounds` on the
  canonical mesh. Chinese combo "自动 LOD / 原始网格" and a status label sit in the existing
  viewport header; `full` appends a load warning.

## S2 – Cycles native image tiling

**Goal.** Use the native Blender 4.2 properties `scene.cycles.use_auto_tile` and
`scene.cycles.tile_size` (verified in
`~/blender/blender-4.2.1-linux-x64/4.2/scripts/addons_core/cycles/properties.py:1032-1042`,
valid range 8–8192) on every real RGB and segmentation render. BlenderProc 2.8 ends in
`bpy.ops.render.render` (`RendererUtility.py:654-729`); there is no second tile knob.

**Non-goals.** Not geometry out-of-core, not streaming object upload, not arbitrary over-VRAM
scenes, not a new output layout. Tiling only reduces the image buffer.

**Decisions.** `RenderSpec.use_tiling: bool = True`, `RenderSpec.tile_size: int = 2048`
(strict int, 8..8192). `src/bproc_runtime/render_tiling.py` saves the original values, applies
the requested ones before `prepare`, before each segmentation and before each RGB pass (undo
can reset scene pointers, so attributes are resolved from the live `bpy.context.scene` each
time), reads back the effective values, and restores the originals on `close` and on exception.
A `render_stats` handler writes `bpy.context.scene.statistics(...)` lines into the task-owned
temporary directory, which is the native execution evidence ("Rendered a/b Tiles"); a pure
attribute read-back alone is not accepted as proof. Metadata (`run_metadata/render_tiling.json`)
records requested/effective values, the capability boundary (`image_buffer_only`) and the log
path. UI adds "图像分块" + tile size under Cycles; `tools/render_demo.py` adds `--tile-size` and
`--no-tiling`, defaulting to the YAML.

## S3 – Group reframing

**Goal.** An explicit opt-in on a genuinely multi-group export recomputes camera distances per
group so each group keeps the same projected framing, and scales point-light distance/energy
with the group's scale. Off by default; a single-group project keeps its scientific config.

**Non-goals.** No model scaling, no moved targets, no K/pixel-size/stable-ID/occlusion-window/
depth-scale changes, no new clip semantics.

**Decisions.** New `ScaleGroupingSpec` on `ProjectConfig`: `reframe: bool = False`,
`margin_fraction: float = 0.10` (finite, `0 <= m < 0.45`). `bproc_core/camera.py` gains the
pixel window `L=-0.5+mW`, `U=W-0.5-mW`, `T=-0.5+mH`, `B=H-0.5-mH`, a corner-fit bisection
against that window plus the existing clip rules, and the random-region closed form
`h = min((cx-L)/fx, (U-cx)/fx, (cy-T)/fy, (B-cy)/fy)`, `fit = R/sin(atan(h))` where `R` is the
largest corner norm of the group envelope around the anchor origin. Fixed cameras keep their
angles/order and get `q_i = max(1, old_radius/fit_ref_i)`, `new = q_i*fit_group_i`. Random
regions get each radius endpoint `max(1, old/fit_ref)*fit_group` (monotone in the old endpoint,
so no interval inversion), same angles/count/RNG. `bproc_core/scale_groups.py` computes both
reference and group `content_bounds` (the same helper already includes full offset ranges),
applies the changes only when `reframe` is true and more than one group exists, records clamps,
and the ratio `s=R_group/R_reference` scales point-light radius endpoints by `s` and energy by
`s²`; HDRI file/strength/rotation are untouched. BOP export rejects when the full group depth
envelope or the largest new camera distance exceeds `65535*depth_scale_mm/1000` m, naming the
group and the required capacity; the runtime uint16 check stays.
`bproc_export/bundle.py` builds every group into private staging directories and publishes the
final job directories only when all groups succeeded. `grouping.json` in each job records the
source project hash, partition, envelopes, reference scale, before/after camera and light
values, clamps and untouched fields. CLI
`--reframe-groups/--no-reframe-groups --framing-margin`; UI checkbox + margin under the size
split, active only when the split actually produces >1 group.

## S4 – Streaming true silhouettes past the old caps

**Goal.** Valid triangle surfaces larger than the old 2M-face / 4M-edge / 1M-crossing limits
still produce a true projected coverage mask with holes, concavities and overlapping components
unioned, using bounded extra memory.

**Non-goals.** No LOD substitute, no raising the old constants, no unbounded topology, no
claiming a 256² estimate is pixel truth.

**Decisions.** `src/bproc_core/silhouette.py` projects triangles in face blocks (default 8192),
computes per-scanline x intervals from the triangle's projected edges and accumulates a
difference array whose `cumsum > 0` is the union of all triangles. Duplicate, separated and
overlapping components therefore add coverage (never XOR); winding and manifoldness are
irrelevant. Work arrays are bounded by a 64 MiB design cap and an 8192-face block; no
`vertices[faces]` for the whole mesh and no global edge table. The 256² grid is the sampler's
existing grid (the window acceptance is still made from the actual render). The dense mask
cache key includes mesh identity (weak reference), matrix, pose, camera and grid, with a byte
cap. Invalid indices, non-finite vertices, no faces, all-degenerate projections and off-camera
projections yield stable reason codes in a public `SilhouetteDiagnostic`
(asset key, method, grid size, faces, degenerate count, fallback reason, `estimate_only`).
`scene.py` routes targets and occluders above `SILHOUETTE_MASK_MAX_FACES` through this path
first; the old topology path and its caps stay for regression and are used only as a fallback
when streaming reports an unsupported input. Hull remains only as an explicit `estimate_only`
fallback. Candidate serialization gains `silhouette_diagnostics` (default empty, old payloads
load empty); runtime frame rows and the UI caption surface it.

## S5 – Production resource acceptance

**Goal.** A reusable offline orchestrator that validates, exports, renders, checks and audits a
manifest of real resources, plus a read-only `verify`.

**Non-goals.** No downloads, no source `render.py`, no reference-tree mutation, no second
render framework, no scans of historical datasets.

**Decisions.** `tools/validate_production_resources.py`:
`run --manifest M --destination D [--num-scenes N] [--case ID]` and `verify --destination D`.
Manifest schema 1 with `resources` (explicit path, role model/occluder/hdri, sha256, source,
`production|fixture` classification) and `cases` (unique id, project YAML, explicit HDRI list,
scene count, seed, grouped/reframe flags). Paths resolve relative to the manifest. Any missing
file, hash mismatch, duplicate id, bad count, uncovered project resource or existing/symlink
destination fails before a task starts. Each (case, HDRI) becomes one deterministic case with
`hdri.randomize=False` so every environment is used, and grouping/reframing flags are applied
through the real export chain. The run executes the exported job's real `--check-only`, the
GPU render, the repository's `tests/blender/check_bop.py`, and the native-mask audit, capturing
full command lines, stdout/stderr, exit codes and durations. Any failure marks the summary
failed and exits non-zero. SIGINT/SIGTERM are forwarded to the child process group; later cases
become `not_started`; exit 130/143. The summary separates proposal acceptance rate from frame
yield, lists requested/written/saved/skipped and resource provenance plus a source snapshot.
Fixture summaries are marked `fixture` and `verify` refuses to call them production success.
Local fact: `~/bproc_project/dataset_rendering/asserts/` is empty, no Haven HDRI exists in the
bounded directories inspected, so the real-resource AC stays unverified and the final verdict
must remain REVISE.

## Cross-cutting

All new fields go through dataclass + validation + old-YAML defaults + round-trip + export
snapshot + real consumer. No new dependency, no network, no native/conda changes. Evidence goes
to a fresh `.artifacts/review13-remaining/<run-id>/`; reports are appended, not rewritten.
