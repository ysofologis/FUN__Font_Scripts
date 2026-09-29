#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v10

v10 is a foundrytools-based rewrite of v8.5. It preserves v8.5's CLI surface and
semantics (dual-mode --scale, percent-based --thickness, --width/--expand/--condense,
--hint-tune, --rebuild-hints, --blue-quantise, --shape-cleanup, --gasp-detail,
--pixel-gasp, --solid, --clear-shaping, etc.) but implements the work with the
canonical foundrytools APIs wherever one exists:

  - foundrytools.Font.scale_upm()        -> uniform font scaling
  - foundrytools.Font.correct_contours() -> overlap removal + winding fix (skia-pathops)
  - foundrytools.Font.t_cff_.round_coordinates() -> integer coordinate snapping (CFF)
  - foundrytools.Font.t_cff_.private_dict        -> CFF Private dict access
  - foundrytools.app.otf_recalc_zones.run()      -> BlueValues/OtherBlues from real glyphs
  - foundrytools.app.otf_recalc_stems.run()      -> StdHW/StdVW/StemSnap* from real stems
  - foundrytools.app.ttf_autohint.run()          -> TrueType autohint (ttfautohint-py)
  - foundrytools.app.otf_autohint.run()          -> CFF autohint (AFDKO otfautohint)
  - foundrytools.Font.t_os_2.* recalc_*          -> OS/2 metric recalculation
  - foundrytools.Font.t_head.set_bit()           -> head flags (Chrome-safe)
  - foundrytools.Font.t_post.underline_thickness  -> underline weight
  - foundrytools.Font.set_production_names()      -> production glyph names

Where foundrytools has no wrapper (GASP table, horizontal-only width transform,
skia stem thickening, hmtx/hhea horizontal scaling, PREP dropout control), we drop
to fontTools / skia-python directly, exactly as v8.5 did, but operate on the same
Font.ttfont object so the whole pipeline stays consistent.

Chrome/Skia compatibility rules from v8.5 are preserved:
  - No CFF ForceBold (Skia rejects it)
  - No head bit 3 (Force PPEM integer) — Chrome uses fractional ppem
  - GASP enables AA at all sizes (no monochrome mode -> no fringes)
  - GASP + head flags saved in two steps (fontTools checksum bug workaround)

Requirements:
  - foundrytools  (pip install foundrytools)
  - fonttools    (foundrytools dependency)
  - afdko        (for CFF autohinting via foundrytools.app.otf_autohint)
  - ttfautohint-py (transitive via foundrytools) for TrueType autohinting
  - skia-python   (optional, only needed for --thickness stem thickening)
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

# skia-python is optional (only needed for --thickness stem thickening)
try:
    import skia  # noqa: F401
    HAS_SKIA = True
except ImportError:
    HAS_SKIA = False

# fontTools pieces used where foundrytools has no wrapper
from fontTools.ttLib import newTable
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen, GlyphCoordinates
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.reverseContourPen import ReverseContourPen
from fontTools.pens.pointPen import SegmentToPointPen, PointToSegmentPen
from fontTools.pens.filterPen import ContourFilterPointPen

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  GASP table ranges (v8.5 / v9 consolidated)
# ---------------------------------------------------------------------------

GASP_RANGES_DETAILED = {
    0:     0x03,   # GRIDFIT | DOGRAY
    7:     0x03,
    12:    0x07,   # + SYMMETRIC_GRIDFIT
    19:    0x0F,   # + SYMMETRIC_SMOOTHING
    65535: 0x0F,
}

GASP_RANGES_SOLID = {
    0:     0x03,   # GRIDFIT | DOGRAY
    12:    0x07,   # + SYMMETRIC_GRIDFIT
    32:    0x0F,   # + SYMMETRIC_SMOOTHING
    65535: 0x0F,
}

GASP_RANGES_SIMPLE = {
    0:     0x03,
    65535: 0x03,
}

GASP_RANGES_PIXEL_ALIGNED = {
    0:     0x01,   # GRIDFIT only (no AA) - sharp at tiny sizes
    7:     0x03,   # + DOGRAY (AA on)
    11:    0x07,   # + SYMMETRIC_GRIDFIT
    15:    0x0F,   # + SYMMETRIC_SMOOTHING
    23:    0x0F,
    31:    0x0F,
    71:    0x0F,
    65535: 0x0F,
}

