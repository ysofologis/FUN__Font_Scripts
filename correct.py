"""Contour correction via foundrytools + Skia pathops.

Wraps ``foundrytools.lib.pathops.correct_glyf_contours`` (TTF) and
``correct_cff_contours`` (CFF/OTF). Both run a Skia pathops union +
simplify pass on every glyph, then fix winding and cull tiny paths.

Use case: fonts with self-intersecting or overlapping outlines (often
from manual editing, conversion from another format, or sloppy upstream
sources) get cleaned up so that downstream transforms (thicken, scale,
hint-tune) operate on a clean topology.

Public API:
    correct_glyphs(font) -> int
    HAS_FOUNDRYTOOLS (bool): False if foundrytools isn't installed.

Mirrors the shape of ``thicken.py`` so the main script treats both the
same way (optional dependency, graceful skip + warning if missing).
"""
from __future__ import annotations

import logging

from fontTools.ttLib import TTFont

log = logging.getLogger(__name__)

# Foundrytools is an OPTIONAL dependency. v12 only needs it for --correct
# and --hint-tune. If it's missing the function logs a warning and returns 0
# so the rest of the pipeline still runs.
try:
    from foundrytools.lib.pathops import correct_glyf_contours, correct_cff_contours
    HAS_FOUNDRYTOOLS = True
except ImportError:
    correct_glyf_contours = None  # type: ignore[assignment]
    correct_cff_contours = None    # type: ignore[assignment]
    HAS_FOUNDRYTOOLS = False


def correct_glyphs(font: TTFont, min_area: int = 25) -> int:
    """Run contour correction on every glyph. Returns count of touched glyphs.

    Auto-detects TTF (glyf) vs OTF (CFF) and dispatches to the matching
    foundrytools helper. ``remove_hinting=True`` because hint instructions
    are pinned to the pre-cleanup topology and would be invalidated by
    the union/simplify pass; ``--hint-tune`` re-hints afterwards.

    ``ignore_errors=True`` so a single broken glyph doesn't abort the
    whole font — Skia pathops is robust but some pathological shapes
    (e.g. fully-degenerate contours) can throw.
    """
    if not HAS_FOUNDRYTOOLS:
        log.warning(
            "foundrytools not installed; --correct skipped. "
            "pip install foundrytools to enable."
        )
        return 0

    if "glyf" in font:
        touched = correct_glyf_contours(
            font=font,
            remove_hinting=True,
            ignore_errors=True,
            min_area=min_area,
        )
    elif "CFF " in font:
        touched = correct_cff_contours(
            font=font,
            remove_hinting=True,
            ignore_errors=True,
            remove_unused_subroutines=True,
            min_area=min_area,
        )
    else:
        log.warning("Font has neither glyf nor CFF table; --correct skipped.")
        return 0

    log.info(f"Contour correction touched {len(touched)} glyph(s)")
    return len(touched)
