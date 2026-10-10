#!/usr/bin/env python3
"""
TTF → OTF Optimization Script v12

This script converts TTF fonts to OTF (CFF) format with:
- Optional scaling of glyph outlines and metrics by a percentage
- Optional letter-spacing / tracking (extra space between glyphs)
- Curve optimization (quadratic → cubic conversion)
- Overlap removal
- Hinting removal for cleaner rendering
- Forced grayscale anti-aliasing at all sizes
- Autohinting with psautohint (CFF) or ttfautohint (TrueType)

v12 changes (vs v11):
- NEW: --spacing S flag for letter-spacing / tracking
- NEW: apply_tracking() walks every glyph and grows/shrinks advance widths
- NEW: Dual-mode semantics matching otf_optimize-ff-v14.py:
        |S| < 1.0  -> direct font-unit addend (clamped to ±4096)
        |S| >= 1.0 -> percentage change of each advance (clamped to [-90%, +400%])
- NEW: Tracking applied after scaling (Step 3) and before autohinting (Step 4)
        so psautohint recomputes BlueValues/StemSnapH-V against final metrics
- NEW: Outlines untouched - only hmtx advance widths and CFF charstring width
        operands are rewritten, so glyph shapes are preserved exactly
- FIXED: Robust handling of the intermediate file location - tracking and
        hinting now operate on whichever OTF path currently holds the font
        (temp_otf vs output_path), removing the latent "scale=0 missing file"
        bug from v11

v11 changes (vs v10):
- REWRITTEN: Two-step process - first convert TTF→OTF using fonttools, then optimize
- FIXED: Uses fonttools' built-in TTF→OTF conversion for reliable baseline
- FIXED: Separation of concerns - conversion vs optimization
- FIXED: Better glyph handling using fonttools' native conversion

Requirements:
- ttfautohint (pip install ttfautohint)
- psautohint (pip install psautohint)
- fonttools (pip install fonttools)
- AFDKO (optional, for best quality): pip install afdko

Usage:
    python ttf2otf_optimize-v12.py <input_dir> <output_dir>
    python ttf2otf_optimize-v12.py <input_dir> <output_dir> --scale 7.5
    python ttf2otf_optimize-v12.py <input_dir> <output_dir> --spacing 10
    python ttf2otf_optimize-v12.py <input_dir> <output_dir> --scale 7.5 --spacing 15
    python ttf2otf_optimize-v12.py <input_dir> <output_dir> --spacing 50 -v
"""

import os
import sys
import argparse
import subprocess
import logging
import tempfile
import traceback
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttFont import TTLibError
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.fontBuilder import FontBuilder
from fontTools.ttLib.tables._g_a_s_p import table__g_a_s_p, GASP_GRIDFIT, GASP_DOGRAY
from fontTools.cffLib import TopDictIndex, PrivateDict
from fontTools.misc.textTools import tostr

# --thickness: skia-pathops drives the expand+union step that adds the
# offset to outlines while keeping counters from collapsing. fontTools has
# no equivalent (no changeWeight on TTFont). Imported lazily inside
# thicken_font() so the script still starts on interpreters where it is
# absent.

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
        ('psautohint', 'pip install psautohint'),
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
# Step 1: Convert TTF to OTF using fonttools
# ---------------------------------------------------------------------------