GASP_RANGES_AGGRESSIVE = {
    0:     0x03,
    13:    0x07,
    19:    0x0F,
    31:    0x0F,
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


# ---------------------------------------------------------------------------
#  Dependency check
# ---------------------------------------------------------------------------

def check_dependencies() -> bool:
    """Verify foundrytools (and its key optional backends) are importable."""
    try:
        import foundrytools  # noqa: F401
    except ImportError:
        print("Missing dependency: foundrytools")
        print("  Install with: pip install foundrytools")
        return False

    missing = []
    for mod in ("fontTools", "ttfautohint", "afdko"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        logger.warning("Optional backend(s) missing: %s. CFF autohinting may fall "
                       "back to the external psautohint binary.", ", ".join(missing))
    return True


# ---------------------------------------------------------------------------
#  v8.5 width/condense/expand resolution
# ---------------------------------------------------------------------------

def _resolve_width_or_die(args) -> None:
    """Validate --width/--expand/--condense mutual exclusivity (v8.5)."""
    explicit_width = args.width_percent != 0
    if args.expand_percent is not None and explicit_width:
        print("ERROR: --width and --expand are mutually exclusive.", file=sys.stderr)
        sys.exit(2)
    if args.condense_percent is not None and explicit_width:
        print("ERROR: --width and --condense are mutually exclusive.", file=sys.stderr)
        sys.exit(2)
    if args.expand_percent is not None and args.condense_percent is not None:
        print("ERROR: --expand and --condense are mutually exclusive.", file=sys.stderr)
        sys.exit(2)


def _resolve_width(args) -> float:
    """Compute final width_percent from --width/--expand/--condense (v8.5)."""
    if args.expand_percent is not None:
        return abs(args.expand_percent)
    if args.condense_percent is not None:
        return -abs(args.condense_percent)
    return args.width_percent


# ---------------------------------------------------------------------------
#  GASP table (no foundrytools wrapper -> fontTools newTable)
# ---------------------------------------------------------------------------

def _optimize_gasp_table(font: Font, ranges: dict) -> None:
    """Write a fresh GASP table with the given ppem->flags ranges."""
    gasp = newTable('gasp')
    gasp.version = 1
    gasp.gaspRange = dict(ranges)
    font.ttfont['gasp'] = gasp
    logger.debug(f"GASP table set: {len(ranges)} ranges")


# ---------------------------------------------------------------------------
#  head flags (foundrytools HeadTable.set_bit + MacStyle)
# ---------------------------------------------------------------------------

def _optimize_head_flags(font: Font) -> None:
    """
    Set optimal head table flags for clear, concrete rendering.

    CRITICAL: bit 3 (Force PPEM integer) is cleared — Chrome/Skia reject it.
    Outline (bit 3) and Shadow (bit 4) bits in macStyle are cleared.
    Bold bit is set only when the font is semibold+ (weight >= 600).
    """
    head = font.t_head
    # Clear bit 3 (Force PPEM integer)
    head.set_bit("flags", pos=3, value=False)
    # Set: baseline at y=0 | lsb at x=0 | rounded layout
    head.set_bit("flags", pos=0, value=True)
    head.set_bit("flags", pos=1, value=True)
    head.set_bit("flags", pos=8, value=True)

    # macStyle: clear Outline (3) and Shadow (4)
    head.set_bit("macStyle", pos=3, value=False)
    head.set_bit("macStyle", pos=4, value=False)
    # Bold bit only for semibold+
    head.mac_style.bold = (font.t_os_2.weight_class >= 600)

    logger.debug("head flags optimised: bit 3 cleared, macStyle cleaned up")


# ---------------------------------------------------------------------------
#  OS/2 tuning (foundrytools OS2Table recalc_* + weight class)
# ---------------------------------------------------------------------------

def _tune_os2(font: Font, weight_offset: int = 0) -> None:
    """Recalculate OS/2 metrics and optionally bump usWeightClass."""
    os2 = font.t_os_2
    try:
        os2.recalc_avg_char_width()
    except Exception as e:
        logger.debug(f"recalc_avg_char_width failed: {e}")
    try:
        os2.recalc_unicode_ranges()
    except Exception as e:
        logger.debug(f"recalc_unicode_ranges failed: {e}")
    try:
        os2.recalc_code_page_ranges()
    except Exception as e:
        logger.debug(f"recalc_code_page_ranges failed: {e}")
    # usMaxContext is only defined in OS/2 versions 2 and up
    if os2.version >= 2:
        try:
            os2.recalc_max_context()
        except Exception as e:
            logger.debug(f"recalc_max_context failed: {e}")
    else:
        logger.debug("OS/2 version < 2, skipping recalc_max_context")

    if weight_offset != 0:
        old = os2.weight_class
        os2.weight_class = min(MAX_US_WEIGHT_CLASS, old + weight_offset)
        logger.debug(f"OS/2 weight_class: {old} -> {os2.weight_class}")


# ---------------------------------------------------------------------------
#  CFF hint tuning (foundrytools private_dict + recalc_zones/recalc_stems)
# ---------------------------------------------------------------------------

def _tune_cff_hinting(font: Font, rebuild: bool = False, blue_quantise: int = 0) -> None:
    """
    Optimise CFF Private dict for better hinting quality.

    Uses foundrytools' canonical accessors:
      - font.t_cff_.private_dict for attribute get/set
      - foundrytools.app.otf_recalc_zones.run() for accurate BlueValues/OtherBlues
        from real glyph metrics (NOT OS/2 which can be wrong/missing)
      - foundrytools.app.otf_recalc_stems.run() for StdHW/StdVW/StemSnap* from
        actual stem widths

    Strategy (v8.5 BUGFIX aware):
      - Always set LanguageGroup=1, ExpansionFactor, BlueShift, BlueFuzz if missing
      - If rebuild and font already has well-tuned zones (>= 6 entries): keep them
        (scale_upem already scaled them during --scale)
      - If rebuild and font has no/weak zones: synthesise from real glyph metrics
      - Always recalc stems from real stem widths when rebuilding
    """
    if not font.is_ps:
        return

    private = font.t_cff_.private_dict

    # Canonical Latin defaults (only if missing)
    if not getattr(private, 'LanguageGroup', None):
        private.LanguageGroup = 1
    if getattr(private, 'ExpansionFactor', None) is None:
        private.ExpansionFactor = 0.06
    if getattr(private, 'BlueFuzz', None) is None:
        private.BlueFuzz = 1
    if getattr(private, 'BlueShift', None) is None:
        private.BlueShift = 7

    existing_blue = getattr(private, 'BlueValues', None)
    existing_other = getattr(private, 'OtherBlues', None)
    has_well_tuned_zones = (existing_blue is not None and len(existing_blue) >= 6)

    if rebuild and not has_well_tuned_zones:
        # Synthesise zones from real glyph metrics
        try:
            from foundrytools.app.otf_recalc_zones import run as recalc_zones
            other_blues, blue_values = recalc_zones(font)
            if existing_other is None and other_blues:
                private.OtherBlues = other_blues
                logger.debug(f"OtherBlues synthesised: {other_blues}")
            if existing_blue is None and len(blue_values) >= 4:
                private.BlueValues = blue_values
                logger.debug(f"BlueValues synthesised: {blue_values}")
        except Exception as e:
            logger.warning(f"otf_recalc_zones failed: {e}")
    elif rebuild:
        logger.debug("Zones preserved from font (scaled by scale_upem): "
                     f"BlueValues={existing_blue}")

    # Recalculate stems from real stem widths
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

    # blue-quantise: round zone values to a clean N-unit grid
    if blue_quantise and blue_quantise > 0:
        for attr in ('BlueValues', 'OtherBlues', 'FamilyBlues', 'FamilyOtherBlues'):
            vals = getattr(private, attr, None)
            if vals:
                quantised = [int(round(v / blue_quantise) * blue_quantise) for v in vals]
                # Drop zero-width zones (top == bot after quantisation)
                merged = []
                for i in range(0, len(quantised) - 1, 2):
                    top, bot = quantised[i], quantised[i + 1]
                    if top != bot:
                        merged.extend([top, bot])
                setattr(private, attr, merged)
                logger.debug(f"CFF {attr} quantised to {blue_quantise}-unit grid: {merged}")


# ---------------------------------------------------------------------------
#  Stem width normalisation (CFF Private dict rounding)
# ---------------------------------------------------------------------------

def _round_stem_widths(font: Font) -> int:
    """Round CFF StdHW/StdVW/StemSnap* to clean integers (v8.5)."""
    if not font.is_ps:
        return 0

    private = font.t_cff_.private_dict
    count = 0

    for attr in ('StdHW', 'StdVW'):
        raw = private.rawDict.get(attr)
        if raw is not None and isinstance(raw, (int, float)):
            rounded = int(round(raw))
            if rounded != raw:
                setattr(private, attr, rounded)
                count += 1

    for attr in ('StemSnapH', 'StemSnapV'):
        val = private.rawDict.get(attr)
        if val:
            new_val = [int(round(v)) for v in val if v is not None]
            if new_val != list(val):
                private.rawDict[attr] = new_val
                count += len(new_val)

    if count:
        logger.debug(f"Stem width normalisation: {count} values rounded")
    return count


# ---------------------------------------------------------------------------
#  Subpixel coordinate snapping (fontTools, both TT and CFF)
# ---------------------------------------------------------------------------

def _snap_to_integer(font: Font) -> int:
    """Force all glyph coordinates to integers (v8.5). Returns count snapped."""
    snapped = 0

    if 'glyf' in font.ttfont:
        glyf = font.ttfont['glyf']
        for glyph_name in font.ttfont.getGlyphOrder():
            if glyph_name not in glyf:
                continue
            glyph = glyf[glyph_name]
            if not hasattr(glyph, 'coordinates') or glyph.coordinates is None:
                continue
            coords = glyph.coordinates
            for i in range(len(coords)):
                x, y = coords[i]
                ix, iy = int(round(x)), int(round(y))
                if (x, y) != (ix, iy):
                    coords[i] = (ix, iy)
                    snapped += 1
        # Composite component offsets
        for glyph_name in font.ttfont.getGlyphOrder():
            if glyph_name not in glyf:
                continue
            glyph = glyf[glyph_name]
            if glyph.numberOfContours >= 0 or not hasattr(glyph, 'components'):
                continue
            for comp in glyph.components or []:
                if comp.x is not None and not isinstance(comp.x, int):
                    comp.x = int(round(comp.x))
                    snapped += 1
                if comp.y is not None and not isinstance(comp.y, int):
                    comp.y = int(round(comp.y))
                    snapped += 1

    if font.is_ps:
        try:
            font.t_cff_.round_coordinates()
        except Exception as e:
            logger.debug(f"CFF round_coordinates failed: {e}")

    if snapped:
        logger.debug(f"Subpixel snap: {snapped} coordinates rounded to integers")
    return snapped


# ---------------------------------------------------------------------------
#  TrueType outline cleanup (collinear + near-duplicate removal, v8.5)
# ---------------------------------------------------------------------------

def _remove_collinear_in_contour(coords, flags=None, tolerance=0.5):
    """Remove collinear middle points from a single contour."""
    if len(coords) < 3:
        return list(coords), list(flags) if flags else None

    n = len(coords)
    keep = [True] * n

    for i in range(n):
        prev_i = (i - 1) % n
        next_i = (i + 1) % n
        if not (keep[prev_i] and keep[next_i]):
            continue
        px, py = coords[prev_i]
        cx, cy = coords[i]
        nx, ny = coords[next_i]
        dx, dy = nx - px, ny - py
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-9:
            continue
        cross = abs((cx - px) * dy - (cy - py) * dx)
        dist = cross / (seg_len_sq ** 0.5)
        if dist < tolerance:
            keep[i] = False

    new_coords = [coords[i] for i in range(n) if keep[i]]
    new_flags = [flags[i] for i in range(n) if keep[i]] if flags else None
    return new_coords, new_flags


def _remove_near_duplicates_in_contour(coords, flags=None, tolerance=0.5):
    """Remove points within tolerance of each other."""
    if len(coords) < 2:
        return list(coords), list(flags) if flags else None

    keep = [True] * len(coords)
    tol_sq = tolerance * tolerance
    for i in range(len(coords) - 1):
        if not keep[i]:
            continue
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        if (x1 - x2) ** 2 + (y1 - y2) ** 2 < tol_sq:
            keep[i + 1] = False

    new_coords = [c for c, k in zip(coords, keep) if k]
    new_flags = [f for f, k in zip(flags, keep) if k] if flags else None
    return new_coords, new_flags


def _cleanup_tt_outlines(font: Font, collinear_tolerance: float = 0.5,
                         dedup_tolerance: float = 0.5) -> int:
    """Remove collinear and near-duplicate points from TrueType outlines (v8.5)."""
    if 'glyf' not in font.ttfont:
        return 0

    glyf = font.ttfont['glyf']
    total_removed = 0

    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]
        if glyph.numberOfContours <= 0 or not hasattr(glyph, 'coordinates'):
            continue
        if glyph.coordinates is None or len(glyph.coordinates) == 0:
            continue

        coords = list(glyph.coordinates)
        flags = list(glyph.flags) if hasattr(glyph, 'flags') else None
        end_pts = glyph.endPtsOfContours
        orig_total = len(coords)

        new_coords = []
        new_flags = []
        new_end_pts = []

        prev_end = -1
        for end_pt in end_pts:
            contour_coords = coords[prev_end + 1:end_pt + 1]
            contour_flags = flags[prev_end + 1:end_pt + 1] if flags else None
            contour_coords, contour_flags = _remove_near_duplicates_in_contour(
                contour_coords, contour_flags, dedup_tolerance)
            contour_coords, contour_flags = _remove_collinear_in_contour(
                contour_coords, contour_flags, collinear_tolerance)
            new_coords.extend(contour_coords)
            if contour_flags:
                new_flags.extend(contour_flags)
            new_end_pts.append(len(new_coords) - 1)
            prev_end = end_pt

        removed = orig_total - len(new_coords)
        if removed > 0:
            # Must be a GlyphCoordinates object, not a plain list, or the glyf
            # table will fail to compile (coords.calcIntBounds missing).
            glyph.coordinates = GlyphCoordinates(new_coords)
            if new_flags:
                glyph.flags = new_flags
            glyph.endPtsOfContours = new_end_pts
            xs = [p[0] for p in new_coords]
            ys = [p[1] for p in new_coords]
            glyph.xMin = min(xs)
            glyph.yMin = min(ys)
            glyph.xMax = max(xs)
            glyph.yMax = max(ys)
            total_removed += removed

    if total_removed:
        logger.info(f"TrueType outline cleanup: removed {total_removed} redundant points")
    return total_removed


# ---------------------------------------------------------------------------
#  Dropout control (PREP table, TrueType only, v8.5)
# ---------------------------------------------------------------------------

def _add_dropout_control(font: Font) -> bool:
    """Add dropout control via PREP table (SCANMODE mode 2)."""
    if 'glyf' not in font.ttfont:
        return False

    try:
        from fontTools.ttLib.tables._p_r_e_p_t.table__p_r_e_p_t import table__p_r_e_p_t
        prep = table__p_r_e_p_t()
        prep.program = [0xB0, 0x02]
        font.ttfont['prep'] = prep
        return True
    except Exception as e:
        logger.debug(f"Could not set PREP dropout control: {e}")
        return False


# ---------------------------------------------------------------------------
#  Horizontal width adjust (v8.5 percent semantics -> multiplier)
# ---------------------------------------------------------------------------

def width_font_glyphs(font: Font, width_percent: float) -> None:
    """
    Adjust horizontal width only (v8.5 semantics).

    width_percent:
      - 0    = no change
      - >0   = expand (e.g. 5 = 5% wider)
      - <0   = condense (e.g. -8 = 8% narrower)

    Affects outlines + advance widths + horizontal metrics. Vertical metrics
    (OS/2 sTypoAscender, post underline) are intentionally untouched.
    """
    if width_percent == 0:
        return

    factor = 1.0 + width_percent / 100.0
    if factor < 0.10:
        logger.warning(f"Width factor {factor:.3f} too small, capping at 0.10")
        factor = 0.10
    elif factor > 4.00:
        logger.warning(f"Width factor {factor:.3f} too large, capping at 4.00")
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
    """Apply an affine transform to all TrueType glyphs (horizontal-only)."""
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
        glyf[glyph_name] = tt_pen.glyph()


def _apply_horizontal_transform_cff(font: Font, transform: tuple) -> None:
    """Apply an affine transform to all CFF charstrings (horizontal-only)."""
    top_dict = font.t_cff_.top_dict
    char_strings = top_dict.CharStrings
    glyph_set = font.ttfont.getGlyphSet()
    hmtx = font.ttfont.get('hmtx')

    if hmtx is not None:
        for gn in font.ttfont.getGlyphOrder():
            if gn not in hmtx.metrics:
                hmtx.metrics[gn] = (0, 0)

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
#  Stem thickening (skia perpendicular offset, v8.5 percent semantics)
# ---------------------------------------------------------------------------

def _thicken_font_glyphs(font: Font, thickness_percent: float) -> None:
    """
    Thicken glyph stems WITHOUT changing overall font size or advance widths.

    v8.5 percent semantics:
      - thickness_percent 0   = no change
      - thickness_percent 2.5 = +2.5% thicker
      - thickness_percent 10  = +10% thicker

    Implemented with skia perpendicular stroke offset (same algorithm as
    FontForge's changeWeight). Converts to a multiplier = 1 + pct/100 and uses
    the skia union approach from v9.
    """
    if thickness_percent <= 0:
        return

    multiplier = 1.0 + thickness_percent / 100.0
    if multiplier < 0.10:
        logger.warning(f"Thickness {multiplier:.3f} below minimum, clamping to 0.10")
        multiplier = 0.10
    elif multiplier > 4.00:
        logger.warning(f"Thickness {multiplier:.3f} above maximum, clamping to 4.00")
        multiplier = 4.00

    logger.info(f"Thickening stems by {thickness_percent:+.2f}% (multiplier {multiplier:.4f})")

    if not HAS_SKIA:
        logger.error("skia-python not installed. Run: pip install skia-python")
        return

    if font.is_ps:
        _thicken_cff_glyphs(font, multiplier)
    elif font.is_tt:
        _thicken_truetype_glyphs(font, multiplier)


def _skia_path_from_recording(rec, path):
    """Replay a RecordingPen value into a skia.Path."""
    for cmd, pts in rec.value:
        if cmd == 'moveTo':
            path.moveTo(float(pts[0][0]), float(pts[0][1]))
        elif cmd == 'lineTo':
            path.lineTo(float(pts[0][0]), float(pts[0][1]))
        elif cmd == 'curveTo':
            path.cubicTo(float(pts[0][0]), float(pts[0][1]),
                         float(pts[1][0]), float(pts[1][1]),
                         float(pts[2][0]), float(pts[2][1]))
        elif cmd == 'qCurveTo':
            path.quadTo(float(pts[0][0]), float(pts[0][1]),
                        float(pts[1][0]), float(pts[1][1]))
        elif cmd == 'closePath':
            path.close()


def _thicken_cff_glyphs(font: Font, multiplier: float) -> None:
    """Thicken CFF charstrings using skia perpendicular stroke offset."""
    cff = font.t_cff_
    private = cff.private_dict
    stem_h = getattr(private, 'StdHW', None)
    stem_v = getattr(private, 'StdVW', None)
    cap_height = getattr(font.t_os_2.table, 'sCapHeight', None) or 700

    if stem_h and stem_h > 0:
        ref_stem = stem_h
    else:
        ref_stem = int(cap_height * 0.07)

    stroke_width = abs(multiplier - 1.0) * ref_stem
    if stroke_width < 0.5:
        logger.debug(f"Stroke width {stroke_width:.2f}u too small, skipping")
        return

    paint = skia.Paint(
        Style=skia.Paint.kStroke_Style,
        StrokeWidth=stroke_width,
        StrokeCap=skia.Paint.kButt_Cap,
        StrokeJoin=skia.Paint.kRound_Join,
        StrokeMiter=4.0,
    )

    top_dict = cff.top_dict
    char_strings = top_dict.CharStrings
    glyph_set = font.ttfont.getGlyphSet()

    try:
        font.ttfont['CFF '].cff.topDictIndex[0].decompileAllCharStrings()
    except Exception:
        pass

    new_charstrings = {}
    thickened_count = 0
    skipped_count = 0

    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in char_strings or glyph_name not in glyph_set:
            continue
        try:
            rec = RecordingPen()
            glyph_set[glyph_name].draw(rec)

            path = skia.Path()
            _skia_path_from_recording(rec, path)
            if path.countVerbs() == 0:
                new_charstrings[glyph_name] = char_strings[glyph_name]
                continue

            stroked_path = skia.Path()
            paint.getFillPath(path, stroked_path, None, 1.0)
            builder = skia.OpBuilder()
            builder.add(path, skia.PathOp.kUnion_PathOp)
            builder.add(stroked_path, skia.PathOp.kUnion_PathOp)
            result_path = builder.resolve()

            scaled_width = glyph_set[glyph_name].width
            t2_pen = T2CharStringPen(scaled_width, glyph_set)

            Verb = skia.Path.Verb
            for verb, pts in result_path:
                if verb == Verb.kMove_Verb:
                    t2_pen.moveTo((int(round(float(pts[0].x()))), int(round(float(pts[0].y())))))
                elif verb == Verb.kLine_Verb:
                    t2_pen.lineTo((int(round(float(pts[0].x()))), int(round(float(pts[0].y())))))
                elif verb == Verb.kQuad_Verb:
                    t2_pen.qCurveTo(
                        (int(round(float(pts[0].x()))), int(round(float(pts[0].y())))),
                        (int(round(float(pts[1].x()))), int(round(float(pts[1].y())))))
                elif verb == Verb.kCubic_Verb:
                    t2_pen.curveTo(
                        (int(round(float(pts[0].x()))), int(round(float(pts[0].y())))),
                        (int(round(float(pts[1].x()))), int(round(float(pts[1].y())))),
                        (int(round(float(pts[2].x()))), int(round(float(pts[2].y())))))
                elif verb == Verb.kConic_Verb:
                    if len(pts) >= 2:
                        t2_pen.qCurveTo(
                            (int(round(float(pts[0].x()))), int(round(float(pts[0].y())))),
                            (int(round(float(pts[1].x()))), int(round(float(pts[1].y())))))
                elif verb == Verb.kClose_Verb:
                    t2_pen.closePath()

            new_cs = t2_pen.getCharString()
            new_cs.private = top_dict.Private
            new_charstrings[glyph_name] = new_cs
            thickened_count += 1
        except Exception as e:
            logger.warning(f"Could not thicken '{glyph_name}': {e}")
            new_charstrings[glyph_name] = char_strings[glyph_name]
            skipped_count += 1

    for name, cs in new_charstrings.items():
        char_strings[name] = cs

    # Fix CFF contour winding after the skia union. Skia uses Y-down
    # coordinates; CFF uses Y-up. The union operation can leave contours
    # with inconsistent winding (e.g. both outer and inner counters with
    # the same sign), which makes the renderer fill counters (o, e, a, b,
    # p, d, g, q) as solid discs. Normalize to the canonical CFF non-zero
    # winding convention: outermost contour CCW (positive area), all holes
    # CW (negative).
    _fix_cff_windings(font, set(new_charstrings.keys()))

    logger.debug(f"Thickening complete: {thickened_count} glyphs thickened, "
                 f"{skipped_count} skipped")


def _fix_cff_windings(font: Font, glyph_names: set) -> None:
    """
    Normalize CFF contour winding after skia stem thickening.

    Skia operates in Y-down screen space while CFF charstrings use Y-up font
    space. After converting a skia union path to a T2 charstring, contour
    winding can become inconsistent: the outer contour and its inner counters
    may end up with the SAME signed area, so the non-zero winding fill rule
    treats the counter as filled (a solid disc instead of a hole).

    This restores the canonical CFF convention used by the non-zero winding
    rule:
      - the outermost contour (largest absolute area) is CCW -> positive area
      - every other contour (a hole) is CW -> negative area

    Glyphs that already satisfy the convention are left untouched.
    """
    if not font.is_ps:
        return

    char_strings = font.t_cff_.top_dict.CharStrings
    glyph_set = font.ttfont.getGlyphSet()
    priv = font.t_cff_.top_dict.Private

    fixed = 0
    for glyph_name in glyph_names:
        if glyph_name not in char_strings:
            continue
        cs = char_strings[glyph_name]
        if cs.program is None or len(cs.program) <= 2:
            continue

        rec = RecordingPen()
        try:
            cs.draw(rec)
        except Exception:
            continue

        # Group the recording into contours (each ends at closePath)
        contours = []
        current = []
        for cmd, pts in rec.value:
            current.append((cmd, pts))
            if cmd == 'closePath':
                if current:
                    contours.append(current)
                current = []
        if not contours:
            continue

        # Signed area of each contour (shoelace formula, Y-up)
        areas = []
        for c in contours:
            pts = []
            for cmd, p in c:
                pts.extend(p)
            if len(pts) < 3:
                areas.append(0.0)
                continue
            n = len(pts)
            a = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
                     for i in range(n)) / 2.0
            areas.append(a)
        if not areas:
            continue

        # Decide which contours to flip: outer must be positive, holes negative
        outer_idx = max(range(len(areas)), key=lambda i: abs(areas[i]))
        flip = [False] * len(areas)
        if areas[outer_idx] < 0:
            flip[outer_idx] = True
        for i in range(len(areas)):
            if i == outer_idx:
                continue
            if areas[i] > 0:
                flip[i] = True
        if not any(flip):
            continue

        t2 = T2CharStringPen(cs.width, glyph_set)
        for ci, c in enumerate(contours):
            if flip[ci]:
                # ReverseContourPen correctly swaps curve control points
                rev = ReverseContourPen(t2)
                for cmd, p in c:
                    if cmd == 'moveTo':
                        rev.moveTo(p[0])
                    elif cmd == 'lineTo':
                        rev.lineTo(p[0])
                    elif cmd == 'curveTo':
                        rev.curveTo(p[0], p[1], p[2])
                    elif cmd == 'qCurveTo':
                        rev.qCurveTo(p[0], p[1])
                    elif cmd == 'closePath':
                        rev.closePath()
            else:
                for cmd, p in c:
                    if cmd == 'moveTo':
                        t2.moveTo(p[0])
                    elif cmd == 'lineTo':
                        t2.lineTo(p[0])
                    elif cmd == 'curveTo':
                        t2.curveTo(p[0], p[1], p[2])
                    elif cmd == 'qCurveTo':
                        t2.qCurveTo(p[0], p[1])
                    elif cmd == 'closePath':
                        t2.closePath()

        new_cs = t2.getCharString()
        new_cs.private = priv
        char_strings[glyph_name] = new_cs
        fixed += 1

    if fixed:
        logger.debug(f"Fixed CFF windings for {fixed} glyphs (skia Y-down -> CFF Y-up)")


