#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v11

This script optimizes fonts by:
- Autohinting TrueType-based fonts with ttfautohint
- Autohinting CFF-based OTF fonts with psautohint
- Properly identifying CFF vs TrueType outlines
- Scaling font size by a percentage (uniform scaling)
- Widening/narrowing font horizontally (non-uniform scaling)
- Maintaining OTF output format

v11 changes (vs v10):
- ADDED: --widen parameter for horizontal scaling (widen/narrow)
- ADDED: Horizontal-only scaling of outlines and metrics
- ADDED: Proper handling of CFF hint values for horizontal scaling
- ADDED: Composite glyph horizontal offset scaling
- FIXED: Better handling of vertical metrics during horizontal scaling

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
import traceback
import shutil
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
# Dependency check
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
# Font type analysis
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
        with TTFont(font_path) as font:
            # Determine file extension
            file_ext = os.path.splitext(font_path)[1].lower()

            # Check for CFF table (CFF-based fonts)
            has_cff = 'CFF ' in font or 'CFF2' in font

            # Check for glyf table (TrueType outlines)
            has_glyf = 'glyf' in font

            return {
                'is_truetype': has_glyf and not has_cff,
                'is_cff': has_cff,
                'is_otf': file_ext == '.otf',
                'is_ttf': file_ext == '.ttf'
            }
    except TTLibError as e:
        logger.error(f"Invalid font file {font_path}: {e}")
        return {
            'is_truetype': False,
            'is_cff': False,
            'is_otf': False,
            'is_ttf': False
        }
    except Exception as e:
        logger.error(f"Error analyzing font {font_path}: {e}")
        return {
            'is_truetype': False,
            'is_cff': False,
            'is_otf': False,
            'is_ttf': False
        }


# ---------------------------------------------------------------------------
# Font scaling (v10 - uniform scaling)
# ---------------------------------------------------------------------------

def _scale_truetype_glyphs(font: TTFont, factor: float) -> None:
    """
    Scale all TrueType (glyf) glyph outlines by *factor* (uniform scaling).

    Handles simple glyphs (coordinate arrays) and composite glyphs
    (component offsets). Recalculates bounding boxes after scaling.
    
    Uses high-precision scaling with optimized rounding to minimize distortion.
    
    Args:
        font: TTFont instance
        factor: Scaling factor (1.0 = no change, 1.1 = 10% larger)
    """
    glyf = font['glyf']

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]

        # number of contours: 0 = empty, positive = simple, negative = composite
        if glyph.numberOfContours == 0:
            continue

        elif glyph.numberOfContours > 0:
            # ---- Simple glyph ----
            if glyph.coordinates is None or len(glyph.coordinates) == 0:
                continue

            # High-precision scaling: scale all coordinates, then round once
            coords = [(int(round(x * factor)), int(round(y * factor))) 
                     for x, y in glyph.coordinates]
            glyph.coordinates = coords

            # Recalculate bounding box from scaled coordinates
            xs = [p[0] for p in coords]
            ys = [p[1] for p in coords]
            glyph.xMin = int(round(min(xs)))
            glyph.yMin = int(round(min(ys)))
            glyph.xMax = int(round(max(xs)))
            glyph.yMax = int(round(max(ys)))

        else:
            # ---- Composite glyph ----
            if not glyph.components:
                continue

            for comp in glyph.components:
                if comp.x is not None:
                    comp.x = int(round(comp.x * factor))
                if comp.y is not None:
                    comp.y = int(round(comp.y * factor))
                # Note: we do NOT scale the component transformation matrix
                # elements (a, b, c, d / scaleX, scaleY) because those are
                # relative to the component's own (already-scaled) outlines.

            # Scale existing bounding box for composites
            if glyph.xMin is not None:
                glyph.xMin = int(round(glyph.xMin * factor))
                glyph.yMin = int(round(glyph.yMin * factor))
                glyph.xMax = int(round(glyph.xMax * factor))
                glyph.yMax = int(round(glyph.yMax * factor))


