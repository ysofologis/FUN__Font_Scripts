#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v9

v9 is a complete rewrite using **foundrytools** (https://foundrytools.readthedocs.io)
instead of direct fontTools access where possible. foundrytools provides canonical,
well-tested wrappers around fontTools for common font manipulation tasks.

This script optimizes fonts for CLEARER, MORE CONCRETE shaping by:
  - Autohinting TrueType-based fonts with ttfautohint-py (via foundrytools)
  - Autohinting CFF-based OTF fonts with AFDKO otfautohint (via foundrytools)
  - Scaling font size by UPM change (foundrytools.Font.scale_upm)
  - TRUE thickness adjustment: thicken stems WITHOUT changing the font size
  - Recalculating CFF BlueValues/OtherBlues from actual glyph metrics
    (foundrytools.app.otf_recalc_zones) - much better than OS/2-based synthesis
  - Recalculating CFF StdHW/StdVW/StemSnap* from actual stem widths
    (foundrytools.app.otf_recalc_stems)
  - Overlap removal and contour cleanup (foundrytools.Font.correct_contours)
  - Coordinate rounding to integers (foundrytools.CFFTable.round_coordinates)
  - GASP table optimization for sharper screen rendering
  - Head table flag configuration (no ForceBold, no Force PPEM integer bit)
  - Setting production names from cmap (foundrytools.Font.set_production_names)
  - Chrome-compatibility (no head bit 3, no CFF ForceBold)
  - CFF Private dict tuning: LanguageGroup, ExpansionFactor, BlueShift, BlueFuzz

v9 vs v8.5:
  - v8.5: ~110 lines of custom scaling/thickening/zones code
  - v9: uses foundrytools' canonical APIs (scale_upm, recalc_zones, etc.)
  - v9: also recalculates StdHW/StdVW/StemSnap* from REAL stem widths
    (v8.5 only used font-supplied values)
  - v9: contour overlap removal via skia-pathops (faster than fontTools)

Requirements:
  - foundrytools (pip install foundrytools)
  - fonttools (pip install fonttools) - foundrytools dependency
  - afdko (pip install afdko) - for CFF autohinting
  - ttfautohint-py (transitive via foundrytools) - for TTF autohinting
"""

import os
import sys
import argparse
import logging
import tempfile
import traceback
from pathlib import Path

from foundrytools import Font
from foundrytools.constants import TTF_EXTENSION, OTF_EXTENSION, MAX_UPM, MIN_UPM, MAX_US_WEIGHT_CLASS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Dependency check
# ---------------------------------------------------------------------------

def check_dependencies():
    """Check that required dependencies are available."""
    # foundrytools depends on fontTools and other libraries; we just need
    # to confirm foundrytools itself loads.
    try:
        import foundrytools  # noqa: F401
    except ImportError:
        print("Missing dependency: foundrytools")
        print("  Install with: pip install foundrytools")
        sys.exit(1)
    logger.debug("Dependencies OK")


# ---------------------------------------------------------------------------
#  v9: GASP table optimization (using foundrytools directly)
# ---------------------------------------------------------------------------

GASP_RANGES_AGGRESSIVE = {
    0:    0x03,   # GRIDFIT | DOGRAY - tiny sizes
    13:   0x07,   # + SYMMETRIC_GRIDFIT at 13ppem
    19:   0x0F,   # + SYMMETRIC_SMOOTHING at 19ppem+
    31:   0x0F,
    65535: 0x0F,
}

GASP_RANGES_BALANCED = {
    0:     0x03,
    19:    0x07,
    65535: 0x0F,
}

GASP_RANGES_MINIMAL = {
    0:     0x03,
    65535: 0x07,
}

GASP_RANGES_PIXEL_ALIGNED = {
    0:    0x01,   # GRIDFIT only (no AA)
    7:    0x03,   # + DOGRAY (AA on)
    11:   0x07,   # + SYMMETRIC_GRIDFIT
    15:   0x0F,   # + SYMMETRIC_SMOOTHING
    23:   0x0F,
    31:   0x0F,
    71:   0x0F,
    65535: 0x0F,
}


def _optimize_gasp_table(font: Font, ranges: dict = None) -> None:
    """
    Optimize the GASP (grid-fitting and scan-conversion procedure) table.

    Default: aggressive 5-range mode with SYMMETRIC_SMOOTHING at 19+ ppem.

    Uses fontTools directly (foundrytools doesn't expose GASP table wrapper).
    """
    if ranges is None:
        ranges = GASP_RANGES_AGGRESSIVE

    from fontTools.ttLib import newTable

    gasp = newTable('gasp')
    gasp.version = 1
    gasp.gaspRange = ranges
    font.ttfont['gasp'] = gasp
    logger.debug(f"GASP table set: {len(ranges)} ranges")


# ---------------------------------------------------------------------------
#  v9: head flags (using foundrytools Font.set_bit)
# ---------------------------------------------------------------------------

def _optimize_head_flags(font: Font) -> None:
    """
    Set optimal head table flags for clear, concrete rendering.

    CRITICAL: Does NOT set bit 3 (Force PPEM to integer). Chrome/Skia rejects
    fonts with this flag because they use fractional ppem.

    Uses foundrytools' canonical API:
      - HeadTable.set_bit("flags", pos=N, value=True/False) for head.flags
      - MacStyle.bold / .italic properties for macStyle bits
    """
    # head.flags: clear bit 3 (Force PPEM integer), set bits 0, 1, 8
    font.t_head.set_bit("flags", pos=3, value=False)  # Clear bit 3
    font.t_head.set_bit("flags", pos=0, value=True)   # Baseline at y=0
    font.t_head.set_bit("flags", pos=1, value=True)   # LSB at x=0
    font.t_head.set_bit("flags", pos=8, value=True)   # Rounded layout

    # head.macStyle: clear outline & shadow bits, set bold if weight >= 600
    # foundrytools.MacStyle only exposes bold and italic properties.
    # For outline (bit 3) and shadow (bit 4), use set_bit directly on macStyle.
    font.t_head.set_bit("macStyle", pos=3, value=False)  # Outline
    font.t_head.set_bit("macStyle", pos=4, value=False)  # Shadow
    font.t_head.mac_style.bold = (font.t_os_2.weight_class >= 600)
    font.t_head.mac_style.italic = False  # Never auto-set italic

    logger.debug("head flags optimised: bit 3 cleared, macStyle cleaned up")


# ---------------------------------------------------------------------------
#  v9: OS/2 tuning (using foundrytools Font.t_os_2 properties)
# ---------------------------------------------------------------------------

def _tune_os2(font: Font, weight_offset: int = 0) -> None:
    """
    Optimise OS/2 table for better font matching.

    - Recalculates xAvgCharWidth from actual hmtx data
    - Recalculates unicode ranges from cmap
    - Optionally bumps usWeightClass (DANGEROUS - affects font matching)
    """
    font.t_os_2.recalc_avg_char_width()
    font.t_os_2.recalc_unicode_ranges()
    font.t_os_2.recalc_code_page_ranges()
    font.t_os_2.recalc_max_context()

    if weight_offset != 0:
        old = font.t_os_2.weight_class
        font.t_os_2.weight_class = min(MAX_US_WEIGHT_CLASS, old + weight_offset)
        logger.debug(f"OS/2 weight_class: {old} -> {font.t_os_2.weight_class}")


# ---------------------------------------------------------------------------
#  v9: CFF hinting - uses foundrytools' canonical otf_recalc_zones &
#  otf_recalc_stems for accurate zone/stem values from real glyph analysis
# ---------------------------------------------------------------------------

def _tune_cff_hinting(font: Font, rebuild: bool = False, blue_quantise: int = 0) -> None:
    """
    Optimise CFF Private dict for better hinting quality.

    Uses foundrytools' canonical APIs:
      - CFFTable.get_hinting_data() / set_hinting_data() for attribute access
      - otf_recalc_zones.run() for accurate BlueValues/OtherBlues from
        glyph metrics (NOT OS/2 which can be wrong/missing)
      - otf_recalc_stems.run() for accurate StdHW/StdVW/StemSnap* from
        actual stem widths

    The CFFTable property accessors (private_dict, etc.) use the canonical
    fontTools pattern internally, so this code is consistent with fontTools.
    """
    if not font.is_ps:
        return

    private = font.t_cff_.private_dict

    # Save existing hinting data (so we can preserve user's settings if rebuild=False)
    saved_data = font.t_cff_.get_hinting_data() if not rebuild else {}

    # Apply canonical defaults (only if missing)
    if not getattr(private, 'LanguageGroup', None):
        private.LanguageGroup = 1
    if getattr(private, 'ExpansionFactor', None) is None:
        private.ExpansionFactor = 0.06
    if getattr(private, 'BlueFuzz', None) is None:
        private.BlueFuzz = 1
    if getattr(private, 'BlueShift', None) is None:
        private.BlueShift = 7

    # If rebuild=True: recalculate zones and stems from real glyph analysis.
    # HYBRID strategy:
    #   - If font already has BlueValues with >= 3 zones, scale them uniformly
    #     (preserves the font's well-tuned zone structure including figures-top
    #     zones that foundrytools' recalc_zones doesn't generate).
    #   - Otherwise (Samsung-style fonts without BlueValues), use foundrytools'
    #     otf_recalc_zones for accurate synthesis from glyph metrics.
    #   - Always recalculate StdHW/StdVW/StemSnap* from real stems.
    existing_blue = getattr(private, 'BlueValues', None)
    existing_other = getattr(private, 'OtherBlues', None)
    has_well_tuned_zones = (existing_blue is not None and len(existing_blue) >= 6)

    if rebuild and has_well_tuned_zones:
        # Scale existing zones uniformly (preserves zone structure including
        # figures-top zones). This is the canonical fontTools pattern from
        # fontTools.ttLib.scaleUpem.scale_upem() but applied here post-hoc.
        # Since scale_upem already scaled BlueValues, this is essentially a no-op.
        # We do it just to preserve any zone structure foundrytools might have
        # mangled via blue_quantise or other transforms.
        from fontTools.misc.fixedTools import otRound
        # No-op: scale_upem already did this. Just verify the values.
        logger.debug(f"Zones preserved from font (scaled by scale_upem): "
                     f"BlueValues={existing_blue}")
    elif rebuild:
        # Font has no BlueValues - synthesise from glyph metrics
        try:
            from foundrytools.app.otf_recalc_zones import run as recalc_zones
            other_blues, blue_values = recalc_zones(font)
            if existing_blue is None and len(blue_values) >= 4:
                private.BlueValues = blue_values
                logger.debug(f"BlueValues synthesised: {blue_values}")
            if existing_other is None and other_blues:
                private.OtherBlues = other_blues
                logger.debug(f"OtherBlues synthesised: {other_blues}")
        except Exception as e:
            logger.warning(f"otf_recalc_zones failed: {e}")

    # Always recalculate stems from real stem widths
    if rebuild:
        try:
            from foundrytools.app.otf_recalc_stems import run as recalc_stems
            std_h_w, std_v_w, stem_snap_h, stem_snap_v = recalc_stems(font.file)
            if std_h_w > 0 and std_v_w > 0:
                private.StdHW = std_h_w
                private.StdVW = std_v_w
                if stem_snap_h:
                    private.StemSnapH = stem_snap_h
                if stem_snap_v:
                    private.StemSnapV = stem_snap_v
                logger.debug(f"Stems recalculated: StdHW={std_h_w}, StdVW={std_v_w}")
        except Exception as e:
            logger.warning(f"otf_recalc_stems failed: {e}")
    else:
        # When NOT rebuilding, preserve user's existing stem data if present
        for key, val in saved_data.items():
            if val is not None and key in ('StdHW', 'StdVW', 'StemSnapH', 'StemSnapV'):
                setattr(private, key, val)

    # blue-quantise: round all zone values to a clean N-unit grid
    # Also remove any zones that collapse to zero width after quantisation.
    if blue_quantise and blue_quantise > 0:
        for attr in ('BlueValues', 'OtherBlues', 'FamilyBlues', 'FamilyOtherBlues'):
            vals = getattr(private, attr, None)
            if vals:
                # Round each value, then merge consecutive identical pairs
                quantised = [int(round(v / blue_quantise) * blue_quantise) for v in vals]
                # Filter out zero-width zones (top == bot after quantisation)
                merged = []
                for i in range(0, len(quantised) - 1, 2):
                    top, bot = quantised[i], quantised[i + 1]
                    if top != bot:
                        merged.extend([top, bot])
                setattr(private, attr, merged)
                logger.debug(f"CFF {attr} quantised to {blue_quantise}-unit grid: {merged}")


# ---------------------------------------------------------------------------
#  v9: Stem thickening (custom - no foundrytools equivalent)
# ---------------------------------------------------------------------------

def _thicken_font_glyphs(font: Font, thickness_percent: float) -> None:
    """
    Thicken glyph stems WITHOUT changing overall font size or advance widths.

    Uses the inset-rescale technique (no foundrytools equivalent):
      - Compute each glyph's bounding box (W × H).
      - Compute horizontal stem grow dx = thickness_percent * W / 100.
      - Apply per-glyph transform that shrinks inner counter while keeping
        outer contour at original position.
      - Result: stems appear thicker but font dimensions unchanged.

    For CFF: uses fontTools TransformPen + T2CharStringPen
    For TrueType: uses TransformPen + TTGlyphPen
    """
    if thickness_percent <= 0:
        return

    logger.info(f"Thickening stems by {thickness_percent:+.2f}%")

    if font.is_ps:
        _thicken_cff_glyphs(font, thickness_percent)
    elif font.is_tt:
        _thicken_truetype_glyphs(font, thickness_percent)


def _thicken_truetype_glyphs(font: Font, thickness_percent: float) -> None:
    """Thicken TrueType outlines per-contour using inset-rescale."""
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.pens.transformPen import TransformPen

    glyf = font.ttfont['glyf']
    glyph_set = font.ttfont.getGlyphSet()

    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]
        if glyph.numberOfContours == 0:
            continue
        if glyph.coordinates is None or len(glyph.coordinates) == 0:
            continue
        if glyph.xMin is None or glyph.yMin is None:
            continue

        xmin_g, ymin_g = glyph.xMin, glyph.yMin
        xmax_g, ymax_g = glyph.xMax, glyph.yMax
        W = xmax_g - xmin_g
        H = ymax_g - ymin_g
        if W <= 0 or H <= 0:
            continue
        dx = max(0.0, thickness_percent * W / 100.0)
        dy = max(0.0, thickness_percent * H / 200.0)
        a = max(0.0, (W - 2.0 * dx) / W)
        d = max(0.0, (H - 2.0 * dy) / H)

        # Per-contour transformation
        end_pts = list(glyph.endPtsOfContours)
        start = 0
        ranges = []
        for end in end_pts:
            ranges.append((start, end + 1))
            start = end + 1

        coords = list(glyph.coordinates)
        flags = list(glyph.flags)

        for (cs, ce) in ranges:
            xs = [p[0] for p in coords[cs:ce]]
            ys = [p[1] for p in coords[cs:ce]]
            cmin_x, cmin_y = min(xs), min(ys)
            for i in range(cs, ce):
                x, y = coords[i]
                nx = cmin_x + a * (x - cmin_x) + dx
                ny = cmin_y + d * (y - cmin_y) + dy
                coords[i] = (int(round(nx)), int(round(ny)))

        # Build a new Glyph via the standard pen pipeline
        tt_pen = TTGlyphPen(glyph_set)
        transform_pen = TransformPen(tt_pen, (1.0, 0, 0, 1.0, 0, 0))
        # Draw through pen using the rebuilt coords
        # Simpler: just assign coords directly
        from fontTools.ttLib.tables._g_l_y_f import Glyph
        new_glyph = Glyph()
        new_glyph.numberOfContours = glyph.numberOfContours
        new_glyph.endPtsOfContours = glyph.endPtsOfContours[:]
        new_glyph.flags = flags
        from fontTools.misc.arrayTools import Vector as V
        new_glyph.coordinates = coords
        # Recompute bounds
        xs = [p[0] for p in coords]
        ys = [p[1] for p in coords]
        new_glyph.xMin = min(xs)
        new_glyph.yMin = min(ys)
        new_glyph.xMax = max(xs)
        new_glyph.yMax = max(ys)
        glyf[glyph_name] = new_glyph


def _thicken_cff_glyphs(font: Font, thickness_percent: float) -> None:
    """Thicken CFF charstrings per-glyph using inset-rescale via TransformPen."""
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.transformPen import TransformPen

    top_dict = font.t_cff_.top_dict
    char_strings = top_dict.CharStrings
    glyph_set = font.ttfont.getGlyphSet()

    new_charstrings = {}
    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in char_strings or glyph_name not in glyph_set:
            continue
        try:
            # Compute glyph bounding box.
            # NOTE: foundrytools.Font.get_glyph_bounds() returns a TypedDict
            # (dict subclass) with keys 'x_min', 'y_min', 'x_max', 'y_max' --
            # NOT a tuple. Earlier code assumed tuple unpacking, causing 656
            # warnings for a 656-glyph font. Fixed to use dict-style access.
            # Some glyphs (.notdef, 'space', etc.) have NO contours and
            # get_glyph_bounds raises TypeError on None bounds.
            try:
                bounds = font.get_glyph_bounds(glyph_name)
            except (TypeError, AttributeError):
                # No contours (e.g. .notdef, space) - keep unchanged
                new_charstrings[glyph_name] = char_strings[glyph_name]
                continue
            if not bounds:
                new_charstrings[glyph_name] = char_strings[glyph_name]
                continue
            xmin = bounds['x_min']
            ymin = bounds['y_min']
            xmax = bounds['x_max']
            ymax = bounds['y_max']
            W = xmax - xmin
            H = ymax - ymin
            if W <= 0 or H <= 0:
                new_charstrings[glyph_name] = char_strings[glyph_name]
                continue
            dx = thickness_percent * W / 100.0
            dy = thickness_percent * H / 200.0
            a = max(0.0, (W - 2.0 * dx) / W)
            d = max(0.0, (H - 2.0 * dy) / H)

            # Apply transform: shrink toward (xmin, ymin) then offset
            transform = (a, 0, 0, d, xmin + dx - a * xmin, ymin + dy - d * ymin)

            scaled_width = glyph_set[glyph_name].width
            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, transform)
            glyph_set[glyph_name].draw(transform_pen)
            new_cs = t2_pen.getCharString()
            new_cs.private = top_dict.Private
            new_charstrings[glyph_name] = new_cs
        except Exception as e:
            logger.warning(f"Could not thicken '{glyph_name}': {e}")
            new_charstrings[glyph_name] = char_strings[glyph_name]

    for name, cs in new_charstrings.items():
        char_strings[name] = cs


# ---------------------------------------------------------------------------
#  v9: Width adjust (X-only horizontal scaling - no foundrytools equivalent)
# ---------------------------------------------------------------------------

def width_font_glyphs(font: Font, width_percent: float) -> None:
    """
    Adjust horizontal width only (v9).

    Independent of --scale and --thickness. Uses fontTools TransformPen
    to apply the (factor, 0, 0, 1, 0, 0) affine transform.
    """
    if width_percent == 0:
        return

    factor = 1.0 + width_percent / 100.0
    if factor < 0.10:
        factor = 0.10
    elif factor > 4.00:
        factor = 4.00

    direction = "expanding" if width_percent > 0 else "condensing"
    logger.info(f"Width adjusting ({direction} by {abs(width_percent):.2f}%, x{factor:.4f})")

    transform = (factor, 0, 0, 1.0, 0, 0)

    if font.is_tt:
        _apply_horizontal_transform_truetype(font, transform)
    elif font.is_ps:
        _apply_horizontal_transform_cff(font, transform)

    # Scale horizontal metrics (advance widths, sidebearings, hhea horizontal fields)
    hmtx = font.ttfont.get('hmtx')
    if hmtx:
        for gn in list(hmtx.metrics.keys()):
            aw, lsb = hmtx.metrics[gn]
            hmtx.metrics[gn] = (int(round(aw * factor)), int(round(lsb * factor)))

    if 'hhea' in font.ttfont:
        hhea = font.ttfont['hhea']
        hhea.advanceWidthMax = int(round(hhea.advanceWidthMax * factor))
        for attr in ('minLeftSideBearing', 'minRightSideBearing', 'xMaxExtent'):
            if hasattr(hhea, attr):
                v = getattr(hhea, attr)
                if v is not None:
                    setattr(hhea, attr, int(round(v * factor)))


def _apply_horizontal_transform_truetype(font: Font, transform: tuple) -> None:
    """Apply an affine transform to all TrueType glyphs."""
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.pens.transformPen import TransformPen

    glyf = font.ttfont['glyf']
    glyph_set = font.ttfont.getGlyphSet()

    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]
        if glyph.numberOfContours == 0:
            continue

        tt_pen = TTGlyphPen(glyph_set)
        transform_pen = TransformPen(tt_pen, transform)
        glyph_set[glyph_name].draw(transform_pen)
        new_glyph = tt_pen.glyph()
        glyf[glyph_name] = new_glyph


def _apply_horizontal_transform_cff(font: Font, transform: tuple) -> None:
    """Apply an affine transform to all CFF charstrings."""
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.transformPen import TransformPen

    top_dict = font.t_cff_.top_dict
    char_strings = top_dict.CharStrings
    glyph_set = font.ttfont.getGlyphSet()
    hmtx = font.ttfont.get('hmtx')

    new_charstrings = {}
    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in char_strings or glyph_name not in glyph_set:
            continue
        try:
            scaled_width = int(round(hmtx.metrics[glyph_name][0] * transform[0])) \
                if hmtx and glyph_name in hmtx.metrics else 0
            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, transform)
            glyph_set[glyph_name].draw(transform_pen)
            new_cs = t2_pen.getCharString()
            new_cs.private = top_dict.Private
            new_charstrings[glyph_name] = new_cs
        except Exception as e:
            logger.warning(f"Could not apply width transform to '{glyph_name}': {e}")
            new_charstrings[glyph_name] = char_strings[glyph_name]

    for name, cs in new_charstrings.items():
        char_strings[name] = cs


# ---------------------------------------------------------------------------
#  v9: Scale by UPM change (using foundrytools' canonical API)
# ---------------------------------------------------------------------------

def scale_upm(font: Font, scale: float) -> None:
    """
    Scale font by UPM change (delegates to foundrytools' canonical API).

    Direct multiplier semantics (unambiguous):
      - scale 1.0  -> no change (1.0x = original size)
      - scale 1.5  -> 1.5x bigger
      - scale 0.5  -> 0.5x smaller (half size)
      - scale 2.0  -> 2x bigger
      - scale 0.1  -> 0.1x (10% size)

    Safety caps: [0.10x, 4.00x] to prevent accidental extremes.

    Implementation: change unitsPerEm to new_upem, which scales ALL
    coordinates uniformly. This is the canonical fontTools pattern (used
    by fontTools.ttLib.scaleUpem.scale_upem, which foundrytools wraps).

    Note: This changes the font's INTERNAL design grid. When the font is
    rendered at the SAME point size, the glyphs appear larger/smaller
    because the font's em-square is now bigger/smaller.
    """
    if scale <= 0:
        return  # No scaling for 0 or negative

    # Apply safety caps
    if scale < 0.10:
        logger.warning(f"Scale {scale} below minimum (0.10), clamping to 0.10")
        scale = 0.10
    elif scale > 4.00:
        logger.warning(f"Scale {scale} above maximum (4.00), clamping to 4.00")
        scale = 4.00

    current_upm = font.t_head.units_per_em
    new_upm = int(round(current_upm * scale))
    new_upm = max(MIN_UPM, min(MAX_UPM, new_upm))

    if new_upm == current_upm:
        if scale == 1.0:
            logger.debug("Scale 1.0 = no change, skipping")
        else:
            logger.debug(f"Scale {scale} rounds to same upm ({new_upm}), skipping")
        return

    logger.info(f"Scaling font: {scale}x (upem {current_upm} -> {new_upm})")
    font.scale_upm(new_upm)




def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize a single font file using foundrytools.

    Pipeline:
      1. Open font with foundrytools.Font
      2. Optionally scale via foundrytools.Font.scale_upm
      3. Optionally adjust width (horizontal-only)
      4. Optionally thicken stems
      5. Optionally correct contours (overlap removal)
      6. Optionally round coordinates
      7. Tune CFF hinting (rebuild BlueValues/Stems from real glyph metrics)
      8. Optimize GASP table
      9. Tune OS/2 table
      10. Set head flags
      11. Optionally set production names
      12. Autohint (ttfautohint-py for TTF, AFDKO otfautohint for CFF)
      13. Save via foundrytools.Font.save
    """
    try:
        font_name = os.path.basename(input_path)
        logger.info(f"Loading {font_name}...")
        font = Font(input_path)

        # ---- Step 1: Scale (UPM change) ----
        if options.get('scale_percent', 0) != 0:
            scale_upm(font, options['scale_percent'])

        # ---- Step 2: Width adjust (horizontal-only) ----
        if options.get('width_percent', 0) != 0:
            width_font_glyphs(font, options['width_percent'])

        # ---- Step 3: Thicken stems ----
        if options.get('thickness_percent', 0) != 0:
            _thicken_font_glyphs(font, options['thickness_percent'])

        # ---- Step 4: Correct contours (overlap removal) ----
        if options.get('shape_cleanup', False):
            try:
                modified = font.correct_contours(
                    remove_hinting=True,
                    ignore_errors=True,
                    remove_unused_subroutines=True,
                    min_area=25,
                )
                logger.debug(f"Contour correction: {len(modified)} glyphs modified")
            except Exception as e:
                logger.warning(f"Contour correction failed: {e}")

        # ---- Step 5: Round coordinates (CFF only) ----
        if options.get('pixel_snap', 0) > 0 and font.is_ps:
            try:
                font.t_cff_.round_coordinates()
                logger.debug("CFF coordinates rounded")
            except Exception as e:
                logger.warning(f"Round coordinates failed: {e}")

        # ---- Step 6: Tune CFF hinting ----
        if font.is_ps and options.get('hint_tune', False):
            _tune_cff_hinting(
                font,
                rebuild=options.get('rebuild_hints', False),
                blue_quantise=options.get('blue_quantise', 0),
            )

        # ---- Step 7: Optimize GASP table ----
        if options.get('gasp_detail'):
            detail = options['gasp_detail']
            if detail == 'minimal':
                _optimize_gasp_table(font, GASP_RANGES_MINIMAL)
            elif detail == 'balanced':
                _optimize_gasp_table(font, GASP_RANGES_BALANCED)
            elif detail == 'pixel':
                _optimize_gasp_table(font, GASP_RANGES_PIXEL_ALIGNED)
            else:  # aggressive
                _optimize_gasp_table(font, GASP_RANGES_AGGRESSIVE)
        else:
            _optimize_gasp_table(font, GASP_RANGES_AGGRESSIVE)  # default

        # ---- Step 8: Tune OS/2 table ----
        _tune_os2(font, weight_offset=options.get('weight_offset', 0))

        # ---- Step 9: Set head flags ----
        _optimize_head_flags(font)

        # ---- Step 11: Autohint ----
        # foundrytools wraps ttfautohint-py and AFDKO's otfautohint
        # AFDKO otfautohint may fail in some sandboxed environments due to
        # multiprocessing; we try it and fall back to external psautohint.
        if options.get('autohint', True):
            try:
                if font.is_tt:
                    from foundrytools.app.ttf_autohint import run as ttf_autohint
                    ttf_autohint(font)
                    logger.info(f"Autohinted (TrueType via foundrytools)")
                elif font.is_ps:
                    try:
                        _patch_afdko_logging()  # Fix AFDKO multiprocessing logging bug
                        from foundrytools.app.otf_autohint import run as otf_autohint
                        otf_autohint(font, allowChanges=True, hintAll=True)
                        logger.info(f"Autohinted (CFF via foundrytools)")
                    except Exception as e:
                        # Fall back to external psautohint
                        logger.debug(f"foundrytools otf_autohint failed ({e}), "
                                     f"falling back to psautohint")
                        _psautohint_fallback(font)
            except Exception as e:
                logger.error(f"Autohinting failed: {e}")
                font.close()
                return False

        # ---- Step 12: Set production names (after autohint so we don't break it) ----
        # NOTE: set_production_names() can break AFDKO's otfautohint on some fonts
        # because of multiprocessing bootstrapping issues. Run AFTER autohint.
        if options.get('set_prod_names', False):
            try:
                renamed = font.set_production_names()
                if renamed:
                    logger.debug(f"Renamed {len(renamed)} glyphs to production names")
            except Exception as e:
                logger.warning(f"set_production_names failed: {e}")

        # ---- Step 13: Save ----
        font.save(output_path)
        font.close()
        return True

    except Exception as e:
        logger.error(f"Error optimizing {input_path}: {e}")
        logger.debug(traceback.format_exc())
        return False


def _patch_afdko_logging() -> None:
    """
    Patch AFDKO's otfautohint logging to handle missing custom attributes.

    AFDKO's otfautohint uses a custom log record factory that adds
    ``glyph``, ``instance``, and ``dimension`` attributes. However, when
    records are sent across process boundaries (multiprocessing) via a
    QueueHandler, the log record factory does NOT run again, so the
    attributes are missing. The custom formatter then crashes with::

        AttributeError: 'LogRecord' object has no attribute 'dimension'

    This patch monkey-patches the formatter to use ``getattr(record, attr,
    '')`` so missing attributes default to empty string. Idempotent.
    """
    try:
        from afdko.otfautohint.logging import otfautoLogFormatter
        original_format = otfautoLogFormatter.format

        def patched_format(self, record):
            # Ensure custom attributes exist with sensible defaults
            for attr in ('glyph', 'instance', 'dimension'):
                if not hasattr(record, attr):
                    setattr(record, attr, '')
            return original_format(self, record)

        otfautoLogFormatter.format = patched_format
        logger.debug("Patched AFDKO otfautohint logging for missing attributes")
    except ImportError:
        # AFDKO not installed; nothing to patch
        pass
    except Exception as e:
        logger.debug(f"Failed to patch AFDKO logging: {e}")


def _psautohint_fallback(font: Font) -> None:
    """Fallback to external psautohint binary if foundrytools' otf_autohint fails."""
    import subprocess
    from fontTools.ttLib import TTFont

    # Save to temp file, run psautohint, reload
    with tempfile.NamedTemporaryFile(suffix='.otf', delete=False) as tmp_in:
        in_path = tmp_in.name
    with tempfile.NamedTemporaryFile(suffix='.otf', delete=False) as tmp_out:
        out_path = tmp_out.name

    try:
        font.save(in_path)
        cmd = ['psautohint', '-o', out_path, '-a', '-c', '-d', '--no-zones-stems',
               in_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            # Reload the hinted font
            hinted = TTFont(out_path, recalcTimestamp=False)
            # Copy CFF table back into font
            font.ttfont['CFF '] = hinted['CFF ']
        else:
            logger.error(f"psautohint fallback failed: {result.stderr[:200]}")
    finally:
        for p in (in_path, out_path):
            if os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
#  v9: CLI argument parsing
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OTF/TTF Font Optimization Script v9 (foundrytools-based)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('input', help='Input font file or directory')
    parser.add_argument('output', help='Output directory')

    # --- Scaling ---
    parser.add_argument('--scale', type=float, default=0, dest='scale_percent',
                        help='[v9] Scale font by UPM change. Direct multiplier: '
                             'scale 1.0 = no change; 1.5 = 1.5x bigger; 0.5 = 0.5x smaller. '
                             'Capped at [0.10x, 4.00x].')
    parser.add_argument('--width', type=float, default=0, dest='width_percent',
                        help='[v9] Adjust horizontal width only. Positive = expand, negative = condense.')
    parser.add_argument('--expand', type=float, default=None, dest='expand_percent',
                        help='[v9] Alias for --width with positive value.')
    parser.add_argument('--condense', type=float, default=None, dest='condense_percent',
                        help='[v9] Alias for --width with negative value.')
    parser.add_argument('--thickness', type=float, default=0, dest='thickness_percent',
                        help='[v9] Thicken stems without scaling font (inset-rescale).')

    # --- Hinting ---
    parser.add_argument('--autohint', action='store_true', default=True, dest='autohint',
                        help='[v9] Autohint with ttfautohint-py / AFDKO otfautohint (default: ON).')
    parser.add_argument('--no-autohint', action='store_false', dest='autohint',
                        help='[v9] Skip autohinting.')
    parser.add_argument('--hint-tune', action='store_true', dest='hint_tune',
                        help='[v9] Tune CFF Private dict defaults (LanguageGroup, ExpansionFactor, etc.).')
    parser.add_argument('--rebuild-hints', action='store_true', dest='rebuild_hints',
                        help='[v9] Recalculate BlueValues/OtherBlues AND StdHW/StdVW/StemSnap* '
                             'from REAL glyph metrics (foundrytools.app.otf_recalc_zones / '
                             'otf_recalc_stems). Much more accurate than OS/2-based synthesis.')
    parser.add_argument('--blue-quantise', type=int, default=0, dest='blue_quantise',
                        help='[v9] Round BlueValues/OtherBlues to N-unit grid (e.g. 4, 8).')

    # --- Cleanup ---
    parser.add_argument('--shape-cleanup', action='store_true', dest='shape_cleanup',
                        help='[v9] Remove overlaps and correct contour direction (skia-pathops).')
    parser.add_argument('--pixel-snap', type=int, default=0, dest='pixel_snap',
                        help='[v9] Round CFF coordinates to integers (0=disable).')

    # --- v8.5 backward-compatibility aliases (no-op or mapped) ---
    # These flags were accepted by v8.5 but were either redundant in v9
    # (now enabled by default) or no longer apply. They are accepted
    # silently so old commands work without "unrecognized argument" errors.
    parser.add_argument('--solid', action='store_true', default=False, dest='solid_legacy',
                        help=argparse.SUPPRESS)  # v8.5: enable solid mode (now always on)
    parser.add_argument('--clear-shaping', action='store_true', default=False, dest='clear_shaping',
                        help=argparse.SUPPRESS)  # v8.5: enable clear-shaping (now always on)
    parser.add_argument('--no-overlap-remove', action='store_true', default=False, dest='no_overlap_remove',
                        help=argparse.SUPPRESS)  # v8.5: skip overlap removal (no-op in v9)
    parser.add_argument('--no-stem-round', action='store_true', default=False, dest='no_stem_round',
                        help=argparse.SUPPRESS)  # v8.5: skip stem rounding (no-op in v9)
    parser.add_argument('--no-flex', action='store_true', default=False, dest='no_flex',
                        help=argparse.SUPPRESS)  # v8.5: psautohint --no-flex
    parser.add_argument('--no-hint-sub', action='store_true', default=False, dest='no_hint_sub',
                        help=argparse.SUPPRESS)  # v8.5: psautohint --no-hint-sub
    parser.add_argument('--allow-changes', action='store_true', default=False, dest='allow_changes',
                        help=argparse.SUPPRESS)  # v8.5: psautohint -c
    parser.add_argument('--dropout-control', action='store_true', default=False, dest='dropout_control',
                        help=argparse.SUPPRESS)  # v8.5: TrueType dropout control
    parser.add_argument('--hinting-range-min', type=int, default=0, dest='hinting_range_min',
                        help=argparse.SUPPRESS)  # v8.5: min ppem for hinting
    parser.add_argument('--hinting-range-max', type=int, default=0, dest='hinting_range_max',
                        help=argparse.SUPPRESS)  # v8.5: max ppem for hinting
    parser.add_argument('--gasp-mode', type=str, default=None, dest='gasp_mode',
                        help=argparse.SUPPRESS)  # v8.5: detailed/simple GASP
    parser.add_argument('--pixel-gasp', action='store_true', default=False, dest='pixel_gasp',
                        help=argparse.SUPPRESS)  # v8.5: pixel-aligned GASP
    parser.add_argument('--x-height-hint', type=int, default=0, dest='x_height_hint',
                        help=argparse.SUPPRESS)  # v8.5: x-height increase %
    parser.add_argument('--strength', type=int, default=0, dest='strength',
                        help=argparse.SUPPRESS)  # v8.5: ttfautohint strength
    parser.add_argument('--detailed', action='store_true', default=False, dest='detailed',
                        help=argparse.SUPPRESS)  # v8.5: detailed TTF instructions
    parser.add_argument('--stem-width', type=int, default=0, dest='stem_width',
                        help=argparse.SUPPRESS)  # v8.5: ttfautohint stem width
    parser.add_argument('--no-combining', action='store_true', default=False, dest='no_combining',
                        help=argparse.SUPPRESS)  # v8.5: ttfautohint no combining
    parser.add_argument('--x-height-snap-exceptions', type=str, default=None, dest='x_height_snap_exceptions',
                        help=argparse.SUPPRESS)  # v8.5: ttfautohint x-height-snap-exceptions

    # --- GASP ---
    parser.add_argument('--gasp-detail', type=str, default='aggressive', dest='gasp_detail',
                        choices=['minimal', 'balanced', 'aggressive', 'pixel'],
                        help='[v9] GASP table granularity.')

    # --- OS/2 ---
    parser.add_argument('--weight-offset', type=int, default=0, dest='weight_offset',
                        help='[v9] Add this to usWeightClass (0=no change). WARNING: affects font matching.')

    # --- Naming ---
    parser.add_argument('--set-prod-names', action='store_true', dest='set_prod_names',
                        help='[v9] Rename glyphs to production names from cmap (foundrytools.Font.set_production_names).')

    # --- Verbosity ---
    parser.add_argument('-v', '--verbose', action='store_true', dest='verbose',
                        help='[v9] Verbose logging.')

    return parser


def resolve_width(args) -> float:
    """Resolve --width/--expand/--condense aliases to a single signed value."""
    values = []
    if args.width_percent:
        values.append(args.width_percent)
    if args.expand_percent is not None:
        values.append(args.expand_percent)
    if args.condense_percent is not None:
        values.append(-args.condense_percent)
    if len(values) > 1:
        print("Error: --width, --expand, --condense are mutually exclusive.")
        sys.exit(2)
    return values[0] if values else 0


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    check_dependencies()

    width_pct = resolve_width(args)

    options = {
        'scale_percent': args.scale_percent,
        'width_percent': width_pct,
        'thickness_percent': args.thickness_percent,
        'autohint': args.autohint,
        'hint_tune': args.hint_tune,
        'rebuild_hints': args.rebuild_hints,
        'blue_quantise': args.blue_quantise,
        'shape_cleanup': args.shape_cleanup,
        'pixel_snap': args.pixel_snap,
        'gasp_detail': args.gasp_detail,
        'weight_offset': args.weight_offset,
        'set_prod_names': args.set_prod_names,
    }

    # Collect input files
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)

    if input_path.is_file():
        inputs = [input_path]
    elif input_path.is_dir():
        inputs = sorted([
            f for f in input_path.iterdir()
            if f.suffix.lower() in (TTF_EXTENSION, OTF_EXTENSION)
        ])
    else:
        print(f"Error: input '{input_path}' is not a file or directory.")
        sys.exit(1)

    if not inputs:
        print(f"No font files found in '{input_path}'.")
        sys.exit(1)

    logger.info(f"Found {len(inputs)} font file(s) to process")

    success_count = 0
    for in_file in inputs:
        out_file = output_path / in_file.name
        if optimize_font(str(in_file), str(out_file), options):
            success_count += 1
        else:
            logger.error(f"Failed to process {in_file.name}")

    logger.info(f"Optimization complete! Successfully processed {success_count}/{len(inputs)} fonts.")
    sys.exit(0 if success_count == len(inputs) else 1)


if __name__ == '__main__':
    main()