def _thicken_truetype_glyphs(font: Font, multiplier: float) -> None:
    """Thicken TrueType outlines using skia perpendicular stroke offset."""
    os2 = font.t_os_2.table
    cap_height = getattr(os2, 'sCapHeight', None) or 700
    ref_stem = int(cap_height * 0.07)
    stroke_width = abs(multiplier - 1.0) * ref_stem

    if stroke_width < 0.5:
        logger.debug(f"Stroke width {stroke_width:.2f}u too small, skipping")
        return

    paint = skia.Paint(
        Style=skia.Paint.kStroke_Style,
        StrokeWidth=stroke_width,
        StrokeCap=skia.Paint.kButt_Cap,
        StrokeJoin=skia.Paint.kRound_Join,
        StrokeMiter=4.0,
    )

    glyf = font.ttfont['glyf']
    glyph_set = font.ttfont.getGlyphSet()
    thickened = set()

    for glyph_name in font.ttfont.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]
        # Skip empty (0) and composite (<0) glyphs; composites reference simple
        # glyphs which are thickened individually.
        if glyph.numberOfContours <= 0:
            continue
        if glyph.coordinates is None or len(glyph.coordinates) == 0:
            continue

        try:
            rec = RecordingPen()
            glyph_set[glyph_name].draw(rec)

            path = skia.Path()
            _skia_path_from_recording(rec, path)
            if path.countVerbs() == 0:
                continue

            stroked_path = skia.Path()
            paint.getFillPath(path, stroked_path, None, 1.0)
            builder = skia.OpBuilder()
            builder.add(path, skia.PathOp.kUnion_PathOp)
            builder.add(stroked_path, skia.PathOp.kUnion_PathOp)
            result_path = builder.resolve()

            # Rebuild the glyph via TTGlyphPen (preserves contour structure)
            tt_pen = TTGlyphPen(glyph_set)
            Verb = skia.Path.Verb
            for verb, pts in result_path:
                if verb == Verb.kMove_Verb:
                    tt_pen.moveTo((int(round(float(pts[0].x()))), int(round(float(pts[0].y())))))
                elif verb == Verb.kLine_Verb:
                    tt_pen.lineTo((int(round(float(pts[0].x()))), int(round(float(pts[0].y())))))
                elif verb == Verb.kQuad_Verb:
                    tt_pen.qCurveTo(
                        (int(round(float(pts[0].x()))), int(round(float(pts[0].y())))),
                        (int(round(float(pts[1].x()))), int(round(float(pts[1].y())))))
                elif verb == Verb.kCubic_Verb:
                    tt_pen.curveTo(
                        (int(round(float(pts[0].x()))), int(round(float(pts[0].y())))),
                        (int(round(float(pts[1].x()))), int(round(float(pts[1].y())))),
                        (int(round(float(pts[2].x()))), int(round(float(pts[2].y())))))
                elif verb == Verb.kConic_Verb:
                    if len(pts) >= 2:
                        tt_pen.qCurveTo(
                            (int(round(float(pts[0].x()))), int(round(float(pts[0].y())))),
                            (int(round(float(pts[1].x()))), int(round(float(pts[1].y())))))
                elif verb == Verb.kClose_Verb:
                    tt_pen.closePath()

            glyf[glyph_name] = tt_pen.glyph()
            thickened.add(glyph_name)
        except Exception as e:
            logger.warning(f"Could not thicken '{glyph_name}': {e}")

    # Fix contour winding after the skia union (counters would otherwise fill)
    _fix_tt_windings(font, thickened)


