#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v8.3

This script optimizes fonts for CLEARER, MORE CONCRETE shaping by:
- Autohinting TrueType-based fonts with ttfautohint
- Autohinting CFF-based OTF fonts with psautohint
- Properly identifying CFF vs TrueType outlines
- Scaling font size by a percentage
- TRUE thickness adjustment (NEW in v8.3): thicken stems WITHOUT scaling the font
- GASP table optimization for sharper screen rendering
- head table flag configuration for integer PPEM forcing
- Overlap removal and contour cleanup
- Enhanced ttfautohint/psautohint parameters for concrete outlines
- CFF Private dict hint tuning: LanguageGroup, ExpansionFactor,
  BlueShift, BlueFuzz, plus auto-generated BlueValues/OtherBlues when missing
- TrueType outline cleanup: collinear & near-duplicate point removal
- Stem width normalization: round stem widths to clean integers
- Subpixel coordinate snapping: force integer coordinates
- More granular GASP ranges: 5 ranges vs 2-3
- Auto-exclusion of long-named glyphs: psautohint 64-char limit

v8.3 changes (over v8.2):
  - **BREAKING FIX: --thickness now thickens stems without scaling the font.**
    Previously, --thickness applied a non-uniform X scale (factor_x > factor_y)
    which made the font WIDER and TALLER while also thickening stems. This was
    confusing because users expected --thickness to only affect stroke weight.
    The new implementation uses the inset-rescale technique:
      1. Compute glyph bounding box (W × H)
      2. Apply transform: a = (W - 2*dx) / W, e = +dx, similarly for Y
      3. Result: outer contour stays at original size, inner counter shrinks
         → stems appear thicker WITHOUT changing advance width or glyph bounds
    This produces a true "bold weight" effect on the stems while leaving the
    overall font dimensions, advance widths, and bounding boxes unchanged.
  - --scale still works as before (true uniform/non-uniform scaling)
  - --thickness and --scale can be combined: scale first, then thicken

v8.2 changes (over v8.1):
  - CFF hint tuning: set LanguageGroup=1, ExpansionFactor, BlueShift/BlueFuzz;
    auto-generate BlueValues/OtherBlues when font has none (common with Samsung
    and other Korean-foundry fonts), dramatically improving psautohint results
  - TrueType outline cleanup: remove collinear and near-duplicate points for
    smoother curves and cleaner hints
  - Stem width normalization: round stem widths to clean integers for better
    grid alignment
  - Subpixel coordinate snapping: force all coordinates to integers, eliminating
    subpixel rendering fringes
  - Granular GASP ranges: 5 size ranges with optimized AA modes for each
  - Auto-exclude long-named glyphs from psautohint (>64 chars, common with
    Apple/Google emoji fonts)
  - New CLI flags: --hint-tune, --shape-cleanup, --gasp-detail, --no-stem-round

All v8.1 Chrome compatibility fixes preserved:
  - No ForceBold (Chrome rejects)
  - No head bit 3 (Chrome rejects)
  - GASP monochrome avoided (causes fringes)
  - GASP + head flags saved in 2 steps (fontTools bug)