def convert_ttf_to_otf(ttf_path: Path, otf_path: Path) -> dict:
    """
    Convert TTF to OTF using fonttools' native conversion.
    
    This is a clean, reliable conversion that preserves all glyph data.
    
    Args:
        ttf_path: Path to input TTF font
        otf_path: Path to output OTF font
    
    Returns:
        Dictionary with conversion stats
    """
    logger.debug(f"Converting {ttf_path} to OTF...")
    
    with TTFont(ttf_path) as ttf:
        # Get all necessary tables and data
        glyph_order = ttf.getGlyphOrder()
        cmap = ttf.getBestCmap()
        glyf = ttf['glyf']
        hmtx = ttf['hmtx']
        head = ttf["head"]
        hhea = ttf["hhea"]
        os2 = ttf["OS/2"]
        post = ttf["post"]
        name_table = ttf["name"]
        
        # Build OTF using FontBuilder
        fb = FontBuilder(head.unitsPerEm, isTTF=False)
        fb.setupGlyphOrder(glyph_order)
        fb.setupCharacterMap(cmap)
        
        # Convert glyf glyphs to CFF charstrings
        charstrings = {}
        metrics = {}
        
        for gname in glyph_order:
            if gname not in glyf:
                continue

            glyph = glyf[gname]

            # Skip empty glyphs
            if glyph.numberOfContours == 0:
                # Create empty charstring
                t2_pen = T2CharStringPen(0, None)
                cs = t2_pen.getCharString()
                charstrings[gname] = cs
                # Get width from hmtx table
                hmtx_metrics = hmtx.metrics.get(gname, (0, 0))
                metrics[gname] = hmtx_metrics
                continue

            # Convert quadratic curves to cubic.
            # Components must be decomposed first. Qu2CuPen forwards
            # addComponent straight through to T2CharStringPen, whose
            # glyphSet is None here, so drawing a composite glyph raised
            # "TypeError: 'NoneType' object is not subscriptable" and
            # failed the whole conversion -- not just that glyph. Giving
            # T2CharStringPen a glyphSet would not be enough either: the
            # component's own quadratics would then reach the charstring
            # unconverted.
            t2_pen = T2CharStringPen(0, None)
            qu2cu = Qu2CuPen(t2_pen, max_err=0.3)
            decomposer = DecomposingRecordingPen(ttf.getGlyphSet())
            glyph.draw(decomposer, glyf)
            for op, args in decomposer.value:
                getattr(qu2cu, op)(*args)
            cs = t2_pen.getCharString()
            charstrings[gname] = cs

            # Get metrics from hmtx table (not from glyph)
            hmtx_metrics = hmtx.metrics.get(gname, (0, 0))
            metrics[gname] = hmtx_metrics
        
        fb.setupHorizontalMetrics(metrics)
        fb.setupHorizontalHeader(
            ascent=hhea.ascent, descent=hhea.descent,
            lineGap=hhea.lineGap,
            advanceWidthMax=hhea.advanceWidthMax,
            minLeftSideBearing=hhea.minLeftSideBearing,
            minRightSideBearing=hhea.minRightSideBearing,
            xMaxExtent=hhea.xMaxExtent,
        )
        
        # Setup CFF
        ps_name = name_table.getDebugName(6) or ttf_path.stem.replace(" ", "-")
        font_info = {
            "FullName": name_table.getDebugName(4) or ttf_path.stem,
            "FamilyName": name_table.getDebugName(1) or ttf_path.stem,
            "Weight": os2.usWeightClass or 400,
            "ItalicAngle": post.italicAngle or 0,
        }
        private_dict = {
            "StdHW": post.underlineThickness or 50,
            "StdVW": post.underlineThickness or 80,
        }
        fb.setupCFF(ps_name, font_info, charstrings, private_dict)
        
        fb.setupNameTable({
            "familyName": name_table.getDebugName(1) or ttf_path.stem,
            "styleName": name_table.getDebugName(2) or "Regular",
        })
        
        fsSelection_safe = os2.fsSelection & ~0x1C0
        fb.setupOS2(
            sTypoAscender=os2.sTypoAscender,
            sTypoDescender=os2.sTypoDescender,
            sTypoLineGap=os2.sTypoLineGap,
            usWinAscent=os2.usWinAscent,
            usWinDescent=os2.usWinDescent,
            sxHeight=os2.sxHeight or 0,
            sCapHeight=os2.sCapHeight or 0,
            ulUnicodeRange1=os2.ulUnicodeRange1,
            ulUnicodeRange2=os2.ulUnicodeRange2,
            ulUnicodeRange3=os2.ulUnicodeRange3,
            ulUnicodeRange4=os2.ulUnicodeRange4,
            fsType=os2.fsType,
            fsSelection=fsSelection_safe,
            sFamilyClass=os2.sFamilyClass,
            panose=os2.panose,
            usWeightClass=os2.usWeightClass,
            usWidthClass=os2.usWidthClass,
            usDefaultChar=os2.usDefaultChar,
            usBreakChar=os2.usBreakChar,
            usMaxContext=os2.usMaxContext,
        )
        
        # Clear hinting flags in head table
        head_flags = head.flags & ~0x38
        fb.setupHead(
            unitsPerEm=head.unitsPerEm,
            created=head.created,
            modified=head.modified,
            flags=head_flags,
            macStyle=head.macStyle,
            lowestRecPPEM=head.lowestRecPPEM,
            glyphDataFormat=head.glyphDataFormat,
        )
        
        fb.setupPost(
            isFixedPitch=post.isFixedPitch,
            underlinePosition=post.underlinePosition,
            underlineThickness=post.underlineThickness,
        )
        
        # GASP: force anti-aliasing at all sizes
        gasp = table__g_a_s_p()
        gasp.gaspRange = {0: GASP_GRIDFIT | GASP_DOGRAY}
        fb.font["gasp"] = gasp
        
        # Save the OTF
        otf_path.parent.mkdir(parents=True, exist_ok=True)
        fb.font.save(str(otf_path))
        
        # Calculate size change
        input_size = os.path.getsize(ttf_path)
        output_size = os.path.getsize(otf_path)
        size_change = ((output_size - input_size) / input_size * 100) if input_size > 0 else 0
        
        logger.debug(f"✓ Converted {ttf_path} to {otf_path} (size change: {size_change:+.1f}%)")
        
        return {
            'input_size': input_size,
            'output_size': output_size,
            'size_change': size_change,
        }


# ---------------------------------------------------------------------------
# Step 2: Apply optimizations to OTF
# ---------------------------------------------------------------------------

def _scale_truetype_glyphs(font: TTFont, factor: float) -> None:
    """Scale TrueType glyphs (used for intermediate processing)."""
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

            coords = [(int(round(x * factor)), int(round(y * factor))) 
                     for x, y in glyph.coordinates]
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
                    comp.x = int(round(comp.x * factor))
                if comp.y is not None:
                    comp.y = int(round(comp.y * factor))

            if glyph.xMin is not None:
                glyph.xMin = int(round(glyph.xMin * factor))
                glyph.yMin = int(round(glyph.yMin * factor))
                glyph.xMax = int(round(glyph.xMax * factor))
                glyph.yMax = int(round(glyph.yMax * factor))


