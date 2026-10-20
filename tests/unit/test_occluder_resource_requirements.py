"""Missing real assets must fail the lifecycle suite, never silently skip it."""
import pytest

from tests.ui import test_occluder_import_lifecycle as lifecycle


@pytest.mark.parametrize("case", ["available", "missing-core", "short-library", "missing-file"])
def test_real_resource_preconditions_fail_closed(tmp_path, case):
    core = tmp_path / "core.ply"
    core.write_bytes(b"presence-only fixture; no geometry parsing")
    files = [tmp_path / f"occluder-{index}.ply" for index in range(4)]
    for path in files:
        path.write_bytes(b"presence-only fixture")
    if case == "missing-core":
        core = tmp_path / "absent-core.ply"
    elif case == "short-library":
        files = files[:3]
    elif case == "missing-file":
        files[-1] = tmp_path / "absent-occluder.ply"
    if case == "available":
        assert lifecycle._require_real_resources(core, files) is None
    else:
        with pytest.raises(pytest.fail.Exception, match="real cores/BOP resources are required"):
            lifecycle._require_real_resources(core, files)
