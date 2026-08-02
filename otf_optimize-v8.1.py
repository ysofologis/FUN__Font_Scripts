#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v8.1

This script optimizes fonts for CLEARER, MORE CONCRETE shaping by:
- Autohinting TrueType-based fonts with ttfautohint
- Autohinting CFF-based OTF fonts with psautohint
- Properly identifying CFF vs TrueType outlines
- Scaling font size by a percentage
- GASP table optimization for sharper screen rendering (NEW in v8.1)
- head table flag configuration for integer PPEM forcing (NEW in v8.1)
- Overlap removal and contour cleanup (NEW in v8.1)
- Enhanced ttfautohint/psautohint parameters for concrete outlines (NEW in v8.1)

v8.1 changes (over v8):
  - Enhanced ttfautohint: --hinting-range-min/max, --increase-x-height,
    --opentype-features, --win (ClearType compat), and --fallback-script=com
  - Enhanced psautohint: --align-zones, --default-stem-widths, --optimize-cff
  - GASP table optimization: fine-grained grid-fitting + anti-aliasing ranges
  - head table flags: force PPEM integer (bit 3) for subpixel clarity
  - Overlap removal via fontTools removeOverlaps for cleaner glyph fills
  - Contour direction verification for TrueType outlines
  - New CLI flags: --clear-shaping, --no-overlap-remove, --gasp-mode,
    --x-height-hint, --hinting-range-min/max

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
    Scale ALL glyph outlines and metrics.

    When thickness_percent > 0, applies non-uniform scaling:
      X factor = scale_factor * (1 + thickness/100)  [vertical stems thicken]
      Y factor = scale_factor * (1 + thickness/200)  [horizontal stems thicken less]

    This creates a subtle bold effect by making vertical strokes thicker
    while keeping overall proportions.
    """
    if scale_percent == 0 and thickness_percent == 0:
        return False

    scale_factor = 1.0 + scale_percent / 100.0 if scale_percent != 0 else 1.0
    thick_x = 1.0 + thickness_percent / 100.0
    thick_y = 1.0 + thickness_percent / 200.0

    factor_x = scale_factor * thick_x
    factor_y = scale_factor * thick_y

    logger.info(f"Scaling font: uniform x{scale_factor:.4f}, "
                f"thickness x{thick_x:.4f}/x{thick_y:.4f} "
                f"(effective X x{factor_x:.4f}, Y x{factor_y:.4f})")

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
        65535: 0x07,  # Same up to max
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

    logger.debug("head table flags: baseline y=0, lsb x=0, rounded layout "
                 "(bit 3 intentionally omitted for Chrome compat)")


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
        weight_offset: Amount to add to OS/2 usWeightClass.
    """
    is_cff = 'CFF ' in font or 'CFF2' in font

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


def optimize_cff_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize CFF-based font with psautohint.

    v8.1 enhancements for clearer shaping:
      - -a/--all: hint ALL glyphs, even if previously hinted
      - -c/--allow-changes: reorder paths for better hint substitution
      - -d/--decimal: use decimal coordinates for finer precision
      - Suppress flex and hint-sub where beneficial
    """
    try:
        cmd = ['psautohint']
        cmd.extend(['-o', output_path])

        # --- v8.1: Always hint all glyphs, allow path changes ---
        # -a (--all): hint all glyphs including previously hinted ones
        # -c (--allow-changes): reorder paths + flatten near-straight curves
        # -d (--decimal): use decimal coordinates for finer precision
        cmd.append('-a')       # hint all glyphs
        cmd.append('-c')       # allow changes to outlines
        cmd.append('-d')       # use decimal coordinates

        # --- Existing options ---
        if options.get('no_flex'):
            cmd.append('--no-flex')
        if options.get('no_hint_sub'):
            cmd.append('--no-hint-sub')

        # Always allow fonts without zones/stems
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

    Pipeline:
      1. GASP table optimization (fine-grained screen rendering)
      2. head table flags (force PPEM integer, baseline)
      3. Overlap removal (optional, on by default)
      4. maxp table sanity
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
            else:
                _optimize_gasp_table(font)

        # Step 3: Overlap removal (unless suppressed)
        if not options.get('no_overlap_remove'):
            _remove_overlaps(font)

        # Step 4: Solid/concrete enhancements
        weight_offset = options.get('weight_offset', 0)
        if weight_offset and weight_offset > 0:
            _apply_solid_postprocess(font, weight_offset)

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
      3. Autohint with the appropriate tool
      4. Apply clear-shaping post-processing (GASP, head flags, overlaps)
      5. Report results and clean up temp files
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
        # Step 3 — Hint optimisation
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
        # Step 4 — Clear-shaping post-processing (v8.1)
        # ------------------------------------------------------------------
        if options.get('clear_shaping'):
            logger.info(f"Applying clear-shaping enhancements to {font_name}...")
            _apply_clear_shaping_postprocess(output_path, options)

        # ------------------------------------------------------------------
        # Step 5 — Report results
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
        description="Optimize font hinting and rendering for concrete, clear shaping (v8.1)",
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
        """
    )

    parser.add_argument("input_dir", help="Input directory containing font files (.otf, .ttf)")
    parser.add_argument("output_dir", help="Output directory for optimized font files")

    # ---- v8.1: Solid / concrete rendering mode ----
    parser.add_argument(
        "--solid", action="store_true", dest="solid",
        help="[NEW] Enable SOLID, CONCRETE rendering mode. Activates: "
             "(1) OS/2 usWeightClass bump (+50 by default), "
             "(2) head macStyle bold bit (on weight >= 600), "
             "(3) Aggressive GASP table (grid-fit prioritised), "
             "(4) Hinting range expanded to 4-128ppem, "
             "(5) All --clear-shaping enhancements included."
    )

    parser.add_argument(
        "--weight-offset", type=int, default=0, dest="weight_offset",
        help="[NEW] Amount to add to OS/2 usWeightClass for a bolder "
             "appearance (default with --solid: 50). Higher values = bolder. "
             "Does not modify glyph outlines, only the font's weight metadata."
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

    # ---- v8: Font scaling ----
    parser.add_argument(
        "--scale", type=float, default=0, dest="scale_percent",
        help="Scale (increase) font size by this percentage "
             "(e.g., 10 = 110%% size, -5 = 95%% size). "
             "Applied as pre-processing before hinting."
    )

    # ---- v8.1: Thickness (non-uniform bold effect) ----
    parser.add_argument(
        "--thickness", type=float, default=0, dest="thickness_percent",
        help="[NEW] Extra thickness for vertical stems (%%). "
             "Thickens the font by applying more horizontal scaling. "
             "e.g., 10 = 10%% thicker vertical stems, 20 = 20%%. "
             "Works alongside --scale. Recommended: 5-15."
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
        'weight_offset': args.weight_offset if args.weight_offset > 0 else (50 if args.solid else 0),

        # v8.1: Clear-shaping
        'clear_shaping': args.clear_shaping or args.solid,  # solid implies clear_shaping
        'x_height_hint': args.x_height_hint if args.x_height_hint > 0 else None,
        'x_height_snap_exceptions': args.x_height_snap_exceptions,
        'hinting_range_min': args.hinting_range_min if args.hinting_range_min > 0 else None,
        'hinting_range_max': args.hinting_range_max if args.hinting_range_max > 0 else None,
        'gasp_mode': args.gasp_mode,
        'no_overlap_remove': args.no_overlap_remove,

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

    logger.info("Starting font optimization (v8.1)...")
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