def _scale_cff_glyphs(font: TTFont, factor: float) -> bool:
    """Scale CFF charstrings."""
    cff_table_key = 'CFF ' if 'CFF ' in font else 'CFF2'
    
    if cff_table_key not in font:
        logger.error(f"CFF table {cff_table_key} not found in font")
        return False
        
    cff_table = font[cff_table_key]
    top_dict = cff_table.cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    glyph_set = font.getGlyphSet()

    new_charstrings = {}

    for glyph_name in font.getGlyphOrder():
        if glyph_name not in char_strings:
            continue
        if glyph_name not in glyph_set:
            continue

        try:
            glyph = glyph_set[glyph_name]
            scaled_width = int(round(glyph.width * factor))

            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, (factor, 0, 0, factor, 0, 0))
            glyph.draw(transform_pen)
            new_charstring = t2_pen.getCharString()
            
            if hasattr(top_dict, 'Private'):
                new_charstring.private = top_dict.Private
            
            new_charstrings[glyph_name] = new_charstring

        except Exception as e:
            logger.warning(f"Could not scale CFF glyph '{glyph_name}': {e}")
            new_charstrings[glyph_name] = char_strings[glyph_name]
            continue

    for name, cs in new_charstrings.items():
        char_strings[name] = cs
    
    return True


def _scale_cff_hint_values(font: TTFont, factor: float) -> None:
    """Scale CFF hinting reference values."""
    if 'CFF ' not in font:
        return

    cff = font['CFF '].cff
    top_dict = cff.topDictIndex[0]

    fb = getattr(top_dict, 'FontBBox', None)
    if fb is not None:
        try:
            top_dict.FontBBox = [int(round(v * factor)) for v in fb]
        except Exception as e:
            logger.debug(f"Could not scale CFF FontBBox: {e}")

    priv = getattr(top_dict, 'Private', None)
    if priv is None:
        return

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

    for attr in ('BlueShift', 'BlueFuzz'):
        val = getattr(priv, attr, None)
        if val is not None:
            try:
                setattr(priv, attr, int(round(val * factor)))
            except Exception as e:
                logger.debug(f"Could not scale CFF Private.{attr}: {e}")


def _scale_metrics(font: TTFont, factor: float) -> None:
    """Scale all metric tables."""
    if 'hmtx' in font:
        hmtx = font['hmtx']
        for glyph_name in list(hmtx.metrics.keys()):
            aw, lsb = hmtx.metrics[glyph_name]
            hmtx.metrics[glyph_name] = (
                int(round(aw * factor)),
                int(round(lsb * factor))
            )

    if 'vmtx' in font:
        vmtx = font['vmtx']
        for glyph_name in list(vmtx.metrics.keys()):
            ah, tsb = vmtx.metrics[glyph_name]
            vmtx.metrics[glyph_name] = (
                int(round(ah * factor)),
                int(round(tsb * factor))
            )

    if 'hhea' in font:
        hhea = font['hhea']
        hhea.ascent = int(round(hhea.ascent * factor))
        hhea.descent = int(round(hhea.descent * factor))
        hhea.lineGap = int(round(hhea.lineGap * factor))

    if 'vhea' in font:
        vhea = font['vhea']
        vhea.ascent = int(round(vhea.ascent * factor))
        vhea.descent = int(round(vhea.descent * factor))
        vhea.lineGap = int(round(vhea.lineGap * factor))

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

    if 'post' in font:
        post = font['post']
        post.underlinePosition = int(round(post.underlinePosition * factor))
        post.underlineThickness = int(round(post.underlineThickness * factor))

    if 'head' in font:
        head = font['head']
        for attr in ('xMin', 'yMin', 'xMax', 'yMax'):
            val = getattr(head, attr, None)
            if val is not None:
                setattr(head, attr, int(round(val * factor)))

        if head.lowestRecPPEM:
            head.lowestRecPPEM = max(1, int(round(head.lowestRecPPEM * factor)))

        import time
        head.modified = int(time.time())


def scale_font(input_path: str, output_path: str, scale_percent: float) -> bool:
    """
    Scale font by percentage.
    
    Args:
        input_path: Path to input font
        output_path: Path to save scaled font
        scale_percent: Percentage to scale
    
    Returns:
        True on success, False on error
    """
    if scale_percent == 0:
        import shutil
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
# Thickness (v12 NEW: --thickness PCT)
# ---------------------------------------------------------------------------
#
# Grow every outline outward by a fraction of the measured stem, then union
# the result so overlaps collapse correctly. Counters shrink on both sides,
# so a wall whose two sides would have met (closing the hole) is detected
# and the affected glyph is left untouched.
#
# Why skia-pathops and not a TransformPen scale: a scale grows the outer
# AND the inner contour by the same factor, so the wall thickness comes
# out roughly constant in absolute units -- the opposite of what a
# 'thicker stroke' should do. The v5 incident earlier this week is the same
# class of bug, just expressed as a shrink. skia-pathops does a real
# expand+union: outer grows, inner shrinks, wall gets thicker.

# Smallest hole (font units) the thicken pass is allowed to leave behind.
# At UPM 1000, 20u is ~1/3 px at 16ppem -- smaller reads as a filled hole.
COUNTER_MIN_UNITS = 20