def _scale_cff_glyphs(font: TTFont, factor: float) -> bool:
    """
    Scale all CFF charstring outlines by *factor* (uniform scaling).
    
    Uses high-precision TransformPen scaling with optimized charstring handling
    to prevent curve artifacts and maintain curve quality.

    Args:
        font: TTFont instance
        factor: Scaling factor
        
    Returns:
        True on success, False on error
    """
    # Determine which CFF table is present
    cff_table_key = 'CFF2' if 'CFF2' in font else 'CFF '
    
    if cff_table_key not in font:
        logger.error(f"CFF table {cff_table_key} not found in font")
        return False
        
    cff_table = font[cff_table_key]
    top_dict = cff_table.cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    glyph_set = font.getGlyphSet()
    hmtx = font.get('hmtx')

    new_charstrings = {}
    had_errors = False

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in char_strings:
            continue
        if glyph_name not in glyph_set:
            continue

        try:
            glyph = glyph_set[glyph_name]

            # Get scaled advance width
            if hmtx and hasattr(hmtx, 'metrics') and glyph_name in hmtx.metrics:
                scaled_width = int(round(hmtx.metrics[glyph_name][0] * factor))
            else:
                scaled_width = 0

            # High-precision scaling using TransformPen
            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, (factor, 0, 0, factor, 0, 0))
            glyph.draw(transform_pen)
            new_charstring = t2_pen.getCharString()
            
            # The new T2CharString needs a reference to the Private dict
            if hasattr(top_dict, 'Private'):
                new_charstring.private = top_dict.Private
            
            new_charstrings[glyph_name] = new_charstring

        except Exception as e:
            logger.warning(f"Could not scale CFF glyph '{glyph_name}': {e}")
            logger.debug(f"CFF glyph '{glyph_name}' error traceback:\n{traceback.format_exc()}")
            new_charstrings[glyph_name] = char_strings[glyph_name]
            had_errors = True
            continue

    # Replace charstrings in-place
    for name, cs in new_charstrings.items():
        char_strings[name] = cs

    if had_errors:
        logger.warning("Some CFF glyphs could not be scaled and were left unchanged.")
    
    return True


def _scale_cff_hint_values(font: TTFont, factor: float) -> None:
    """
    Scale CFF hinting reference values so psautohint re-aligns stems and
    blue zones to the *scaled* outlines.
    
    For uniform scaling, both horizontal and vertical hint values are scaled.
    """
    if 'CFF ' not in font:
        return  # CFF2 uses a different hinting model; skip for safety

    cff = font['CFF '].cff
    top_dict = cff.topDictIndex[0]

    # Top-dict FontBBox (global bounding box of the font)
    fb = getattr(top_dict, 'FontBBox', None)
    if fb is not None:
        try:
            top_dict.FontBBox = [int(round(v * factor)) for v in fb]
        except Exception as e:
            logger.debug(f"Could not scale CFF FontBBox: {e}")

    priv = getattr(top_dict, 'Private', None)
    if priv is None:
        return

    # Pairwise / list hint arrays — each entry is an absolute position or a
    # stem width that must be scaled by the same factor.
    list_attrs = [
        'BlueValues', 'OtherBlues', 'FamilyBlues', 'FamilyOtherBlues',
        'StemSnapH', 'StemSnapV',
    ]
    for attr in list_attrs:
        val = getattr(priv, attr, None)
        if val is not None and len(val) > 0:
            try:
                setattr(priv, attr, [int(round(v * factor)) for v in val])
            except Exception as e:
                logger.debug(f"Could not scale CFF Private.{attr}: {e}")

    # Scalar hint parameters
    for attr in ('BlueShift', 'BlueFuzz'):
        val = getattr(priv, attr, None)
        if val is not None:
            try:
                setattr(priv, attr, int(round(val * factor)))
            except Exception as e:
                logger.debug(f"Could not scale CFF Private.{attr}: {e}")