def _fix_tt_windings(font: Font, glyph_names: set) -> None:
    """
    Normalize TrueType contour winding after skia stem thickening.

    Same root cause as CFF: skia's union operation can leave the outer contour
    and its inner counters with the same signed area, filling counters (o, e, a,
    b, p, d, g, q) as solid discs. Restore the canonical non-zero winding
    convention: outermost contour CCW (positive area), all holes CW (negative).
    """
    if 'glyf' not in font.ttfont:
        return

    glyf = font.ttfont['glyf']
    glyph_set = font.ttfont.getGlyphSet()

    fixed = 0
    for glyph_name in glyph_names:
        if glyph_name not in glyf:
            continue
        g = glyf[glyph_name]
        if g.numberOfContours <= 0:  # skip empty and composite glyphs
            continue

        rec = RecordingPen()
        try:
            glyph_set[glyph_name].draw(rec)
        except Exception:
            continue

        contours = []
        current = []
        for cmd, pts in rec.value:
            current.append((cmd, pts))
            if cmd == 'closePath':
                if current:
                    contours.append(current)
                current = []
        if not contours:
            continue

        areas = []
        for c in contours:
            pts = []
            for cmd, p in c:
                pts.extend(p)
            if len(pts) < 3:
                areas.append(0.0)
                continue
            n = len(pts)
            a = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
                     for i in range(n)) / 2.0
            areas.append(a)
        if not areas:
            continue

        outer_idx = max(range(len(areas)), key=lambda i: abs(areas[i]))
        flip = [False] * len(areas)
        if areas[outer_idx] < 0:
            flip[outer_idx] = True
        for i in range(len(areas)):
            if i == outer_idx:
                continue
            if areas[i] > 0:
                flip[i] = True
        if not any(flip):
            continue

        tt_pen = TTGlyphPen(glyph_set)
        for ci, c in enumerate(contours):
            if flip[ci]:
                rev = ReverseContourPen(tt_pen)
                for cmd, p in c:
                    if cmd == 'moveTo':
                        rev.moveTo(p[0])
                    elif cmd == 'lineTo':
                        rev.lineTo(p[0])
                    elif cmd == 'curveTo':
                        rev.curveTo(p[0], p[1], p[2])
                    elif cmd == 'qCurveTo':
                        rev.qCurveTo(p[0], p[1])
                    elif cmd == 'closePath':
                        rev.closePath()
            else:
                for cmd, p in c:
                    if cmd == 'moveTo':
                        tt_pen.moveTo(p[0])
                    elif cmd == 'lineTo':
                        tt_pen.lineTo(p[0])
                    elif cmd == 'curveTo':
                        tt_pen.curveTo(p[0], p[1], p[2])
                    elif cmd == 'qCurveTo':
                        tt_pen.qCurveTo(p[0], p[1])
                    elif cmd == 'closePath':
                        tt_pen.closePath()

        glyf[glyph_name] = tt_pen.glyph()
        fixed += 1

    if fixed:
        logger.debug(f"Fixed TT windings for {fixed} glyphs (skia Y-down -> TT Y-up)")


# ---------------------------------------------------------------------------
#  Uniform scaling (v8.5 dual-mode semantics -> foundrytools.scale_upm)
# ---------------------------------------------------------------------------

def scale_font_glyphs(font: Font, scale_percent: float, thickness_percent: float = 0) -> None:
    """
    Scale and/or thicken glyph outlines and metrics (v8.5 semantics).

    --scale value interpretation (v8.5):
      - |value| < 1.0  -> direct multiplier (e.g. 0.5 = 50% size)
      - |value| >= 1.0 -> percentage change (e.g. 5 = +5%)

    Delegates uniform scaling to foundrytools.Font.scale_upm(). Thickening is
    custom (skia).
    """
    if scale_percent == 0 and thickness_percent == 0:
        return

    scale_abs = abs(scale_percent)
    if 0 < scale_abs < 1.0:
        scale_factor = scale_percent
        mode = "multiplier"
    elif scale_abs >= 1.0:
        scale_factor = 1.0 + scale_percent / 100.0
        mode = "percentage"
    else:
        scale_factor = 1.0
        mode = "no-scale"

    if scale_factor < 0.10:
        logger.warning(f"Scale factor {scale_factor:.3f} too small, capping at 0.10")
        scale_factor = 0.10
    elif scale_factor > 4.00:
        logger.warning(f"Scale factor {scale_factor:.3f} too large, capping at 4.00")
        scale_factor = 4.00

    logger.info(f"Scaling font ({mode}): scale x{scale_factor:.4f}, "
                f"thickness {thickness_percent:+.2f}%")

    if scale_factor != 1.0:
        current_upm = font.t_head.units_per_em
        scaled_upm = int(round(current_upm * scale_factor))
        scaled_upm = max(MIN_UPM, min(MAX_UPM, scaled_upm))
        if scaled_upm != current_upm:
            font.scale_upm(scaled_upm)
            logger.debug(f"Scaled via foundrytools.scale_upm to upem={scaled_upm}")

    if thickness_percent > 0:
        _thicken_font_glyphs(font, thickness_percent)


# ---------------------------------------------------------------------------
#  Em-square scaling (--em-scale)
#  Scales outlines/metrics/kerning/anchors by `scale_factor` within the SAME
#  em-square (i.e. head.unitsPerEm is preserved). Gives "bigger glyphs at the
#  same point size" while keeping the existing em-square unchanged.
#
#  Implementation: reuse fontTools.ttLib.scaleUpem.ScalerVisitor (the same
#  visitor engine behind scale_upem). It scales every relevant table field
#  (glyf, CFF/CFF2, gvar, HVAR/MVAR, hmtx/vmtx, hhea/vhea, OS/2, head bbox,
#  GPOS ValueRecord/Anchor, kern, VORG, COLRv1, VARC, ItemVariationStore, MATH,
#  BASE). The one field we DON'T want changed is head.unitsPerEm; the visitor
#  scales it, so we restore the original value after the visit. All other
#  scaling operations are independent of head.unitsPerEm so this is safe.
# ---------------------------------------------------------------------------

