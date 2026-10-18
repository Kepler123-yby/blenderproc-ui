from __future__ import annotations

from pathlib import Path
import shlex


def build_command(job_dir: Path, conda_env: str, *, num_samples: int | None = None,
                  num_scenes: int | None = None, output: Path | None = None) -> str:
    """Full job command; `num_samples` limits frames, `num_scenes` limits scenes."""
    if num_samples is not None and num_scenes is not None:
        raise ValueError('--num-samples 与 --num-scenes 只能二选一；前者按张数、后者按场景数截断')
    job = Path(job_dir).expanduser().absolute()
    args = ['conda', 'run', '--no-capture-output', '-n', conda_env,
            'python', str(job / 'run_job.py'), '--config', str(job / 'scene.yaml')]
    if num_samples is not None:
        args.extend(['--num-samples', str(num_samples)])
    if num_scenes is not None:
        args.extend(['--num-scenes', str(num_scenes)])
    if output is not None:
        args.extend(['--output', str(Path(output).expanduser().absolute())])
    return shlex.join(args)