def _scale_metrics(font: TTFont, factor: float) -> None:
    """Scale all metric tables by *factor* (uniform scaling)."""

    # ---- hmtx (horizontal metrics) ----
    if 'hmtx' in font:
        hmtx = font['hmtx']
        for glyph_name in list(hmtx.metrics.keys()):
            aw, lsb = hmtx.metrics[glyph_name]
            hmtx.metrics[glyph_name] = (
                int(round(aw * factor)),
                int(round(lsb * factor))
            )

    # ---- vmtx (vertical metrics, if present) ----
    if 'vmtx' in font:
        vmtx = font['vmtx']
        for glyph_name in list(vmtx.metrics.keys()):
            ah, tsb = vmtx.metrics[glyph_name]
            vmtx.metrics[glyph_name] = (
                int(round(ah * factor)),
                int(round(tsb * factor))
            )

    # ---- hhea table ----
    if 'hhea' in font:
        hhea = font['hhea']
        hhea.ascent = int(round(hhea.ascent * factor))
        hhea.descent = int(round(hhea.descent * factor))
        hhea.lineGap = int(round(hhea.lineGap * factor))

    # ---- vhea table (if present) ----
    if 'vhea' in font:
        vhea = font['vhea']
        vhea.ascent = int(round(vhea.ascent * factor))
        vhea.descent = int(round(vhea.descent * factor))
        vhea.lineGap = int(round(vhea.lineGap * factor))

    # ---- OS/2 table ----
    if 'OS/2' in font:
        os2 = font['OS/2']
        os2.sTypoAscender = int(round(os2.sTypoAscender * factor))
        os2.sTypoDescender = int(round(os2.sTypoDescender * factor))
        os2.sTypoLineGap = int(round(os2.sTypoLineGap * factor))
        os2.usWinAscent = int(round(os2.usWinAscent * factor))
        os2.usWinDescent = int(round(os2.usWinDescent * factor))
        if hasattr(os2, 'sxHeight') and os2.sxHeight:
            os2.sxHeight = int(round(os2.sxHeight * factor))
        if hasattr(os2, 'sCapHeight') and os2.sCapHeight:
            os2.sCapHeight = int(round(os2.sCapHeight * factor))

    # ---- post table ----
    if 'post' in font:
        post = font['post']
        post.underlinePosition = int(round(post.underlinePosition * factor))
        post.underlineThickness = int(round(post.underlineThickness * factor))

    # ---- head table ----
    if 'head' in font:
        head = font['head']
        for attr in ('xMin', 'yMin', 'xMax', 'yMax'):
            val = getattr(head, attr, None)
            if val is not None:
                setattr(head, attr, int(round(val * factor)))

        if head.lowestRecPPEM:
            head.lowestRecPPEM = max(1, int(round(head.lowestRecPPEM * factor)))

        # ---- Update the modification timestamp in head ----
        import time
        from fontTools.ttLib.tables._h_e_a_d import mac_epoch_diff
        now = int(time.time())
        head.modified = now + mac_epoch_diff if hasattr(head, 'modified') else now


# ---------------------------------------------------------------------------
# Font widening/narrowing (v11 - horizontal scaling)
# ---------------------------------------------------------------------------