def _em_scale_glyphs(font: Font, em_scale_factor: float) -> None:
    """Scale outlines/metrics within the em-square (keep UPM unchanged)."""
    from fontTools.ttLib.scaleUpem import ScalerVisitor

    if em_scale_factor == 1.0:
        return

    original_upem = font.ttfont["head"].unitsPerEm
    visitor = ScalerVisitor(em_scale_factor)
    visitor.visit(font.ttfont)
    font.ttfont["head"].unitsPerEm = original_upem  # restore: the only field we don't want changed


def em_scale_font_glyphs(font: Font, em_scale_percent: float) -> None:
    """
    Scale outlines/metrics by em_scale_percent within the em-square
    (UPM preserved). v8.5-style dual-mode semantics:
      - |value| < 1.0  -> direct multiplier (e.g. 0.5 = 50% size)
      - |value| >= 1.0 -> percentage change (e.g. 50 = +50%)
    """
    if em_scale_percent == 0:
        return

    scale_abs = abs(em_scale_percent)
    if 0 < scale_abs < 1.0:
        scale_factor = em_scale_percent
        mode = "multiplier"
    elif scale_abs >= 1.0:
        scale_factor = 1.0 + em_scale_percent / 100.0
        mode = "percentage"
    else:
        scale_factor = 1.0
        mode = "no-scale"

    if scale_factor < 0.10:
        logger.warning(f"Em-scale factor {scale_factor:.3f} too small, capping at 0.10")
        scale_factor = 0.10
    elif scale_factor > 4.00:
        logger.warning(f"Em-scale factor {scale_factor:.3f} too large, capping at 4.00")
        scale_factor = 4.00

    logger.info(f"Em-square scaling ({mode}): scale x{scale_factor:.4f} "
                f"(UPM preserved at {font.t_head.units_per_em})")

    _em_scale_glyphs(font, scale_factor)


# ---------------------------------------------------------------------------
#  Autohinting (foundrytools wrappers)
# ---------------------------------------------------------------------------

def _patch_afdko_logging() -> None:
    """Patch AFDKO otfautohint logging for missing custom attributes."""
    try:
        from afdko.otfautohint.logging import otfautoLogFormatter
        original_format = otfautoLogFormatter.format

        def patched_format(self, record):
            for attr in ('glyph', 'instance', 'dimension'):
                if not hasattr(record, attr):
                    setattr(record, attr, '')
            return original_format(self, record)

        otfautoLogFormatter.format = patched_format
    except Exception:
        pass