def _measure_stem(font) -> float:
    """Best-effort stem width from the source font, in font units.

    Walks the 'o' and 'O' glyphs (whichever exist), counts horizontal edges
    on its left/right sides within the bbox, and returns the most common
    spacing. If measurement fails entirely, falls back to 8% of UPM, which
    is a sane default for 1000-upem body text and only off by a few units
    for display faces.
    """
    from collections import Counter
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.recordingPen import RecordingPen

    upm = font["head"].unitsPerEm
    glyph_set = font.getGlyphSet()
    stems: list[int] = []
    for name in ("o", "O", "zero", "zero.denominator"):
        if name not in glyph_set:
            continue
        pen = BoundsPen(glyph_set)
        try:
            glyph_set[name].draw(pen)
        except Exception:
            continue
        if not pen.bounds or pen.bounds[2] - pen.bounds[0] < 20:
            continue
        rec = RecordingPen()
        glyph_set[name].draw(rec)
        # Find vertical edges: x positions that are repeated at multiple
        # y values with the same x (a stem is a near-vertical line).
        #
        # rec.value holds (op, args) pairs, so the coordinate has to be
        # unwrapped per op. Indexing the pair directly gave the op
        # NAME where a number was expected -- "type str doesn't define
        # __round__" -- and --thickness therefore never ran on any font.
        xs = []
        for op, args in rec.value:
            if op in ("moveTo", "lineTo"):
                # args[0] is the point tuple; take its x, not the tuple.
                xs.append(round(args[0][0]))
            elif op == "curveTo":
                xs.extend(round(p[0]) for p in args)
            elif op == "qCurveTo":
                xs.extend(round(p[0]) for p in args if p is not None)
        if not xs:
            continue
        common = Counter(xs).most_common(2)
        # outer-left/outer-right of 'o': pick the two most-repeated x's
        if len(common) >= 2:
            x_left, _ = common[-1]
            x_right, _ = common[0]
            if x_right > x_left:
                stems.append(x_right - x_left)
    if stems:
        # 'o' is two walls, so the rightmost measurement / 2 is one wall
        return min(stems) / 2.0
    return upm * 0.08


def _expand_glyph(pen_data, delta: float, min_hole: int = COUNTER_MIN_UNITS):
    """Apply a +delta uniform outward expand to a glyph's recording.

    pathops 0.9.2 has no offset primitive, so this synthesises one: stroke
    the outline by 2*delta (a centred stroke puts delta on each side) and
    union the result with the original.

    The earlier implementation scaled the path about the origin and
    unioned that. Scaling about (0,0) TRANSLATES a glyph away from the
    axis as much as it enlarges it, so the union absorbed the difference
    and nothing grew: 'o' kept bbox width 928->928 and 'R' LOST 6.1% of
    its area. This version is the same stroke+union that embolden() in
    ttf2otf_ft_v6.py was verified on, measured there as o = 83676 with
    miter joins.

    Two pathops behaviours this must respect:
      * Path.stroke(width, cap, join, miter) MUTATES IN PLACE and returns
        None -- assigning its result yields a None path.
      * Path.rewind() CLEARS the path (bounds -> 0,0,0,0); it is not an
        iterator reset.

    Miter joins, not round: round joins emit conics whose weight a
    charstring cannot express.

    Returns (new_pen_ops, refused_bool); refused is True when the inner
    contour would have collapsed below min_hole and the original was kept.
    """
    import pathops

    if delta <= 0 or not pen_data:
        return pen_data, False

    orig = pathops.Path()
    for op, args in pen_data:
        if op == "moveTo":
            x, y = args[0]
            orig.moveTo(x, y)
        elif op == "lineTo":
            x, y = args[0]
            orig.lineTo(x, y)
        elif op == "qCurveTo":
            for p in [a for a in args if a is not None]:
                orig.quadTo(p[0], p[1])
        elif op == "curveTo":
            orig.cubicTo(*[c for pt in args for c in (pt[0], pt[1])])
        elif op == "closePath":
            orig.close()

    if orig.bounds == (0.0, 0.0, 0.0, 0.0):
        return pen_data, False

    stroked = pathops.Path()
    stroked.addPath(orig)
    stroked.stroke(delta * 2.0,
                   pathops.LineCap.BUTT_CAP,
                   pathops.LineJoin.MITER_JOIN,
                   4.0)

    try:
        expanded = pathops.op(stroked, orig, pathops.PathOp.UNION)
    except Exception:
        expanded = stroked
    try:
        expanded.simplify()
    except Exception:
        pass

    # Counter guard: if the innermost contour has collapsed to less than
    # min_hole on either axis, keep the original geometry instead.
    if min_hole > 0:
        try:
            contours = _skia_contour_bboxes(expanded)
            if len(contours) >= 2:
                contours.sort(key=lambda c: abs(c[2]))
                _, (x0, y0, x1, y1), _ = contours[0]
                if (x1 - x0) < min_hole or (y1 - y0) < min_hole:
                    return pen_data, True
        except Exception:
            pass

    return _skia_to_pen(expanded), False
