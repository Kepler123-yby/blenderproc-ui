import blenderproc as bproc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blenderproc.python.utility.SetupUtility import SetupUtility


def _deny_install(*args, **kwargs):
    raise RuntimeError('渲染中禁止安装依赖；请先在终端准备 Blender 环境，再执行 --check-only')


SetupUtility.setup_pip = staticmethod(_deny_install)

from bproc_runtime.cli import main

if __name__ == '__main__':
    sys.exit(main())