def _psautohint_fallback(font: Font) -> bool:
    """Fallback to external psautohint binary if foundrytools otf_autohint fails."""
    import subprocess
    from fontTools.ttLib import TTFont

    with tempfile.NamedTemporaryFile(suffix='.otf', delete=False) as tmp_in:
        in_path = tmp_in.name
    with tempfile.NamedTemporaryFile(suffix='.otf', delete=False) as tmp_out:
        out_path = tmp_out.name

    try:
        font.save(in_path)
        cmd = ['psautohint', '-o', out_path, '-a', '-c', '-d', '--no-zones-stems', in_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            hinted = TTFont(out_path, recalcTimestamp=False)
            font.ttfont['CFF '] = hinted['CFF ']
            return True
        logger.error(f"psautohint fallback failed: {result.stderr[:200]}")
        return False
    except Exception as e:
        logger.error(f"psautohint fallback error: {e}")
        return False
    finally:
        for p in (in_path, out_path):
            if os.path.exists(p):
                os.unlink(p)


def autohint_font(font: Font) -> bool:
    """Autohint using foundrytools wrappers (ttf_autohint-py / AFDKO otfautohint)."""
    try:
        if font.is_tt:
            from foundrytools.app.ttf_autohint import run as ttf_autohint
            ttf_autohint(font)
            logger.info("Autohinted (TrueType via foundrytools/ttfautohint-py)")
            return True
        elif font.is_ps:
            try:
                _patch_afdko_logging()
                from foundrytools.app.otf_autohint import run as otf_autohint
                otf_autohint(font, allowChanges=True, hintAll=True)
                logger.info("Autohinted (CFF via foundrytools/AFDKO otfautohint)")
                return True
            except Exception as e:
                logger.debug(f"foundrytools otf_autohint failed ({e}), "
                             f"falling back to psautohint")
                return _psautohint_fallback(font)
    except Exception as e:
        logger.error(f"Autohinting failed: {e}")
        return False
    return False


# ---------------------------------------------------------------------------
#  Curve smoothing (--curve-delta)
# ---------------------------------------------------------------------------
#
# Smooths glyph outlines by removing redundant on-curve points within `delta`
# font units, using a Bezier-aware Douglas-Peucker simplification on the
# on-curve polygon. Adjacent curve segments between dropped anchors are merged
# into a single fitted curve (cubic for CFF, quadratic for TrueType), so the
# outline becomes smoother (fewer control points) while staying format-
# preserving. Higher delta = more aggressive smoothing.


def _smoothing_point_line_dist(p, a, b):
    ax, ay = a; bx, by = b
    dx = bx - ax; dy = by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return ((p[0] - ax) ** 2 + (p[1] - ay) ** 2) ** 0.5
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    px = ax + t * dx; py = ay + t * dy
    return ((p[0] - px) ** 2 + (p[1] - py) ** 2) ** 0.5


def _smoothing_dp_open(pts, eps):
    n = len(pts)
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        dmax = 0.0; idx = -1
        for i in range(s + 1, e):
            d = _smoothing_point_line_dist(pts[i], pts[s], pts[e])
            if d > dmax:
                dmax = d; idx = i
        if dmax > eps and idx != -1:
            keep[idx] = True
            stack.append((s, idx)); stack.append((idx, e))
    return [i for i in range(n) if keep[i]]


def _smoothing_dp_closed(anchors, eps):
    n = len(anchors)
    if n < 3:
        return list(range(n))
    pts = anchors + [anchors[0]]
    idx = _smoothing_dp_open(pts, eps)
    out = []
    for i in idx:
        if i == n:
            continue
        if not out or out[-1] != i:
            out.append(i)
    if not out:
        out = [0]
    return out


def _smoothing_cubic(p0, c1, c2, p3, t):
    mt = 1 - t
    a = mt ** 3; b = 3 * mt * mt * t; c = 3 * mt * t * t; d = t ** 3
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p3[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p3[1])


def _smoothing_quad(p0, c1, p2, t):
    mt = 1 - t
    a = mt * mt; b = 2 * mt * t; c = t * t
    return (a * p0[0] + b * c1[0] + c * p2[0], a * p0[1] + b * c1[1] + c * p2[1])


def _smoothing_flatten_cubic(p0, c1, c2, p3, n=12):
    return [_smoothing_cubic(p0, c1, c2, p3, i / n) for i in range(1, n + 1)]


def _smoothing_flatten_quad(p0, controls, p2, n=12):
    if not controls:
        controls = [((p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2)]
    prev = p0
    out = []
    for c in controls:
        for _i in range(1, n + 1):
            out.append(_smoothing_quad(prev, c, c, _i / n))
        prev = c
    for _i in range(1, n + 1):
        out.append(_smoothing_quad(prev, p2, p2, _i / n))
    return out


def _smoothing_fit_cubic(p0, p3, samples):
    n = len(samples)
    if n < 2:
        mid = ((p0[0] + p3[0]) / 2, (p0[1] + p3[1]) / 2)
        return mid, mid
    ts = [i / (n - 1) for i in range(n)]
    Saa = Sab = Sbb = 0.0
    rx_ay = rx_by = ry_ay = ry_by = 0.0
    for t, (x, y) in zip(ts, samples):
        mt = 1 - t
        A = 3 * mt * mt * t
        B = 3 * mt * t * t
        rx = x - (mt ** 3 * p0[0] + t ** 3 * p3[0])
        ry = y - (mt ** 3 * p0[1] + t ** 3 * p3[1])
        Saa += A * A; Sab += A * B; Sbb += B * B
        rx_ay += A * rx; rx_by += B * rx
        ry_ay += A * ry; ry_by += B * ry
    det = Saa * Sbb - Sab * Sab
    if abs(det) < 1e-9:
        mid = ((p0[0] + p3[0]) / 2, (p0[1] + p3[1]) / 2)
        return mid, mid
    c1x = (Sbb * rx_ay - Sab * rx_by) / det
    c1y = (Sbb * ry_ay - Sab * ry_by) / det
    c2x = (Saa * rx_by - Sab * rx_ay) / det
    c2y = (Saa * ry_by - Sab * ry_ay) / det
    return (c1x, c1y), (c2x, c2y)


def _smoothing_fit_quad(p0, p2, samples):
    n = len(samples)
    if n < 2:
        return ((p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2)
    ts = [i / (n - 1) for i in range(n)]
    Sbb = 0.0; rx_b = 0.0; ry_b = 0.0
    for t, (x, y) in zip(ts, samples):
        mt = 1 - t
        B = 2 * mt * t
        rx = x - (mt * mt * p0[0] + t * t * p2[0])
        ry = y - (mt * mt * p0[1] + t * t * p2[1])
        Sbb += B * B; rx_b += B * rx; ry_b += B * ry
    if abs(Sbb) < 1e-9:
        return ((p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2)
    cx = rx_b / Sbb; cy = ry_b / Sbb
    return (cx, cy)


def _smoothing_max_dev(samples, curve_pts):
    """Max distance from any sampled original point to the fitted curve.

    Used to verify that a merged curve never deviates from the original
    outline by more than the requested delta. The lists are short (a handful
    of points per span), so the O(n*m) scan is cheap.
    """
    if not samples or not curve_pts:
        return float('inf')
    worst = 0.0
    for sx, sy in samples:
        best = float('inf')
        for cx, cy in curve_pts:
            d = (sx - cx) ** 2 + (sy - cy) ** 2
            if d < best:
                best = d
        if best > worst:
            worst = best
    return worst ** 0.5


class SmoothingPointPen(ContourFilterPointPen):
    """Contour filter that simplifies each contour via Douglas-Peucker.

    On-curve anchors whose deviation from the chord between surviving anchors
    is <= delta are dropped; the curve(s) spanning a dropped run are merged
    into a single fitted Bezier (cubic for CFF, quadratic for TrueType).
    """

    def __init__(self, outPen, delta, is_cubic):
        super().__init__(outPen)
        self.delta = delta
        self.is_cubic = is_cubic

    def filterContour(self, contour):
        # contour: list of (pt, segmentType, smooth, name, kwargs)
        # In fontTools' point-pen representation, off-curve points have
        # segmentType=None; on-curve points use 'move'/'line'/'curve'/'qcurve'.
        if len(contour) < 3:
            return None
        anchors = []  # (index_in_contour, pt)
        for i, (pt, st, smooth, name, kw) in enumerate(contour):
            if st is not None:
                anchors.append((i, pt))
        m = len(anchors)
        if m < 3:
            return None
        anchor_pts = [pt for _, pt in anchors]
        keep = _smoothing_dp_closed(anchor_pts, self.delta)
        if len(keep) >= m:
            return None  # nothing removed
        new_contour = []
        mk = len(keep)
        for k in range(mk):
            a_idx = keep[k]
            b_idx = keep[(k + 1) % mk]
            ai = anchors[a_idx][0]
            bi = anchors[b_idx][0]
            seg_pts = []
            j = ai
            while True:
                seg_pts.append(contour[j])
                if j == bi:
                    break
                j = (j + 1) % len(contour)
            start_pt = seg_pts[0][0]
            end_pt = seg_pts[-1][0]
            controls = [p[0] for p in seg_pts[1:-1] if p[1] is None]
            is_line = (len(controls) == 0)
            if k == 0:
                new_contour.append((start_pt, "move", False, None, {}))
            if is_line:
                new_contour.append((end_pt, "line", False, None, {}))
            else:
                samples = self._flatten_span(seg_pts, self.is_cubic)
                if self.is_cubic:
                    c1, c2 = _smoothing_fit_cubic(start_pt, end_pt, samples)
                    fitted = _smoothing_flatten_cubic(
                        start_pt, c1, c2, end_pt, n=max(8, len(samples)))
                    if _smoothing_max_dev(samples, fitted) > self.delta:
                        # Merged curve would overshoot the tolerance: keep the
                        # original (exact) segments for this span instead.
                        new_contour.extend(seg_pts[1:])
                    else:
                        new_contour.append((c1, None, False, None, {}))
                        new_contour.append((c2, None, False, None, {}))
                        new_contour.append((end_pt, "curve", True, None, {}))
                else:
                    c1 = _smoothing_fit_quad(start_pt, end_pt, samples)
                    fitted = _smoothing_flatten_quad(
                        start_pt, [c1], end_pt, n=max(8, len(samples)))
                    if _smoothing_max_dev(samples, fitted) > self.delta:
                        new_contour.extend(seg_pts[1:])
                    else:
                        new_contour.append((c1, None, False, None, {}))
                        new_contour.append((end_pt, "qcurve", True, None, {}))
        return new_contour

    def _flatten_span(self, seg_pts, is_cubic):
        samples = []
        pending = []
        prev = seg_pts[0][0]
        for p in seg_pts[1:]:
            if p[1] is None:
                pending.append(p[0])
            else:
                if not pending:
                    samples.append(p[0])
                else:
                    if is_cubic:
                        if len(pending) >= 2:
                            samples.extend(
                                _smoothing_flatten_cubic(prev, pending[0], pending[-1], p[0]))
                        else:
                            samples.append(p[0])
                    else:
                        samples.extend(_smoothing_flatten_quad(prev, pending, p[0]))
                pending = []
                prev = p[0]
        return samples


def _smooth_curves(font: Font, delta: float) -> None:
    """Smooth all glyph outlines in `font` with tolerance `delta` (font units).

    Format-preserving: CFF charstrings stay cubic, TrueType glyf stays
    quadratic. Composite glyphs are left untouched. Variable-font gvar
    deltas are not updated (same limitation as the rest of this pipeline).
    """
    if delta <= 0:
        return
    tt = font.ttfont

    is_cff = 'CFF ' in tt
    is_tt = 'glyf' in tt
    if not (is_cff or is_tt):
        logger.debug("Curve smoothing: no CFF/glyf table; skipping")
        return

    glyph_set = tt.getGlyphSet()
    modified = 0
    skipped = 0

    if is_cff:
        cff = tt['CFF '].cff.topDictIndex[0]
        char_strings = cff.CharStrings
        private = cff.Private
        try:
            cff.decompileAllCharStrings()
        except Exception:
            pass
        for glyph_name in tt.getGlyphOrder():
            if glyph_name not in char_strings or glyph_name not in glyph_set:
                continue
            try:
                width = glyph_set[glyph_name].width
                inner = T2CharStringPen(width, glyph_set)
                inner_point = PointToSegmentPen(inner)
                smooth = SmoothingPointPen(inner_point, delta, is_cubic=True)
                adapter = SegmentToPointPen(smooth)
                glyph_set[glyph_name].draw(adapter)
                char_strings[glyph_name] = inner.getCharString(private=private)
                modified += 1
            except Exception as e:
                logger.warning(f"Curve smoothing failed for '{glyph_name}': {e}")
                skipped += 1
    else:
        glyf = tt['glyf']
        for glyph_name in tt.getGlyphOrder():
            if glyph_name not in glyf or glyph_name not in glyph_set:
                continue
            if glyf[glyph_name].numberOfContours <= 0:
                continue  # composite / empty
            try:
                width = glyph_set[glyph_name].width
                inner = TTGlyphPen(glyph_set)
                inner_point = PointToSegmentPen(inner)
                smooth = SmoothingPointPen(inner_point, delta, is_cubic=False)
                adapter = SegmentToPointPen(smooth)
                glyph_set[glyph_name].draw(adapter)
                g = inner.glyph()
                g.recalcBounds(glyf)
                glyf[glyph_name] = g
                modified += 1
            except Exception as e:
                logger.warning(f"Curve smoothing failed for '{glyph_name}': {e}")
                skipped += 1

    logger.debug(f"Curve smoothing (delta={delta}): {modified} smoothed, {skipped} skipped")


# ---------------------------------------------------------------------------
#  Counter rounding (--counter-curve)
#  Replaces straight lineTo segments in glyph contours with curved segments
#  that bow outward by `delta` font units (perpendicular to the line). Targets
#  the flat-top look common in 'e', 'a', 'o' counters.
#
#  Implementation: a SegmentPen wrapper that intercepts lineTo and rewrites
#  it as curveTo (cubic, for CFF) or qCurveTo (quadratic, for TT). Tangents
#  at both endpoints are perpendicular to the original line. The bulge
#  direction is chosen to point AWAY from the contour's centroid — which
#  corresponds to "outward" for both outer contours and inner counters, so
#  the same heuristic works on both.
#
#  Only lineTos with length >= min_length are touched (default 50 units), so
#  tiny notches and intentional straight edges aren't perturbed.
# ---------------------------------------------------------------------------

class _CounterCurvePen:
    """SegmentPen wrapper that rewrites long lineTo segments as curves.

    Forwards every call to `out`, but intercepts lineTo: if the line is at
    least `min_length` units long, it's replaced with a curveTo / qCurveTo
    that bulges outward (away from the contour centroid) by `delta` units,
    using perpendicular tangents at both endpoints.

    The centroid of the current contour is needed to decide the bulge
    direction, and the SegmentPen protocol is streaming. So we record
    every call, post-process the recording at endPath/closePath to rewrite
    lineTos, and replay through `out`.
    """

    def __init__(self, out, delta: float, min_length: float, is_cubic: bool):
        self.out = out
        self.delta = delta
        self.min_length = min_length
        self.is_cubic = is_cubic
        self._rec = RecordingPen()
        self._current = None
        # Scratch state for replay
        self._replay_contour = []
        self._replay_contour_start = None

    # --- helpers ---

    @staticmethod
    def _dist(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    @staticmethod
    def _centroid_of(points):
        if not points:
            return (0.0, 0.0)
        sx = sum(p[0] for p in points)
        sy = sum(p[1] for p in points)
        return (sx / len(points), sy / len(points))

    def _replay_contour_to(self, out_pen, contour_ops):
        """Replay one recorded contour through out_pen, rewriting lineTos."""
        # Build a flat list of on-curve endpoints for centroid calculation.
        endpoints = []
        for op_name, args in contour_ops:
            if op_name == 'moveTo':
                endpoints.append(args[0])
            elif op_name == 'lineTo':
                endpoints.append(args[0])
            elif op_name == 'curveTo':
                endpoints.append(args[-1])
            elif op_name == 'qCurveTo':
                endpoints.append(args[-1])
        centroid = self._centroid_of(endpoints)

        # Walk the contour, rewriting eligible lineTos.
        current = None
        for op_name, args in contour_ops:
            if op_name == 'moveTo':
                out_pen.moveTo(args[0])
                current = args[0]
            elif op_name == 'lineTo':
                p2 = args[0]
                if current is not None and self._dist(current, p2) >= self.min_length:
                    new_segs = self._bulge_line(current, p2, centroid)
                    for seg in new_segs:
                        kind = seg[0]
                        if kind == 'lineTo':
                            out_pen.lineTo(seg[1])
                        elif kind == 'curveTo':
                            out_pen.curveTo(seg[1], seg[2], seg[3])
                        elif kind == 'qCurveTo':
                            out_pen.qCurveTo(seg[1], seg[2])
                else:
                    out_pen.lineTo(p2)
                current = p2
            elif op_name == 'curveTo':
                out_pen.curveTo(*args)
                current = args[-1]
            elif op_name == 'qCurveTo':
                out_pen.qCurveTo(*args)
                current = args[-1]
            elif op_name == 'closePath':
                out_pen.closePath()
            elif op_name == 'endPath':
                out_pen.endPath()

    def _bulge_line(self, p1, p2, centroid):
        """Replace lineTo(p1 -> p2) with a curve bulging outward by self.delta."""
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        length = self._dist(p1, p2)
        if length < 1e-6:
            return [('lineTo', p2)]

        n1 = (-dy / length, dx / length)
        n2 = (dy / length, -dx / length)

        # Outward = away from centroid.
        mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
        sample1 = (mid[0] + n1[0], mid[1] + n1[1])
        sample2 = (mid[0] + n2[0], mid[1] + n2[1])
        if self._dist(sample1, centroid) > self._dist(sample2, centroid):
            outward = n1
        else:
            outward = n2

        d = self.delta
        if self.is_cubic:
            c1 = (p1[0] + outward[0] * d, p1[1] + outward[1] * d)
            c2 = (p2[0] + outward[0] * d, p2[1] + outward[1] * d)
            return [('curveTo', c1, c2, p2)]
        else:
            c = (mid[0] + outward[0] * d, mid[1] + outward[1] * d)
            return [('qCurveTo', c, p2)]

    def _flush(self):
        """Replay the recorded glyph through self.out, rewriting lineTos."""
        # _rec.value is a flat list of (op_name, args) tuples.
        # Group into contours: split before each moveTo, keep closePath/endPath with it.
        contours = []
        current = []
        for op in self._rec.value:
            if op[0] == 'moveTo':
                if current:
                    contours.append(current)
                current = [op]
            else:
                current.append(op)
        if current:
            contours.append(current)

        # Also handle components (composite glyphs): pass them straight through.
        # Components are recorded as ('addComponent', (name, transform)).
        # We process them by replaying contour ops first, then components.
        # But _rec.value ordering: if a glyph has both contours and components,
        # components come after the closePaths. Filter:
        contour_ops_list = [c for c in contours]
        components = [op for op in self._rec.value if op[0] == 'addComponent']

        for contour_ops in contour_ops_list:
            self._replay_contour_to(self.out, contour_ops)

        for op in components:
            self.out.addComponent(*op[1])

        self._rec.value = []

    # --- SegmentPen protocol ---

    def moveTo(self, p):
        self._rec.moveTo(p)
        self._current = p

    def lineTo(self, p):
        self._rec.lineTo(p)
        self._current = p

    def curveTo(self, *points):
        self._rec.curveTo(*points)
        self._current = points[-1]

    def qCurveTo(self, *points):
        self._rec.qCurveTo(*points)
        self._current = points[-1]

    def closePath(self):
        self._rec.closePath()

    def endPath(self):
        self._rec.endPath()

    def addComponent(self, *args, **kwargs):
        self._rec.addComponent(*args, **kwargs)

    def _finalize(self):
        """Called after the glyph is drawn: replay with rewriting applied."""
        self._flush()


def _round_counters(font: Font, delta: float, min_length: float = 50.0) -> None:
    """Replace long lineTo segments with bulged curves (--counter-curve)."""
    if delta <= 0:
        return
    tt = font.ttfont

    is_cff = 'CFF ' in tt
    is_tt = 'glyf' in tt
    if not (is_cff or is_tt):
        logger.debug("Counter rounding: no CFF/glyf table; skipping")
        return

    glyph_set = tt.getGlyphSet()
    modified = 0
    skipped = 0

    if is_cff:
        cff = tt['CFF '].cff.topDictIndex[0]
        char_strings = cff.CharStrings
        private = cff.Private
        try:
            cff.decompileAllCharStrings()
        except Exception:
            pass
        for glyph_name in tt.getGlyphOrder():
            if glyph_name not in char_strings or glyph_name not in glyph_set:
                continue
            try:
                width = glyph_set[glyph_name].width
                inner = T2CharStringPen(width, glyph_set)
                cc_pen = _CounterCurvePen(inner, delta, min_length, is_cubic=True)
                glyph_set[glyph_name].draw(cc_pen)
                cc_pen._finalize()
                char_strings[glyph_name] = inner.getCharString(private=private)
                modified += 1
            except Exception as e:
                logger.warning(f"Counter rounding failed for '{glyph_name}': {e}")
                skipped += 1
    else:
        glyf = tt['glyf']
        for glyph_name in tt.getGlyphOrder():
            if glyph_name not in glyf or glyph_name not in glyph_set:
                continue
            if glyf[glyph_name].numberOfContours <= 0:
                continue  # composite / empty
            try:
                width = glyph_set[glyph_name].width
                inner = TTGlyphPen(glyph_set)
                cc_pen = _CounterCurvePen(inner, delta, min_length, is_cubic=False)
                glyph_set[glyph_name].draw(cc_pen)
                cc_pen._finalize()
                g = inner.glyph()
                g.recalcBounds(glyf)
                glyf[glyph_name] = g
                modified += 1
            except Exception as e:
                logger.warning(f"Counter rounding failed for '{glyph_name}': {e}")
                skipped += 1

    logger.debug(f"Counter rounding (delta={delta}, min_length={min_length}): "
                 f"{modified} processed, {skipped} skipped")


# ---------------------------------------------------------------------------
#  Main optimization pipeline
# ---------------------------------------------------------------------------

def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize a single font file using foundrytools.

    Pipeline:
      1. Open font with foundrytools.Font
      2. Scale (uniform) + thicken stems
      3. Width adjust (horizontal-only)
      4. Correct contours (overlap removal) if --shape-cleanup
      5. Round coordinates (CFF) if --pixel-snap
      6. Tune CFF hinting (rebuild zones/stems from real metrics)
      7. Optimize GASP table
      8. Tune OS/2 table
      9. Set head flags
      10. Autohint (ttfautohint-py / AFDKO otfautohint)
      11. Set production names (optional)
      12. Save
    """
    try:
        font_name = os.path.basename(input_path)
        logger.info(f"Loading {font_name}...")
        font = Font(input_path)

        # ---- Step 0: Variable-font instantiation (for --curve-delta / --em-scale) ----
        # gvar/CFF2 deltas are tied to the original point counts, so any
        # outline change desyncs them. If curve smoothing or em-scale is
        # requested on a variable font, instantiate to a static default
        # instance up front (before any outline edit) so the rest of the
        # pipeline operates on a normal static font. Scoped to when
        # --curve-delta or --em-scale is actually used.
        curve_delta = options.get('curve_delta', 0)
        em_scale_pct = options.get('em_scale_percent', 0)
        if (curve_delta and curve_delta > 0 or em_scale_pct != 0) and 'fvar' in font.ttfont:
            try:
                from fontTools.varLib.instancer import instantiateVariableFont
                axes = {ax.axisTag: ax.defaultValue for ax in font.ttfont['fvar'].axes}
                instantiateVariableFont(font.ttfont, axes, inplace=True)
                logger.debug("Variable font instantiated to default static "
                             "instance (gvar/fvar dropped) for --curve-delta/--em-scale")
            except Exception as e:
                logger.warning(f"Could not instantiate variable font for "
                               f"--curve-delta/--em-scale ({e}); outline changes may misalign")

        # ---- Step 1: Scale + thicken ----
        scale_pct = options.get('scale_percent', 0)
        thickness_pct = options.get('thickness_percent', 0)
        if scale_pct != 0 or thickness_pct != 0:
            scale_font_glyphs(font, scale_pct, thickness_pct)

        # ---- Step 1b: Em-square scale (--em-scale) ----
        # Scales outlines/metrics by N within the SAME em-square (UPM unchanged).
        # Gives "bigger glyphs at the same point size" while keeping UPM/precision.
        # Runs AFTER --scale so they compose: --scale changes UPM, --em-scale then
        # pushes outlines further inside that em-square. Order is otherwise
        # arbitrary -- both happen before any rounding/cleanup steps.
        if em_scale_pct != 0:
            em_scale_font_glyphs(font, em_scale_pct)

        # ---- Step 2: Width adjust ----
        width_pct = options.get('width_percent', 0)
        if width_pct != 0:
            width_font_glyphs(font, width_pct)

        # ---- Step 3: Correct contours (overlap removal) ----
        # Must run BEFORE thickening (skia ops can flip CFF winding).
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

        # ---- Step 4: Thicken stems (skia) ----
        # Done inside scale_font_glyphs if thickness requested; nothing extra here.

        # ---- Step 5: Round coordinates (CFF) ----
        if options.get('pixel_snap', 0) > 0 and font.is_ps:
            try:
                font.t_cff_.round_coordinates()
                logger.debug("CFF coordinates rounded")
            except Exception as e:
                logger.warning(f"Round coordinates failed: {e}")

        # ---- Step 6: Tune CFF hinting ----
        if font.is_ps and (options.get('hint_tune', False) or options.get('rebuild_hints', False)):
            _tune_cff_hinting(
                font,
                rebuild=options.get('rebuild_hints', False),
                blue_quantise=options.get('blue_quantise', 0),
            )
            # Subpixel coordinate snapping (both TT and CFF)
            _snap_to_integer(font)
            # Stem width normalisation
            if not options.get('no_stem_round'):
                _round_stem_widths(font)

        # ---- Step 7: TrueType outline cleanup ----
        if options.get('shape_cleanup') and font.is_tt:
            _cleanup_tt_outlines(font)

        # ---- Step 8: Dropout control (TrueType) ----
        if options.get('dropout_control') and font.is_tt:
            _add_dropout_control(font)

        # ---- Step 9: GASP table ----
        if options.get('solid'):
            _optimize_gasp_table(font, GASP_RANGES_SOLID)
        else:
            gasp_mode = options.get('gasp_mode', 'detailed')
            if gasp_mode == 'simple':
                _optimize_gasp_table(font, GASP_RANGES_SIMPLE)
            elif options.get('pixel_gasp'):
                _optimize_gasp_table(font, GASP_RANGES_PIXEL_ALIGNED)
            elif options.get('gasp_detail'):
                detail = options['gasp_detail']
                if detail == 'minimal':
                    _optimize_gasp_table(font, GASP_RANGES_MINIMAL)
                elif detail == 'balanced':
                    _optimize_gasp_table(font, GASP_RANGES_BALANCED)
                else:
                    _optimize_gasp_table(font, GASP_RANGES_AGGRESSIVE)
            else:
                _optimize_gasp_table(font, GASP_RANGES_DETAILED)

        # ---- Step 10: Tune OS/2 ----
        _tune_os2(font, weight_offset=options.get('weight_offset', 0))

        # ---- Step 11: Set head flags ----
        _optimize_head_flags(font)

        # ---- Step 11: Curve smoothing (--curve-delta) ----
        # Must run before autohint so hints are built on the smoothed outlines.
        curve_delta = options.get('curve_delta', 0)
        if curve_delta and curve_delta > 0:
            _smooth_curves(font, curve_delta)

        # ---- Step 11b: Counter rounding (--counter-curve) ----
        # Replaces long straight lineTo segments in glyph contours with bulged
        # curves, rounding flat-top counters (e.g. 'e', 'a', 'o'). Runs after
        # curve-delta (which only simplifies existing curves) and before autohint
        # so hints are built on the rounded outlines.
        counter_curve = options.get('counter_curve', 0)
        if counter_curve and counter_curve > 0:
            _round_counters(font, counter_curve)

        # ---- Step 12: Autohint ----
        if options.get('autohint', True):
            if not autohint_font(font):
                font.close()
                return False

        # ---- Step 13: Set production names (after autohint) ----
        if options.get('set_prod_names', False):
            try:
                renamed = font.set_production_names()
                if renamed:
                    logger.debug(f"Renamed {len(renamed)} glyphs to production names")
            except Exception as e:
                logger.warning(f"set_production_names failed: {e}")

        # ---- Step 14: Save ----
        font.save(output_path)
        font.close()

        input_size = os.path.getsize(input_path)
        output_size = os.path.getsize(output_path)
        if input_size > 0:
            reduction = ((input_size - output_size) / input_size) * 100
            logger.info(f"Optimized {font_name} "
                        f"({input_size:,} -> {output_size:,} bytes, {reduction:.1f}% change)")
        else:
            logger.info(f"Optimized {font_name}")
        return True

    except Exception as e:
        logger.error(f"Error optimizing {input_path}: {e}")
        logger.debug(traceback.format_exc())
        return False


def process_directory(input_dir: str, output_dir: str, options: dict) -> int:
    """Process all .otf/.ttf files in a directory. Returns success count."""
    input_path = Path(input_dir)
    output_path = Path(output_dir)

    if not input_path.exists():
        logger.error(f"Input directory does not exist: {input_dir}")
        return 0
    if not input_path.is_dir():
        logger.error(f"Input path is not a directory: {input_dir}")
        return 0

    output_path.mkdir(parents=True, exist_ok=True)

    font_files = []
    for ext in ('*.otf', '*.ttf'):
        font_files.extend(input_path.glob(ext))

    if not font_files:
        logger.warning(f"No font files found in {input_dir}")
        return 0

    logger.info(f"Found {len(font_files)} font file(s) to process")
    success_count = 0

    for font_file in sorted(font_files):
        output_file = output_path / font_file.name
        if optimize_font(str(font_file), str(output_file), options):
            success_count += 1

    return success_count


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OTF/TTF Font Optimization Script v10 (foundrytools-based)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --solid --hint-tune --rebuild-hints --thickness 2.5 --scale 5.0 input/ output/
  %(prog)s --clear-shaping input/ output/
  %(prog)s --hint-tune --blue-quantise 8 input/ output/
  %(prog)s --condense 8 input/ output/
  %(prog)s --expand 5 input/ output/
        """
    )

    parser.add_argument("input_dir", help="Input directory containing font files (.otf, .ttf)")
    parser.add_argument("output_dir", help="Output directory for optimized font files")

    # Solid / clear-shaping
    parser.add_argument("--solid", action="store_true", dest="solid",
                        help="Master switch for solid, concrete rendering.")
    parser.add_argument("--clear-shaping", action="store_true", dest="clear_shaping",
                        help="Clearer shaping: GASP, head flags, overlap removal.")

    # Weight
    parser.add_argument("--weight-offset", type=int, default=0, dest="weight_offset",
                        help="Add this to OS/2 usWeightClass (0=no change). WARNING: "
                             "affects font matching.")

    # Scaling (v8.5 dual-mode semantics)
    parser.add_argument("--scale", type=float, default=0, dest="scale_percent",
                        help="Scale font size (UPM + outlines together). |value|<1.0 = "
                             "direct multiplier (0.5 = 50%% size); |value|>=1.0 = "
                             "percentage change (5 = +5%%). Capped [0.10, 4.00]. "
                             "Higher UPM = more precision at same visual size.")
    parser.add_argument("--em-scale", type=float, default=0, dest="em_scale_percent",
                        help="Scale outlines/metrics within the em-square (UPM preserved). "
                             "Same dual-mode semantics as --scale. Use this for 'bigger "
                             "glyphs at the same point size' without changing UPM. "
                             "Composes with --scale (outlines get both factors). "
                             "Capped [0.10, 4.00].")
    parser.add_argument("--counter-curve", type=float, default=18, dest="counter_curve",
                        help="Replace long lineTo segments in glyph contours with curved "
                             "segments that bulge outward by N font units (default 18). "
                             "Rounds flat-top counters like 'e' and 'a'. Only segments "
                             "at least 50 units long are touched. Set to 0 to disable.")
    parser.add_argument("--thickness", type=float, default=0, dest="thickness_percent",
                        help="Thicken stems by percent (2.5 = +2.5%% thicker). "
                             "Advance widths preserved. Recommended 1-10.")
    parser.add_argument("--no-thickness", action="store_true", dest="no_thickness",
                        help="Disable --thickness (escape hatch).")

    # Width / expand / condense (v8.5 semantics)
    parser.add_argument("--width", type=float, default=0, dest="width_percent",
                        help="Adjust horizontal width. Positive = expand, negative = "
                             "condense. Independent of --scale.")
    parser.add_argument("--expand", type=float, default=None, dest="expand_percent",
                        help="Alias for --width positive (--expand 5 = --width 5).")
    parser.add_argument("--condense", type=float, default=None, dest="condense_percent",
                        help="Alias for --width negative (--condense 8 = --width -8).")

    # Hinting
    parser.add_argument("--hint-tune", action="store_true", dest="hint_tune",
                        help="Tune CFF Private dict defaults (LanguageGroup, etc.).")
    parser.add_argument("--rebuild-hints", action="store_true", dest="rebuild_hints",
                        help="Recalculate BlueValues/OtherBlues AND StdHW/StdVW/StemSnap* "
                             "from real glyph metrics.")
    parser.add_argument("--blue-quantise", type=int, default=0, dest="blue_quantise",
                        help="Round BlueValues/OtherBlues to N-unit grid (0=off).")
    parser.add_argument("--no-stem-round", action="store_true", dest="no_stem_round",
                        help="Skip stem width rounding (StdHW/StdVW/StemSnap).")
    parser.add_argument("--no-autohint", action="store_false", dest="autohint",
                        help="Skip autohinting.")

    # Cleanup
    parser.add_argument("--shape-cleanup", action="store_true", dest="shape_cleanup",
                        help="Remove overlaps + correct contour direction (skia-pathops).")
    parser.add_argument("--pixel-snap", type=int, default=0, dest="pixel_snap",
                        help="Round CFF coordinates to integers (0=disable).")
    parser.add_argument("--dropout-control", action="store_true", dest="dropout_control",
                        help="Add dropout control via PREP table (TrueType only).")

    # GASP
    parser.add_argument("--gasp-mode", type=str, default="detailed", dest="gasp_mode",
                        choices=["detailed", "simple"],
                        help="GASP table mode (default detailed).")
    parser.add_argument("--gasp-detail", type=str, default=None, dest="gasp_detail",
                        choices=["aggressive", "balanced", "minimal"],
                        help="Granular GASP control (overrides --gasp-mode).")
    parser.add_argument("--pixel-gasp", action="store_true", dest="pixel_gasp",
                        help="Pixel-aligned 7-range GASP (overrides --gasp-detail).")

    # Naming
    parser.add_argument("--set-prod-names", action="store_true", dest="set_prod_names",
                        help="Rename glyphs to production names from cmap.")

    # Verbosity
    parser.add_argument("-v", "--verbose", action="store_true", dest="verbose",
                        help="Verbose logging.")

    # Curve smoothing
    parser.add_argument("--curve-delta", type=float, default=12, dest="curve_delta",
                        help="Curve smoothing tolerance in font units (default 12; 0 = off). "
                             "Higher = smoother outlines (fewer control points). Suggested "
                             "range 4-32; values above ~32 can distort fine details. "
                             "Format-preserving: CFF stays cubic, TT stays quadratic.")

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not check_dependencies():
        sys.exit(1)

    _resolve_width_or_die(args)

    options = {
        'solid': args.solid,
        'clear_shaping': args.clear_shaping or args.solid,
        'weight_offset': args.weight_offset if args.weight_offset > 0 else 0,
        'scale_percent': args.scale_percent,
        'em_scale_percent': args.em_scale_percent,
        'thickness_percent': args.thickness_percent if not args.no_thickness else 0,
        'width_percent': _resolve_width(args),
        'hint_tune': args.hint_tune,
        'rebuild_hints': args.rebuild_hints,
        'blue_quantise': args.blue_quantise,
        'no_stem_round': args.no_stem_round,
        'autohint': args.autohint,
        'shape_cleanup': args.shape_cleanup,
        'pixel_snap': args.pixel_snap,
        'dropout_control': args.dropout_control,
        'gasp_mode': args.gasp_mode,
        'gasp_detail': args.gasp_detail,
        'pixel_gasp': args.pixel_gasp,
        'set_prod_names': args.set_prod_names,
        'curve_delta': args.curve_delta,
        'counter_curve': args.counter_curve,
    }

    logger.info("Starting font optimization (v10, foundrytools)...")
    logger.info(f"Input directory:  {args.input_dir}")
    logger.info(f"Output directory: {args.output_dir}")
    if args.solid:
        logger.info("Solid mode: ENABLED")
    if args.clear_shaping:
        logger.info("Clear-shaping mode: ENABLED")
    if args.scale_percent != 0:
        logger.info(f"Font scaling: {args.scale_percent:+g}")
    if args.em_scale_percent != 0:
        logger.info(f"Em-square scaling: {args.em_scale_percent:+g} (UPM preserved)")
    if args.thickness_percent:
        logger.info(f"Thickness: +{args.thickness_percent}%")
    if args.hint_tune:
        logger.info("CFF hint tuning: ENABLED")
    if args.rebuild_hints:
        logger.info("Hint rebuild: ENABLED")
    if args.shape_cleanup:
        logger.info("Shape cleanup: ENABLED")
    if args.gasp_detail:
        logger.info(f"GASP detail: {args.gasp_detail}")
    if args.pixel_gasp:
        logger.info("Pixel-aligned GASP: ENABLED")

    success_count = process_directory(args.input_dir, args.output_dir, options)
    logger.info(f"Optimization complete! Successfully processed {success_count} fonts.")


if __name__ == "__main__":
    main()
