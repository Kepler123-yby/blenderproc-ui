import numpy as np
import pytest

from bproc_ui.views.scene_view import floor_grid, nice_step


def test_cell_size_snaps_to_human_units():
    assert nice_step(.0255) == pytest.approx(.02)
    assert nice_step(.031) == pytest.approx(.02)
    assert nice_step(.04) == pytest.approx(.05)
    assert nice_step(.009) == pytest.approx(.005)
    assert nice_step(0) > 0


def test_grid_scales_with_the_model_instead_of_a_fixed_size():
    small, large = floor_grid(.1), floor_grid(1.0)
    assert small["step"] == pytest.approx(.02)          # 0.1/4 = 0.025 → 0.02
    assert large["step"] == pytest.approx(.2)           # 1.0/4 = 0.25 → 0.2
    assert large["extent"] == pytest.approx(small["extent"] * 10)


def test_grid_is_bounded_but_fades_out_before_its_edge():
    grid = floor_grid(.1)
    assert 10 * grid["step"] <= grid["extent"] <= 16 * grid["step"]
    assert np.allclose(np.abs(grid["minor"]).max(), grid["extent"])


def test_grid_keeps_section_lines_and_full_length_axes():
    grid = floor_grid(.1)
    assert len(grid["section"]) == 7 * 4      # every 5th of 31 lines
    assert len(grid["minor"]) == 24 * 4
    for axis in grid["axes"][:2]:
        assert np.allclose(np.abs(axis).max(), grid["extent"])
    assert grid["axes"][2][0, 2] == 0 and grid["axes"][2][1, 2] > 0


def test_grid_never_collapses_to_zero_for_a_millimetre_model():
    assert floor_grid(0)["extent"] > 0
    assert floor_grid(1e-6)["extent"] > 0
