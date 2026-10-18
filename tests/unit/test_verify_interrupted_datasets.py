"""`tools/verify_interrupted_datasets.py` must name artefacts the way the writer does and fail on
any counter, exit-code or completeness deviation instead of trusting the journal."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))


def load():
    spec = importlib.util.spec_from_file_location(
        'verify_interrupted_datasets_tool', ROOT / 'tools' / 'verify_interrupted_datasets.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load()

EXPECTED = {'requested': 48, 'written': 2, 'saved': 2, 'skipped': 46, 'journal': 2}


def status(**overrides):
    base = {'status': 'interrupted', 'requested': 48, 'written_frames': 2, 'saved': 2,
            'skipped': 46, 'exit_code': 130}
    base.update(overrides)
    return base


def counters(**overrides):
    arguments = dict(manifest_rows=2, journal=2, final=True, total_shots=48, expected_exit=130,
                     expected=dict(EXPECTED))
    arguments.update(overrides)
    return arguments


def test_rgb_masks_reference_uses_the_object_index(tmp_path):
    row = {'frame_index': 7, 'relative_rgb': 'rgb/000007.png', 'gt_obj_ids': [2, 5]}
    paths = tool.referenced_artifacts(tmp_path, row, 'rgb_masks')
    assert ('rgb', tmp_path / 'rgb/000007.png') in paths
    assert ('mask', tmp_path / 'mask/000007_000002.png') in paths
    assert ('mask_visib', tmp_path / 'mask_visib/000007_000002.png') in paths
    assert ('mask', tmp_path / 'mask/000007_000005.png') in paths


def test_bop_reference_uses_the_ground_truth_index_not_the_object_id(tmp_path):
    row = {'frame_index': 3, 'relative_rgb': 'train_pbr/000003/rgb/000003.jpg',
           'bop_scene_id': 3, 'bop_image_id': 3, 'gt_obj_ids': [2]}
    paths = tool.referenced_artifacts(tmp_path, row, 'bop')
    scene = tmp_path / 'train_pbr/000003'
    assert ('rgb', tmp_path / 'train_pbr/000003/rgb/000003.jpg') in paths
    assert ('depth', scene / 'depth/000003.png') in paths
    assert ('mask', scene / 'mask/000003_000000.png') in paths
    assert ('mask_visib', scene / 'mask_visib/000003_000000.png') in paths
    # The object id is not the GT index; the writer never emits `000003_000002.png` for one target.
    assert ('mask', scene / 'mask/000003_000002.png') not in paths


def test_consistent_interrupted_counters_pass():
    assert tool.counter_problems(status(), **counters()) == []


def test_sigterm_must_report_143():
    problems = tool.counter_problems(status(exit_code=130), **counters(expected_exit=143))
    assert any('exit_code' in problem and '143' in problem for problem in problems)


def test_wrong_status_fails():
    problems = tool.counter_problems(status(status='completed'), **counters())
    assert any("expected 'interrupted'" in problem for problem in problems)


def test_wrong_frame_counters_fail():
    problems = tool.counter_problems(status(written_frames=3, saved=3, skipped=45),
                                     **counters(manifest_rows=3, journal=3))
    assert any(problem.startswith('written is 3') for problem in problems)
    assert any(problem.startswith('saved is 3') for problem in problems)
    assert any(problem.startswith('skipped is 45') for problem in problems)
    assert any('journal is 3' in problem for problem in problems)


def test_non_final_manifest_fails():
    problems = tool.counter_problems(status(), **counters(final=False))
    assert any('final' in problem for problem in problems)


def test_missing_metadata_fails_closed(tmp_path):
    problems, facts = tool.verify_case(tool.CASES[0], dataset=tmp_path)
    assert problems and not facts.get('config_format')
    assert any('run_metadata/status.json' in problem for problem in problems)