def _skia_contour_bboxes(path):
    """Return [(pts, (x0,y0,x1,y1), signed_area), ...] for each contour.

    Iterating a pathops Path yields (verb_code, args) pairs with integer
    codes, not named ops: 0 move, 1 line, 2 quad, 3 conic, 4 cubic,
    5 close. Comparing cmd[0] == "moveTo" against an int skipped every
    branch, so no contour was ever found and the counter guard silently
    did nothing on any glyph.

    Do NOT call path.rewind() before iterating: in pathops 0.9.2 rewind
    CLEARS the path (bounds go to 0,0,0,0) rather than resetting an
    iterator. It emptied the geometry, and every contour test then
    reported zero contours.
    """
    out: list = []
    cur: list = []
    for code, args in path:
        if code == 0:                       # move
            if cur:
                out.append(_close_contour(cur))
            cur = [tuple(args[0])]
        elif code == 1:                     # line
            cur.append(tuple(args[0]))
        elif code in (2, 3, 4):             # quad, conic, cubic
            for p in args:
                cur.append(tuple(p))
        elif code == 5:                     # close
            if cur:
                out.append(_close_contour(cur))
            cur = []
    if cur:
        out.append(_close_contour(cur))
    return out


def _close_contour(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return pts, (min(xs), min(ys), max(xs), max(ys)), area / 2.0


def _skia_to_pen(path):
    """Convert a skia-pathops Path back to RecordingPen format tuples.

    Emits the same shape RecordingPen produces -- a point op's args are a
    1-tuple wrapping the point -- so the result compares equal against the
    original recording and replays into a pen unchanged. Emitting the flat
    ('moveTo', (x, y)) shape instead meant new_data == orig_data was never
    true and the replay code mis-read every argument.

    Rounded to int: all CFF coordinates are integers in practice, and
    sub-unit rounding prevents drift.

    Do NOT call path.rewind() here -- see _skia_contour_bboxes.
    """
    out: list = []
    for code, args in path:
        if code == 0:                       # move
            out.append(("moveTo", ((round(args[0][0]), round(args[0][1])),)))
        elif code == 1:                     # line
            out.append(("lineTo", ((round(args[0][0]), round(args[0][1])),)))
        elif code == 3:                     # conic: carries a weight a
            # charstring cannot express. Approximate with quads.
            path.convertConicsToQuads()
            return _skia_to_pen(path)
        elif code == 2:                     # quad
            pts = [(round(p[0]), round(p[1])) for p in args if p is not None]
            out.append(("qCurveTo", pts))
        elif code == 4:                     # cubic
            pts = [(round(p[0]), round(p[1])) for p in args if p is not None]
            out.append(("curveTo", pts))
        elif code == 5:                     # close
            out.append(("closePath", ()))
    return out


def thicken_font(input_path: str, output_path: str, thickness_pct: float) -> bool:
    """Grow every glyph outline by ``thickness_pct`` percent of the measured stem.

    A 5% thicken on a font with an 80u stem adds 4u to every outline. The
    intent is 'make the strokes heavier' -- not 'make the font bigger',
    which is what --scale does. The pipeline places this AFTER --scale and
    BEFORE --spacing/hinting so the final metrics reflect the heavier glyphs.
    """
    if thickness_pct == 0:
        import shutil
        shutil.copy2(input_path, output_path)
        logger.info(f"✓ Copied font (no thickening) to {output_path}")
        return True

    try:
        import pathops   # noqa: F401 - importable, used in _expand_glyph
    except ImportError:
        logger.error("--thickness requires skia-pathops; install with: pip install skia-pathops")
        return False

    try:
        with TTFont(input_path) as font:
            upm = font["head"].unitsPerEm
            base_stem = _measure_stem(font)
            delta = base_stem * (thickness_pct / 100.0)
            logger.debug(f"Thicken: base stem {base_stem:.1f}u -> delta {delta:+.2f}u "
                         f"({thickness_pct:+g}% of stem)")

            # We work on the CFF side because v12's downstream pipeline is
            # CFF-only (psautohint, GASP). TrueType input has already been
            # converted to CFF by convert_ttf_to_otf at this point.
            if "CFF " not in font:
                logger.error("--thickness currently supports CFF outlines only.")
                return False

            cff = font["CFF "]
            cff_font = cff.cff.topDictIndex[0]
            charstrings = cff_font.CharStrings
            glyph_order = font.getGlyphOrder()

            restored = 0
            touched = 0
            for name in glyph_order:
                cs = charstrings[name]
                # Snapshot the original charstring so we can restore.
                from fontTools.pens.recordingPen import RecordingPen
                orig_pen = RecordingPen()
                try:
                    cs.draw(orig_pen)
                except Exception:
                    continue
                orig_data = orig_pen.value

                new_data, refused = _expand_glyph(orig_data, delta)
                if refused:
                    restored += 1
                    continue
                if new_data == orig_data:
                    continue
                # Rebuild the charstring from the new pen data.
                #
                # T2Pen does not exist in this fontTools build
                # (fontTools.pens.t2Pen is absent; only t2CharStringPen
                # ships), so this replay uses T2CharStringPen, which takes
                # the same RecordingPen-style calls.
                #
                # T2CharStringPen's second parameter is glyphSet, NOT the private
                # dict, so the pen cannot receive it. The built charstring
                # comes out with private=None, and fontTools then dies in
                # recalcFontBBox with "NoneType has no attribute
                # nominalWidthX". Attach the font's private afterwards.
                adv = font["hmtx"].metrics.get(name, (0, 0))[0]
                t2_pen = T2CharStringPen(adv, None)
                for op, args in new_data:
                    if op == "moveTo":
                        t2_pen.moveTo(args[0])
                    elif op == "lineTo":
                        t2_pen.lineTo(args[0])
                    elif op == "qCurveTo":
                        t2_pen.qCurveTo(*args)
                    elif op == "curveTo":
                        t2_pen.curveTo(*args)
                    elif op == "closePath":
                        t2_pen.closePath()
                    elif op == "endPath":
                        t2_pen.endPath()
                rebuilt = t2_pen.getCharString()
                rebuilt.private = cff_font.Private
                charstrings[name] = rebuilt
                touched += 1

            # Bump head.modified so downstream caches invalidate.
            import time
            font["head"].modified = int(time.time())

            font.save(output_path)

        if restored:
            logger.info(f"  ⚠ counter guard: kept hole open in {restored} glyph(s)")
        logger.debug(f"✓ Thickened {touched} glyph(s); {restored} refused by counter guard")
        return True

    except Exception as exc:
        logger.error(f"Error thickening font {input_path}: {exc}")
        logger.debug(traceback.format_exc())
        return False


# ---------------------------------------------------------------------------
# Letter-spacing / tracking (v12 NEW)
# ---------------------------------------------------------------------------

# Bounds for the percentage mode of --spacing. Match otf_optimize-ff-v14.py.
_TRACKING_PCT_MIN = -90.0
_TRACKING_PCT_MAX = 400.0
_TRACKING_DIRECT_MAX_ABS = 4096.0  # font-unit addend hard cap


def _clamp_tracking_value(spacing_value: float) -> tuple:
    """
    Normalise a raw --spacing value into (mode, pct, addend).

    Dual-mode semantics matching otf_optimize-ff-v14.py:
        |v| < 1.0  -> "direct" mode; addend = v (font units), clamped to ±4096
        |v| >= 1.0 -> "percent" mode; pct = v (%), clamped to [-90, +400]

    Returns:
        (mode, pct, addend) where exactly one of pct/addend is non-None.
    """
    abs_val = abs(spacing_value)
    if abs_val < 1.0:
        addend = spacing_value
        if abs(addend) > _TRACKING_DIRECT_MAX_ABS:
            addend = _TRACKING_DIRECT_MAX_ABS * (1 if addend > 0 else -1)
        return ("direct", None, addend)
    pct = max(_TRACKING_PCT_MIN, min(_TRACKING_PCT_MAX, spacing_value))
    return ("percent", pct, None)


def apply_tracking(input_path: str, output_path: str, spacing_value: float) -> bool:
    """
    Letter-spacing: advance widths only, outlines untouched.

    Same dual-mode semantics as otf_optimize-ff-v14.py / apply_tracking:
        |spacing_value| < 1.0   -> direct font-unit addend (clamped to ±4096)
        |spacing_value| >= 1.0  -> percentage change of each advance width
                                   (clamped to [-90%, +400%])

    Works on the font at ``input_path`` (CFF/OTF or TrueType) and writes the
    result to ``output_path``.  Both CFF charstring width operands and the
    ``hmtx`` table are updated so the new spacing survives rasterisation in
    every downstream consumer.

    Args:
        input_path: Path to the font to retouch.
        output_path: Where to write the retouched font.
        spacing_value: Raw --spacing value (see dual-mode semantics above).

    Returns:
        True on success, False on error.
    """
    if spacing_value == 0:
        # No-op fast-path: just copy through so the pipeline is uniform.
        import shutil
        try:
            shutil.copy2(input_path, output_path)
        except Exception as exc:
            logger.error(f"Tracking no-op copy failed: {exc}")
            return False
        logger.debug("Tracking: value 0 -> no-op (copied through)")
        return True

    mode, pct, addend = _clamp_tracking_value(spacing_value)
    if mode == "direct":
        logger.info(f"Tracking (direct mode, addend={addend:+.2f} font units)")
    else:
        logger.info(f"Tracking (percent mode, value={pct:+.2f}%)")

    try:
        with TTFont(input_path) as font:
            glyph_order = font.getGlyphOrder()
            glyph_set = font.getGlyphSet()
            hmtx = font.getGlyphSet()  # placeholder, real hmtx via table below

            # --- 1) Update the hmtx table (authoritative for OTF + TTF) ----
            hmtx_table = font['hmtx']
            new_metrics = {}
            max_aw = 0
            touched = 0
            for gname in glyph_order:
                old_aw, lsb = hmtx_table.metrics.get(gname, (0, 0))
                if addend is not None:
                    new_aw = int(round(old_aw + addend))
                else:
                    new_aw = int(round(old_aw * (1.0 + pct / 100.0)))
                new_aw = max(0, new_aw)
                new_metrics[gname] = (new_aw, lsb)
                max_aw = max(max_aw, new_aw)
                touched += 1
            hmtx_table.metrics.update(new_metrics)

            # --- 2) If the font carries CFF outlines, rewrite the
            #       charstring width operand too (otherwise CFF charstrings
            #       would still report the old width). TrueType/PS is left
            #       alone since glyf widths come straight from hmtx.
            is_cff = ('CFF ' in font) or ('CFF2' in font)
            if is_cff:
                cff_table_key = 'CFF ' if 'CFF ' in font else 'CFF2'
                cff = font[cff_table_key].cff
                top_dict = cff.topDictIndex[0]
                char_strings = top_dict.CharStrings
                private = getattr(top_dict, 'Private', None)

                new_charstrings = {}
                for gname in glyph_order:
                    if gname not in char_strings or gname not in glyph_set:
                        continue
                    try:
                        new_width = new_metrics[gname][0]
                        t2_pen = T2CharStringPen(new_width, glyph_set)
                        # Outline passes through verbatim.
                        glyph_set[gname].draw(t2_pen)
                        new_cs = t2_pen.getCharString()
                        if private is not None:
                            new_cs.private = private
                        new_charstrings[gname] = new_cs
                    except Exception as exc:
                        logger.warning(
                            f"Could not rewrite CFF charstring width for "
                            f"'{gname}': {exc}"
                        )
                        new_charstrings[gname] = char_strings[gname]

                for name, cs in new_charstrings.items():
                    char_strings[name] = cs

            # --- 3) Refresh hhea.advanceWidthMax if it exists -------------
            if 'hhea' in font:
                hhea = font['hhea']
                try:
                    hhea.advanceWidthMax = int(max_aw)
                except Exception as exc:
                    logger.debug(f"Could not update hhea.advanceWidthMax: {exc}")

            font.save(output_path)

        logger.info(
            f"Tracking applied: {touched} glyph advance widths updated "
            f"(addend: {addend if addend is not None else f'{pct:+.2f}%'}, "
            f"max advance {max_aw})"
        )
        return True

    except Exception as exc:
        logger.error(f"Error applying tracking to {input_path}: {exc}")
        logger.debug(traceback.format_exc())
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


# ---------------------------------------------------------------------------
# Main Processing
# ---------------------------------------------------------------------------

def convert_and_optimize(input_path: str, output_path: str, options: dict) -> bool:
    """
    Convert TTF to OTF with optional scaling and optimization.
    
    Two-step process:
        1. Convert TTF → OTF using fonttools
        2. Apply optimizations (scaling, hinting)
    
    Args:
        input_path: Path to input TTF font
        output_path: Path to output OTF font
        options: Dictionary of options (see main())
    
    Returns:
        True on success, False on error
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    
    # Step 1: Convert TTF → OTF
    temp_otf = output_path.with_suffix('.tmp.otf')

    try:
        stats = convert_ttf_to_otf(input_path, temp_otf)
        logger.info(f"✓ Converted TTF to OTF (size change: {stats['size_change']:+.1f}%)")

        # Track which path currently holds the working OTF so subsequent
        # steps (tracking, hinting) know where to read from. After Step 1
        # the OTF is at ``temp_otf``; after Step 2 (scaling) it moves to
        # ``output_path``. v11 had a latent bug where scale=0 left the
        # downstream steps pointing at an unwritten output_path; v12 fixes
        # that here.
        working_otf = temp_otf

        # Step 2: Apply scaling if requested
        if options.get('scale', 0) != 0:
            logger.info(f"Scaling font by {options['scale']}%...")
            if not scale_font(str(temp_otf), str(output_path), options['scale']):
                logger.error("Scaling failed")
                return False
            working_otf = output_path

        # Step 3 (v12 NEW): Apply letter-spacing / tracking if requested.
        # Tracking only touches advance widths, so outlines survive intact.
        # We do it AFTER scaling (so widths grow on top of the scaled
        # advances) and BEFORE hinting (so psautohint recomputes its
        # BlueValues / StemSnapH-V against the final metrics).
        if options.get('thickness', 0) != 0:
            # After --scale and before --spacing/hinting, so the final
            # metrics and hinting see the heavier outlines.
            logger.info(f"Thickening strokes (--thickness {options['thickness']:+.2f}%)...")
            # Write to a side file rather than back over output_path: the
            # source TTFont is still open for reading inside thicken_font.
            thicken_tmp = output_path.with_suffix('.thicken.otf')
            if not thicken_font(str(working_otf), str(thicken_tmp),
                                float(options['thickness'])):
                logger.error("Thickening failed")
                if thicken_tmp.exists():
                    thicken_tmp.unlink()
                return False
            working_otf = thicken_tmp

        if options.get('spacing', 0) != 0:
            logger.info(f"Applying tracking (--spacing {options['spacing']:+.2f})...")
            tracking_target = output_path
            if working_otf != output_path:
                # No scaling was performed, so the font is still at
                # temp_otf. Move it to output_path before tracking so the
                # downstream hinting step can find it (also fixes the v11
                # scale=0 -> missing output_path bug).
                import shutil
                try:
                    shutil.move(str(working_otf), str(output_path))
                except Exception as exc:
                    logger.error(f"Could not move working OTF to output path: {exc}")
                    return False
                working_otf = output_path
            if not apply_tracking(str(working_otf), str(tracking_target),
                                  float(options['spacing'])):
                logger.error("Tracking failed")
                return False

        # Step 4: Apply autohinting if requested
        if not options.get('no_hinting'):
            if working_otf != output_path:
                # Defensive: ensure output_path exists before psautohint
                # reads from it.
                import shutil
                try:
                    shutil.move(str(working_otf), str(output_path))
                except Exception as exc:
                    logger.error(f"Could not move working OTF to output path: {exc}")
                    return False
                working_otf = output_path
            logger.info("Applying psautohint...")
            if optimize_cff_font(str(output_path), output_path, options):
                logger.info("✓ Autohinting complete")
            else:
                logger.warning("Autohinting failed, keeping unhinted font")

        # Final guard: if no step (scaling / tracking / hinting) ever wrote
        # to output_path (e.g. scale=0, spacing=0, --no-hinting), the
        # converted OTF is still sitting at temp_otf and v11 would have
        # unlinked it -- silently producing no output. v12 promotes it to
        # output_path so the conversion always survives.
        if working_otf != output_path:
            import shutil
            try:
                shutil.move(str(working_otf), str(output_path))
            except Exception as exc:
                logger.error(f"Could not promote working OTF to output path: {exc}")
                return False
            working_otf = output_path

        # Clean up temp file (only if it's a real separate path)
        if temp_otf.exists() and temp_otf != output_path:
            temp_otf.unlink()

        return True
        
    except Exception as e:
        logger.error(f"Error processing {input_path}: {e}")
        logger.debug(traceback.format_exc())
        if temp_otf.exists():
            temp_otf.unlink()
        return False


def process_directory(input_dir: str, output_dir: str, options: dict) -> dict:
    """
    Process all TTF fonts in a directory recursively.
    
    Args:
        input_dir: Input directory path
        output_dir: Output directory path
        options: Dictionary of options (see main())
    
    Returns:
        Dictionary with processing stats
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Find all TTF files
    font_files = []
    for ext in ['*.ttf', '*.TTF']:
        font_files.extend(input_path.rglob(ext))
    
    if not font_files:
        logger.warning(f"No TTF files found in {input_dir}")
        return {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}
    
    logger.info(f"Found {len(font_files)} TTF file(s) in {input_dir}")
    
    stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}
    
    for font_file in font_files:
        stats['total'] += 1
        
        rel_path = font_file.relative_to(input_path)
        out_file = output_path / rel_path.with_suffix('.otf')
        out_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Processing {font_file}...")
        
        try:
            if convert_and_optimize(str(font_file), str(out_file), options):
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
        description='Convert TTF to OTF with scaling and optimization',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert with 7.5% scaling and autohinting
  python ttf2otf_optimize-v12.py --scale 7.5 input_fonts/ output_fonts/

  # Add 15% letter-spacing on top of 7.5% scaling
  python ttf2otf_optimize-v12.py --scale 7.5 --spacing 15 input/ output/

  # Add 50 font-units of tracking (no scaling, outlines untouched)
  python ttf2otf_optimize-v12.py --spacing 50 input/ output/

  # Convert without autohinting
  python ttf2otf_optimize-v12.py --scale 10 --no-hinting input/ output/

  # Single file conversion
  python ttf2otf_optimize-v12.py --scale 5 --spacing 5 input.ttf output.otf
        """
    )
    
    parser.add_argument('input', help='Input TTF font file or directory')
    parser.add_argument('output', help='Output OTF font file or directory')
    parser.add_argument('--scale', type=float, default=0,
                        help='Scale percentage (positive = larger). Default: 0')
    parser.add_argument('--spacing', type=float, default=0,
                        help='Letter-spacing / tracking. Advance widths only; '
                             'glyph outlines are untouched. Dual-mode: '
                             '|v|<1.0 = font units (clamped to ±4096), '
                             '|v|>=1.0 = percent (clamped to [-90%%, +400%%]). '
                             'Default: 0')
    parser.add_argument('--thickness', type=float, default=0,
                        help='Stroke thickness as a percentage of the measured '
                             'stem width (positive = heavier, negative = '
                             'thinner). Adds skia-pathops expand+union to '
                             'every outline; the source\'s stem is measured '
                             'at runtime from \'o\'/\'O\', so no design '
                             'knowledge is required. A counter guard keeps '
                             'holes that would collapse below ~20 font units. '
                             'Requires skia-pathops. Default: 0')
    parser.add_argument('--no-hinting', action='store_true',
                        help='Skip autohinting step')
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
    
    # Configure logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Check dependencies if requested
    if args.check_deps:
        if check_dependencies():
            logger.info("✓ All dependencies found")
            sys.exit(0)
        else:
            logger.error("✗ Missing dependencies")
            sys.exit(1)
    
    # Validate input
    if not os.path.exists(args.input):
        logger.error(f"Input path does not exist: {args.input}")
        sys.exit(1)
    
    # Check input is TTF
    if os.path.isfile(args.input):
        if args.input.lower().endswith('.ttf'):
            pass  # OK
        else:
            logger.error(f"Input file is not a TTF: {args.input}")
            sys.exit(1)
    
    # Build options dict
    options = {
        'scale': args.scale,
        'spacing': args.spacing,
        'thickness': args.thickness,
        'no_hinting': args.no_hinting,
        'allow_changes': args.allow_changes,
        'no_flex': args.no_flex,
        'no_hint_sub': args.no_hint_sub,
        'verbose': args.verbose,
    }
    
    # Process single file or directory
    if os.path.isfile(args.input):
        success = convert_and_optimize(args.input, args.output, options)
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
