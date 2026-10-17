"""Phong preview parameters that make the sampled surface visible in the viewport.

The renderer uses Principled BSDF; a viewport that only shows flat colour cannot tell a rough
plastic from a polished metal. vispy's ShadingFilter is a Phong model, so map the sampled
Principled inputs onto it:

* roughness -> shininess, with the usual 2/rough^2 - 2 mapping (rough 0.1 -> ~198, 0.8 -> ~1)
* metallic  -> less diffuse, more and tinted specular (metal reflects its own colour)
* base colour -> mesh colour, modulated by the same inputs
"""

import numpy as np
from vispy.visuals.filters import ShadingFilter

DEFAULT_BASE = (0.55, 0.58, 0.61)
# Direction the light travels: down, towards the camera and slightly from the left, so both the
# top face and the side facing the viewport's default turntable camera receive light.
LIGHT_DIR = (-0.45, -0.5, -0.74)


def shininess(roughness):
    """Principled roughness to Phong exponent, clamped so 0.05 still has a visible glare."""
    rough = min(max(float(roughness), .02), 1.)
    return float(min(max(2. / rough ** 2 - 2., 2.), 220.))


def specular_strength(roughness, metallic):
    """Phong Ks: a rough surface scatters the highlight away instead of blowing it out.

    Without the roughness falloff a 0.9-rough metal renders a broad, clipped white cap that
    looks nothing like a matte metal.
    """
    rough = min(max(float(roughness), 0.), 1.)
    metal = min(max(float(metallic), 0.), 1.)
    return (.30 + .70 * metal) * (.05 + .95 * (1. - rough) ** 1.5)


def phong_params(roughness=0.5, metallic=0.0, base_rgb=DEFAULT_BASE):
    """Phong coefficients for one sampled material state, in vispy's filter vocabulary."""
    base = tuple(float(c) for c in base_rgb[:3])
    metal = min(max(float(metallic), 0.), 1.)
    # A headlight alone leaves the sides black; a strong ambient floor keeps the solid readable
    # while the directional term still shows where the light comes from.
    diffuse = tuple(c * (.72 - .45 * metal) for c in base)
    # Dielectrics keep a weak neutral reflection; metals reflect their own colour.
    tint = tuple(.3 + .7 * c for c in base) if metal > 0 else (1., 1., 1.)
    strength = specular_strength(roughness, metal)
    return {
        "ambient_coefficient": tuple((.24 - .06 * metal) * c for c in base),
        "diffuse_coefficient": diffuse,
        "specular_coefficient": tuple(strength * c for c in tint),
        "shininess": shininess(roughness),
        "light_dir": LIGHT_DIR,
        "ambient_light": (1., 1., 1.),
        "diffuse_light": (.9, .9, .9),
        "specular_light": (.95, .95, .95),
    }


def attach_preview(mesh_visual, roughness=0.5, metallic=0.0, base_rgb=DEFAULT_BASE):
    """Swap the default headlight for a Phong filter driven by the sampled material."""
    mesh_visual.shading = None
    params = phong_params(roughness, metallic, base_rgb)
    shading = ShadingFilter(shading="smooth")
    for key, value in params.items():
        setattr(shading, key, value)
    mesh_visual.attach(shading)
    mesh_visual.shading_filter = shading
    return shading
