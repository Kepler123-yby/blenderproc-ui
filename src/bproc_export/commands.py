from __future__ import annotations

from pathlib import Path
import shlex


def build_command(job_dir: Path, conda_env: str, *, num_samples: int | None = None,
                  output: Path | None = None) -> str:
    job = Path(job_dir).expanduser().absolute()
    args = ['conda', 'run', '--no-capture-output', '-n', conda_env,
            'python', str(job / 'run_job.py'), '--config', str(job / 'scene.yaml')]
    if num_samples is not None:
        args.extend(['--num-samples', str(num_samples)])
    if output is not None:
        args.extend(['--output', str(Path(output).expanduser().absolute())])
    return shlex.join(args)