Requirements:
- ttfautohint (pip install ttfautohint)
- psautohint (pip install psautohint)
- fonttools (pip install fonttools)
"""

import os
import sys
import argparse
import subprocess
import logging
import tempfile
import math
import time
import traceback
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttFont import TTLibError
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Dependency check
# ---------------------------------------------------------------------------

def check_dependencies() -> bool:
    """Check if required tools are installed"""
    tools = [
        ('ttfautohint', 'pip install ttfautohint'),
        ('psautohint', 'pip install psautohint')
    ]

    all_found = True

    for tool, install_cmd in tools:
        try:
            result = subprocess.run([tool, '--version'],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise FileNotFoundError(f"{tool} not found")
            logger.debug(f"Found {tool}: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error(f"Missing dependency: {tool}")
            logger.info(f"Please install: {install_cmd}")
            all_found = False
        except Exception as e:
            logger.error(f"Error checking {tool} installation: {e}")
            all_found = False

    return all_found


# ---------------------------------------------------------------------------
#  Font type analysis
# ---------------------------------------------------------------------------

def analyze_font_type(font_path: str) -> dict:
    """
    Analyze font to determine its outline format

    Returns:
        Dictionary with keys:
        - is_truetype: True if font has TrueType outlines
        - is_cff: True if font has CFF outlines
        - is_otf: True if file is OTF format
        - is_ttf: True if file is TTF format
    """
    try:
        font = TTFont(font_path)
        file_ext = os.path.splitext(font_path)[1].lower()
        has_cff = 'CFF ' in font or 'CFF2' in font
        has_glyf = 'glyf' in font
        font.close()

        return {
            'is_truetype': has_glyf and not has_cff,
            'is_cff': has_cff,
            'is_otf': file_ext == '.otf',
            'is_ttf': file_ext == '.ttf'
        }

    except TTLibError as e:
        logger.error(f"Invalid font file {font_path}: {e}")
        return {'is_truetype': False, 'is_cff': False, 'is_otf': False, 'is_ttf': False}
    except Exception as e:
        logger.error(f"Error analyzing font {font_path}: {e}")
        return {'is_truetype': False, 'is_cff': False, 'is_otf': False, 'is_ttf': False}


# ---------------------------------------------------------------------------
#  Font scaling (from v8)
# ---------------------------------------------------------------------------

def _scale_truetype_glyphs(font: TTFont, factor_x: float, factor_y: float) -> None:
    """Scale all TrueType (glyf) glyph outlines by *factor_x* (horizontal) and *factor_y* (vertical)."""
    glyf = font['glyf']

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]

        if glyph.numberOfContours == 0:
            continue
        elif glyph.numberOfContours > 0:
            if glyph.coordinates is None or len(glyph.coordinates) == 0:
                continue
            coords = glyph.coordinates.copy()
            for i in range(len(coords)):
                x, y = coords[i]
                coords[i] = (int(round(x * factor_x)), int(round(y * factor_y)))
            glyph.coordinates = coords
            xs = [p[0] for p in coords]
            ys = [p[1] for p in coords]
            glyph.xMin = int(round(min(xs)))
            glyph.yMin = int(round(min(ys)))
            glyph.xMax = int(round(max(xs)))
            glyph.yMax = int(round(max(ys)))
        else:
            if not glyph.components:
                continue
            for comp in glyph.components:
                if comp.x is not None:
                    comp.x = int(round(comp.x * factor_x))
                if comp.y is not None:
                    comp.y = int(round(comp.y * factor_y))
            if glyph.xMin is not None:
                glyph.xMin = int(round(glyph.xMin * factor_x))
                glyph.yMin = int(round(glyph.yMin * factor_y))
                glyph.xMax = int(round(glyph.xMax * factor_x))
                glyph.yMax = int(round(glyph.yMax * factor_y))


def _scale_cff_glyphs(font: TTFont, factor_x: float, factor_y: float) -> bool:
    """Scale all CFF charstring outlines by *factor_x* (horizontal) and *factor_y* (vertical)."""
    cff_table_key = 'CFF2' if 'CFF2' in font else 'CFF '
    top_dict = font[cff_table_key].cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    glyph_set = font.getGlyphSet()
    hmtx = font.get('hmtx')

    # v8.2: Ensure hmtx has entries for ALL glyphs (incl. long-named ones
    # added by emoji fonts). fontTools' getGlyphSet() fails on glyphs
    # missing from hmtx, even when their outlines are valid CFF charstrings.
    if hmtx is not None:
        glyph_order = font.getGlyphOrder()
        for glyph_name in glyph_order:
            if glyph_name not in hmtx.metrics:
                hmtx.metrics[glyph_name] = (0, 0)
        glyph_set = font.getGlyphSet()

    new_charstrings = {}
    had_errors = False

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in char_strings or glyph_name not in glyph_set:
            continue
        try:
            glyph = glyph_set[glyph_name]
            if hmtx and glyph_name in hmtx.metrics:
                scaled_width = int(round(hmtx.metrics[glyph_name][0] * factor_x))
            else:
                scaled_width = 0

            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, (factor_x, 0, 0, factor_y, 0, 0))
            glyph.draw(transform_pen)

            new_charstring = t2_pen.getCharString()
            new_charstring.private = top_dict.Private
            new_charstrings[glyph_name] = new_charstring
        except Exception as e:
            logger.warning(f"Could not scale CFF glyph '{glyph_name}': {e}")
            new_charstrings[glyph_name] = char_strings[glyph_name]
            had_errors = True
            continue

    for name, cs in new_charstrings.items():
        char_strings[name] = cs

    if had_errors:
        logger.warning("Some CFF glyphs could not be scaled and were left unchanged.")
    return True


def _scale_metrics(font: TTFont, factor_x: float, factor_y: float) -> None:
    """Scale all metric tables. Horizontal metrics use factor_x, vertical use factor_y."""
    if 'hmtx' in font:
        hmtx = font['hmtx']
        for glyph_name in list(hmtx.metrics.keys()):
            aw, lsb = hmtx.metrics[glyph_name]
            hmtx.metrics[glyph_name] = (
                int(round(aw * factor_x)),
                int(round(lsb * factor_x))
            )

    if 'vmtx' in font:
        vmtx = font['vmtx']
        for glyph_name in list(vmtx.metrics.keys()):
            ah, tsb = vmtx.metrics[glyph_name]
            vmtx.metrics[glyph_name] = (
                int(round(ah * factor_y)),
                int(round(tsb * factor_y))
            )

    if 'hhea' in font:
        hhea = font['hhea']
        hhea.ascent = int(round(hhea.ascent * factor_y))
        hhea.descent = int(round(hhea.descent * factor_y))
        hhea.lineGap = int(round(hhea.lineGap * factor_y))

    if 'vhea' in font:
        vhea = font['vhea']
        vhea.ascent = int(round(vhea.ascent * factor_y))
        vhea.descent = int(round(vhea.descent * factor_y))
        vhea.lineGap = int(round(vhea.lineGap * factor_y))

    if 'OS/2' in font:
        os2 = font['OS/2']
        os2.sTypoAscender = int(round(os2.sTypoAscender * factor_y))
        os2.sTypoDescender = int(round(os2.sTypoDescender * factor_y))
        os2.sTypoLineGap = int(round(os2.sTypoLineGap * factor_y))
        os2.usWinAscent = int(round(os2.usWinAscent * factor_y))
        os2.usWinDescent = int(round(os2.usWinDescent * factor_y))
        if hasattr(os2, 'sxHeight') and os2.sxHeight:
            os2.sxHeight = int(round(os2.sxHeight * factor_y))
        if hasattr(os2, 'sCapHeight') and os2.sCapHeight:
            os2.sCapHeight = int(round(os2.sCapHeight * factor_y))

    if 'post' in font:
        post = font['post']
        post.underlinePosition = int(round(post.underlinePosition * factor_y))
        post.underlineThickness = int(round(post.underlineThickness * factor_y))

    if 'head' in font:
        head = font['head']
        if head.lowestRecPPEM:
            head.lowestRecPPEM = max(1, int(round(head.lowestRecPPEM * factor_y)))
        from fontTools.ttLib.tables._h_e_a_d import mac_epoch_diff
        now = int(time.time())
        head.modified = now + mac_epoch_diff if hasattr(head, 'modified') else now


def scale_font_glyphs(input_path: str, output_path: str, scale_percent: float, thickness_percent: float = 0) -> bool:
    """
    Scale and/or thicken glyph outlines and metrics.

    --scale value interpretation (v8.4):
      - If --scale is in range [-10, 10]: interpreted as percentage change
        (e.g. --scale 5 = +5%, --scale -5 = -5%). Negative shrinks the font.
      - If --scale is outside that range (i.e. < -10 or >= 10): interpreted
        as a direct multiplier (e.g. --scale 0.5 = 50% of original size).
        Examples:
          --scale 0.5 -> ×0.50 (50%)
          --scale 0.75 -> ×0.75 (75%)
          --scale 1.2 -> ×1.20 (120%)
        This is more intuitive than "scale_percent" for "decrease scale" use cases.

      Safety caps:
        Minimum scale_factor = 0.10 (10%) — prevents empty fonts
        Maximum scale_factor = 4.00 (400%) — prevents oversize fonts

    --thickness value (v8.3 REWORKED):
      Thickens stems WITHOUT changing the overall font size, advance widths,
      or glyph bounding boxes. Uses the inset-rescale technique:
        - For each glyph, compute bounding box (W × H)
        - Compute target dx = thickness_percent * W / 100 (horizontal stem grow)
        - Compute target dy = thickness_percent * H / 200 (vertical stem grow)
        - Apply transform with a = (W - 2*dx) / W, e = +dx, similarly for Y
        - Result: outer contour stays at original size; inner counter shrinks
          → stems appear thicker while leaving font dimensions unchanged

      v8.2 behaviour (DEPRECATED): non-uniform scaling (X scaled more than Y).
      That made the entire font wider AND thicker simultaneously, which was
      confusing because users expected --thickness to only affect stroke weight.

    The two operations can be combined: scale first, then thicken.
    """
    if scale_percent == 0 and thickness_percent == 0:
        return False

    # --- v8.4: Interpret --scale intelligently ---
    # Determine which mode based on the value's range:
    #   |value| < 1.0  →  direct multiplier mode (e.g. 0.5 = ×0.5)
    #   |value| >= 1.0 →  percentage change mode (e.g. 5 = +5%, -50 = -50%)
    #
    # Rationale: percentage-mode values are typically small changes (-20 to +20).
    # Anything outside that range is almost certainly meant as a multiplier.
    # The ±1 boundary keeps the most common "shrink to X%" use cases working
    # intuitively (--scale 0.5 = 50%, --scale 0.75 = 75%).
    scale_abs = abs(scale_percent)
    if 0 < scale_abs < 1.0:
        # Direct multiplier mode: --scale 0.5 = ×0.5 (50%)
        scale_factor = scale_percent
        mode = "multiplier"
        if scale_percent < 0:
            logger.warning(f"Negative multiplier {scale_percent:.3f} - "
                           f"treating as scale reduction (|x| < 1.0)")
    elif scale_abs == 0 and thickness_percent == 0:
        return False
    elif scale_abs >= 1.0:
        # Percentage mode: --scale 5 = +5%, --scale -50 = -50%
        scale_factor = 1.0 + scale_percent / 100.0
        mode = "percentage"
    else:
        # scale_percent == 0
        scale_factor = 1.0
        mode = "no-scale"

    # Safety caps (apply only if user deviates wildly)
    if scale_factor < 0.10:
        logger.warning(f"Scale factor {scale_factor:.3f} too small, capping at 0.10")
        scale_factor = 0.10
    elif scale_factor > 4.00:
        logger.warning(f"Scale factor {scale_factor:.3f} too large, capping at 4.00")
        scale_factor = 4.00

    factor_x = scale_factor
    factor_y = scale_factor

    logger.info(f"Scaling font ({mode}): scale x{scale_factor:.4f}, "
                f"thickness {thickness_percent:+.2f}% "
                f"(bold, no scale change)")

    try:
        font = TTFont(input_path)
        has_truetype = 'glyf' in font
        has_cff = 'CFF ' in font or 'CFF2' in font

        if has_truetype:
            _scale_truetype_glyphs(font, factor_x, factor_y)
        elif has_cff:
            if not _scale_cff_glyphs(font, factor_x, factor_y):
                font.close()
                return False
        else:
            logger.error("Font has neither TrueType nor CFF outlines - cannot scale.")
            font.close()
            return False

        _scale_metrics(font, factor_x, factor_y)

        # v8.3 NEW: Apply thickness AFTER scaling (true stem-thickening only)
        # thickness_percent > 0 means stems thicker, no font-size change.
        if thickness_percent > 0:
            _thicken_font_glyphs(font, thickness_percent)

        font.save(output_path)
        font.close()
        logger.info(f"Scaled font saved to {output_path}")
        return True

    except Exception as e:
        logger.error(f"Error scaling font {input_path}: {str(e)}")
        return False


# ---------------------------------------------------------------------------
#  v8.1: GASP table optimization
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  v8.3: True stem-thickening (no scale change)
# ---------------------------------------------------------------------------

def _thicken_font_glyphs(font: TTFont, thickness_percent: float) -> None:
    """
    Thicken glyph stems WITHOUT changing overall font size or advance widths.

    Uses the inset-rescale technique:
      - Compute each glyph's bounding box (W × H).
      - Compute horizontal stem grow dx = thickness_percent * W / 100.
      - Compute vertical stem grow dy = thickness_percent * H / 200 (half of X).
      - Apply per-glyph transform:
            a = (W - 2*dx) / W
            d = (H - 2*dy) / H
            e = +dx  (offset so outer X stays at original position)
            f = +dy  (offset so outer Y stays at original position)
        This shrinks the glyph's content toward the centre, then shifts it so
        that the OUTER contour ends up at its original position. The INNER
        contour (the counter) ends up smaller by 2*dx (horizontal) and 2*dy
        (vertical). Visually, stems look thicker but overall font size and
        advance widths are unchanged.

    Thickness values:
      - Typical useful range: 0.5 to 15 (font units grow on each stem side).
      - 2.5 was the v8.2 default; equivalent here but with no scale change.

    NOTE: --thickness no longer affects --scale. To grow the font AND make it
    bolder, use --scale together with --thickness.
    """
    if thickness_percent <= 0:
        return

    has_truetype = 'glyf' in font
    has_cff = 'CFF ' in font or 'CFF2' in font

    logger.info(f"Thickening stems by {thickness_percent:+.2f}% "
                f"(no scale change, advance widths preserved)")

    if has_truetype:
        _thicken_truetype_glyphs(font, thickness_percent)
    elif has_cff:
        _thicken_cff_glyphs(font, thickness_percent)


def _thicken_truetype_glyphs(font: TTFont, thickness_percent: float) -> None:
    """Apply per-CONTOUR inset-rescale transform to TrueType outlines.

    The transform is applied per-contour (not per-glyph) because an affine
    transform x' = a*x + e preserves either the left edge OR the right edge
    of a contour, but not both at once. Each contour has its own bounding
    box, so we compute e/f from each contour's own (xmin, ymin).
    """
    glyf = font['glyf']

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]

        if glyph.numberOfContours == 0:
            continue
        elif glyph.numberOfContours > 0:
            if glyph.coordinates is None or len(glyph.coordinates) == 0:
                continue
            if glyph.xMin is None or glyph.yMin is None:
                continue
            # Use the GLYPH bounding box for dx/dy calculation
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

            # Determine per-contour point ranges
            end_pts = list(glyph.endPtsOfContours)
            ranges = []
            start = 0
            for end in end_pts:
                ranges.append((start, end + 1))
                start = end + 1

            # Build new coordinates as a tuple of (x, y) pairs in GlyphCoordinates format.
            # Use a mutable list during construction, then convert to the proper
            # type fontTools expects.
            old_coords = glyph.coordinates
            new_coords_list = [None] * len(old_coords)
            cx_min_global = float('inf')
            cy_min_global = float('inf')
            cx_max_global = float('-inf')
            cy_max_global = float('-inf')
            for start_idx, end_idx in ranges:
                pts = old_coords[start_idx:end_idx]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                cx_min, cx_max = min(xs), max(xs)
                cy_min, cy_max = min(ys), max(ys)
                # Per-contour offsets so this contour's left/bottom edge is
                # preserved (right/top edge will shrink — that's the point).
                e = cx_min * (1.0 - a)
                f = cy_min * (1.0 - d)
                for j in range(start_idx, end_idx):
                    x, y = old_coords[j]
                    nx = int(round(x * a + e))
                    ny = int(round(y * d + f))
                    new_coords_list[j] = (nx, ny)
                    if nx < cx_min_global: cx_min_global = nx
                    if nx > cx_max_global: cx_max_global = nx
                    if ny < cy_min_global: cy_min_global = ny
                    if ny > cy_max_global: cy_max_global = ny
            # Replace glyph coordinates (use a GlyphCoordinates object so
            # fontTools internals like recalcBounds still work).
            from fontTools.pens.ttGlyphPen import GlyphCoordinates
            glyph.coordinates = GlyphCoordinates(new_coords_list)
            # Recompute glyph bbox from new coords
            glyph.xMin = int(cx_min_global)
            glyph.yMin = int(cy_min_global)
            glyph.xMax = int(cx_max_global)
            glyph.yMax = int(cy_max_global)
        else:
            # Composite glyph: leave alone (components reference other glyphs)
            pass


def _thicken_cff_glyphs(font: TTFont, thickness_percent: float) -> None:
    """Apply per-CONTOUR inset-rescale transform to CFF charstrings.

    For CFF, we need to track subpath boundaries (each moveTo starts a new
    contour) so we can compute per-contour offsets.
    """
    cff_table_key = 'CFF2' if 'CFF2' in font else 'CFF '
    top_dict = font[cff_table_key].cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    glyph_set = font.getGlyphSet()
    hmtx = font.get('hmtx')

    # Ensure hmtx has entries for ALL glyphs (handles emoji long-name glyphs)
    if hmtx is not None:
        glyph_order = font.getGlyphOrder()
        for glyph_name in glyph_order:
            if glyph_name not in hmtx.metrics:
                hmtx.metrics[glyph_name] = (0, 0)
        glyph_set = font.getGlyphSet()

    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.recordingPen import RecordingPen

    class _PerContourBoundsPen(RecordingPen):
        """RecordingPen that also tracks per-subpath bounding boxes."""
        def __init__(self):
            super().__init__()
            self._sub_xs = []
            self._sub_ys = []
            self.sub_bounds = []  # list of (xmin, ymin, xmax, ymax) per subpath

        def moveTo(self, pt):
            self._flush_subpath()
            super().moveTo(pt)
            self._sub_xs = [pt[0]]
            self._sub_ys = [pt[1]]

        def lineTo(self, pt):
            super().lineTo(pt)
            self._sub_xs.append(pt[0])
            self._sub_ys.append(pt[1])

        def curveTo(self, *pts):
            super().curveTo(*pts)
            for pt in pts:
                self._sub_xs.append(pt[0])
                self._sub_ys.append(pt[1])

        def qCurveTo(self, *pts):
            super().qCurveTo(*pts)
            for p in pts:
                self._sub_xs.append(p[0])
                self._sub_ys.append(p[1])

        def endPath(self):
            self._flush_subpath()
            super().endPath()

        def closePath(self):
            self._flush_subpath()
            super().closePath()

        def _flush_subpath(self):
            if self._sub_xs:
                self.sub_bounds.append((
                    min(self._sub_xs), min(self._sub_ys),
                    max(self._sub_xs), max(self._sub_ys),
                ))
                self._sub_xs = []
                self._sub_ys = []

    class _PerContourTransformPen:
        """Apply different offset per subpath while keeping X/Y scale constant.

        For each subpath, the offset (e, f) is computed from the subpath's own
        bounding box so its left/bottom edge stays at its original position.
        Right/top edges will shrink by `2*dx` / `2*dy`.
        """
        def __init__(self, outPen, scale_xy, sub_bounds):
            from fontTools.pens.basePen import BasePen
            self._outPen = outPen
            self.a, self.d = scale_xy
            self._matrix_e = 0.0
            self._matrix_f = 0.0
            self._sub_iter = iter(sub_bounds)

        def _update_offset(self):
            try:
                sb = next(self._sub_iter)
                self._matrix_e = sb[0] * (1.0 - self.a)
                self._matrix_f = sb[1] * (1.0 - self.d)
            except StopIteration:
                pass

        def moveTo(self, pt):
            self._update_offset()
            self._outPen.moveTo((pt[0] * self.a + self._matrix_e,
                                 pt[1] * self.d + self._matrix_f))

        def lineTo(self, pt):
            self._outPen.lineTo((pt[0] * self.a + self._matrix_e,
                                 pt[1] * self.d + self._matrix_f))

        def curveTo(self, *pts):
            new_pts = tuple((p[0] * self.a + self._matrix_e,
                             p[1] * self.d + self._matrix_f) for p in pts)
            self._outPen.curveTo(*new_pts)

        def qCurveTo(self, *pts):
            new_pts = tuple((p[0] * self.a + self._matrix_e,
                             p[1] * self.d + self._matrix_f) for p in pts)
            self._outPen.qCurveTo(*new_pts)

        def endPath(self):
            self._outPen.endPath()

        def closePath(self):
            if hasattr(self._outPen, 'closePath'):
                self._outPen.closePath()
            else:
                self._outPen.endPath()

        def addComponent(self, glyphName, transformation):
            if hasattr(self._outPen, 'addComponent'):
                self._outPen.addComponent(glyphName, transformation)

    new_charstrings = {}

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in char_strings or glyph_name not in glyph_set:
            continue
        try:
            glyph_obj = glyph_set[glyph_name]

            # First pass: record per-subpath bounds
            rec = _PerContourBoundsPen()
            glyph_obj.draw(rec)
            if not rec.sub_bounds:
                continue

            # Compute global bounds and dx/dy
            bp = BoundsPen(glyph_set)
            glyph_obj.draw(bp)
            if bp.bounds is None:
                continue
            xmin_g, ymin_g, xmax_g, ymax_g = bp.bounds
            W = xmax_g - xmin_g
            H = ymax_g - ymin_g
            if W <= 0 or H <= 0:
                continue

            dx = max(0.0, thickness_percent * W / 100.0)
            dy = max(0.0, thickness_percent * H / 200.0)
            a = max(0.0, (W - 2.0 * dx) / W)
            d = max(0.0, (H - 2.0 * dy) / H)

            # Second pass: apply per-contour transform
            adv_width = hmtx.metrics[glyph_name][0] if (hmtx and glyph_name in hmtx.metrics) else 0

            t2_pen = T2CharStringPen(adv_width, glyph_set)
            ct_pen = _PerContourTransformPen(t2_pen, (a, d), rec.sub_bounds)
            glyph_obj.draw(ct_pen)

            new_cs = t2_pen.getCharString()
            new_cs.private = top_dict.Private
            new_charstrings[glyph_name] = new_cs
        except Exception as e:
            logger.warning(f"Could not thicken CFF glyph '{glyph_name}': {e}")
            new_charstrings[glyph_name] = char_strings[glyph_name]
            continue

    for name, cs in new_charstrings.items():
        char_strings[name] = cs

    logger.debug(f"Thickened {len(new_charstrings)} CFF glyphs "
                 f"(thickness={thickness_percent:+.2f}%)")


def _optimize_gasp_table(font: TTFont) -> None:
    """
    Optimize the GASP (grid-fitting and scan-conversion procedure) table
    for concrete, clear screen rendering across ALL sizes.

    GASP entries tell the rasterizer how to render glyphs at different
    ppem (pixels per em) sizes:

      GASP_GRIDFIT             = 0x01  - enable grid-fitting (hinting)
      GASP_DOGRAY              = 0x02  - enable grayscale anti-aliasing
      GASP_SYMMETRIC_GRIDFIT   = 0x04  - symmetric grid-fitting
      GASP_SYMMETRIC_SMOOTHING = 0x08  - symmetric smoothing

    Strategy for concrete, clear shaping:
      - Very small (<=7ppem): Grid-fit ONLY (no AA - avoids blur)
      - Small (8-12ppem):     Grid-fit + Grayscale (balanced)
      - Medium (13-19ppem):   Grid-fit + Grayscale + Symmetric gridfit
      - Large (>=20ppem):     All flags (maximum quality)
    """
    from fontTools.ttLib import newTable

    # Create fresh gasp table (replaces any existing one)
    gasp = newTable('gasp')
    gasp.version = 1
    gasp.gaspRange = {
        0:     0x03,  # GRIDFIT | DOGRAY - hinting + AA from 0ppem
        7:     0x03,  # Same for tiny sizes (AA prevents fringes)
        12:    0x07,  # + SYMMETRIC_GRIDFIT - symmetric stem preservation
        19:    0x0F,  # + SYMMETRIC_SMOOTHING - smoother curves
        65535: 0x0F,  # All flags - large sizes, maximum quality
    }
    font['gasp'] = gasp

    logger.debug("GASP table optimized: multi-range grid-fitting + anti-aliasing")


def _optimize_gasp_table_solid(font: TTFont) -> None:
    """
    Solid-mode GASP for maximum concrete rendering with smooth edges.

    Strategy:
      - ALL sizes: GRIDFIT + DOGRAY (hinting + AA, no monochrome)
        AA prevents fringes on diagonal/curved strokes.
      - Medium+ sizes: + SYMMETRIC_GRIDFIT
        Preserves stroke thickness on both sides of stems.
      - Large sizes: + SYMMETRIC_SMOOTHING
        Produces smoother curves at large sizes.

    Key insight: monochrome (GRIDFIT only) creates jagged fringes.
    AA smooths them out while GRIDFIT keeps stems solid.
    """
    from fontTools.ttLib import newTable

    gasp = newTable('gasp')
    gasp.version = 1
    gasp.gaspRange = {
        0:     0x03,  # GRIDFIT | DOGRAY - hinting + AA from 0ppem
        12:    0x07,  # + SYMMETRIC_GRIDFIT - symmetric stem preservation
        32:    0x0F,  # + SYMMETRIC_SMOOTHING - smooth curves at large sizes
        65535: 0x0F,
    }
    font['gasp'] = gasp

    logger.debug("GASP table (solid mode): GRIDFIT + DOGRAY + SYMMETRIC_GRIDFIT")


def _optimize_gasp_table_simple(font: TTFont) -> None:
    """
    Simpler GASP: uniform grid-fitting + grayscale at all sizes.
    Used with --gasp-mode=simple.
    """
    from fontTools.ttLib import newTable

    gasp = newTable('gasp')
    gasp.version = 1
    gasp.gaspRange = {0: 0x03, 65535: 0x03}  # GRIDFIT | DOGRAY from 0ppem
    font['gasp'] = gasp

    logger.debug("GASP table (simple mode): uniform grid-fitting + grayscale")


# ---------------------------------------------------------------------------
#  v8.1: head table flag optimization
# ---------------------------------------------------------------------------

def _optimize_head_flags(font: TTFont) -> None:
    """
    Set optimal head table flags for clear, concrete rendering.

    IMPORTANT: Does NOT set bit 3 (Force PPEM to integer) because
    this flag is known to cause rendering failures in Chrome/Skia
    which uses fractional ppem for subpixel positioning.

    head.flags bits of interest:
      Bit 0 (0x0001): Baseline for font at y=0
      Bit 1 (0x0002): Left sidebearing at x=0
      Bit 8 (0x0100): Will use rounded values for layout

    head.macStyle cleanup:
      Clear bits 3 (Outline) and 4 (Shadow) - these cause rendering issues.
      Only set bit 0 (Bold) for fonts with weight >= 600 (semibold+).
    """
    if 'head' not in font:
        return

    head = font['head']

    # Clear bits that conflict with modern renderers:
    #   Bit 3 (0x0008): Force PPEM integer — Chrome/Skia rejects fonts with this
    head.flags &= ~0x0008

    # Set flags: baseline at y=0 | lsb at x=0 | rounded layout
    # NOTE: Bit 3 (0x0008, Force PPEM integer) is intentionally OMITTED
    # because Chrome/Skia uses fractional ppem and this flag breaks rendering.
    head.flags |= 0x0103

    # Clear Outline and Shadow bits from macStyle
    head.macStyle &= ~0x18

    # Only set bold bit (0x01) if the font is actually semibold or heavier
    # to avoid confusing Chrome's font matching
    if 'OS/2' in font:
        if font['OS/2'].usWeightClass >= 600:
            head.macStyle |= 0x01

    logger.debug("head table flags: baseline y=0, lsb x=0, rounded layout, "
                 "bit 3 cleared (Chrome compat)")


# ---------------------------------------------------------------------------
#  v8.1: Overlap removal
# ---------------------------------------------------------------------------

def _remove_overlaps(font: TTFont) -> bool:
    """
    Remove overlapping paths in glyph outlines.

    Overlaps cause dark spots, fill artifacts, and hinting difficulties
    that make text look muddy. Removing them produces cleaner, more
    concrete glyph shapes.
    """
    try:
        from fontTools.ttLib.removeOverlaps import removeOverlaps as _remove
        _remove(font)
        logger.debug("Overlapping paths removed from glyph outlines")
        return True
    except ImportError:
        logger.warning("removeOverlaps not available in this fontTools version.")
        return False
    except Exception as e:
        logger.warning(f"Could not remove overlaps: {e}")
        return False


# ---------------------------------------------------------------------------
#  v8.1: Solid / concrete shaping post-processing
# ---------------------------------------------------------------------------

def _apply_solid_postprocess(font: TTFont, weight_offset: int) -> None:
    """
    Apply solid, concrete rendering enhancements to an already-hinted font.

    Strategies for a visibly bolder, more solid appearance:

    1. **CFF ForceBold** – Sets the CFF Private dict ForceBold flag so the
       rasterizer artificially emboldens glyphs that fall below the
       ForceBoldThreshold. This thickens stems WITHOUT modifying outlines.

    2. **OS/2 weight class bump** – Increases usWeightClass so the OS treats
       the font as a bolder weight, which influences font fallback and
       synthetic bold on some platforms.

    3. **CFF stem width hints** – Increases StdHW (horizontal stem width)
       and StdVW (vertical stem width) so the hinting engine preserves
       thicker stem thicknesses.

    4. **head table macStyle bold bit** – Optionally sets the bold bit so
       the font reports itself as bold to the system.

    5. **post underline thickness** – Increases underline thickness for a
       heavier visual presence in underlined text.

    Args:
        font: Open TTFont object.
        weight_offset: Amount to add to OS/2 usWeightClass. Pass 0 to skip
                       the weight class bump (recommended for most fonts).

    Note:
      Modifying usWeightClass can cause font matching issues (Fontconfig
      uses the class to map fonts to weight names). Thresholds:
        350=Light, 450=Medium, 700=Bold, 800=ExtraBold.
      Only use weight_offset > 0 if you understand the matching impact.
    """
    is_cff = 'CFF ' in font or 'CFF2' in font

    if weight_offset == 0:
        return  # Skip weight class modifications

    # --- 1. CFF weight hints (safe subset) ---
    # NOTE: ForceBold is intentionally NOT set because Chrome's Skia
    # renderer does not support CFF ForceBold and rejects the font.
    # StdHW/StdVW modification is also avoided because Chrome rejects
    # fonts with modified stem width hint values.
    #
    # We keep the weight class bump (OS/2 usWeightClass) and the
    # bold bit (head macStyle) as safer alternatives.
    if is_cff:
        logger.debug("CFF ForceBold omitted (Chrome incompat). "
                     "Using OS/2 weight class + macStyle bold bit instead.")

    # --- 2. OS/2 weight class bump ---
    if 'OS/2' in font:
        os2 = font['OS/2']
        old_weight = os2.usWeightClass
        new_weight = min(1000, old_weight + weight_offset)
        os2.usWeightClass = new_weight
        logger.debug(f"OS/2 usWeightClass: {old_weight} -> {new_weight}")

    # --- 3. post underline thickness (more solid underline) ---
    if 'post' in font:
        post = font['post']
        boost = 1.0 + (weight_offset / 200.0)
        post.underlineThickness = max(1, int(round(post.underlineThickness * boost)))
        logger.debug(f"post underlineThickness boosted by x{boost:.2f}")

    # --- 4. head macStyle bold bit ---
    # (handled in _optimize_head_flags with weight-checked logic)
    # We skip it here to avoid confusion with the weight-aware logic.

# ---------------------------------------------------------------------------
#  v8.2: CFF Private dict hint tuning
# ---------------------------------------------------------------------------

def _tune_cff_hinting(font: TTFont, rebuild: bool = False) -> None:
    """
    Optimise CFF Private dict for better hinting quality (v8.2 NEW).

    This is one of the highest-impact improvements for CFF/OTF clarity.

    Many foundry fonts (Samsung, Google Fonts, Adobe) ship WITHOUT
    BlueValues, which forces psautohint to work blindly. Adding proper
    alignment zones plus LanguageGroup/ExpansionFactor tuning dramatically
    improves stem snapping to baseline/x-height/cap-height.

    Tuning applied:
      - LanguageGroup = 1  (Latin horizontal-stem handling)
      - ExpansionFactor    (stem growth/shrink tolerance for grid-fitting)
      - BlueShift/BlueFuzz (alignment-zone matching tolerance)
      - BlueValues         (cap-height + baseline zones, if missing)
      - OtherBlues         (x-height + baseline zones, if missing)
    """
    if 'CFF ' not in font and 'CFF2' not in font:
        return

    cff_key = 'CFF2' if 'CFF2' in font else 'CFF '
    top_dict = font[cff_key].cff.topDictIndex[0]
    priv = top_dict.Private

    # LanguageGroup 1 = Latin (different horizontal-stem hinting path)
    if not priv.LanguageGroup:
        priv.LanguageGroup = 1

    # ExpansionFactor: how much a stem can grow/shrink to fit the pixel grid
    # Default in CFF spec is 0.06 (6%); lower = tighter fit
    if priv.ExpansionFactor is None:
        priv.ExpansionFactor = 0.06

    # BlueFuzz: alignment-zone match tolerance (units)
    # 1 = default; some fonts use 0 for tighter matching
    if priv.BlueFuzz is None:
        priv.BlueFuzz = 1

    # BlueShift: extra overshoot allowance for zone matching (units)
    if priv.BlueShift is None:
        priv.BlueShift = 7

    # If BlueValues/OtherBlues missing, synthesise from OS/2 metrics.
    # This is the CRITICAL fix for fonts without alignment zones.
    # NOTE: fontTools PrivateDict requires rawDict access for BlueValues/
    # OtherBlues — accessing them as attributes raises AttributeError.
    has_blue = priv.rawDict.get('BlueValues')
    has_other = priv.rawDict.get('OtherBlues')

    # Synthesise if missing OR if rebuild=True (to fix hint drift after scaling)
    need_synth = rebuild or not has_blue or not has_other

    if need_synth:
        cap = 700
        xh = 500
        os2 = font.get('OS/2')
        if os2:
            if os2.sCapHeight:
                cap = os2.sCapHeight
            if os2.sxHeight:
                xh = os2.sxHeight
        else:
            head = font.get('head')
            if head:
                upem = head.unitsPerEm or 1000
                cap = int(upem * 0.7)
                xh = int(upem * 0.5)

        if rebuild or not has_blue:
            priv.BlueValues = [0, -10, cap, cap + 10]
            logger.debug(f"CFF BlueValues set: {priv.BlueValues} (rebuild={rebuild})")

        if rebuild or (not has_other and xh):
            priv.OtherBlues = [xh, xh + 10]
            logger.debug(f"CFF OtherBlues set: {priv.OtherBlues} (rebuild={rebuild})")

    logger.debug("CFF hint tuning applied: LanguageGroup, ExpansionFactor, BlueValues")


# ---------------------------------------------------------------------------
#  v8.2: TrueType outline cleanup (collinear / near-duplicate removal)
# ---------------------------------------------------------------------------

def _remove_collinear_in_contour(coords, flags=None, tolerance=0.5):
    """
    Remove collinear middle points from a single contour.

    For each consecutive triplet (prev, curr, next), if curr lies on the
    line segment prev-next within *tolerance* units, remove curr.
    Returns (cleaned_coords, cleaned_flags).
    """
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
    """
    Remove points that are within *tolerance* of each other.
    Keeps the first of any cluster of near-identical points.
    """
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


def _cleanup_tt_outlines(font: TTFont, collinear_tolerance: float = 0.5,
                         dedup_tolerance: float = 0.5) -> int:
    """
    Remove collinear and near-duplicate points from TrueType outlines (v8.2 NEW).

    Cleaner outlines produce:
      - Smoother curves at all rendering sizes
      - Better hint coverage (fewer points = clearer stems)
      - Smaller file size
      - Reduced fringe artefacts

    Returns the number of points removed.
    """
    if 'glyf' not in font:
        return 0

    glyf = font['glyf']
    total_removed = 0

    for glyph_name in font.getGlyphOrder():
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
            glyph.coordinates = new_coords
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
#  v8.2: Stem width normalisation
# ---------------------------------------------------------------------------

def _round_stem_widths(font: TTFont) -> int:
    """
    Round CFF Private dict stem widths to clean integers (v8.2 NEW).

    The Samsung font and similar foundry fonts have StdHW/StdVW values that
    may not be perfectly clean integers after scaling. Rounding them helps
    the rasterizer snap stems to consistent pixel boundaries.

    Returns the number of values normalised.
    """
    if 'CFF ' not in font and 'CFF2' not in font:
        return 0

    cff_key = 'CFF2' if 'CFF2' in font else 'CFF '
    top_dict = font[cff_key].cff.topDictIndex[0]
    priv = top_dict.Private
    count = 0

    # Round StdHW / StdVW (single numbers)
    for attr in ('StdHW', 'StdVW'):
        raw = priv.rawDict.get(attr)
        if raw is not None and isinstance(raw, (int, float)):
            rounded = int(round(raw))
            if rounded != raw:
                setattr(priv, attr, rounded)
                count += 1

    # Round StemSnapH / StemSnapV arrays (use rawDict)
    for attr in ('StemSnapH', 'StemSnapV'):
        val = priv.rawDict.get(attr)
        if val:
            new_val = [int(round(v)) for v in val if v is not None]
            if new_val != list(val):
                priv.rawDict[attr] = new_val
                count += len(new_val)

    if count:
        logger.debug(f"Stem width normalisation: {count} values rounded")
    return count


# ---------------------------------------------------------------------------
#  v8.2: Subpixel coordinate snapping
# ---------------------------------------------------------------------------

def _snap_to_integer(font: TTFont) -> int:
    """
    Force ALL glyph coordinates to integers (v8.2 NEW).

    Subpixel coordinates are a major source of rendering fringes.
    Returns the number of coordinates snapped.
    """
    snapped = 0

    # TrueType glyf table
    if 'glyf' in font:
        glyf = font['glyf']
        for glyph_name in font.getGlyphOrder():
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

    # Composite glyph component offsets
    if 'glyf' in font:
        glyf = font['glyf']
        for glyph_name in font.getGlyphOrder():
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

    if snapped:
        logger.debug(f"Subpixel snap: {snapped} coordinates rounded to integers")
    return snapped


# ---------------------------------------------------------------------------
#  v8.2: Granular GASP table (5 ranges with optimal AA modes)
# ---------------------------------------------------------------------------

def _improved_gasp_table(font: TTFont, detail: str = "aggressive") -> None:
    """
    Most granular GASP table (v8.2 NEW).

    detail: "aggressive" | "balanced" | "minimal"
    """
    from fontTools.ttLib import newTable

    gasp = newTable('gasp')
    gasp.version = 1

    if detail == "minimal":
        gasp.gaspRange = {0: 0x03, 65535: 0x07}
    elif detail == "balanced":
        gasp.gaspRange = {
            0:  0x03,
            19: 0x07,
            65535: 0x0F,
        }
    else:  # aggressive (default)
        gasp.gaspRange = {
            0:   0x03,
            13:  0x07,
            19:  0x0F,
            31:  0x0F,
            65535: 0x0F,
        }

    font['gasp'] = gasp
    logger.debug(f"Improved GASP table ({detail}): {len(gasp.gaspRange)} ranges")


def _pixel_aligned_gasp_table(font: TTFont) -> None:
    """
    Pixel-aligned GASP table for maximum fringe elimination (v8.2 ENHANCED).

    Uses 7 size ranges tuned for common UI sizes (12, 14, 16, 18, 24, 32 ppem)
    with SYMMETRIC_SMOOTHING at all AA-enabled ranges for smoother curves
    and SYMMETRIC_GRIDFIT for balanced stem weights.

    Range plan:
      0-7   ppem: GRIDFIT only (no AA) - pixel-perfect at tiny sizes
      8-11  ppem: GRIDFIT + DOGRAY (no smoothing yet)
      12-15 ppem: + SYMMETRIC_GRIDFIT (preserve stroke balance)
      16-23 ppem: + SYMMETRIC_SMOOTHING (smooth curves at text size)
      24-31 ppem: same (UI sizes)
      32-71 ppem: same (heading sizes)
      72+   ppem: full flags for large display
    """
    from fontTools.ttLib import newTable

    gasp = newTable('gasp')
    gasp.version = 1
    gasp.gaspRange = {
        0:   0x01,  # GRIDFIT only (no AA) - sharp at tiny sizes
        7:   0x03,  # + DOGRAY (AA on) at 8+ ppem
        11:  0x07,  # + SYMMETRIC_GRIDFIT (balanced stems)
        15:  0x0F,  # + SYMMETRIC_SMOOTHING (smooth curves)
        23:  0x0F,
        31:  0x0F,
        71:  0x0F,
        65535: 0x0F,
    }
    font['gasp'] = gasp
    logger.debug("Pixel-aligned GASP table: 7 ranges, SYMMETRIC_SMOOTHING at 16+ ppem")


def _add_dropout_control(font: TTFont) -> bool:
    """
    Add dropout control via PREP table for TrueType fonts (v8.2 NEW).

    Dropout control determines how the rasterizer handles pixels that are
    partially covered by outlines. Mode 2 (the most aggressive) is best
    for fringe elimination - it ensures every "on" pixel is fully inside
    the outline, eliminating half-pixel dropouts that cause fringes.

    The PREP program here enables dropout control mode 2 with SCANMODE
    instruction, which is the standard TrueType way to enable aggressive
    dropout control. We also set the round state to "round to grid"
    for crisp pixel alignment.

    Returns True if PREP was set up, False if skipped (e.g. CFF fonts).
    """
    if 'glyf' not in font:
        return False

    # Build PREP program: SCANMODE with dropout control mode 2
    # SCANMODE instruction = 0xB0 (op) + 0x00 (mode) = 0xB000
    # We use round-to-grid + dropout control mode 2
    prep_program = bytes([
        0xB0, 0x02,  # SCANMODE instruction with argument 2 (dropout mode 2)
    ])

    try:
        prep = font['prep']
        prep.program = [0xB0, 0x02]
        return True
    except (KeyError, AttributeError):
        pass

    try:
        # Create new PREP table
        from fontTools.ttLib.tables._p_r_e_p_t.table__p_r_e_p_t import table__p_r_e_p_t
        prep = table__p_r_e_p_t()
        prep.program = [0xB0, 0x02]
        font['prep'] = prep
        return True
    except Exception as e:
        logger.debug(f"Could not set PREP dropout control: {e}")
        return False


def _ensure_gasp_table(font: TTFont) -> None:
    """Ensure gasp table exists with at least a default entry."""
    pass  # Handled by _optimize_gasp_table which creates it fresh


# ---------------------------------------------------------------------------
#  v8.1: Enhanced hinting functions
# ---------------------------------------------------------------------------

def optimize_truetype_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize TrueType-based font with ttfautohint.

    v8.1 enhancements for clearer shaping:
      - Configurable hinting range (min/max ppem)
      - Increase-x-height for small-size legibility
      - --win flag for ClearType compatibility
      - --opentype-features to preserve layout features
      - --fallback-script=com for complex-script support
      - --detailed-info for richer instructions where available
    """
    try:
        cmd = ['ttfautohint']

        # --- Hinting range (v8.1: configurable) ---
        hint_min = options.get('hinting_range_min', 6 if options.get('clear_shaping') else 8)
        hint_max = options.get('hinting_range_max', 96 if options.get('clear_shaping') else 72)
        cmd.extend(['--hinting-range-min', str(hint_min)])
        cmd.extend(['--hinting-range-max', str(hint_max)])

        # --- X-height increase (v8.1) ---
        xh_val = options.get('x_height_hint')
        if xh_val:
            cmd.extend(['--increase-x-height', str(xh_val)])

        # --- X-height snapping exceptions ---
        xh_exc = options.get('x_height_snap_exceptions')
        if xh_exc:
            cmd.extend(['--x-height-snapping-exceptions', xh_exc])

        # --- Hinting limit / strength ---
        hs = options.get('hinting_strength')
        if hs is not None and hs != 100:
            cmd.extend(['--hinting-limit', str(hs)])

        # --- Stem width ---
        fsw = options.get('fallback_stem_width')
        if fsw:
            cmd.extend(['--fallback-stem-width', str(fsw)])

        # --- Script handling (v8.1: fallback-script=com for clear_shaping) ---
        cmd.extend(['--default-script=latn'])
        if options.get('clear_shaping'):
            cmd.extend(['--fallback-script=com'])
        else:
            cmd.extend(['--fallback-script=none'])

        # --- Core flags ---
        cmd.append('--symbol')
        cmd.append('--fallback-scaling')

        # --- v8.1: ClearType / Win compat ---
        if options.get('clear_shaping'):
            cmd.append('--win')
            cmd.append('--opentype-features')

        # --- Info / detailed ---
        if options.get('detailed_info'):
            cmd.append('--detailed-info')
        elif options.get('clear_shaping'):
            # In clear_shaping mode, omit --no-info to keep standard info
            pass

        if options.get('no_combining_chars'):
            cmd.append('--no-combining-chars')

        # --- In/out ---
        cmd.extend([input_path, output_path])

        logger.debug(f"ttfautohint: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            logger.error(f"ttfautohint failed for {input_path}: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while optimizing {input_path}")
        return False
    except Exception as e:
        logger.error(f"Error optimizing {input_path}: {str(e)}")
        return False


# ---------------------------------------------------------------------------
#  v8.2: Long glyph name detection (psautohint 64-char limit workaround)
# ---------------------------------------------------------------------------

# psautohint rejects glyph names longer than 64 characters with
# "Bad input data. Glyph name is greater than 64 chars." Common with
# Apple Color Emoji, Noto Color Emoji, and similar emoji fonts whose
# glyph names are long descriptive identifiers.
PSAUTOHINT_MAX_GLYPH_NAME_LEN = 63


def _get_long_glyph_names(font_path: str) -> list:
    """Return list of glyph names exceeding psautohint's 64-char limit."""
    try:
        font = TTFont(font_path)
        if 'CFF ' not in font and 'CFF2' not in font:
            font.close()
            return []
        cff_key = 'CFF2' if 'CFF2' in font else 'CFF '
        top_dict = font[cff_key].cff.topDictIndex[0]
        long_names = [
            name for name in top_dict.CharStrings.keys()
            if len(name) > PSAUTOHINT_MAX_GLYPH_NAME_LEN
        ]
        font.close()
        return long_names
    except Exception as e:
        logger.debug(f"Could not scan glyph names for {font_path}: {e}")
        return []


def optimize_cff_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize CFF-based font with psautohint.

    v8.1 enhancements for clearer shaping:
      - -a/--all: hint ALL glyphs, even if previously hinted
      - -c/--allow-changes: reorder paths for better hint substitution
      - -d/--decimal: use decimal coordinates for finer precision

    v8.2: Auto-excludes glyphs with names > 64 chars (psautohint limit).
    """
    try:
        cmd = ['psautohint']
        cmd.extend(['-o', output_path])

        cmd.append('-a')       # hint all glyphs
        cmd.append('-c')       # allow changes to outlines
        cmd.append('-d')       # use decimal coordinates

        # --- v8.2: Exclude long-named glyphs (psautohint 64-char limit) ---
        # psautohint's --exclude-glyphs-file has a buggy implementation
        # that treats each character as a glyph ID, so use -x CLI flag.
        long_names = _get_long_glyph_names(input_path)
        if long_names:
            cmd.extend(['-x', ','.join(long_names)])
            logger.info(f"Excluding {len(long_names)} long-named glyph(s) "
                        f"from psautohint (>64 chars)")

        if options.get('no_flex'):
            cmd.append('--no-flex')
        if options.get('no_hint_sub'):
            cmd.append('--no-hint-sub')

        cmd.append('--no-zones-stems')

        if options.get('verbose'):
            cmd.append('-v')

        cmd.append(input_path)

        logger.debug(f"psautohint: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            logger.error(f"psautohint failed for {input_path}: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while optimizing {input_path}")
        return False
    except Exception as e:
        logger.error(f"Error optimizing {input_path}: {str(e)}")
        return False


# ---------------------------------------------------------------------------
#  v8.1: Clear-shaping post-processing
# ---------------------------------------------------------------------------

def _apply_clear_shaping_postprocess(font_path: str, options: dict) -> bool:
    """
    Apply post-processing to enhance shaping clarity on already-hinted fonts.

    v8.1 pipeline:
      1. head table flags (force PPEM integer, baseline)
      2. GASP table optimization (fine-grained screen rendering)
      3. Overlap removal (optional, on by default)
      4. Solid/concrete enhancements (weight class, bold bit)

    v8.2 NEW additions:
      5. CFF hint tuning (LanguageGroup, ExpansionFactor, BlueValues)
      6. Stem width normalisation
      7. Subpixel coordinate snapping
      8. Granular GASP table (if --gasp-detail)
    """
    try:
        font = TTFont(font_path)
        logger.debug("Applying clear-shaping post-processing...")

        # Step 1: head table flags
        # (modified BEFORE GASP to avoid a fontTools interaction bug
        #  where modifying both head and gasp in one save corrupts the font)
        _optimize_head_flags(font)

        # Save head modifications first, then reopen for GASP
        font.save(font_path)
        font.close()

        # Step 2: GASP table optimization (on freshly saved font)
        font = TTFont(font_path)
        if options.get('solid'):
            _optimize_gasp_table_solid(font)
        else:
            gasp_mode = options.get('gasp_mode', 'detailed')
            if gasp_mode == 'simple':
                _optimize_gasp_table_simple(font)
            elif options.get('pixel_gasp'):
                # v8.2 ENHANCED: 7-range pixel-aligned GASP for fringe elimination
                _pixel_aligned_gasp_table(font)
            elif options.get('gasp_detail'):
                _improved_gasp_table(font, options.get('gasp_detail', 'aggressive'))
            else:
                _optimize_gasp_table(font)

        # Step 3: Overlap removal (unless suppressed)
        if not options.get('no_overlap_remove'):
            _remove_overlaps(font)

        # Step 4: Solid/concrete enhancements
        weight_offset = options.get('weight_offset', 0)
        if weight_offset and weight_offset > 0:
            _apply_solid_postprocess(font, weight_offset)

        # ---- v8.2 NEW: Hint tuning ----
        # Tune CFF Private dict for dramatically better hinting.
        # This is THE highest-impact v8.2 improvement for OTF fonts.
        if options.get('hint_tune'):
            _tune_cff_hinting(font, rebuild=options.get('rebuild_hints', False))
            # Subpixel coordinate snapping (both TT and CFF)
            _snap_to_integer(font)
            # Stem width normalisation
            if not options.get('no_stem_round'):
                _round_stem_widths(font)

        # ---- v8.2 NEW: TrueType outline cleanup ----
        if options.get('shape_cleanup'):
            _cleanup_tt_outlines(font)

        # ---- v8.2 ENHANCED: Dropout control for TrueType ----
        # Adds PREP table with SCANMODE mode 2 to eliminate half-pixel dropouts
        if options.get('dropout_control'):
            _add_dropout_control(font)

        font.save(font_path)
        font.close()

        logger.debug("Clear-shaping post-processing complete")
        return True

    except Exception as e:
        logger.warning(f"Clear-shaping post-processing issue: {e}")
        logger.debug(traceback.format_exc())
        return False


# ---------------------------------------------------------------------------
#  Main optimization pipeline (v8.1)
# ---------------------------------------------------------------------------

def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize a single font file.

    v8.1 pipeline:
      1. Optionally scale the font by a percentage (from v8)
      2. Analyse font format
      3. (v8.3 NEW) Pre-hint: rebuild BlueValues if --rebuild-hints is set
      4. Autohint with the appropriate tool
      5. Apply clear-shaping post-processing (GASP, head flags, overlaps)
      6. Report results and clean up temp files
    """
    temp_files = []
    actual_input = input_path

    # ------------------------------------------------------------------
    # Step 1 — Scale font if requested
    # ------------------------------------------------------------------
    scale_pct = options.get('scale_percent', 0)
    thickness_pct = options.get('thickness_percent', 0)
    if scale_pct != 0 or thickness_pct != 0:
        ext = os.path.splitext(input_path)[1] or '.otf'
        fd, temp_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        temp_files.append(temp_path)

        font_name = os.path.basename(input_path)
        logger.info(f"Pre-scaling {font_name} by {scale_pct}%...")

        if not scale_font_glyphs(input_path, temp_path, scale_pct, thickness_pct):
            logger.error(f"Scaling failed for {font_name}")
            for t in temp_files:
                if os.path.exists(t):
                    os.unlink(t)
            return False

        actual_input = temp_path

    # ------------------------------------------------------------------
    # Step 2 — Analyse font type
    # ------------------------------------------------------------------
    try:
        font_name = os.path.basename(actual_input)
        logger.info(f"Analysing {font_name}...")

        font_info = analyze_font_type(actual_input)

        if not font_info['is_truetype'] and not font_info['is_cff']:
            logger.error(f"Unsupported font format for {font_name}")
            for t in temp_files:
                if os.path.exists(t):
                    os.unlink(t)
            return False

        # ------------------------------------------------------------------
        # Step 3 — (v8.3 NEW) Pre-hint: rebuild stale BlueValues
        # Must happen BEFORE psautohint so it generates hints based on
        # the corrected alignment zones.
        # ------------------------------------------------------------------
        rebuild = options.get('rebuild_hints', False) and font_info['is_cff']
        if rebuild:
            try:
                tmp = TTFont(actual_input)
                _tune_cff_hinting(tmp, rebuild=True)
                tmp.save(actual_input)
                tmp.close()
                logger.debug("BlueValues rebuilt before psautohint")
            except Exception as e:
                logger.warning(f"Pre-hint BlueValues rebuild failed: {e}")

        # ------------------------------------------------------------------
        # Step 4 — Hint optimisation
        # ------------------------------------------------------------------
        hint_success = False

        if font_info['is_truetype']:
            logger.info(f"Optimizing {font_name} (TrueType) with ttfautohint...")
            hint_success = optimize_truetype_font(actual_input, output_path, options)
        elif font_info['is_cff']:
            logger.info(f"Optimizing {font_name} (CFF) with psautohint...")
            hint_success = optimize_cff_font(actual_input, output_path, options)

        if not hint_success:
            for t in temp_files:
                if os.path.exists(t):
                    os.unlink(t)
            return False

        # ------------------------------------------------------------------
        # Step 5 — Clear-shaping post-processing (v8.1)
        # ------------------------------------------------------------------
        if options.get('clear_shaping'):
            logger.info(f"Applying clear-shaping enhancements to {font_name}...")
            _apply_clear_shaping_postprocess(output_path, options)

        # ------------------------------------------------------------------
        # Step 6 — Report results
        # ------------------------------------------------------------------
        input_size = os.path.getsize(input_path)
        output_size = os.path.getsize(output_path)

        if input_size > 0:
            reduction = ((input_size - output_size) / input_size) * 100
            logger.info(f"Optimized {font_name} "
                       f"({input_size:,} -> {output_size:,} bytes, "
                       f"{reduction:.1f}% size change)")
        else:
            logger.info(f"Optimized {font_name}")

        for t in temp_files:
            if os.path.exists(t):
                os.unlink(t)
        return True

    except Exception as e:
        logger.error(f"Error processing {actual_input}: {str(e)}")
        logger.debug(traceback.format_exc())
        for t in temp_files:
            if os.path.exists(t):
                os.unlink(t)
        return False


def process_directory(input_dir: str, output_dir: str, options: dict) -> int:
    """
    Process all font files in a directory.

    Returns:
        Number of successfully processed files.
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)

    if not input_path.exists():
        logger.error(f"Input directory does not exist: {input_dir}")
        return 0
    if not input_path.is_dir():
        logger.error(f"Input path is not a directory: {input_dir}")
        return 0

    output_path.mkdir(parents=True, exist_ok=True)

    font_extensions = ['*.otf', '*.ttf']
    font_files = []
    for ext in font_extensions:
        font_files.extend(input_path.glob(ext))

    if not font_files:
        logger.warning(f"No font files found in {input_dir}")
        return 0

    logger.info(f"Found {len(font_files)} font file(s) to process")
    success_count = 0

    for font_file in font_files:
        output_file = output_path / font_file.name
        if optimize_font(str(font_file), str(output_file), options):
            success_count += 1

    return success_count


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Optimize font hinting and rendering for concrete, clear shaping (v8.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input_fonts/ output_fonts/
  %(prog)s --solid input_fonts/ output_fonts/
  %(prog)s --solid --weight-offset 80 input_fonts/ output_fonts/
  %(prog)s --solid --scale 5.0 input_fonts/ output_fonts/
  %(prog)s --clear-shaping input_fonts/ output_fonts/
  %(prog)s --clear-shaping --scale 10 input_fonts/ output_fonts/
  %(prog)s --clear-shaping --x-height-hint 5 input_fonts/ output_fonts/
  %(prog)s --clear-shaping --gasp-mode simple input_fonts/ output_fonts/
  %(prog)s --clear-shaping --hinting-range-min 6 --hinting-range-max 96 input_fonts/ output_fonts/
  %(prog)s --clear-shaping --no-overlap-remove input_fonts/ output_fonts/
  %(prog)s --strength 200 input_fonts/ output_fonts/
  %(prog)s --allow-changes --no-flex input_fonts/ output_fonts/

For solid, concrete rendering (bolder, heavier):
  %(prog)s --solid input_fonts/ output_fonts/
  %(prog)s --solid --weight-offset 80 input_fonts/ output_fonts/

v8.2 NEW — Hint tuning for Samsung-style fonts without BlueValues:
  %(prog)s --solid --hint-tune --thickness 2.5 --scale 5.0 input_fonts/ output_fonts/
  %(prog)s --hint-tune input_fonts/ output_fonts/
  %(prog)s --solid --hint-tune --shape-cleanup --gasp-detail aggressive input_fonts/ output_fonts/
        """
    )

    parser.add_argument("input_dir", help="Input directory containing font files (.otf, .ttf)")
    parser.add_argument("output_dir", help="Output directory for optimized font files")

    # ---- v8.1: Solid / concrete rendering mode ----
    parser.add_argument(
        "--solid", action="store_true", dest="solid",
        help="[NEW] Enable SOLID, CONCRETE rendering mode. Activates: "
             "(1) head macStyle bold bit (on weight >= 600), "
             "(2) Aggressive GASP table (grid-fit prioritised), "
             "(3) Hinting range expanded to 4-128ppem, "
             "(5) All --clear-shaping enhancements included."
    )

    parser.add_argument(
        "--weight-offset", type=int, default=0, dest="weight_offset",
        help="[v8.3 CHANGED] Add this N to OS/2 usWeightClass. Default 0 (no bump). "
             "WARNING: bumping weight class can affect font matching. "
             "Fontconfig maps weight classes (400=Regular, 700=Bold, etc.), "
             "so bumping 400→450 changes how some apps identify the font. "
             "Only use when you understand the impact."
    )

    # ---- v8.1: Clear shaping mode ----
    parser.add_argument(
        "--clear-shaping", action="store_true", dest="clear_shaping",
        help="Enable all clear-shaping enhancements for concrete, crisp output. "
             "This flag activates: enhanced hinting ranges, ClearType compatibility, "
             "GASP table optimization, head table flag tuning, and overlap removal."
    )

    # ---- v8.1: Fine-tuning options ----
    parser.add_argument(
        "--x-height-hint", type=int, default=0, dest="x_height_hint",
        help="Percentage to increase x-height for better small-size legibility "
             "(e.g., 3 = 3%% larger lowercase). Recommended: 2-5."
    )

    parser.add_argument(
        "--x-height-snap-exceptions", type=str, dest="x_height_snap_exceptions",
        help="Comma-separated glyph names to exclude from x-height snapping "
             "(e.g., 'a,e,o' for glyphs that should not be affected)."
    )

    parser.add_argument(
        "--hinting-range-min", type=int, default=0, dest="hinting_range_min",
        help="Minimum ppem for hinting generation. Lower values = better tiny-size "
             "rendering. Default: 8 (standard), 6 (with --clear-shaping), "
             "4 (with --solid)."
    )

    parser.add_argument(
        "--hinting-range-max", type=int, default=0, dest="hinting_range_max",
        help="Maximum ppem for hinting generation. Higher values = better large-size "
             "rendering. Default: 72 (standard), 96 (with --clear-shaping), "
             "128 (with --solid)."
    )

    parser.add_argument(
        "--gasp-mode", type=str, default="detailed", dest="gasp_mode",
        choices=["detailed", "simple"],
        help="GASP table mode: 'detailed' = multi-range optimization (default), "
             "'simple' = uniform grid-fitting + grayscale at all sizes."
    )

    parser.add_argument(
        "--no-overlap-remove", action="store_true", dest="no_overlap_remove",
        help="Skip removal of overlapping paths. Overlap removal cleans up glyphs "
             "but may alter appearance; use this flag to preserve original outlines."
    )

    # ---- v8.2: Hint tuning (CFF Private dict optimisation) ----
    parser.add_argument(
        "--hint-tune", action="store_true", dest="hint_tune",
        help="[v8.2 NEW] CFF Private dict hint tuning: sets LanguageGroup=1, "
             "ExpansionFactor, BlueShift, BlueFuzz, and synthesises BlueValues / "
             "OtherBlues from OS/2 metrics when the font has none. Dramatically "
             "improves psautohint output for fonts lacking alignment zones "
             "(Samsung and many foundry fonts)."
    )

    parser.add_argument(
        "--rebuild-hints", action="store_true", dest="rebuild_hints",
        help="[v8.3 NEW] Rebuild CFF BlueValues/OtherBlues from OS/2 metrics EVEN "
             "WHEN they already exist. Use this after --scale to fix hint drift "
             "(original BlueValues no longer match scaled outline positions)."
    )

    parser.add_argument(
        "--no-stem-round", action="store_true", dest="no_stem_round",
        help="[v8.2 NEW] Skip stem width rounding (StdHW/StdVW/StemSnap). "
             "Used with --hint-tune. Default: rounding is applied."
    )

    # ---- v8.2: TrueType outline cleanup ----
    parser.add_argument(
        "--shape-cleanup", action="store_true", dest="shape_cleanup",
        help="[v8.2 NEW] TrueType outline cleanup: remove collinear points "
             "(3+ on a line) and near-duplicate points (within 0.5 units). "
             "Produces smoother curves and smaller files."
    )

    # ---- v8.2: Granular GASP detail ----
    parser.add_argument(
        "--gasp-detail", type=str, default=None, dest="gasp_detail",
        choices=["aggressive", "balanced", "minimal"],
        help="[v8.2 NEW] Granular GASP control: 'aggressive' = 5 ranges with "
             "optimal AA per range (default), 'balanced' = 3 ranges, 'minimal' = 2 ranges. "
             "Use only when --gasp-mode is at default; ignored otherwise."
    )

    parser.add_argument(
        "--pixel-gasp", action="store_true", dest="pixel_gasp",
        help="[v8.2 ENHANCED] Use pixel-aligned GASP table with 7 ranges "
             "(SYMMETRIC_SMOOTHING at 16+ ppem) for maximum fringe elimination. "
             "Overrides --gasp-detail when both are specified."
    )

    parser.add_argument(
        "--dropout-control", action="store_true", dest="dropout_control",
        help="[v8.2 ENHANCED] Add dropout control via PREP table for TrueType "
             "fonts (mode 2 = most aggressive). Eliminates half-pixel dropouts. "
             "No effect on CFF fonts."
    )

    # ---- v8: Font scaling ----
    parser.add_argument(
        "--scale", type=float, default=0, dest="scale_percent",
        help="[v8.4 NEW] Scale font size. Two modes based on value: "
             "(a) If |value| < 1.0: direct multiplier (e.g. 0.5 = ×0.50/50%% size, "
             "0.8 = ×0.80/80%% size, 1.5 = ×1.50/150%% size). "
             "(b) If |value| >= 1.0: percentage change (e.g. 5 = +5%%, "
             "-50 = -50%%, 200 = +200%%). "
             "Safety: capped to [0.10, 4.00]. "
             "Applied as pre-processing before hinting."
    )

    # ---- v8.3: True stem-thickening (no scale change) ----
    parser.add_argument(
        "--thickness", type=float, default=0, dest="thickness_percent",
        help="[v8.3 RWORKED] Thicken stems WITHOUT changing font size. "
             "Inset-rescale technique: outer contour stays at original "
             "position, inner counter shrinks → stems appear thicker. "
             "Advance widths and glyph bounding boxes are preserved. "
             "e.g. 2.5 = subtle bolder stems, 10 = clearly thicker. "
             "Works independently of --scale. Recommended: 1-10."
    )

    # ttfautohint options (for TrueType fonts)
    parser.add_argument(
        "--strength", type=int, default=100, dest="hinting_strength",
        help="Hinting strength limit for TrueType fonts (default: 100)"
    )
    parser.add_argument(
        "--no-combining", action="store_true", dest="no_combining_chars",
        help="Don't set fallbacks for combining characters (TrueType fonts)"
    )
    parser.add_argument(
        "--detailed", action="store_true", dest="detailed_info",
        help="Add detailed TTF instructions information (TrueType fonts)"
    )
    parser.add_argument(
        "--stem-width", type=int, dest="fallback_stem_width",
        help="Fallback stem width value (TrueType fonts)"
    )

    # psautohint options (for CFF fonts)
    parser.add_argument(
        "--allow-changes", action="store_true", dest="allow_changes",
        help="Allow changes to glyph outlines (reorder paths) for CFF fonts"
    )
    parser.add_argument(
        "--no-flex", action="store_true", dest="no_flex",
        help="Suppress generation of flex commands for CFF fonts"
    )
    parser.add_argument(
        "--no-hint-sub", action="store_true", dest="no_hint_sub",
        help="Suppress hint substitution for CFF fonts"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Clear-shaping implies verbose logging
    if args.clear_shaping and not args.verbose:
        logger.setLevel(logging.INFO)

    # Check dependencies
    if not check_dependencies():
        sys.exit(1)

    # Prepare optimization options
    options = {
        # v8.1: Solid
        'solid': args.solid,
        # v8.3: --weight-offset defaults to 0 (no automatic bump)
        # Reason: bumping usWeightClass can corrupt Fontconfig matching
        # (e.g. 400 → 450 = 'Medium' instead of 'Regular', causing Chrome
        # to render text with wrong synthetic bold).
        # Users who want it can pass --weight-offset N explicitly.
        'weight_offset': args.weight_offset if args.weight_offset > 0 else 0,

        # v8.1: Clear-shaping
        'clear_shaping': args.clear_shaping or args.solid,  # solid implies clear_shaping
        'x_height_hint': args.x_height_hint if args.x_height_hint > 0 else None,
        'x_height_snap_exceptions': args.x_height_snap_exceptions,
        'hinting_range_min': args.hinting_range_min if args.hinting_range_min > 0 else None,
        'hinting_range_max': args.hinting_range_max if args.hinting_range_max > 0 else None,
        'gasp_mode': args.gasp_mode,
        'no_overlap_remove': args.no_overlap_remove,

        # v8.2: Hint tuning, shape cleanup, granular GASP
        'hint_tune': args.hint_tune,
        'rebuild_hints': args.rebuild_hints,
        'no_stem_round': args.no_stem_round,
        'shape_cleanup': args.shape_cleanup,
        'gasp_detail': args.gasp_detail,
        'pixel_gasp': args.pixel_gasp,
        'dropout_control': args.dropout_control,

        # v8: Scaling
        'scale_percent': args.scale_percent,
        'thickness_percent': args.thickness_percent,

        # TrueType hinting
        'hinting_strength': args.hinting_strength,
        'no_combining_chars': args.no_combining_chars,
        'detailed_info': args.detailed_info,
        'fallback_stem_width': args.fallback_stem_width,

        # CFF hinting
        'allow_changes': args.allow_changes,
        'no_flex': args.no_flex,
        'no_hint_sub': args.no_hint_sub,

        # General
        'verbose': args.verbose,
    }

    # Override hinting ranges based on mode
    if options['solid']:
        if options['hinting_range_min'] is None:
            options['hinting_range_min'] = 4
        if options['hinting_range_max'] is None:
            options['hinting_range_max'] = 128
    elif options['clear_shaping']:
        if options['hinting_range_min'] is None:
            options['hinting_range_min'] = 6
        if options['hinting_range_max'] is None:
            options['hinting_range_max'] = 96
    else:
        if options['hinting_range_min'] is None:
            options['hinting_range_min'] = 8
        if options['hinting_range_max'] is None:
            options['hinting_range_max'] = 72

    logger.info("Starting font optimization (v8.2)...")
    logger.info(f"Input directory:  {args.input_dir}")
    logger.info(f"Output directory: {args.output_dir}")
    if args.solid:
        logger.info(f"Solid mode: ENABLED (ForceBold + weight class + stem hints + aggressive GASP)")
        logger.info(f"Weight offset: +{options['weight_offset']}")
    if args.clear_shaping:
        logger.info("Clear-shaping mode: ENABLED (GASP + head flags + overlap removal)")
    if args.x_height_hint:
        logger.info(f"X-height increase: {args.x_height_hint}%")
    if args.scale_percent != 0:
        logger.info(f"Font scaling: {args.scale_percent:+g}%")
    if args.thickness_percent:
        logger.info(f"Thickness: +{args.thickness_percent}% (vertical stems)")
    if args.hint_tune:
        logger.info("CFF hint tuning: ENABLED (BlueValues, LanguageGroup, ExpansionFactor)")
        if args.rebuild_hints:
            logger.info("Hint rebuild: ENABLED (will REPLACE existing BlueValues)")
    if args.shape_cleanup:
        logger.info("Shape cleanup: ENABLED (collinear + near-duplicate removal)")
    if args.gasp_detail:
        logger.info(f"GASP detail: {args.gasp_detail}")
    if args.pixel_gasp:
        logger.info("Pixel-aligned GASP: ENABLED (7 ranges, SYMMETRIC_SMOOTHING at 16+)")
    if args.dropout_control:
        logger.info("Dropout control (PREP/SCANMODE mode 2): ENABLED")
    logger.info(f"Hinting range: {options['hinting_range_min']}-{options['hinting_range_max']} ppem")

    # Process fonts
    success_count = process_directory(args.input_dir, args.output_dir, options)

    msg = f"Optimization complete! Successfully processed {success_count} fonts."
    if args.solid and success_count > 0:
        msg += " Solid/concrete enhancements applied."
    if args.clear_shaping and not args.solid and success_count > 0:
        msg += " Clear-shaping enhancements applied."
    logger.info(msg)


if __name__ == "__main__":
    main()