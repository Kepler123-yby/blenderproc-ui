"""The viewport must make roughness and metallic legible, not just tint the mesh."""
import pytest

from bproc_ui.material_preview import phong_params, shininess, specular_strength


def test_shininess_falls_as_roughness_rises_and_stays_bounded():
    assert shininess(.05) > shininess(.4) > shininess(.9)
    assert shininess(0) == pytest.approx(220)      # clamped, no division by zero
    assert shininess(1) == pytest.approx(2)


def test_rough_surfaces_lose_their_highlight():
    assert specular_strength(.05, 0) > 5 * specular_strength(.9, 0)
    assert specular_strength(.05, 1) > specular_strength(.05, 0)


def test_metal_trades_diffuse_for_a_tinted_highlight():
    plastic = phong_params(.3, 0., (.8, .8, .8))
    metal = phong_params(.3, 1., (.8, .8, .8))
    assert sum(metal["diffuse_coefficient"]) < sum(plastic["diffuse_coefficient"])
    assert sum(metal["ambient_coefficient"]) < sum(plastic["ambient_coefficient"])
    assert sum(metal["specular_coefficient"]) > sum(plastic["specular_coefficient"])
    # A metal's highlight carries its own hue; a dielectric's stays neutral.
    warm = phong_params(.3, 1., (.8, .5, .3))
    assert warm["specular_coefficient"][0] > warm["specular_coefficient"][2]
    assert len(set(plastic["specular_coefficient"])) == 1


def test_base_colour_reaches_every_term_so_edits_are_visible():
    grey, red = phong_params(.4, 0., (.5, .5, .5)), phong_params(.4, 0., (.9, .1, .1))
    assert red["diffuse_coefficient"][0] > grey["diffuse_coefficient"][0]
    assert red["ambient_coefficient"][1] < grey["ambient_coefficient"][1]


def test_out_of_range_inputs_are_clamped_instead_of_producing_nan():
    for rough, metal in ((-1, -1), (2, 2)):
        params = phong_params(rough, metal)
        assert all(v == v for v in params["diffuse_coefficient"])   # NaN-free
        assert params["shininess"] >= 2
