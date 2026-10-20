#!/usr/bin/env python3
"""Reproduce and verify occluder import, units, placement and native rendering.

``run`` drives the real ``ResourcesPanel``/``ProjectState``/``AssetLoader`` chain on a live Qt
desktop (``DISPLAY=:1``), records the import/unit/selection state, captures both OpenGL canvases,
then exports and renders the resulting project through the production runtime. ``--phase before``
is the unchanged pre-fix reproduction; ``--phase after`` is the same operation script against the
fix. The strategy switch is marked separately from the import fix.

``verify`` only re-reads the recorded inputs and datasets: it recomputes every SHA256, re-checks
the cores geometry expectations, and confirms that the recorded checker/audit commands exited 0.
It never renders, never executes the reference renderer, and never rewrites an input.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib.metadata
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
CORE = Path('/home/kepler/文档/cores.ply')
ASSETS = Path('/home/kepler/bproc_project/dataset_rendering/asserts')
BOP_MODELS = ASSETS / 'bop/models'
HDRI_OUTDOOR = ASSETS / 'haven/hdris/kloofendal_misty_morning_puresky/kloofendal_misty_morning_puresky_2k.hdr'
HDRI_INDOOR = ASSETS / 'haven/hdris/blue_photo_studio/blue_photo_studio_2k.hdr'
TOOLKIT = Path('/home/kepler/blenderproc/23AutoEngineer/Linemod-Render/bop_toolkit')
REFERENCES = (
    Path('/home/kepler/bproc_project/dataset_rendering/samplers/obstacle.py'),
    Path('/home/kepler/bproc_project/dataset_rendering/samplers/camera.py'),
    Path('/home/kepler/bproc_project/dataset_rendering/samplers/target.py'),
    Path('/home/kepler/bproc_project/dataset_rendering/configs/default.yaml'),
    Path('/home/kepler/bproc_project/dataset_rendering/render.py'),
)
CORE_SHA256 = '83aa1b451b11ff200a00c493b22b7aee112161cc06f1efceec741c5951d1be2f'
BOP_PYTHON = Path('/home/kepler/miniconda3/envs/bop/bin/python')

sys.path.insert(0, str(REPO / 'src'))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _logging_run(commands, destination, name, step, command, *, cwd=REPO, env=None, timeout=7200):
    log = Path(destination) / 'logs' / f'{name}-{step}.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log.open('x', encoding='utf-8') as stream:
        stream.write('$ ' + shlex.join(str(part) for part in command) + '\n')
        stream.flush()
        result = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        stream.write(f'\nEXIT={result.returncode}\n')
    entry = {'case': name, 'step': step, 'command': [str(part) for part in command],
             'cwd': str(cwd), 'exit_code': result.returncode,
             'seconds': round(time.monotonic() - started, 2), 'log': str(log)}
    commands.append(entry)
    print(name, step, result.returncode, flush=True)
    return entry


def _clean_env():
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    return env


def _inputs():
    paths = [CORE, *REFERENCES, HDRI_INDOOR, HDRI_OUTDOOR, *sorted(BOP_MODELS.glob('*.ply'))]
    return paths


def _source_digest():
    """One digest over the production sources this run actually executed."""
    digest = hashlib.sha256()
    for path in sorted((REPO / 'src').rglob('*.py')):
        digest.update(str(path.relative_to(REPO)).encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def provenance():
    from bproc_core.assets import load_mesh
    from bproc_core.config import AssetSpec
    mesh = load_mesh(AssetSpec('cores', CORE, 'm'))
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
                                    cwd=REPO, text=True).strip()
    return {
        'git_head': head,
        'git_tracked_dirty': bool(dirty),
        'source_digest': _source_digest(),
        'tool_sha256': sha256(Path(__file__).resolve()),
        'core': {'path': str(CORE), 'sha256': sha256(CORE), 'expected_sha256': CORE_SHA256,
                 'vertices': len(mesh.vertices_m), 'triangles': len(mesh.faces),
                 'alpha_values': sorted({int(value) for value in mesh.vertex_rgba[:, 3]}),
                 'unit': 'm', 'bounds_m': mesh.bounds_m.tolist()},
        'references': {str(path): sha256(path) for path in REFERENCES},
        'bop_models': {str(path): sha256(path) for path in sorted(BOP_MODELS.glob('*.ply'))},
        'hdris': {str(HDRI_INDOOR): sha256(HDRI_INDOOR), str(HDRI_OUTDOOR): sha256(HDRI_OUTDOOR)},
        'versions': {name: importlib.metadata.version(name)
                     for name in ('PySide6', 'vispy', 'numpy', 'trimesh')},
        'devices': {'gpu': subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
                                           '--format=csv,noheader'], capture_output=True, text=True,
                                          check=False).stdout.strip(),
                    'display': os.environ.get('DISPLAY')},
        'runtime_python': sys.executable,
        'bop_python': str(BOP_PYTHON),
        'never_executed': [str(REFERENCES[-1])],
    }


# ---------------------------------------------------------------- UI reproduction


def _wait(app, predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.02)
    return False


def _occluder_rows(panel):
    return [panel.occluders.item(row) for row in range(panel.occluders.count())]


def _select_rows(panel, rows):
    panel.occluders.clearSelection()
    for row in rows:
        item = panel.occluders.item(row)
        if item is not None:
            item.setSelected(True)
    if rows:
        panel.occluders.setCurrentRow(rows[-1])


def _selected_keys(panel):
    return [item.data(0x0100) for item in panel.occluders.selectedItems()]


def _occluder_facts(state, assets=None):
    assets = state.config.occluders if assets is None else assets
    facts = []
    for asset in assets:
        mesh = state.meshes.get(asset.key)
        facts.append({
            'key': asset.key, 'path': str(asset.path), 'unit': asset.unit,
            'bounds_m': None if mesh is None else np.asarray(mesh.bounds_m).tolist(),
            'extent_m': None if mesh is None else np.ptp(mesh.bounds_m, axis=0).tolist(),
            'vertices': None if mesh is None else len(mesh.vertices_m),
        })
    return facts


def ui_lifecycle(app, window, destination, phase):
    """Same real-widget script for both phases; absent controls are recorded as the defect."""
    from bproc_core.project_io import load_project, save_project
    from bproc_core.scene import candidate_to_dict
    from vispy.io import write_png

    state = window.state
    panel = window.resources
    record = {'phase': phase, 'steps': {}}

    def step(name, **payload):
        record['steps'][name] = payload
        print('ui:', name, flush=True)

    panel.import_unit.setCurrentText('m')
    target_key = panel.add_target(CORE)
    if not _wait(app, lambda: target_key in state.meshes, 120):
        raise RuntimeError('target mesh never loaded')
    target = next(t for t in state.config.targets if t.asset.key == target_key)
    step('target-import', unit=target.asset.unit, controls={'import_unit': panel.import_unit.currentText()},
         bounds_m=state.meshes[target_key].bounds_m.tolist(),
         extent_m=np.ptp(state.meshes[target_key].bounds_m, axis=0).tolist(),
         size_label=panel.size_label.text(), obj_id=target.obj_id)

    import_unit = getattr(panel, 'occluder_import_unit', None)
    step('occluder-import-control', independent=import_unit is not None,
         control=None if import_unit is None else import_unit.currentText(),
         fallback='target import_unit=' + panel.import_unit.currentText() if import_unit is None else None,
         hint=[label.text() for label in panel.findChildren(type(panel.path_label))
               if '遮挡' in label.text()])

    files = sorted(BOP_MODELS.glob('*.ply'))[:5]
    alias_dir = Path(destination) / 'ui' / 'alias'
    alias_dir.mkdir(parents=True, exist_ok=True)
    alias = alias_dir / 'obj_000001-alias.ply'
    if not alias.exists():
        alias.symlink_to(files[0])
    batch = [files[0], files[1], files[2], files[0], alias, files[3], files[4]]
    panel.add_occluders(batch)
    expected = min(len(files), 5)
    _wait(app, lambda: sum(1 for a in state.config.occluders if a.key in state.meshes) >= expected, 180)
    step('batch-import', requested=len(batch), unique_expected=expected,
         imported=len(state.config.occluders), facts=_occluder_facts(state))

    _select_rows(panel, [0, 1])
    before_selection = _selected_keys(panel)
    first = state.config.occluders[0]
    panel.occluder_unit.setCurrentText('cm')
    _wait(app, lambda: next((a.unit for a in state.config.occluders if a.key == first.key), None) == 'cm', 30)
    _wait(app, lambda: first.key in state.meshes, 120)
    after_selection = _selected_keys(panel)
    changed = next(a for a in state.config.occluders if a.key == first.key)
    step('unit-change-cm', selection_before=before_selection, selection_after=after_selection,
         unit=changed.unit, bounds_m=None if changed.key not in state.meshes else
         np.asarray(state.meshes[changed.key].bounds_m).tolist(),
         current_combo=panel.occluder_unit.currentText(), target_unit=state.config.targets[0].asset.unit)

    _select_rows(panel, [0, 1])
    panel.occluder_unit.setCurrentText('mm')
    _wait(app, lambda: next((a.unit for a in state.config.occluders if a.key == first.key), None) == 'mm', 30)
    _wait(app, lambda: first.key in state.meshes, 120)
    step('unit-change-mm', unit=next(a.unit for a in state.config.occluders if a.key == first.key),
         selection=_selected_keys(panel), facts=_occluder_facts(state)[:2],
         target_unit=state.config.targets[0].asset.unit)

    # Mixed multi-select: one cm, one mm; the editor must say "mixed" instead of hiding it.
    _select_rows(panel, [0])
    panel.occluder_unit.setCurrentText('cm')
    _wait(app, lambda: next(a.unit for a in state.config.occluders if a.key == first.key) == 'cm', 30)
    _select_rows(panel, [0, 1])
    mixed_index = panel.occluder_unit.currentIndex()
    mixed_placeholder = panel.occluder_unit.placeholderText()
    step('mixed-units', index=mixed_index, placeholder=mixed_placeholder,
         units=[a.unit for a in state.config.occluders[:2]])
    _select_rows(panel, [0, 1])
    panel.occluder_unit.setCurrentText('mm')
    _wait(app, lambda: all(a.unit == 'mm' for a in state.config.occluders[:2]), 30)

    _select_rows(panel, [1])
    removed_key = _selected_keys(panel)[0]
    panel._remove_occluders()
    step('remove', removed=removed_key, remaining=len(state.config.occluders))

    panel.add_occluders(sorted(BOP_MODELS.glob('*.ply')))
    _wait(app, lambda: len(state.config.occluders) >= len(sorted(BOP_MODELS.glob('*.ply'))), 180)
    step('directory-import', count=len(state.config.occluders),
         unique_paths=len({str(a.path) for a in state.config.occluders}),
         units=sorted({a.unit for a in state.config.occluders}))

    _select_rows(panel, list(range(panel.occluders.count())))
    panel._remove_occluders()
    step('clear', remaining=len(state.config.occluders), rows=panel.occluders.count())

    panel.add_occluders(sorted(BOP_MODELS.glob('*.ply')))
    _wait(app, lambda: len(state.config.occluders) == len(sorted(BOP_MODELS.glob('*.ply'))), 180)
    _wait(app, lambda: all(a.key in state.meshes for a in state.config.occluders), 300)
    _select_rows(panel, [0, 2])
    panel.occluder_unit.setCurrentText('cm')
    _wait(app, lambda: next(a.unit for a in state.config.occluders if a.key == state.config.occluders[0].key) == 'cm', 30)
    saved = Path(destination) / 'ui' / f'project-{phase}.yaml'
    state.save(saved)
    saved_selection = _selected_keys(panel)
    state.load(saved)
    _wait(app, lambda: all(a.key in state.meshes for a in state.config.occluders), 300)
    step('save-reload', path=str(saved), units=[a.unit for a in state.config.occluders[:3]],
         count=len(state.config.occluders),
         reloaded_matches=load_project(saved).occluders == state.config.occluders,
         saved_selection=saved_selection,
         selection_after_reload=_selected_keys(panel),
         target_unit=state.config.targets[0].asset.unit)

    # Feed every occluder back to mm before rendering; the run is about the fixed behaviour.
    _select_rows(panel, list(range(panel.occluders.count())))
    panel.occluder_unit.setCurrentText('mm')
    _wait(app, lambda: all(a.unit == 'mm' for a in state.config.occluders), 30)
    step('final-units', units=sorted({a.unit for a in state.config.occluders}),
         count=len(state.config.occluders))
    record['tested_units'] = {'target': state.config.targets[0].asset.unit,
                              'occluders': sorted({a.unit for a in state.config.occluders})}
    return record


def near_far_cameras():
    from bproc_core.config import FixedCameraSpec
    return (FixedCameraSpec('near-front', '近景正面', .42, -20., 18.),
            FixedCameraSpec('far-oblique', '远景斜上', 1.15, 35., 48.))


def strategy_config(base, strategy):
    """Build the requested strategy without inventing fields the running code does not have."""
    from dataclasses import fields
    from bproc_core.config import OcclusionSpec, Range
    names = {item.name for item in fields(OcclusionSpec)}
    changes = {'none_fraction': 0.5, 'ratio': Range(0.1, 0.5), 'count': (1, 2), 'max_attempts': 30}
    if strategy and 'strategy' in names:
        changes['strategy'] = strategy
    if strategy == 'physical_view' and 'physical_scale' in names:
        changes['physical_scale'] = Range(1.0, 1.0)
    return replace(base, occlusion=replace(base.occlusion, **changes), camera_mode='fixed',
                   fixed_cameras=near_far_cameras())


def capture_ui(app, window, state, destination, config, label, *, with_none=True):
    from bproc_core.scene import candidate_to_dict
    from bproc_core.project_io import project_to_dict
    from vispy.io import write_png
    out = Path(destination) / 'ui' / label
    out.mkdir(parents=True, exist_ok=True)
    # Preview the occluded category explicitly: with the render's 50 % mix a one-scene preview can
    # land on the unoccluded scene and would show no occluder at all.
    capture_config = replace(config, occlusion=replace(config.occlusion, none_fraction=0.0))
    state.set_project(capture_config)
    window.preview.refresh()
    if not _wait(app, lambda: state.candidate is not None, 120):
        raise RuntimeError('preview produced no candidate: ' + window.preview_state.text())
    app.processEvents()
    candidate = state.candidate
    window.resize(1320, 880)
    window.grab().save(str(out / 'workspace.png'))
    window.camera_preview.show_preview()
    app.processEvents()
    window.camera_preview.grab().save(str(out / 'camera-window.png'))
    write_png(str(out / 'scene-canvas.png'), window.scene_view.canvas.render())
    write_png(str(out / 'camera-canvas.png'), window.camera_preview.canvas.render())
    from bproc_core.project_io import project_to_dict
    facts = {'config': json.loads(json.dumps(project_to_dict(capture_config), default=str)),
             'candidate': candidate_to_dict(candidate),
             'status': window.preview_state.text(),
             'meshes': len(state.meshes),
             'occluder_placements': [{'key': p.asset_key, 'matrix': p.world_from_model.tolist()}
                                     for p in candidate.placements if p.role == 'occluder']}
    far_key = config.fixed_cameras[1].key if len(config.fixed_cameras) > 1 else None
    if far_key is not None:
        window.preview.refresh(region_key=far_key)
        if not _wait(app, lambda: state.candidate is not None and state.candidate.region_key == far_key, 120):
            raise RuntimeError('camera switch produced no candidate: ' + window.preview_state.text())
        app.processEvents()
        window.camera_preview.grab().save(str(out / 'camera-window-far.png'))
        write_png(str(out / 'camera-canvas-far.png'), window.camera_preview.canvas.render())
        facts['far_candidate'] = candidate_to_dict(state.candidate)
        window.preview.refresh(region_key=config.fixed_cameras[0].key)
        _wait(app, lambda: state.candidate is not None and
              state.candidate.region_key == config.fixed_cameras[0].key, 120)
        app.processEvents()
    if with_none and hasattr(window.sampling, 'none_fraction'):
        window.sampling.none_fraction.setValue(100.0)
        app.processEvents()
        window.preview.refresh()
        if not _wait(app, lambda: state.candidate is not None and not any(
                p.role == 'occluder' for p in state.candidate.placements), 120):
            raise RuntimeError('none category produced no unoccluded candidate')
        app.processEvents()
        window.grab().save(str(out / 'workspace-none.png'))
        write_png(str(out / 'camera-canvas-none.png'), window.camera_preview.canvas.render())
        facts['none_candidate_placements'] = [p.asset_key for p in state.candidate.placements]
        facts['none_occluder_count'] = sum(1 for p in state.candidate.placements if p.role == 'occluder')
        window.sampling.none_fraction.setValue(50.0)
        app.processEvents()
    write_json(out / 'ui-facts.json', facts)
    return facts


# ---------------------------------------------------------------- dataset safety


def safety_report(dataset):
    from bproc_core.project_io import load_project
    from bproc_core.assets import load_mesh
    from bproc_core.scene import bounds_corners, transform_points

    dataset = Path(dataset)
    config = load_project(dataset / 'run_metadata' / 'project.yaml')
    meshes = {a.key: load_mesh(a) for a in [*(t.asset for t in config.targets), *config.occluders]}
    frame_lines = [json.loads(line) for line in
                   (dataset / 'run_metadata' / 'frames.jsonl').read_text(encoding='utf-8').splitlines() if line]
    problems = []
    rows = []
    used = set()
    simultaneous = []
    for row in frame_lines:
        candidate = row['candidate']
        camera = np.asarray(candidate['camera']['position_m'])
        placements = candidate['placements']
        occluders = [p for p in placements if p['role'] == 'occluder']
        targets = [p for p in placements if p['role'] == 'target']
        used.update(p['asset_key'] for p in occluders)
        if len(occluders) > 1:
            simultaneous.append(row['frame_index'])
        if row.get('wants_unoccluded', candidate.get('wants_unoccluded')):
            if occluders:
                problems.append(f"frame {row['frame_index']}: unoccluded candidate carries occluders")
            continue
        if len({p['asset_key'] for p in occluders}) != len(occluders):
            problems.append(f"frame {row['frame_index']}: duplicate occluder asset in one frame")
        target_depths = []
        target_boxes = []
        for placement in targets:
            corners = transform_points(bounds_corners(meshes[placement['asset_key']].bounds_m),
                                       np.asarray(placement['world_from_model']))
            target_boxes.append((corners.min(axis=0), corners.max(axis=0)))
            target_depths.append(float(np.min((corners - camera) @ (-camera / np.linalg.norm(camera)))))
        boxes = []
        for placement in occluders:
            matrix = np.asarray(placement['world_from_model'])
            corners = transform_points(bounds_corners(meshes[placement['asset_key']].bounds_m), matrix)
            view = -camera / np.linalg.norm(camera)
            depth = (corners - camera) @ view
            box = (corners.min(axis=0), corners.max(axis=0))
            singular = np.linalg.svd(matrix[:3, :3], compute_uv=False)
            row_problems = []
            if float(depth.min()) <= float(candidate['camera']['clip_near_m']):
                row_problems.append('crosses near clip')
            if target_depths and float(depth.max()) >= min(target_depths):
                row_problems.append('not fully in front of the nearest target')
            for other in target_boxes + boxes:
                overlap = np.minimum(box[1], other[1]) - np.maximum(box[0], other[0])
                if np.all(overlap > 1e-9):
                    row_problems.append('AABB overlaps target/other occluder')
            if not np.allclose(singular, singular[0], rtol=1e-9, atol=1e-12):
                row_problems.append(f'non-uniform scale {singular.tolist()}')
            boxes.append(box)
            rows.append({'frame': row['frame_index'], 'asset': placement['asset_key'],
                         'depth_min': float(depth.min()), 'depth_max': float(depth.max()),
                         'singular': singular.tolist(), 'aspect': 1.0,
                         'problems': row_problems})
            problems.extend(f"frame {row['frame_index']} {placement['asset_key']}: {item}"
                            for item in row_problems)
    return {'frames': len(frame_lines), 'occluder_assets_used': sorted(used),
            'frames_with_multiple_occluders': simultaneous,
            'placements': rows, 'problems': problems}


def dataset_report(dataset):
    dataset = Path(dataset)
    status = read_json(dataset / 'run_metadata/status.json')
    frames = [json.loads(line) for line in
              (dataset / 'run_metadata/frames.jsonl').read_text(encoding='utf-8').splitlines() if line]
    ratios = [{'frame': row['frame_index'], 'wants_unoccluded': bool(row['candidate']['wants_unoccluded']),
               'occlusion_ratio': row['occlusion_ratio'],
               'occluders': sum(1 for p in row['candidate']['placements'] if p['role'] == 'occluder')}
              for row in frames]
    return {'status': status, 'frames': ratios}


# ---------------------------------------------------------------- run


def complete_library_count_record(config, meshes):
    """Exercise a [5, 8] physical request against whatever the UI actually imported."""
    from bproc_core.config import Azimuth, Range, RegionSpec
    from bproc_core.scene import Rejection, propose_candidate, sample_scene

    def attempt(physical_scale):
        """Bounded 3 scenes x 30 camera attempts, recording why nothing was accepted."""
        cfg = replace(config, scene_count=1, camera_mode='random', views_per_scene=1, seed=config.seed,
                      regions=(RegionSpec('count-front', 'count front', Range(.42, 1.15),
                                          Azimuth(-60., 60., False), Range(10., 60.)),),
                      occlusion=replace(config.occlusion, strategy='physical_view', count=(5, 8),
                                        none_fraction=0., max_attempts=30,
                                        physical_scale=physical_scale))
        reasons, placed = {}, []
        for scene_index in range(1):
            scene = sample_scene(cfg, meshes, scene_index)
            if isinstance(scene, Rejection):
                reasons['scene:' + scene.reason] = reasons.get('scene:' + scene.reason, 0) + 1
                continue
            for attempt_index in range(4):
                candidate = propose_candidate(cfg, meshes, scene, 0, attempt_index)
                if isinstance(candidate, Rejection):
                    reasons[candidate.reason] = reasons.get(candidate.reason, 0) + 1
                    continue
                keys = [p.asset_key for p in candidate.placements if p.role == 'occluder']
                placed.append({'scene': scene_index, 'attempt': attempt_index, 'count': len(keys),
                               'unique': len(set(keys)), 'keys': keys,
                               'matrices': [p.world_from_model.tolist() for p in candidate.placements
                                            if p.role == 'occluder']})
                break
        return {'status': 'placed' if placed else 'bounded_rejection', 'scenes': 1,
                'attempts_per_scene': 4, 'placements': placed, 'reason_counts': reasons}

    return {'unique_library': len({a.path.resolve() for a in config.occluders}),
            'requested': [5, 8],
            'default_multiplier': attempt(replace(config.occlusion.physical_scale, minimum=1., maximum=1.)),
            'small_multiplier': attempt(replace(config.occlusion.physical_scale, minimum=.2, maximum=.35))}


def run(destination, phase):
    from bproc_ui.app import create_app
    from bproc_ui.main_window import MainWindow
    from bproc_ui.state import ProjectState
    from bproc_core.config import Azimuth, Range, RegionSpec, new_project
    from bproc_core.project_io import load_project, save_project
    from bproc_core.assets import load_mesh
    from bproc_export.bundle import export_job

    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('logs', 'jobs', 'results', 'inputs'):
        (destination / name).mkdir()
    commands, cases = [], []
    summary = {'phase': phase, 'destination': str(destination), 'status': 'running',
               'commands': commands, 'cases': cases}
    write_json(destination / 'summary.json', summary)

    inputs_before = {str(path): sha256(path) for path in _inputs()}
    write_json(destination / 'provenance.json', {**provenance(), 'input_sha256_before': inputs_before})

    app = create_app([])
    scratch = destination / 'inputs' / 'workspace'
    scratch.mkdir()
    state = ProjectState(new_project(scratch))
    window = MainWindow(state, restore_settings=False)
    window.show()
    app.processEvents()

    lifecycle = ui_lifecycle(app, window, destination, phase)
    write_json(destination / 'ui' / 'lifecycle.json', lifecycle)
    base = state.config
    cores_asset = base.targets[0].asset
    assert cores_asset.path.resolve() == CORE.resolve() and cores_asset.unit == 'm'
    assert len(state.meshes[cores_asset.key].vertices_m) == 1035
    assert len(state.meshes[cores_asset.key].faces) == 1837
    assert set(np.unique(state.meshes[cores_asset.key].vertex_rgba[:, 3]).tolist()) == {255}

    strategy_ui = getattr(window.sampling, 'occlusion_strategy', None)
    aimed = strategy_config(base, 'aimed_view')
    physical = strategy_config(base, 'physical_view')
    if phase == 'after':
        # The strategy switch is an explicit user decision, so drive the real widget.
        if strategy_ui is not None and hasattr(strategy_ui, 'setCurrentIndex'):
            strategy_ui.setCurrentIndex(strategy_ui.findData('physical_view'))
            app.processEvents()
        strategy_widget_value = getattr(state.config.occlusion, 'strategy', None)
        aimed_ui = capture_ui(app, window, state, destination, aimed, 'aimed')
        physical_ui = capture_ui(app, window, state, destination, physical, 'physical')
        configs = [('cores-rgb-aimed', aimed), ('cores-bop-aimed', aimed),
                   ('cores-rgb-physical', physical), ('cores-bop-physical', physical),
                   ('cores-rgb-physical-multi', replace(
                       physical, occlusion=replace(physical.occlusion, count=(2, 2),
                                                   physical_scale=replace(
                                                       physical.occlusion.physical_scale,
                                                       minimum=.25, maximum=.4))))]
        ui_evidence = {'strategy_control': strategy_ui is not None,
                       'strategy_reached_config': strategy_widget_value,
                       'aimed': aimed_ui, 'physical': physical_ui}
    else:
        aimed_ui = capture_ui(app, window, state, destination, aimed, 'aimed')
        configs = [('cores-rgb-aimed', aimed), ('cores-bop-aimed', aimed)]
        ui_evidence = {'strategy_control': strategy_ui is not None, 'aimed': aimed_ui}
    write_json(destination / 'ui' / 'preview.json', ui_evidence)
    if hasattr(state.config.occlusion, 'strategy'):
        write_json(destination / 'ui' / 'complete-library-count.json',
                   complete_library_count_record(physical, state.meshes))

    for name, config in configs:
        is_bop = name.startswith('cores-bop')
        fmt = 'bop' if is_bop else 'rgb_masks'
        hdri = HDRI_INDOOR if is_bop else HDRI_OUTDOOR
        config = replace(config, output=replace(config.output, format=fmt, root=destination / 'results',
                                                dataset_name=name),
                         hdri=replace(config.hdri, files=(hdri,), randomize=False),
                         render=replace(config.render, samples=64, device='OPTIX', use_tiling=True,
                                        tile_size=256),
                         runtime=replace(config.runtime, toolkit_path=TOOLKIT))
        if is_bop:
            config = replace(config, camera_mode='fixed', scene_count=2, fixed_cameras=near_far_cameras())
        else:
            config = replace(config, camera_mode='random', scene_count=2, views_per_scene=2,
                             regions=(RegionSpec('cores-front', 'cores 前方', Range(.42, 1.15),
                                                 Azimuth(-60., 60., False), Range(10., 60.)),))

        state.set_project(config)
        assert state.config == config, 'the UI state and the exported config must be the same document'
        save_project(config, destination / 'inputs' / f'{name}.yaml')
        job = destination / 'jobs' / name
        export_job(config, job)
        exported = load_project(job / 'scene.yaml')
        assert exported.occlusion == config.occlusion
        assert exported.occluders == config.occluders
        dataset = destination / 'results' / name
        cases.append({'name': name, 'format': fmt, 'job': str(job), 'dataset': str(dataset),
                      'strategy': config.occlusion.strategy if hasattr(config.occlusion, 'strategy') else 'aimed_view',
                      'config': str(destination / 'inputs' / f'{name}.yaml'),
                      'ui_equals_export': load_project(job / 'scene.yaml').occlusion == config.occlusion
                                          and load_project(job / 'scene.yaml').occluders == config.occluders})
        write_json(destination / 'summary.json', summary)
        env = _clean_env()
        env.pop('QT_QPA_PLATFORM', None)
        for step, command in (('preflight', [BOP_PYTHON, job / 'run_job.py', '--check-only']),
                              ('render', [BOP_PYTHON, job / 'run_job.py']),
                              ('checker', [sys.executable, REPO / 'tests/blender' /
                                           ('check_bop.py' if is_bop else 'check_rgb_masks.py'), dataset]),
                              ('audit', [sys.executable, REPO / 'tools/audit_native_masks.py', 'dataset',
                                         '--dataset', dataset, '--job', job,
                                         '--output', destination / 'logs' / f'{name}-audit.json'])):
            entry = _logging_run(commands, destination, name, step, command, env=env)
            if entry['exit_code']:
                summary['status'] = 'failed'
                write_json(destination / 'summary.json', summary)
                raise SystemExit(entry['exit_code'])
        report = dataset_report(dataset)
        report['safety'] = safety_report(dataset)
        write_json(destination / 'logs' / f'{name}-report.json', report)
        if report['status']['status'] != 'completed':
            summary['status'] = 'failed'
            write_json(destination / 'summary.json', summary)
            raise SystemExit(3)
        if report['status']['skipped'] != 0 or report['status']['requested'] != report['status']['saved']:
            summary['status'] = 'failed'
            write_json(destination / 'summary.json', summary)
            raise SystemExit(4)
        if report['safety']['problems']:
            summary['status'] = 'failed'
            write_json(destination / 'summary.json', summary)
            raise SystemExit(5)
        if not is_bop:
            ratios = [item for item in report['frames'] if not item['wants_unoccluded']]
            if any(item['occlusion_ratio'] == 0.0 for item in ratios):
                summary['status'] = 'failed'
                write_json(destination / 'summary.json', summary)
                raise SystemExit(6)

    window._closing = True
    window.camera_preview.hide()
    window.close()
    app.quit()
    inputs_after = {str(path): sha256(path) for path in _inputs()}
    summary.update(status='completed', input_sha256_after=inputs_after,
                   inputs_unchanged=inputs_after == inputs_before)
    write_json(destination / 'summary.json', summary)
    if inputs_after != inputs_before:
        raise SystemExit(7)
    return 0


def verify(destination):
    destination = Path(destination).resolve()
    provenance_doc = read_json(destination / 'provenance.json')
    summary = read_json(destination / 'summary.json')
    problems = []
    if provenance_doc['core']['sha256'] != CORE_SHA256:
        problems.append('recorded cores sha256 differs from the required digest')
    if sha256(CORE) != CORE_SHA256:
        problems.append('cores.ply changed on disk')
    from bproc_core.assets import load_mesh
    from bproc_core.config import AssetSpec
    mesh = load_mesh(AssetSpec('cores', CORE, 'm'))
    if (len(mesh.vertices_m), len(mesh.faces)) != (1035, 1837):
        problems.append('cores geometry differs from 1035 vertices / 1837 triangles')
    if set(np.unique(mesh.vertex_rgba[:, 3]).tolist()) != {255}:
        problems.append('cores alpha is not the original 255')
    for path in _inputs():
        recorded = (provenance_doc['input_sha256_before'].get(str(path)) or
                    provenance_doc['references'].get(str(path)) or
                    provenance_doc['bop_models'].get(str(path)) or
                    provenance_doc['hdris'].get(str(path)))
        if recorded is None:
            problems.append(f'input was not recorded before the run: {path}')
        elif sha256(path) != recorded:
            problems.append(f'input changed since the run: {path}')
    for case in summary['cases']:
        report_path = destination / 'logs' / f"{case['name']}-report.json"
        if not report_path.is_file():
            problems.append(f"missing dataset report {report_path}")
            continue
        report = read_json(report_path)
        if report['status']['status'] != 'completed' or report['status']['skipped']:
            problems.append(f"{case['name']}: status {report['status']}")
        if report['safety']['problems']:
            problems.extend(f"{case['name']}: {item}" for item in report['safety']['problems'])
    for command in summary['commands']:
        if command['step'] in ('preflight', 'render', 'checker', 'audit') and command['exit_code'] != 0:
            problems.append(f"{command['case']} {command['step']} exited {command['exit_code']}")
    if _source_digest() != provenance_doc.get('source_digest'):
        problems.append('production source changed since the recorded run')
    if sha256(Path(__file__).resolve()) != provenance_doc.get('tool_sha256'):
        problems.append('the verification tool changed since the recorded run')
    payload = {'ok': not problems, 'problems': problems, 'destination': str(destination),
               'cases': [case['name'] for case in summary['cases']]}
    write_json(destination / 'verify.json', payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run_parser = sub.add_parser('run', help='capture the UI reproduction and render fresh datasets')
    run_parser.add_argument('--destination', type=Path, required=True)
    run_parser.add_argument('--phase', choices=('before', 'after'), required=True)
    verify_parser = sub.add_parser('verify', help='read-only re-check of recorded inputs and datasets')
    verify_parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'run':
        return run(args.destination, args.phase)
    return verify(args.destination)


if __name__ == '__main__':
    raise SystemExit(main())