def _widen_truetype_glyphs(font: TTFont, factor: float) -> None:
    """
    Widen/narrow all TrueType (glyf) glyph outlines horizontally by *factor*.
    
    Factor < 1.0 = narrow, Factor > 1.0 = widen, Factor = 1.0 = no change.
    Only X coordinates and horizontal metrics are affected.
    
    Args:
        font: TTFont instance
        factor: Horizontal scaling factor
    """
    glyf = font['glyf']

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in glyf:
            continue
        glyph = glyf[glyph_name]

        if glyph.numberOfContours == 0:
            continue

        elif glyph.numberOfContours > 0:
            # ---- Simple glyph ----
            if glyph.coordinates is None or len(glyph.coordinates) == 0:
                continue

            # Scale only X coordinates
            coords = [(int(round(x * factor)), y) for x, y in glyph.coordinates]
            glyph.coordinates = coords

            # Recalculate bounding box from scaled coordinates
            xs = [p[0] for p in coords]
            ys = [p[1] for p in coords]
            glyph.xMin = int(round(min(xs)))
            glyph.yMin = int(round(min(ys)))
            glyph.xMax = int(round(max(xs)))
            glyph.yMax = int(round(max(ys)))

        else:
            # ---- Composite glyph ----
            if not glyph.components:
                continue

            for comp in glyph.components:
                # Only scale X offset
                if comp.x is not None:
                    comp.x = int(round(comp.x * factor))
                # Y offset unchanged
                # Note: we do NOT scale the component transformation matrix

            # Scale horizontal bounds only
            if glyph.xMin is not None:
                glyph.xMin = int(round(glyph.xMin * factor))
                glyph.xMax = int(round(glyph.xMax * factor))
                # yMin, yMax unchanged


def _widen_cff_glyphs(font: TTFont, factor: float) -> bool:
    """
    Widen/narrow all CFF charstring outlines horizontally by *factor*.
    
    Uses TransformPen with horizontal-only scaling matrix.
    
    Args:
        font: TTFont instance
        factor: Horizontal scaling factor
        
    Returns:
        True on success, False on error
    """
    cff_table_key = 'CFF2' if 'CFF2' in font else 'CFF '
    
    if cff_table_key not in font:
        logger.error(f"CFF table {cff_table_key} not found in font")
        return False
        
    cff_table = font[cff_table_key]
    top_dict = cff_table.cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    glyph_set = font.getGlyphSet()
    hmtx = font.get('hmtx')

    new_charstrings = {}
    had_errors = False

    # Horizontal scaling matrix: (sx, 0, 0, sy, tx, ty)
    # factor for X, 1.0 for Y
    transform_matrix = (factor, 0, 0, 1.0, 0, 0)

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in char_strings:
            continue
        if glyph_name not in glyph_set:
            continue

        try:
            glyph = glyph_set[glyph_name]

            # Get scaled advance width (horizontal only)
            if hmtx and hasattr(hmtx, 'metrics') and glyph_name in hmtx.metrics:
                scaled_width = int(round(hmtx.metrics[glyph_name][0] * factor))
            else:
                scaled_width = 0

            # Horizontal-only scaling using TransformPen
            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, transform_matrix)
            glyph.draw(transform_pen)
            new_charstring = t2_pen.getCharString()
            
            if hasattr(top_dict, 'Private'):
                new_charstring.private = top_dict.Private
            
            new_charstrings[glyph_name] = new_charstring

        except Exception as e:
            logger.warning(f"Could not widen CFF glyph '{glyph_name}': {e}")
            logger.debug(f"CFF glyph '{glyph_name}' error traceback:\n{traceback.format_exc()}")
            new_charstrings[glyph_name] = char_strings[glyph_name]
            had_errors = True
            continue

    # Replace charstrings in-place
    for name, cs in new_charstrings.items():
        char_strings[name] = cs

    if had_errors:
        logger.warning("Some CFF glyphs could not be widened and were left unchanged.")
    
    return True


