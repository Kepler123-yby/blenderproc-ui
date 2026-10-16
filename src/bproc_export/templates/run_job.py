"""Python 3.8 standard-library launcher. Never import BlenderProc before preflight."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def _run_child(command, env):
    child = [None]
    interrupted = [None]
    pending = [None]
    def forward(signum, frame):
        interrupted[0] = 128 + signum
        if child[0] is None:
            pending[0] = signum
        elif child[0].poll() is None:
            child[0].send_signal(signum)
    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        child[0] = subprocess.Popen(command, env=env, start_new_session=True)
        if pending[0] is not None:
            child[0].send_signal(pending[0])
        code = child[0].wait()
        return interrupted[0] or (128 - code if code < 0 else code)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线检查并运行 BlenderProc 任务')
    root = Path(__file__).resolve().parent
    parser.add_argument('--config', type=Path, default=root / 'scene.yaml')
    parser.add_argument('--format', choices=['rgb_masks', 'bop'])
    parser.add_argument('--num-samples', type=int)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        locator = json.loads((root / 'launch.json').read_text(encoding='utf-8'))
        if locator.get('schema_version') != 1 or locator.get('ready') is not True:
            raise ValueError('任务导出未完成或启动格式不受支持')
        forwarded = ['--config', str(args.config.resolve())]
        for key in ('format', 'num_samples', 'output'):
            value = getattr(args, key)
            if value is not None:
                forwarded.extend(['--' + key.replace('_', '-'), str(value)])
        env = dict(os.environ, PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONNOUSERSITE='1')
        env.pop('PYTHONPATH', None)
        with tempfile.TemporaryDirectory(prefix='bproc-job-') as temporary:
            env['MPLCONFIGDIR'] = str(Path(temporary) / 'matplotlib')
            code = _run_child([locator['blender_python'], str(root / 'bproc_runtime/preflight.py'),
                               '--launch', str(root / 'launch.json')] + forwarded, env)
            if code or args.check_only:
                return code
            # Match BlenderProc 2.8's CLI startup arguments, while forwarding signals
            # directly to Blender instead of through the CLI's SIGINT->SIGTERM handler.
            env['PYTHONPATH'] = str(Path(locator['blenderproc_package_dir']).parent)
            env['INSIDE_OF_THE_INTERNAL_BLENDER_PYTHON_ENVIRONMENT'] = '1'
            entry = str(root / 'render_scene.py')
            return _run_child([str(Path(locator['blender_dir']) / 'blender'), '--background',
                               '--python-use-system-env', '--python-exit-code', '2',
                               '--python', entry, '--', entry, temporary] + forwarded, env)
    except (OSError, ValueError, KeyError) as exc:
        print('无法启动任务：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
