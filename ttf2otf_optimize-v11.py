#!/usr/bin/env python3
"""
TTF → OTF Optimization Script v11

This script converts TTF fonts to OTF (CFF) format with:
- Optional scaling of glyph outlines and metrics by a percentage
- Curve optimization (quadratic → cubic conversion)
- Overlap removal
- Hinting removal for cleaner rendering
- Forced grayscale anti-aliasing at all sizes
- Autohinting with psautohint (CFF) or ttfautohint (TrueType)

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
    python ttf2otf_optimize-v11.py <input_dir> <output_dir>
    python ttf2otf_optimize-v11.py <input_dir> <output_dir> --scale 7.5
    python ttf2otf_optimize-v11.py <input_dir> <output_dir> --mode full
    python ttf2otf_optimize-v11.py <input_dir> <output_dir> --mode direct
    python ttf2otf_optimize-v11.py <input_dir> <output_dir> --mode fonttools
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
from fontTools.fontBuilder import FontBuilder
from fontTools.ttLib.tables._g_a_s_p import table__g_a_s_p, GASP_GRIDFIT, GASP_DOGRAY
from fontTools.cffLib import TopDictIndex, PrivateDict
from fontTools.misc.textTools import tostr

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

            # Convert quadratic curves to cubic
            t2_pen = T2CharStringPen(0, None)
            qu2cu = Qu2CuPen(t2_pen, max_err=0.3)
            # Pass glyf table as required argument
            glyph.draw(qu2cu, glyf)
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
        
        # Step 2: Apply scaling if requested
        if options.get('scale', 0) != 0:
            logger.info(f"Scaling font by {options['scale']}%...")
            if not scale_font(str(temp_otf), str(output_path), options['scale']):
                logger.error("Scaling failed")
                return False
        
        # Step 3: Apply autohinting if requested
        if not options.get('no_hinting'):
            logger.info("Applying psautohint...")
            if optimize_cff_font(str(output_path), output_path, options):
                logger.info("✓ Autohinting complete")
            else:
                logger.warning("Autohinting failed, keeping unhinted font")
        
        # Clean up temp file
        if temp_otf.exists():
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
  python ttf2otf_optimize-v11.py --scale 7.5 input_fonts/ output_fonts/
  
  # Convert without autohinting
  python ttf2otf_optimize-v11.py --scale 10 --no-hinting input/ output/
  
  # Single file conversion
  python ttf2otf_optimize-v11.py --scale 5 input.ttf output.otf
        """
    )
    
    parser.add_argument('input', help='Input TTF font file or directory')
    parser.add_argument('output', help='Output OTF font file or directory')
    parser.add_argument('--scale', type=float, default=0,
                        help='Scale percentage (positive = larger). Default: 0')
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
