import numpy as np
import pytest

from bproc_ui.views.scene_view import floor_grid


def test_grid_scales_with_the_model_instead_of_a_fixed_size():
    small, large = floor_grid(.1), floor_grid(1.0)
    assert small["extent"] == pytest.approx(.16)
    assert large["extent"] == pytest.approx(1.6)
    # Both grids keep the same density: twenty cells, so the cell size follows the model.
    assert len(small["minor"]) + len(small["section"]) == len(large["minor"]) + len(large["section"])


def test_grid_has_section_lines_every_ten_cells_and_full_length_axes():
    grid = floor_grid(.1)
    assert len(grid["minor"]) == 18 * 4     # twenty cells minus the three section lines × two directions
    assert len(grid["section"]) == 3 * 4
    for axis in grid["axes"][:2]:
        assert np.allclose(np.abs(axis).max(), grid["extent"])
    assert grid["axes"][2][0, 2] == 0 and grid["axes"][2][1, 2] > 0


def test_grid_never_collapses_to_zero_for_a_millimetre_model():
    assert floor_grid(0)["extent"] > 0
    assert floor_grid(1e-6)["extent"] > 0