def _widen_cff_hint_values(font: TTFont, factor: float) -> None:
    """
    Scale CFF horizontal hinting reference values for horizontal scaling.
    
    Only horizontal hints are scaled: StemSnapH, BlueValues (x-coordinates),
    BlueShift, BlueFuzz. Vertical hints (StemSnapV, vertical BlueValues) 
    are left unchanged.
    """
    if 'CFF ' not in font:
        return  # CFF2 uses a different hinting model; skip for safety

    cff = font['CFF '].cff
    top_dict = cff.topDictIndex[0]

    # Top-dict FontBBox - scale only horizontal components
    fb = getattr(top_dict, 'FontBBox', None)
    if fb is not None:
        try:
            # FontBBox = [xMin, yMin, xMax, yMax]
            top_dict.FontBBox = [
                int(round(fb[0] * factor)),  # xMin
                fb[1],                        # yMin (unchanged)
                int(round(fb[2] * factor)),  # xMax
                fb[3]                         # yMax (unchanged)
            ]
        except Exception as e:
            logger.debug(f"Could not scale CFF FontBBox horizontally: {e}")

    priv = getattr(top_dict, 'Private', None)
    if priv is None:
        return

    # Horizontal stem snap values - scale these
    for attr in ('StemSnapH',):
        val = getattr(priv, attr, None)
        if val is not None and len(val) > 0:
            try:
                setattr(priv, attr, [int(round(v * factor)) for v in val])
            except Exception as e:
                logger.debug(f"Could not scale CFF Private.{attr}: {e}")

    # BlueValues and related - these are Y-coordinates (vertical zones)
    # For horizontal widening, vertical alignment zones should NOT be scaled
    # But if the font has horizontal BlueValues (rare), they would need scaling
    # Standard BlueValues are vertical, so we leave them unchanged

    # BlueShift and BlueFuzz are vertical measurements - leave unchanged
    # for pure horizontal scaling


def _widen_metrics(font: TTFont, factor: float) -> None:
    """Scale horizontal metric tables by *factor*. Vertical metrics unchanged."""

    # ---- hmtx (horizontal metrics) ----
    if 'hmtx' in font:
        hmtx = font['hmtx']
        for glyph_name in list(hmtx.metrics.keys()):
            aw, lsb = hmtx.metrics[glyph_name]
            hmtx.metrics[glyph_name] = (
                int(round(aw * factor)),  # advance width scaled
                int(round(lsb * factor))  # left side bearing scaled
            )

    # ---- vmtx (vertical metrics) - UNCHANGED for horizontal scaling ----
    # Vertical metrics remain the same

    # ---- hhea table - horizontal metrics only ----
    if 'hhea' in font:
        hhea = font['hhea']
        hhea.ascent = int(round(hhea.ascent * factor))      # horizontal ascent
        hhea.descent = int(round(hhea.descent * factor))    # horizontal descent
        # lineGap is typically vertical, but in hhea it's horizontal context
        # For safety, scale it horizontally
        hhea.lineGap = int(round(hhea.lineGap * factor))

    # ---- vhea table - UNCHANGED (vertical metrics) ----

    # ---- OS/2 table - horizontal metrics only ----
    if 'OS/2' in font:
        os2 = font['OS/2']
        os2.sTypoAscender = int(round(os2.sTypoAscender * factor))
        os2.sTypoDescender = int(round(os2.sTypoDescender * factor))
        os2.sTypoLineGap = int(round(os2.sTypoLineGap * factor))
        os2.usWinAscent = int(round(os2.usWinAscent * factor))
        os2.usWinDescent = int(round(os2.usWinDescent * factor))
        # sxHeight and sCapHeight are vertical measurements - leave unchanged

    # ---- post table - horizontal metrics ----
    if 'post' in font:
        post = font['post']
        post.underlinePosition = int(round(post.underlinePosition * factor))
        post.underlineThickness = int(round(post.underlineThickness * factor))

    # ---- head table - horizontal bounds only ----
    if 'head' in font:
        head = font['head']
        for attr in ('xMin', 'xMax'):
            val = getattr(head, attr, None)
            if val is not None:
                setattr(head, attr, int(round(val * factor)))
        # yMin, yMax unchanged

        # lowestRecPPEM is in pixels (both directions) - scale for horizontal
        if head.lowestRecPPEM:
            head.lowestRecPPEM = max(1, int(round(head.lowestRecPPEM * factor)))

        # Update timestamp
        import time
        from fontTools.ttLib.tables._h_e_a_d import mac_epoch_diff
        now = int(time.time())
        head.modified = now + mac_epoch_diff if hasattr(head, 'modified') else now


