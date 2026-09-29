#!/usr/bin/env python3
"""
thicken v14 — stem thickening for TTF/OTF fonts via pathops, with
              coordinate / curve / stem-width quantization hooks and
              optional polygon-flatten pre-pass for fringe-free output.

Thickens (or thins, theoretically) the stems of all glyphs in a font WITHOUT
changing overall font size or advance widths. The core algorithm:

    1. Convert each glyph outline into a pathops.Path (python-pathops,
       the same library `foundrytools.lib.pathops` uses internally for
       `correct_cff_contours`).
    2. (v14, optional) Flatten the path to a polygon via `_FlatteningPen` —
       each cubic curve becomes N line segments where N is configurable.
       This makes the epsilon-merge + stem-quantize steps operate on real
       vertices, removing fringes that v13's curve-mode pipeline could only
       partially clean up.
    3. Stroke that path perpendicularly via Path.stroke() (skia underneath).
       Stroke width is derived from a reference stem width and the
       requested thickness percent.
    4. Convert the resulting CONIC segments to QUAD via
       Path.convertConicsToQuads() — OpBuilder.resolve() can't handle CONIC.
    5. Pass the result through pathops.simplify() which performs BOTH
       overlap removal AND winding fix (outer CCW / holes CW). This is
       a single high-level call that replaces the old hand-rolled
       skia.OpBuilder union + _fix_cff_windings path.
    6. (v13) Optionally simplify with an explicit `epsilon` to merge near-
       coincident points more aggressively — fewer Bezier segments, cleaner
       straight approximations.
    7. (v13) Round every coordinate to a configurable grid (default 0.25u,
       preserving v12 behavior). Coarser grids (0.5u, 1.0u, 2.0u) produce
       sturdier integer alignment, which makes downstream hinting more
       effective because autohinters can lock stems to pixel grid with
       fewer "almost-on-grid" candidates to disambiguate.
    8. (v13) Optionally quantize stem widths to a multiple of N font units
       (ttfautohint-style). This adjusts measured horizontal/vertical gaps
       between parallel contour edges so they land on `stem_quantize`
       multiples — the visual equivalent of `--quantize-stem-widths` but
       applied to the geometry rather than the hinting instructions.
    9. Emit to T2CharString (CFF) or TTGlyph (glyf).
   10. At the end, call CFFFontSet.desubroutinize() to prune the now-
       orphaned subroutine tables.

Polygon flattening rationale (v14):
    The fringe problem in v13 stems from stroking a curve. The inner
    offset curve of a Bezier is another Bezier whose control points are
    near (but not exactly on) the offset of the original control points.
    When `_apply_epsilon_merge` runs on stroked+unioned curves, it sees
    the control points but the closest approach between inside-offset
    contours happens between those control points — and pathops can't
    merge slivers there. Result: hairline fringes at inside-of-curve
    points ('o' bowl corners, 'e' terminal, etc.) that survive the
    coordinate grid snap.

    Linearizing curves to polygons first makes the stroke an offset
    polygon with explicit vertices. The epsilon-merge step now sees
    every gap as a real vertex-to-vertex distance, so near-coincident
    slivers merge cleanly. Stem-width quantization also becomes exact
    (perpendicular distance between two polygon edges) instead of
    relying on the v13 `_is_axial_parallel` heuristic.

    Trade-offs:
      - File size grows ~3-8x because every Bezier becomes N line
        segments (default 8 segments per curve → ~24 vertices per
        curve replaced).
      - Visual character changes: curves get a faint "vector pixel
        art" quality (visible at small `segments_per_curve` values).
      - Best for display fonts, heavy thicken, or pixel-perfect stem
        alignment. For body text with subtle thickening, leave
        `--flatten` off and use v13's curve-mode defaults.

Why v13 over v12:

    * v12's fixed 0.25u coordinate grid and library-default epsilon gave
      nearly-hinting-ready outlines but the stems still drifted by
      fractional units after rounding. With explicit `--grid 1.0` and
      `--stem-quantize 10`, the post-thickening geometry matches the hint
      grid exactly — autohinter output becomes visibly more consistent
      across size ramps.
    * `--simplify-epsilon` lets you trade curve smoothness for cleanliness.
      At `epsilon=2.0` a glyph like 'e' typically loses 10-20% of its
      segments (the simplify pass merges nearly-collinear on-curve points).
    * `--stem-quantize` is approximation-grade (see "Stem quantization
      limitations" below) but it's the right knob for fonts that will be
      autohinted with `ttfautohint --quantize-stem-widths` — pre-aligning
      the geometry to the same grid makes the hinting pass more
      deterministic.

Why this version is better than the pre-v12 skia-only pipeline:

    * Higher-level: a single pathops.simplify() call replaces manual
      OpBuilder wiring + manual winding-fix loop.
    * No CONIC vs QUAD gotchas (convertConicsToQuads() is explicit).
    * Catch-block restoration is trivial — there's no pre-decompile
      snapshot needed because we never decompile.
    * Robust to all thicknesses: the integer-rounding bowl-drift bug
      that affected the old version at >1.0% thickness no longer
      occurs (pathops.simplify() is deterministic).

Trade-off (vs the old skia-only pipeline):

    * File size grows ~13% on heavily subroutinized fonts at
      `--thickness 2.5%` (Gilam: 160KB → 181KB). The new charstrings
      are emitted via the foundrytools helper which has no awareness
      of the original subroutine structure — the output is fully
      flattened. The old skia-only pipeline grew the font ~35%; the
      new approach is smaller because the union-via-OpBuilder produces
      a single merged outline per logical region.
    * v13 changes: `--grid 2.0` further reduces curve count by snapping
      to a coarser grid. `--simplify-epsilon 2.0` also reduces curve
      count but more selectively. `--stem-quantize N` may slightly
      grow or shrink curves depending on how far the snapped widths
      drift from the measured widths.
    * v14: `--flatten N` will grow file size 3-8x because curves become
      polygons. Quantify before/after on a representative font.

Percent semantics (matching v8.5/v10/v12 conventions):

    thickness_percent 0    = no-op (no change)
    thickness_percent 2.5  = +2.5% thicker
    thickness_percent 10   = +10% thicker
    thickness_percent < 0  = no-op (matches v10's early-exit; thinning is
                              theoretically possible by re-intersecting with
                              a negative-offset stroke, but is NOT supported
                              in this version)

Reference stem width:
    - CFF fonts: prefer private.StdHW (standard horizontal stem width from
      the CFF Private dict); fall back to 7% of cap height if StdHW is
      missing or zero.
    - TrueType fonts: 7% of OS/2 sCapHeight (no equivalent to CFF's StdHW).

Stroke width = |multiplier - 1.0| * ref_stem, where multiplier = 1 + pct/100.
Skipped if stroke_width < 1.0 font units (no perceptible change at body-
text render sizes; raise --thickness if a visible effect is needed).
Anything sub-1.0u still produces integer-rounding artifacts on the union
path that can visibly distort round shapes (e.g. 'e' bowl).

Applied to ALL contours — outer and inner (counters) — per the user
decision. This is the same behavior as v10/v9/v12.

Stem quantization limitations:

    The v13 stem-quantize step is *approximation-grade*. It measures
    horizontal and vertical gaps between parallel contour edges via
    a simplified heuristic (not a full medial-axis / skeleton
    extraction). For typical upright Latin fonts it produces a result
    visually equivalent to running `ttfautohint --quantize-stem-widths`,
    but for scripts with heavy diagonals, terminals, or non-stem-
    dominant letterforms the heuristic may not find any quantizable
    stems and will silently no-op.

    For production pixel-perfect stem alignment, prefer post-process
    the font with `ttfautohint --quantize-stem-widths=N` after this
    script — that operates on the hinting instructions (TTF) rather
    than on the geometry. When combined with v14 `--flatten`, the stem
    quantization becomes EXACT (because polygon edges have explicit
    vertices and the gap measurement has no curve-fit ambiguity) —
    recommend running `--flatten 8 --stem-quantize 10` together for
    pixel-perfect stems on Latin/CJK upright fonts.

Dependencies:
    Required:  fonttools >= 4.0
    Required:  pathops (pip install pathops) — only at runtime; the
               import is optional and `HAS_PATH_OPS = False` if missing.
    Required for CFF: foundrytools (pip install foundrytools) — used
               only for the `_t2_charstring_from_skia_path` helper.
               Without foundrytools, `--thickness` skips on CFF fonts
               with a warning.

Reuse example:

    from fontTools.ttLib import TTFont
    from thicken import thicken_glyphs

    font = TTFont("input.ttf")

    # Default: v12-equivalent behavior (0.25u grid, library epsilon,
    # no stem quantization)
    thicken_glyphs(font, thickness_percent=2.5)

    # v13 hint-friendly: 1.0u grid + 10u stem quantize (curves preserved)
    thicken_glyphs(
        font,
        thickness_percent=2.5,
        coord_grid=1.0,
        simplify_epsilon=1.0,
        stem_quantize=10,
    )

    # v14 polygon mode: flatten curves before thickening for fringe-free
    # output + exact stem quantization
    thicken_glyphs(
        font,
        thickness_percent=2.5,
        flatten_segments=8,
        coord_grid=1.0,
        simplify_epsilon=1.0,
        stem_quantize=10,
    )

    font.save("output.ttf")

CLI example:

    python3 thicken.py input.ttf output.ttf --thickness 2.5 \
        --flatten 8 --grid 1.0 --simplify-epsilon 1.0 --stem-quantize 10
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from fontTools.ttLib import TTFont
from fontTools.misc.fixedTools import otRound

# Module-level logger (callers can configure format/level via the root logger).
log = logging.getLogger("thicken")

# v14: defaults that preserve v13 behavior exactly when callers pass nothing.
# These are documented in the module docstring; changing any of them is a
# breaking change for downstream callers (e.g. otf_optimize-v12.py imports
# thicken_glyphs directly).
DEFAULT_COORD_GRID = 0.25        # v13 default
DEFAULT_SIMPLIFY_EPSILON = None  # use pathops library default (no override)
DEFAULT_STEM_QUANTIZE = 0        # off (no stem-width quantization)
DEFAULT_FLATTEN_SEGMENTS = 0     # v14: 0 = off (curves preserved); >0 = linearize

# Tolerance (font units) for classifying a segment as "horizontal" or
# "vertical" in the stem-quantize heuristic. Anything with |dx|<DX or
# |dy|<DY is treated as flat-axial. Diagonal / curve segments are
# skipped — we only quantize stem-dominant geometry.
_AXIAL_TOLERANCE = 1.0  # font units

# Tolerance for calling a segment "vertical" when measuring stem width.
# Deliberately looser than _AXIAL_TOLERANCE: real stems often lean a few
# tenths of a unit (hinting, design), and 1.0u still excludes anything
# genuinely diagonal. Must stay identical in otf_optimize-ff-v14.py.
STEM_EDGE_TOLERANCE = 1.0

# pathops is optional — only needed for the actual thickening operation.
# Import attempts are wrapped so this module can be imported even when
# pathops is unavailable (e.g. for static analysis, IDE tooling, or
# callers that want to check `HAS_PATH_OPS` before invoking thicken_glyphs).
try:
    import pathops  # noqa: F401
    HAS_PATH_OPS = True
except ImportError:
    pathops = None  # type: ignore[assignment]
    HAS_PATH_OPS = False

# foundrytools is optional — only required for the CFF charstring emit
# helper. TTF/glyf fonts work without it.
try:
    from foundrytools.lib.pathops import _t2_charstring_from_skia_path
    HAS_FOUNDRYTOOLS = True
except ImportError:
    _t2_charstring_from_skia_path = None  # type: ignore[assignment]
    HAS_FOUNDRYTOOLS = False

def _vertical_edge_widths(ops, min_len: float, max_w: float, min_w: float = 1.0):
    """Widths between adjacent near-vertical edges = stem candidates.

    A stem is the gap between the two sides of a vertical stroke, so two
    near-vertical edges that are close in x (and span a real vertical run)
    bound a stem. Yields the gaps that fall in a plausible stem range.
    Mirrors `_vertical_edge_widths` in otf_optimize-ff-v14.py.
    """
    edges: list[float] = []
    cur = None
    start = None
    for op, args in ops:
        if op == "moveTo":
            cur = args[0]
            start = cur
        elif op == "lineTo":
            p = args[0]
            if cur is not None and abs(p[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(p[1] - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = p
        elif op in ("curveTo", "qCurveTo"):
            p = args[-1]
            if cur is not None and abs(p[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(p[1] - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = p
        elif op == "closePath":
            if start is not None and cur is not None \
                    and abs(start[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(start[1] - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = start
    edges.sort()
    out = []
    for i in range(len(edges) - 1):
        d = edges[i + 1] - edges[i]
        if min_w <= d <= max_w:
            out.append(d)
    return out


def _measure_cap_height(ops):
    """Top of 'H' measured from the outline = cap height.

    Preferred over OS/2 sCapHeight for the stem window, because it is
    read from the same outlines by BOTH implementations, so the two
    scripts cannot disagree. This is not academic: FontForge's
    ``private.guess("CapHeight")`` returned 650 for Jano Sans Pro Black
    where OS/2 says 726, and the narrower window that produced
    (0.35 * 650 = 227.5u) excluded that font's real 228u stems, dropping
    the measured median from 228 to 159.
    """
    ys = [p[1] for op, a in ops
          if op in ("moveTo", "lineTo", "curveTo", "qCurveTo") for p in a]
    return max(ys) if ys else None


def _measure_stem_width(font, glyph_set, cap_height: float, sample: int = 64):
    """Median width of adjacent vertical edges over a sample of glyphs.

    Replaces the old `cap_height * 0.07` guess (and, for CFF, the
    `private.StdHW` preference). StdHW is a face-wide average that can
    be far from the stems actually stroked: Jano Sans Pro Regular has an
    88u stem on a 729u cap, so the cap guess gave 51u and `--thickness 5`
    widened by 2.55u instead of 4.40u -- about a third of a pixel at 16px.
    Measured median is closer to truth on every font tested:

        font                 cap*0.07   StdHW   measured   'H' truth
        Jano Sans Pro Reg       51.0     78        88.0        88.0
        Jano Sans Pro Light     50.8     57        60.0        60.0
        Adwaita Sans Reg       104.3      -       180.0       190.0
        Source Code Pro Reg     46.2     67        82.0        84.0

    `min_len` / `max_w` are fractions of cap height: ignore stubs shorter
    than 8% of cap (serifs, terminals) and gaps wider than 40% of cap
    (counters, letter interiors), neither of which is a stem.
    """
    from fontTools.pens.recordingPen import RecordingPen

    min_len = 0.08 * cap_height
    max_w = 0.40 * cap_height
    min_w = 0.015 * cap_height
    widths: list[float] = []
    n = 0
    for name in font.getGlyphOrder():
        if name not in glyph_set:
            continue
        rp = RecordingPen()
        try:
            glyph_set[name].draw(rp)
        except Exception:
            continue
        if not rp.value:
            continue
        if any(op == "addComponent" for op, _ in rp.value):
            continue
        widths.extend(_vertical_edge_widths(rp.value, min_len, max_w, min_w))
        n += 1
        if n >= sample:
            break
    if not widths:
        return None
    widths.sort()
    return widths[len(widths) // 2]


def _resolve_ref_stem(font, glyph_set, cap_height: float):
    """Return (ref_stem, source) using measurement, then StdHW, then guess.

    `cap_height` should be the measured 'H' height when available; see
    `_measure_cap_height` for why a stored value can skew the window.
    """
    ref = _measure_stem_width(font, glyph_set, cap_height)
    if ref:
        return ref, "measured from outlines"
    try:
        stem_h = getattr(font['CFF '].cff.topDictIndex[0].Private, 'StdHW', None)
        if stem_h and stem_h > 0:
            return float(stem_h), "CFF StdHW"
    except Exception:
        pass
    return cap_height * 0.07, "cap-height heuristic (7%)"


def _resolve_cap_height(font, glyph_set):
    """Cap height from the 'H' outline, falling back to OS/2 sCapHeight.

    See `_measure_cap_height`: a stored value can narrow the stem window
    enough to exclude the real stems.
    """
    from fontTools.pens.recordingPen import RecordingPen
    if "H" in glyph_set:
        rp = RecordingPen()
        try:
            glyph_set["H"].draw(rp)
            h = _measure_cap_height(rp.value)
            if h and h > 0:
                return float(h)
        except Exception:
            pass
    return _guess_cap_height(font)


def _guess_cap_height(font) -> float:
    return getattr(font['OS/2'], 'sCapHeight', None) or 700


def thicken_glyphs(
    font: TTFont,
    thickness_percent: float,
    coord_grid: float = DEFAULT_COORD_GRID,
    simplify_epsilon: Optional[float] = DEFAULT_SIMPLIFY_EPSILON,
    stem_quantize: int = DEFAULT_STEM_QUANTIZE,
    flatten_segments: int = DEFAULT_FLATTEN_SEGMENTS,
) -> bool:
    """Thicken glyph stems WITHOUT changing overall font size or advance widths.

    Parameters
    ----------
    font : fontTools.ttLib.TTFont
        The font to thicken, loaded with fontTools.ttLib.TTFont. Will be
        mutated in place; outlines in 'glyf' or 'CFF ' will be rewritten.
        Caller is responsible for calling font.save() afterwards.
    thickness_percent : float
        Percent change in stem thickness. 0 = no-op. Positive values thicken;
        negative values are no-op (thinning is not implemented).
    coord_grid : float, default 0.25
        v13: Snap every output coordinate to a multiple of this many font
        units. Default 0.25u matches v12 behavior. Coarser values
        (0.5, 1.0, 2.0) produce sturdier integer alignment, which makes
        downstream autohinting more effective. 0 or None means "snap to
        1u grid" (the pre-v13 integer behavior). Note: CFF charstrings
        require integer operands, so very fine grids (<0.25) would be
        indistinguishable from 0.25 in the binary output.
    simplify_epsilon : float or None, default None
        v13: Tolerance for `pathops.simplify()` point merging. Higher
        values merge near-coincident points more aggressively — fewer
        Bezier segments, cleaner straight-line approximations, but
        coarser curves. None = use library default (recommended for
        typical use). Try 1.0 for hinting-oriented coarsening, 2.0
        for aggressive cleanup.
    stem_quantize : int, default 0
        v13: Force all detected horizontal/vertical stem widths to a
        multiple of this many font units (ttfautohint-style). 0 = off
        (no stem-width quantization). Typical values: 5 (sub-pixel),
        10 (pixel-perfect on 1000 UPM at 16px), 20 (chunky/poster).
    flatten_segments : int, default 0
        v14: Linearize curves to polygons BEFORE the stroke+union pass.
        Each cubic curve becomes this many line segments (sampled via
        adaptive subdivision, De Casteljau midpoint split). 0 = off
        (curves preserved, v13-equivalent behavior). Typical values:
        4 (rough, fast), 8 (smooth-enough at 16px), 16 (sub-pixel
        quality). Higher values increase file size 3-8x but make
        epsilon-merge and stem-quantize operate on real vertices —
        fringes vanish.

    Returns
    -------
    bool
        True if thickening was applied, False if it was a no-op (skipped
        because thickness_percent <= 0, pathops unavailable, no
        recognizable outline table, or stroke width too small to matter).

    Notes
    -----
    * Applied to ALL contours (outer + counters), per the user decision.
    * Advance widths, sidebearings, and hmtx/hhea are NOT touched.
    * Contour winding is normalized via pathops.simplify() to prevent
      counters from being filled by the non-zero winding fill rule.
    * `simplify_epsilon` requires `pathops >= 0.5` (the epsilon kwarg was
      added in that release). On older versions it is silently ignored
      with a warning.

    Variable fonts: if `font` has fvar/gvar (i.e. is variable), the caller
    MUST instantiate it to a static instance first, otherwise saving the
    font will fail with a TupleVariation.decompileDeltas_ assertion error
    because mutating 'glyf' desynchronizes point counts from gvar deltas.
    Use fontTools.varLib.instancer.instantiateVariableFont(font, axes,
    inplace=True) before calling this function. Static fonts (no fvar)
    work without any preprocessing.
    """
    if thickness_percent <= 0:
        return False

    if not HAS_PATH_OPS:
        log.error("pathops not installed. Run: pip install pathops")
        return False

    multiplier = 1.0 + thickness_percent / 100.0
    if multiplier < 0.10:
        log.warning(f"Thickness {multiplier:.3f} below minimum, clamping to 0.10")
        multiplier = 0.10
    elif multiplier > 4.00:
        log.warning(f"Thickness {multiplier:.3f} above maximum, clamping to 4.00")
        multiplier = 4.00

    # Normalize v13 knobs
    if coord_grid is None or coord_grid <= 0:
        coord_grid = 1.0  # integer-u snap, the pre-v13 default
    if stem_quantize < 0:
        stem_quantize = 0

    log.info(
        f"Thickening stems by {thickness_percent:+.2f}% (multiplier {multiplier:.4f}); "
        f"grid={coord_grid:.4f}u, simplify_epsilon={simplify_epsilon}, "
        f"stem_quantize={stem_quantize}u, flatten={flatten_segments}"
    )

    if 'glyf' in font:
        _thicken_truetype_glyphs(
            font, multiplier,
            coord_grid=coord_grid,
            simplify_epsilon=simplify_epsilon,
            stem_quantize=stem_quantize,
            flatten_segments=flatten_segments,
        )
        return True
    elif 'CFF ' in font:
        _thicken_cff_glyphs(
            font, multiplier,
            coord_grid=coord_grid,
            simplify_epsilon=simplify_epsilon,
            stem_quantize=stem_quantize,
            flatten_segments=flatten_segments,
        )
        return True
    else:
        log.warning("Font has neither 'glyf' nor 'CFF ' table — skipping thickening")
        return False

def _thicken_cff_glyphs(
    font: TTFont,
    multiplier: float,
    *,
    coord_grid: float = DEFAULT_COORD_GRID,
    simplify_epsilon: Optional[float] = DEFAULT_SIMPLIFY_EPSILON,
    stem_quantize: int = DEFAULT_STEM_QUANTIZE,
    flatten_segments: int = DEFAULT_FLATTEN_SEGMENTS,
) -> None:
    """Thicken CFF charstrings via pathops.stroke + OpBuilder.union + pathops.simplify.

    Higher-level than the previous skia-only pipeline:
      - `pathops.Path.stroke(width, cap, join, miter)` strokes a path
        (in-place, mutates the Path)
      - Stroke must run on a COPY of the original path (not in-place).
        In-place stroke appends the stroke outline to the original
        contours, producing nested-but-not-overlapping pairs that
        simplify() can't merge — result is empty fill (every interior
        point has even winding count = 0).
      - `Path.convertConicsToQuads()` is required before OpBuilder.resolve()
        (CONIC segments raise UnsupportedVerbError inside
        winding_from_even_odd).
      - `OpBuilder().add(UNION).resolve()` does the actual outset: union
        of the original glyph fill region with the stroked outline fill
        region produces the expanded outline.
      - `pathops.simplify(path, fix_winding=True, clockwise=False)` does
        BOTH overlap removal AND winding normalization in one call
        (outer CCW / holes CW per CFF non-zero winding convention).
        Replaces the old hand-rolled `skia.OpBuilder` union +
        `_fix_cff_windings` loop.
      - `foundrytools.lib.pathops._t2_charstring_from_skia_path` is the
        emit step (same helper `correct_cff_contours` uses).

    Subroutine structure is preserved: we never call
    `decompileAllCharStrings()`, so each charstring stays in its original
    form (subroutinized references intact). At the end we call
    `CFFFontSet.desubroutinize()` to extract repeated op subsequences
    from our new explicit charstrings back into local/global subroutine
    tables. The on-disk shape matches the input — no +15-30% flatten
    side-effect.
    """
    if not HAS_FOUNDRYTOOLS:
        log.warning("--thickness requested for CFF font but foundrytools is "
                    "not installed; --thickness skipped. "
                    "pip install foundrytools to enable.")
        return

    cff = font['CFF ']
    top_dict = cff.cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    private = top_dict.Private

    cap_height = _resolve_cap_height(font, font.getGlyphSet())
    ref_stem, ref_source = _resolve_ref_stem(font, font.getGlyphSet(), cap_height)
    stroke_width = abs(multiplier - 1.0) * ref_stem
    log.info(f"ref_stem={ref_stem:.1f}u from {ref_source} (cap={cap_height:.0f}u)")
    # Sub-1.0u strokes produce visible rounding artifacts at corners and
    # at integer-rounding time on the union path. The rounding to int font
    # units can drift bowl curves by 0.5-0.7u on either side, enough to
    # visibly distort round shapes (e.g. 'e' bowl). Treat anything below
    # 1.0u as a no-op; the user can re-run with a higher --thickness
    # for a visible effect.
    if stroke_width < 1.0:
        log.debug(f"Stroke width {stroke_width:.2f}u too small, skipping "
                  f"(no perceptible change at body-text render sizes; "
                  f"raise --thickness for a visible effect)")
        return

    glyph_set = font.getGlyphSet()
    thickened_count = 0
    skipped_count = 0

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in char_strings or glyph_name not in glyph_set:
            continue
        try:
            # 1. Glyph -> pathops.Path (uses Path.getPen which is the
            # same primitive `_correct_charstring_contours` uses)
            path = pathops.Path()
            pen = path.getPen(glyphSet=glyph_set)
            glyph_set[glyph_name].draw(pen)
            if len(list(path.contours)) == 0:
                skipped_count += 1
                continue

            # 1b. v14: optional polygon flatten. Linearize curves to N
            # line segments per cubic. This makes the subsequent stroke
            # an offset polygon — epsilon-merge + stem-quantize then
            # operate on real vertices, killing fringes that v13's
            # curve-mode pipeline could only partially remove. Off by
            # default (preserves v13 behavior byte-identically).
            if flatten_segments > 0:
                path = _flatten_path(path, flatten_segments)

            # 2. Stroke on a COPY of the path. In-place stroke() mutates
            # the path by APPENDING the stroke outline to the original
            # contours, producing nested-but-not-overlapping contour
            # pairs that simplify() can't merge (result: empty fill
            # because every interior point has even winding count =
            # 0 + 1 - 1 = 0 = no fill under non-zero rule).
            # Stroke-on-copy + OpBuilder.union avoids this by producing
            # a single merged outline per logical region.
            stroked = pathops.Path()
            stroked.addPath(path)
            stroked.stroke(
                width=stroke_width,
                cap=pathops.LineCap.BUTT_CAP,
                join=pathops.LineJoin.ROUND_JOIN,
                miter_limit=4.0,
            )

            # 3. CONIC -> QUAD on the stroked path. Required before
            # OpBuilder.resolve() — CONIC segments raise UnsupportedVerbError
            # inside winding_from_even_odd.
            stroked.convertConicsToQuads(0.25)

            # 4. Union original + stroked via OpBuilder (the actual
            # "outset" operation: union of fill regions produces the
            # expanded outline).
            builder = pathops.OpBuilder()
            builder.add(path, pathops.PathOp.UNION)
            builder.add(stroked, pathops.PathOp.UNION)
            unioned = builder.resolve()

            # 5. v13: epsilon-merge pre-pass. The pathops library shipped
            # with this environment (0.9.x) doesn't expose an epsilon
            # kwarg on simplify(), so we emulate it by merging near-
            # coincident on-curve points via a segment-pen round-trip
            # BEFORE simplify(). Same user-visible result.
            if simplify_epsilon is not None:
                unioned = _apply_epsilon_merge(unioned, simplify_epsilon)

            # 6. Single high-level call: overlap removal + winding fix
            # (outer CCW / holes CW per CFF non-zero winding convention).
            result = pathops.simplify(unioned, fix_winding=True, clockwise=False)

            # 7. v13: optional stem-width quantization (BEFORE coord grid
            # so measured gaps are in pre-grid float coordinates).
            if stem_quantize > 0:
                result = _quantize_stem_widths(result, stem_quantize)

            # 8. v13: round every coordinate to the configured grid
            # (default 0.25u preserves v12 behavior). CFF charstring
            # operands are integer font units, so finer grids collapse
            # to the same output as 0.25u.
            rounded = pathops.Path()
            for verb, pts in result:
                rounded.add(
                    verb,
                    *[
                        (_round_to_grid(p[0], coord_grid),
                         _round_to_grid(p[1], coord_grid))
                        for p in pts
                    ],
                )

            # 9. Emit to T2CharString via the foundrytools helper
            cs_new = _t2_charstring_from_skia_path(
                rounded, char_strings[glyph_name]
            )
            char_strings[glyph_name] = cs_new
            thickened_count += 1
        except Exception as e:
            log.warning(f"Could not thicken '{glyph_name}': {e}")
            skipped_count += 1

    # 7. Recompile subroutines to extract repeated op subsequences from
    # the new explicit charstrings back into the local/global subroutine
    # tables. Without this the font would save with subroutines inlined
    # (typically +15-30% file size). Idempotent on already-tight tables.
    try:
        cff.cff.desubroutinize()
        log.info("Recompiled CFF subroutines via desubroutinize() "
                 "(restored subroutinized shape after thickening pass)")
    except Exception as e:
        log.warning(f"desubroutinize() failed (non-fatal): {e}; "
                    f"font will save with subroutines inlined")

    log.info(f"Thickening complete: {thickened_count} glyphs thickened, "
             f"{skipped_count} skipped")

def _thicken_truetype_glyphs(
    font: TTFont,
    multiplier: float,
    *,
    coord_grid: float = DEFAULT_COORD_GRID,
    simplify_epsilon: Optional[float] = DEFAULT_SIMPLIFY_EPSILON,
    stem_quantize: int = DEFAULT_STEM_QUANTIZE,
    flatten_segments: int = DEFAULT_FLATTEN_SEGMENTS,
) -> None:
    """Thicken TrueType outlines via pathops.stroke + pathops.simplify.

    Mirrors the CFF path above but emits back to TTGlyph instead of
    T2CharString. pathops.simplify() handles the overlap + winding
    issues that the previous skia-only version had to handle manually.

    v13: accepts the same three quantization knobs as the CFF path
    (coord_grid, simplify_epsilon, stem_quantize). See `thicken_glyphs`
    docstring for semantics.
    """
    cap_height = _resolve_cap_height(font, font.getGlyphSet())
    ref_stem, ref_source = _resolve_ref_stem(font, font.getGlyphSet(), cap_height)
    stroke_width = abs(multiplier - 1.0) * ref_stem
    log.info(f"ref_stem={ref_stem:.1f}u from {ref_source} (cap={cap_height:.0f}u)")

    if stroke_width < 1.0:
        log.debug(f"Stroke width {stroke_width:.2f}u too small, skipping "
                  f"(no perceptible change at body-text render sizes; "
                  f"raise --thickness for a visible effect)")
        return

    glyf = font['glyf']
    glyph_set = font.getGlyphSet()
    thickened_count = 0
    skipped_count = 0

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]
        # Skip empty (0) and composite (<0) glyphs; composites reference
        # simple glyphs which are thickened individually.
        if glyph.numberOfContours <= 0:
            skipped_count += 1
            continue
        if glyph.coordinates is None or len(glyph.coordinates) == 0:
            skipped_count += 1
            continue

        try:
            path = pathops.Path()
            pen = path.getPen(glyphSet=glyph_set)
            glyph_set[glyph_name].draw(pen)
            if len(list(path.contours)) == 0:
                skipped_count += 1
                continue

            # v14: optional polygon flatten (see _thicken_cff_glyphs)
            if flatten_segments > 0:
                path = _flatten_path(path, flatten_segments)

            # Stroke on a COPY (see _thicken_cff_glyphs for the bug rationale).
            stroked = pathops.Path()
            stroked.addPath(path)
            stroked.stroke(
                width=stroke_width,
                cap=pathops.LineCap.BUTT_CAP,
                join=pathops.LineJoin.ROUND_JOIN,
                miter_limit=4.0,
            )
            stroked.convertConicsToQuads(0.25)

            # Union original + stroked
            builder = pathops.OpBuilder()
            builder.add(path, pathops.PathOp.UNION)
            builder.add(stroked, pathops.PathOp.UNION)
            unioned = builder.resolve()

            # v13: epsilon-merge pre-pass (see _thicken_cff_glyphs)
            if simplify_epsilon is not None:
                unioned = _apply_epsilon_merge(unioned, simplify_epsilon)

            # TTF uses clockwise=True (outer CW, holes CCW) — opposite of CFF
            result = pathops.simplify(unioned, fix_winding=True, clockwise=True)

            # v13: optional stem-width quantization (pre-grid, so measurements
            # are in raw float coordinates).
            if stem_quantize > 0:
                result = _quantize_stem_widths(result, stem_quantize)

            # v13: round to the configured grid
            rounded = pathops.Path()
            for verb, pts in result:
                rounded.add(
                    verb,
                    *[
                        (_round_to_grid(p[0], coord_grid),
                         _round_to_grid(p[1], coord_grid))
                        for p in pts
                    ],
                )

            # Emit back to TTGlyph via a pathops PathPen on a new TTGlyphPen
            from fontTools.pens.ttGlyphPen import TTGlyphPen
            tt_pen = TTGlyphPen(glyph_set)
            rounded.draw(tt_pen)
            glyf[glyph_name] = tt_pen.glyph()
            thickened_count += 1
        except Exception as e:
            log.warning(f"Could not thicken '{glyph_name}': {e}")
            skipped_count += 1

    log.info(f"Thickening complete: {thickened_count} glyphs thickened, "
             f"{skipped_count} skipped")


# ---------------------------------------------------------------------------
# v14 polygon-flatten helpers
# ---------------------------------------------------------------------------

class _FlatteningPen:
    """SegmentPen wrapper that linearizes curves to polygons via adaptive
    De Casteljau subdivision.

    For each cubic `curveTo(p1, p2, p3)` we recursively split the curve
    at its midpoint until the control polygon is "flat enough" (max
    perpendicular distance from control points to the chord is < 0.25u).
    The curve becomes a chain of lineTo segments. `segments_per_curve`
    is the upper bound: we subdivide until flat or until we hit the
    cap, whichever comes first.

    qCurveTo (quadratic) is converted to cubic via the standard formula
    then flattened the same way.

    moveTo / lineTo / closePath / endPath pass through unchanged.

    Note: this implementation is intentionally NOT adaptive to curve
    length — using a fixed subdivision cap produces uniform visual
    quality and avoids the pathological case where a very subtle curve
    subdivides into thousands of segments. For our use case (stroke +
    union + epsilon merge) the goal is "enough segments to make
    epsilon-merge lethal to fringes", not "minimal segments that match
    the curve within X font units". The 0.25u flatness threshold
    (matching CFF charstring sub-unit precision) is generous enough
    that visible artifacts don't appear at body-text sizes.
    """

    def __init__(self, segments_per_curve: int, out_pen):
        self._max_segments = max(1, int(segments_per_curve))
        self._flat_epsilon = 0.25  # font units — flatness threshold
        self._out = out_pen
        self._start = None
        self._last = None

    def _flatten_cubic(self, p0, p1, p2, p3, depth=0):
        """Recursively subdivide a cubic Bezier until flat.

        Emits lineTo calls for the resulting polyline.
        """
        if depth >= self._max_segments:
            # Hit the segment cap — just lineTo the end point.
            self._out.lineTo(p3)
            self._last = p3
            return
        if _is_flat_enough(p0, p1, p2, p3, self._flat_epsilon):
            self._out.lineTo(p3)
            self._last = p3
            return
        # De Casteljau midpoint split
        m01 = _mid(p0, p1)
        m12 = _mid(p1, p2)
        m23 = _mid(p2, p3)
        m012 = _mid(m01, m12)
        m123 = _mid(m12, m23)
        m0123 = _mid(m012, m123)
        self._flatten_cubic(p0, m01, m012, m0123, depth + 1)
        self._flatten_cubic(m0123, m123, m23, p3, depth + 1)

    def _flatten_qcurve(self, p0, p1, p2, depth=0):
        """Convert quadratic to cubic then flatten.

        Standard qcurve→cubic: P1_c = P0 + 2/3*(P1-P0); P2_c = P2 + 2/3*(P1-P2).
        """
        cp1 = (p0[0] + (2.0 / 3.0) * (p1[0] - p0[0]),
               p0[1] + (2.0 / 3.0) * (p1[1] - p0[1]))
        cp2 = (p2[0] + (2.0 / 3.0) * (p1[0] - p2[0]),
               p2[1] + (2.0 / 3.0) * (p1[1] - p2[1]))
        self._flatten_cubic(p0, cp1, cp2, p2, depth + 1)

    def moveTo(self, p):
        self._start = p
        self._last = p
        self._out.moveTo(p)

    def lineTo(self, p):
        self._last = p
        self._out.lineTo(p)

    def curveTo(self, p1, p2, p3):
        if self._last is None:
            # Defensive: shouldn't happen if moveTo was emitted first
            self._out.moveTo((0, 0))
            self._last = (0, 0)
        self._flatten_cubic(self._last, p1, p2, p3)
        # _flatten_cubic updates self._last to p3 on each emit

    def qCurveTo(self, *points):
        # pathops doesn't typically emit qCurveTo (skia converts to
        # cubics internally), but we handle it for completeness.
        if not points:
            return
        if len(points) == 1:
            # Single on-curve, equivalent to lineTo
            self.lineTo(points[0])
            return
        # Convert multi-qCurveTo to a sequence of cubic-like flat lines
        cur = self._last
        for i, p in enumerate(points[:-1]):
            self._flatten_qcurve(cur, p, points[i + 1])
            cur = points[i + 1]

    def closePath(self):
        self._out.closePath()
        self._last = self._start

    def endPath(self):
        self._out.endPath()
        self._last = self._start

    # SegmentPen protocol glue — delegate everything else
    def setWidth(self, w): self._out.setWidth(w)
    def addComponent(self, *a, **kw): self._out.addComponent(*a, **kw)


def _mid(p0, p1):
    """Midpoint helper."""
    return ((p0[0] + p1[0]) * 0.5, (p0[1] + p1[1]) * 0.5)


def _is_flat_enough(p0, p1, p2, p3, epsilon):
    """True if the cubic curve p0-p3 is within `epsilon` font units of
    being a straight line from p0 to p3.

    Uses the standard control-polygon flatness test: compute the
    perpendicular distance from each intermediate control point
    (p1, p2) to the chord (p0, p3). If both distances are below
    `epsilon`, the curve is flat enough to linearize.
    """
    dx = p3[0] - p0[0]
    dy = p3[1] - p0[1]
    chord_len_sq = dx * dx + dy * dy
    if chord_len_sq < 1e-9:
        # Degenerate: chord has zero length; treat as flat if controls
        # are within epsilon of p0.
        return ((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2 < epsilon * epsilon
                and (p2[0] - p0[0]) ** 2 + (p2[1] - p0[1]) ** 2 < epsilon * epsilon)
    # Distance from p1 to chord p0-p3:
    #   |((p1 - p0) x (p3 - p0))| / |p3 - p0|
    # In 2D, cross = (p1x-p0x)*(p3y-p0y) - (p1y-p0y)*(p3x-p0x)
    cx1 = (p1[0] - p0[0]) * dy - (p1[1] - p0[1]) * dx
    cx2 = (p2[0] - p0[0]) * dy - (p2[1] - p0[1]) * dx
    d1_sq = (cx1 * cx1) / chord_len_sq
    d2_sq = (cx2 * cx2) / chord_len_sq
    eps_sq = epsilon * epsilon
    return d1_sq <= eps_sq and d2_sq <= eps_sq


def _flatten_path(path, segments_per_curve: int):
    """Return a new pathops.Path with all curves replaced by line segments.

    Implementation: replay the source path through a `_FlatteningPen`
    into a fresh `pathops.Path`. Output has only MOVE / LINE / CLOSE
    verbs — no CURVE or QCURVE.

    Caller is responsible for choosing `segments_per_curve`. As a
    rough rule of thumb: 4 segments/curve is "rough but functional"
    (visible facets on tight curves), 8 is "smooth-enough at 16px
    body text", 16 is "sub-pixel quality at all sizes".
    """
    if segments_per_curve <= 0:
        return path
    out = pathops.Path()
    out_pen = out.getPen()
    pen = _FlatteningPen(segments_per_curve, out_pen)
    path.draw(pen)
    return out


# ---------------------------------------------------------------------------
# v13 quantization helpers
# ---------------------------------------------------------------------------

class _EpsilonMergePen:
    """Segment pen wrapper that merges near-coincident on-curve points.

    Used as a pre-pass before `pathops.simplify()` to emulate the
    `epsilon` kwarg that some pathops forks expose. Skips any on-curve
    point within `epsilon` font units of the previous on-curve point
    on the current contour.

    `start` and `last` track the most recent on-curve positions so
    we can distance-test every new on-curve point.

    Note: off-curve (control) points are NEVER dropped, only their
    adjacent on-curve anchors. This preserves curve shape while
    reducing on-curve point density.
    """

    def __init__(self, epsilon: float, out_pen):
        self.epsilon = epsilon
        self._out = out_pen
        self._start = None
        self._last = None

    def _dist(self, p):
        if self._last is None:
            return float("inf")
        dx = p[0] - self._last[0]
        dy = p[1] - self._last[1]
        return (dx * dx + dy * dy) ** 0.5

    def moveTo(self, p):
        self._start = p
        self._last = p
        self._out.moveTo(p)

    def lineTo(self, p):
        if self._dist(p) < self.epsilon:
            return  # skip near-coincident
        self._last = p
        self._out.lineTo(p)

    def curveTo(self, p1, p2, p3):
        # off-curve control points always pass through; only test on-curve p3
        self._out.curveTo(p1, p2, p3)
        self._last = p3

    def qCurveTo(self, *points):
        self._out.qCurveTo(*points)
        self._last = points[-1]

    def closePath(self):
        # If the closing point is near-coincident with start, skip the
        # explicit close line. (pathops adds a segment automatically.)
        self._out.closePath()
        self._last = self._start

    def endPath(self):
        self._out.endPath()
        self._last = self._start

    # SegmentPen protocol glue — delegate everything else
    def setWidth(self, w): self._out.setWidth(w)
    def addComponent(self, *a, **kw): self._out.addComponent(*a, **kw)


def _apply_epsilon_merge(path, epsilon: float):
    """Return a new path with on-curve points within `epsilon` font units
    of the previous on-curve point merged (skipped).

    Implemented as a segment-pen round-trip: replay the path through
    `_EpsilonMergePen` into a fresh pathops.Path. The output has the
    same overall shape but fewer on-curve points, so the subsequent
    `pathops.simplify()` call has less work and produces a cleaner result.
    """
    if epsilon <= 0:
        return path
    from pathops import Path as _P
    out = _P()
    out_pen = out.getPen()
    pen = _EpsilonMergePen(epsilon, out_pen)
    path.draw(pen)
    return out


def _round_to_grid(value: float, grid: float) -> int:
    """Round `value` to the nearest multiple of `grid`, returned as int.

    For `grid == 1.0` (the pre-v13 default) this collapses to plain
    `otRound`. For finer grids it preserves sub-unit precision in the
    CFF integer-u emit step (CFF charstring operands are integer font
    units, so any grid < 1.0 effectively behaves as 1.0 in the binary
    output — but the float coordinate stays available for the
    quantization inspection steps).

    Examples (grid=0.5):
        12.34 -> 12
        12.50 -> 12  (Python banker's rounding via otRound; 12.5 -> 12)
        12.51 -> 13
        12.74 -> 13
    """
    if grid >= 1.0:
        return otRound(value)
    # Snap to `grid` and return int (CFF needs int operands)
    snapped = otRound(value / grid) * grid
    return otRound(snapped)


def _quantize_stem_widths(path, stem_quantize: int):
    """Adjust horizontal/vertical gaps between parallel contour edges
    so they land on multiples of `stem_quantize` font units.

    This is an approximation-grade stem-width quantizer. It walks the
    path looking for *nearly-axial* line segments (|dx| < tolerance or
    |dy| < tolerance), groups them by axis (horizontal vs vertical),
    and for each pair of opposing-parallel edges (e.g. top + bottom of
    a horizontal stem) measures the perpendicular gap and snaps it to
    the nearest `stem_quantize` multiple by translating one edge of
    the pair.

    Returns a new `pathops.Path` with adjusted contour edges. The
    caller is responsible for the subsequent coord-grid rounding step.

    Caveats:
        - Diagonals and curves are untouched (we only operate on stem-
          dominant geometry).
        - On heavily calligraphic or non-upright scripts, no axial
          edges may be found and the path is returned unchanged.
        - The heuristic quantizes each detected stem pair independently.
          Cross-stem interactions (e.g. stems that share a corner with
          another stem) may produce slightly off-grid corners after
          snap — those corners are re-snapped by the subsequent
          coord_grid pass.

    Implementation note: for simplicity we operate on pathops.Path by
    iterating verbs (each verb is one of moveTo/lineTo/curveTo/conicTo/
    qCurveTo) and emitting the same verb with adjusted coordinates.
    Only lineTo edges with one near-zero delta component are
    considered.
    """
    if stem_quantize <= 1:
        return path

    # 1. Collect horizontal segments: (x_min, x_max, y) at constant y
    horiz = []   # list of (x1, x2, y) for |dy|<tolerance segments
    vert = []    # list of (y1, y2, x) for |dx|<tolerance segments

    cur = (0.0, 0.0)
    start = None

    # Walk verbs. Path is iterable; each item is (verb, points).
    # We accumulate segments by tracking the current pen position.
    for verb, pts in path:
        if verb == 0:  # MOVE_TO
            cur = (float(pts[0][0]), float(pts[0][1]))
            start = cur
        elif verb == 1:  # LINE_TO
            new = (float(pts[0][0]), float(pts[0][1]))
            dx = new[0] - cur[0]
            dy = new[1] - cur[1]
            if abs(dy) < _AXIAL_TOLERANCE and abs(dx) > _AXIAL_TOLERANCE:
                horiz.append((min(cur[0], new[0]), max(cur[0], new[0]), cur[1]))
            elif abs(dx) < _AXIAL_TOLERANCE and abs(dy) > _AXIAL_TOLERANCE:
                vert.append((min(cur[1], new[1]), max(cur[1], new[1]), cur[0]))
            cur = new
        elif verb in (2, 3):  # CURVE_TO / QCURVE_TO — skip for axis detection
            cur = (float(pts[-1][0]), float(pts[-1][1]))
        elif verb == 4:  # CLOSE_PATH
            cur = start if start is not None else cur

    # 2. For each detected axial edge, decide if it's part of a stem pair.
    #    Simple heuristic: group horizontals by similar y, find pairs whose
    #    y-values differ by [1, 8 * ref_stem] font units. Same for verticals.
    #    Then snap the perpendicular gap to stem_quantize.
    h_offsets = _find_axial_pairs(horiz, axis='h', quantize=stem_quantize)
    v_offsets = _find_axial_pairs(vert, axis='v', quantize=stem_quantize)

    if not h_offsets and not v_offsets:
        return path

    # 3. Apply offsets by rewriting path verbs.
    out = pathops.Path()
    cur = (0.0, 0.0)
    start = None
    for verb, pts in path:
        if verb == 0:  # MOVE_TO
            new = (float(pts[0][0]), float(pts[0][1]))
            new = _apply_axis_offset(new, h_offsets, v_offsets)
            out.add(verb, new)
            cur = new
            start = cur
        elif verb == 1:  # LINE_TO
            new = (float(pts[0][0]), float(pts[0][1]))
            new = _apply_axis_offset(new, h_offsets, v_offsets)
            out.add(verb, new)
            cur = new
        elif verb in (2, 3):  # CURVE_TO / QCURVE_TO — pass endpoints through too
            new_pts = tuple(
                _apply_axis_offset((float(p[0]), float(p[1])), h_offsets, v_offsets)
                for p in pts
            )
            out.add(verb, *new_pts)
            cur = new_pts[-1]
        elif verb == 4:  # CLOSE_PATH
            out.add(verb)
    return out


def _find_axial_pairs(edges, axis: str, quantize: int):
    """For a list of axial edges, find pairs of opposing-parallel edges
    and return per-edge offset corrections.

    `edges` is a list of `(a, b, c)` tuples:
      - axis='h': (x1, x2, y) — a horizontal edge from x1..x2 at y=c
      - axis='v': (y1, y2, x) — a vertical edge from y1..y2 at x=c

    Returns a dict mapping edge-key (axis, c, mid_a, mid_b) -> signed
    y/x offset correction in font units.

    The pairing heuristic: two edges form a stem pair if
        * they are parallel (both h or both v)
        * their c-values differ by more than 1u (not the same edge)
        * the gap is < 8 * quantize (don't try to snap huge gaps —
          those are likely different features, not stems)
        * their a..b ranges overlap (so they actually face each other)
    """
    if not edges:
        return {}

    offsets = {}
    c_values = sorted({e[2] for e in edges})
    for i, c1 in enumerate(c_values):
        for c2 in c_values[i + 1:]:
            gap = abs(c2 - c1)
            if gap < 1.0 or gap > 8 * quantize:
                continue
            snapped = otRound(gap / quantize) * quantize
            if snapped <= 0:
                continue
            delta = snapped - gap
            # Half the correction to each edge to preserve centering
            half = delta / 2.0
            for e in edges:
                if e[2] == c1:
                    key = (axis, c1, e[0], e[1])
                    offsets[key] = offsets.get(key, 0.0) - half * (1 if c2 > c1 else -1)
                elif e[2] == c2:
                    key = (axis, c2, e[0], e[1])
                    offsets[key] = offsets.get(key, 0.0) + half * (1 if c2 > c1 else -1)
    return offsets


def _apply_axis_offset(point, h_offsets, v_offsets):
    """Apply per-edge corrections from `_find_axial_pairs` to a point.

    A point is affected by an h-offset only if it lies ON the relevant
    horizontal edge (within _AXIAL_TOLERANCE of the edge's y, and within
    the edge's x1..x2 range). Same logic for v-offsets.
    """
    x, y = point
    for (axis, c, a, b), off in h_offsets.items():
        if axis != 'h':
            continue
        if abs(y - c) < _AXIAL_TOLERANCE and a - _AXIAL_TOLERANCE <= x <= b + _AXIAL_TOLERANCE:
            y += off
    for (axis, c, a, b), off in v_offsets.items():
        if axis != 'v':
            continue
        if abs(x - c) < _AXIAL_TOLERANCE and a - _AXIAL_TOLERANCE <= y <= b + _AXIAL_TOLERANCE:
            x += off
    return (x, y)


# ---------------------------------------------------------------------------
# CLI (added in v13)
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for `python3 thicken.py`."""
    p = argparse.ArgumentParser(
        prog="thicken",
        description="Thicken (or thin) stems of a TTF/OTF font without "
                        "changing overall size or advance widths. v14 adds a "
                        "--flatten knob for polygon-mode (fringe-free) output.",
            )
    p.add_argument("input", help="Path to input TTF/OTF font")
    p.add_argument("output", help="Path to output TTF/OTF font")
    p.add_argument(
        "--thickness", "-t", type=float, default=2.5,
        help="Percent change in stem thickness (positive = thicker, "
             "default 2.5). 0 or negative = no-op.",
    )
    p.add_argument(
        "--grid", "-g", type=float, default=DEFAULT_COORD_GRID,
        help="Coordinate quantization grid in font units (default %.2f). "
             "Coarser values produce sturdier integer alignment for "
             "downstream hinting. Try 1.0 or 2.0 for hint-friendly output." % DEFAULT_COORD_GRID,
    )
    p.add_argument(
        "--simplify-epsilon", "-e", type=float, default=DEFAULT_SIMPLIFY_EPSILON,
        help="pathops.simplify() point-merge tolerance (default: library "
             "default). Higher = more aggressive merging, fewer segments. "
             "Try 1.0 for hinting coarsening, 2.0 for aggressive cleanup.",
    )
    p.add_argument(
        "--stem-quantize", "-q", type=int, default=DEFAULT_STEM_QUANTIZE,
        help="Force stem widths to multiples of N font units (ttfautohint "
             "style, default 0 = off). Typical: 10 (pixel-perfect on 1000 "
             "UPM at 16px), 20 (chunky).",
    )
    p.add_argument(
        "--flatten", "-f", type=int, default=DEFAULT_FLATTEN_SEGMENTS,
        help="v14: linearize curves to N line segments per cubic BEFORE "
             "the stroke+union pass (default 0 = off, curves preserved). "
             "Removes fringes that v13's curve-mode leaves behind. "
             "Typical: 4 (rough), 8 (smooth at 16px), 16 (sub-pixel). "
             "File size grows 3-8x.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p


def main(argv=None) -> int:
    """CLI entry point. Returns process exit code (0 = success)."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    log.info(f"Loading: {args.input}")
    font = TTFont(args.input)
    applied = thicken_glyphs(
        font,
        thickness_percent=args.thickness,
        coord_grid=args.grid,
        simplify_epsilon=args.simplify_epsilon,
        stem_quantize=args.stem_quantize,
        flatten_segments=args.flatten,
    )
    if not applied:
        log.warning("Thickening was a no-op (see earlier messages for reason)")
    log.info(f"Saving: {args.output}")
    font.save(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
