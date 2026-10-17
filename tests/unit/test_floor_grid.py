import numpy as np
import pytest

from bproc_ui.views.scene_view import floor_grid


def test_grid_scales_with_the_model_instead_of_a_fixed_size():
    small, large = floor_grid(.1), floor_grid(1.0)
    assert small["extent"] == pytest.approx(.7)
    assert large["extent"] == pytest.approx(7.)
    assert small["step"] == pytest.approx(.1 / 6)


def test_grid_reaches_far_past_the_framed_area_so_no_border_is_visible():
    grid = floor_grid(.1)
    # The camera frames roughly 2.5 radii; the floor must extend well beyond that.
    assert grid["extent"] > 5 * .1
    assert np.allclose(np.abs(grid["minor"]).max(), grid["extent"])


def test_grid_keeps_section_lines_and_full_length_axes():
    grid = floor_grid(.1)
    assert len(grid["section"]) == 9 * 4
    assert len(grid["minor"]) == 76 * 4
    for axis in grid["axes"][:2]:
        assert np.allclose(np.abs(axis).max(), grid["extent"])
    assert grid["axes"][2][0, 2] == 0 and grid["axes"][2][1, 2] > 0


def test_grid_never_collapses_to_zero_for_a_millimetre_model():
    assert floor_grid(0)["extent"] > 0
    assert floor_grid(1e-6)["extent"] > 0