def widen_font_glyphs(input_path: str, output_path: str, widen_factor: float) -> bool:
    """
    Widen/narrow ALL glyph outlines and horizontal metrics by the given factor.

    Args:
        input_path: Path to input font file (.otf / .ttf)
        output_path: Path to save the widened font
        widen_factor: Horizontal scaling factor (<1.0 = narrow, >1.0 = widen, 1.0 = no change)

    Returns:
        True on success, False on error.
    """
    if widen_factor == 1.0:
        # No-op copy
        shutil.copy2(input_path, output_path)
        logger.info(f"✓ Copied font (no widening) to {output_path}")
        return True

    logger.debug(f"Widening font by factor {widen_factor:.4f} ({'widen' if widen_factor > 1.0 else 'narrow'})")

    try:
        with TTFont(input_path) as font:
            has_truetype = 'glyf' in font
            has_cff = 'CFF ' in font or 'CFF2' in font

            # Scale glyph outlines horizontally
            if has_truetype:
                _widen_truetype_glyphs(font, widen_factor)
            elif has_cff:
                result = _widen_cff_glyphs(font, widen_factor)
                if not result:
                    return False
                # Scale CFF horizontal hint values
                _widen_cff_hint_values(font, widen_factor)
            else:
                logger.error("Font has neither TrueType nor CFF outlines — cannot widen.")
                return False

            # Scale horizontal metric tables only
            _widen_metrics(font, widen_factor)

            # Save the widened font
            font.save(output_path)

        logger.debug(f"✓ Widened font saved to {output_path}")
        return True

    except Exception as e:
        logger.error(f"Error widening font {input_path}: {str(e)}")
        logger.debug(traceback.format_exc())
        return False


def scale_font_glyphs(input_path: str, output_path: str, scale_percent: float) -> bool:
    """
    Scale ALL glyph outlines and metrics by the given percentage (uniform scaling).

    Args:
        input_path: Path to input font file (.otf / .ttf)
        output_path: Path to save the scaled font
        scale_percent: Percentage to scale (10 = make 10% larger)

    Returns:
        True on success, False on error.
    """
    if scale_percent == 0:
        shutil.copy2(input_path, output_path)
        logger.info(f"✓ Copied font (no scaling) to {output_path}")
        return True

    factor = 1.0 + scale_percent / 100.0
    logger.debug(f"Scaling font by {scale_percent}% (factor ×{factor:.4f})")

    try:
        with TTFont(input_path) as font:
            has_truetype = 'glyf' in font
            has_cff = 'CFF ' in font or 'CFF2' in font

            if has_truetype:
                _scale_truetype_glyphs(font, factor)
            elif has_cff:
                result = _scale_cff_glyphs(font, factor)
                if not result:
                    return False
                _scale_cff_hint_values(font, factor)
            else:
                logger.error("Font has neither TrueType nor CFF outlines — cannot scale.")
                return False

            _scale_metrics(font, factor)
            font.save(output_path)

        logger.debug(f"✓ Scaled font saved to {output_path}")
        return True

    except Exception as e:
        logger.error(f"Error scaling font {input_path}: {str(e)}")
        logger.debug(traceback.format_exc())
        return False


# ---------------------------------------------------------------------------
# Hinting — unchanged from v10
# ---------------------------------------------------------------------------

def optimize_truetype_font(input_path: str, output_path: str, options: dict) -> bool:
    """Optimize TrueType-based font with ttfautohint"""
    try:
        cmd = ['ttfautohint']

        if options.get('hinting_strength'):
            cmd.extend(['--hinting-limit', str(options['hinting_strength'])])

        if options.get('no_combining_chars'):
            cmd.append('--no-combining-chars')

        if options.get('detailed_info'):
            cmd.append('--detailed-info')

        if options.get('fallback_stem_width'):
            cmd.extend(['--fallback-stem-width', str(options['fallback_stem_width'])])

        cmd.extend([
            '--default-script=latn',
            '--fallback-script=none',
            '--symbol',
            '--fallback-scaling',
        ])

        if options.get('verbose'):
            cmd.append('-v')

        cmd.extend([input_path, output_path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            logger.error(f"ttfautohint failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("ttfautohint timed out after 300 seconds")
        return False
    except Exception as e:
        logger.error(f"Error running ttfautohint: {e}")
        return False


def optimize_cff_font(input_path: str, output_path: str, options: dict) -> bool:
    """Optimize CFF-based font with psautohint"""
    try:
        cmd = ['psautohint', '--no-zones-stems', '-o', output_path]

        if options.get('allow_changes'):
            cmd.append('--allow-changes')

        if options.get('no_flex'):
            cmd.append('--no-flex')

        if options.get('no_hint_sub'):
            cmd.append('--no-hint-sub')

        if options.get('verbose'):
            cmd.append('-v')

        cmd.append(input_path)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            logger.error(f"psautohint failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("psautohint timed out after 300 seconds")
        return False
    except Exception as e:
        logger.error(f"Error running psautohint: {e}")
        return False


def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize a single font file with optional scaling and/or widening.

    Processing order:
    1. Scale (uniform) if --scale specified
    2. Widen (horizontal) if --widen specified
    3. Autohint

    Args:
        input_path: Path to input font
        output_path: Path to save optimized font
        options: Dictionary of options

    Returns:
        True on success, False on error
    """
    current_input = input_path
    temp_files = []

    try:
        # Step 1: Uniform scaling
        if options.get('scale', 0) != 0:
            with tempfile.NamedTemporaryFile(suffix='.otf', delete=False) as tmp:
                scaled_path = tmp.name
            temp_files.append(scaled_path)

            if not scale_font_glyphs(current_input, scaled_path, options['scale']):
                logger.error(f"Failed to scale font {current_input}")
                return False
            current_input = scaled_path

        # Step 2: Horizontal widening/narrowing
        if options.get('widen', 1.0) != 1.0:
            with tempfile.NamedTemporaryFile(suffix='.otf', delete=False) as tmp:
                widened_path = tmp.name
            temp_files.append(widened_path)

            if not widen_font_glyphs(current_input, widened_path, options['widen']):
                logger.error(f"Failed to widen font {current_input}")
                return False
            current_input = widened_path

        # Step 3: Autohint
        font_info = analyze_font_type(current_input)

        if not font_info['is_truetype'] and not font_info['is_cff']:
            logger.error(f"Cannot determine font type for {current_input}")
            return False

        if font_info['is_truetype']:
            success = optimize_truetype_font(current_input, output_path, options)
        elif font_info['is_cff']:
            success = optimize_cff_font(current_input, output_path, options)
        else:
            logger.error(f"Unknown font type for {current_input}")
            return False

        return success

    finally:
        # Clean up temp files
        for tmp_file in temp_files:
            try:
                if os.path.exists(tmp_file):
                    os.unlink(tmp_file)
            except Exception as e:
                logger.warning(f"Could not remove temp file {tmp_file}: {e}")


def process_directory(input_dir: str, output_dir: str, options: dict) -> dict:
    """
    Process all fonts in a directory recursively.

    Args:
        input_dir: Input directory path
        output_dir: Output directory path
        options: Dictionary of options

    Returns:
        Dictionary with processing stats
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    font_files = []
    for ext in ['*.otf', '*.ttf', '*.OTF', '*.TTF']:
        font_files.extend(input_path.rglob(ext))

    if not font_files:
        logger.warning(f"No font files found in {input_dir}")
        return {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}

    logger.info(f"Found {len(font_files)} font file(s) in {input_dir}")

    stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}

    for font_file in font_files:
        stats['total'] += 1

        rel_path = font_file.relative_to(input_path)
        out_file = output_path / rel_path
        out_file.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Processing {font_file}...")

        try:
            if optimize_font(str(font_file), str(out_file), options):
                stats['success'] += 1
            else:
                stats['failed'] += 1
        except Exception as e:
            logger.error(f"Error processing {font_file}: {e}")
            logger.debug(traceback.format_exc())
            stats['failed'] += 1

    logger.info(f"Directory processing complete: {stats['success']}/{stats['total']} successful")
    return stats


def main() -> None:
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Optimize OTF/TTF fonts with scaling, widening, and autohinting',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scale up 7.5% and autohint
  python otf_optimize-v11.py --scale 7.5 input.otf output.otf

  # Widen font by 20% (factor 1.2) and autohint
  python otf_optimize-v11.py --widen 1.2 input.otf output.otf

  # Narrow font by 10% (factor 0.9) and autohint
  python otf_optimize-v11.py --widen 0.9 input.otf output.otf

  # Combine scaling and widening
  python otf_optimize-v11.py --scale 5 --widen 1.15 input.otf output.otf

  # Batch process a directory with widening
  python otf_optimize-v11.py --widen 1.1 input_fonts/ output_fonts/

  # CFF-only tuning with widening
  python otf_optimize-v11.py --widen 1.1 --allow-changes --no-flex in.otf out.otf

  # Just autohint, no scaling/widening
  python otf_optimize-v11.py input.otf output.otf
        """
    )

    parser.add_argument('input', help='Input font file or directory')
    parser.add_argument('output', help='Output font file or directory')
    parser.add_argument('--scale', type=float, default=0,
                        help='Uniform scale percentage (positive = larger, negative = smaller). Default: 0')
    parser.add_argument('--widen', type=float, default=1.0,
                        help='Horizontal scaling factor (<1.0 = narrow, >1.0 = widen, 1.0 = no change). Default: 1.0')
    parser.add_argument('--strength', type=int, dest='hinting_strength',
                        help='ttfautohint --hinting-limit (hinting strength ceiling)')
    parser.add_argument('--no-combining', action='store_true', dest='no_combining_chars',
                        help='ttfautohint --no-combining-chars')
    parser.add_argument('--fallback-stem', type=int, dest='fallback_stem_width',
                        help='ttfautohint --fallback-stem-width')
    parser.add_argument('--detailed', action='store_true', dest='detailed_info',
                        help='ttfautohint --detailed-info')
    parser.add_argument('--allow-changes', action='store_true',
                        help='psautohint --allow-changes (reorder paths)')
    parser.add_argument('--no-flex', action='store_true',
                        help='psautohint --no-flex')
    parser.add_argument('--no-hint-sub', action='store_true',
                        help='psautohint --no-hint-sub')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable DEBUG-level logging')
    parser.add_argument('--check-deps', action='store_true',
                        help='Check dependencies and exit')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.check_deps:
        if check_dependencies():
            logger.info("✓ All dependencies found")
            sys.exit(0)
        else:
            logger.error("✗ Missing dependencies")
            sys.exit(1)

    if not os.path.exists(args.input):
        logger.error(f"Input path does not exist: {args.input}")
        sys.exit(1)

    # Validate widen factor
    if args.widen <= 0:
        logger.error(f"Invalid widen factor: {args.widen}. Must be > 0")
        sys.exit(1)

    options = {
        'scale': args.scale,
        'widen': args.widen,
        'hinting_strength': args.hinting_strength,
        'no_combining_chars': args.no_combining_chars,
        'fallback_stem_width': args.fallback_stem_width,
        'detailed_info': args.detailed_info,
        'allow_changes': args.allow_changes,
        'no_flex': args.no_flex,
        'no_hint_sub': args.no_hint_sub,
        'verbose': args.verbose,
    }

    if os.path.isfile(args.input):
        success = optimize_font(args.input, args.output, options)
        sys.exit(0 if success else 1)
    elif os.path.isdir(args.input):
        stats = process_directory(args.input, args.output, options)
        if stats['failed'] > 0:
            sys.exit(1)
        sys.exit(0)
    else:
        logger.error(f"Input path is neither file nor directory: {args.input}")
        sys.exit(1)


if __name__ == '__main__':
    main()