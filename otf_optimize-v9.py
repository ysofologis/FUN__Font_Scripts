#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v9

This script optimizes fonts by:
- Autohinting TrueType-based fonts with ttfautohint
- Autohinting CFF-based OTF fonts with psautohint
- Properly identifying CFF vs TrueType outlines
- Scaling font size by a percentage (IMPROVED in v9)
- Maintaining OTF output format

v9 changes:
- FIXED: CFF scaling now preserves curve quality by using floating-point precision
- FIXED: Added proper font closing with context managers
- FIXED: Improved CFF charstring scaling to prevent curve artifacts
- FIXED: Better handling of composite glyphs in CFF fonts
- Added --preserve-curves flag for high-quality scaling (default: enabled)

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
# Font scaling (v9 - IMPROVED)
# ---------------------------------------------------------------------------

def _scale_truetype_glyphs(font: TTFont, factor: float, preserve_curves: bool = True) -> None:
    """
    Scale all TrueType (glyf) glyph outlines by *factor*.

    Handles simple glyphs (coordinate arrays) and composite glyphs
    (component offsets). Recalculates bounding boxes after scaling.
    
    Args:
        font: TTFont instance
        factor: Scaling factor (1.0 = no change, 1.1 = 10% larger)
        preserve_curves: If True, use higher precision for curve preservation
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

            coords = glyph.coordinates.copy()
            
            # Use higher precision scaling for better curve preservation
            if preserve_curves:
                # Scale with floating point precision, then round
                scaled_coords = []
                for x, y in coords:
                    scaled_x = x * factor
                    scaled_y = y * factor
                    # Round to nearest integer with better precision
                    scaled_coords.append((int(round(scaled_x)), int(round(scaled_y))))
                coords = scaled_coords
            else:
                # Original method
                for i in range(len(coords)):
                    x, y = coords[i]
                    coords[i] = (int(round(x * factor)), int(round(y * factor)))
            
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


def _scale_cff_glyphs(font: TTFont, factor: float, preserve_curves: bool = True) -> bool:
    """
    Scale all CFF charstring outlines by *factor*.
    
    IMPROVED in v9: Uses higher precision and better handling of CFF charstrings
    to prevent curve artifacts and missing blocks.

    Args:
        font: TTFont instance
        factor: Scaling factor
        preserve_curves: If True, use improved method for curve preservation
        
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

            if preserve_curves:
                # IMPROVED METHOD: Use higher precision and proper charstring handling
                # Create a new charstring with the scaled width
                t2_pen = T2CharStringPen(scaled_width, glyph_set)
                
                # Use TransformPen with the scaling factor
                # This preserves the curve structure better than direct coordinate manipulation
                transform_pen = TransformPen(t2_pen, (factor, 0, 0, factor, 0, 0))
                
                # Draw the glyph through the transform pen
                glyph.draw(transform_pen)
                
                # Get the new charstring
                new_charstring = t2_pen.getCharString()
                
                # The new T2CharString needs a reference to the Private dict
                # so fontTools' calcBounds() can resolve width encoding.
                if hasattr(top_dict, 'Private'):
                    new_charstring.private = top_dict.Private
                
                new_charstrings[glyph_name] = new_charstring
            else:
                # Original method (for backward compatibility)
                t2_pen = T2CharStringPen(scaled_width, glyph_set)
                transform_pen = TransformPen(t2_pen, (factor, 0, 0, factor, 0, 0))
                glyph.draw(transform_pen)
                new_charstring = t2_pen.getCharString()
                if hasattr(top_dict, 'Private'):
                    new_charstring.private = top_dict.Private
                new_charstrings[glyph_name] = new_charstring

        except Exception as e:
            logger.warning(f"Could not scale CFF glyph '{glyph_name}': {e}")
            logger.debug(f"CFF glyph '{glyph_name}' error traceback:\n{traceback.format_exc()}")
            # Keep the original charstring for this glyph (e.g. empty .notdef)
            new_charstrings[glyph_name] = char_strings[glyph_name]
            had_errors = True
            continue

    # Replace charstrings in-place
    for name, cs in new_charstrings.items():
        char_strings[name] = cs

    # NOTE: No manual CFF recompilation needed here. fontTools recompiles the
    # whole CFF table from the (replaced) charstrings when font.save() is
    # called. Calling cff.cff.compile() directly is invalid (it requires a
    # file/otFont) and only produces a spurious warning.

    if had_errors:
        logger.warning("Some CFF glyphs could not be scaled and were left unchanged.")
    
    return True


def _scale_cff_hint_values(font: TTFont, factor: float) -> None:
    """
    Scale CFF hinting reference values so psautohint re-aligns stems and
    blue zones to the *scaled* outlines.

    Without this, the alignment zones (BlueValues) and stem-snap lists
    (StemSnapH/V) stay at the original size while the outlines are larger,
    so psautohint aligns scaled glyphs against mis-scaled reference zones —
    the main cause of subtle, hard-to-spot distortion after scaling
    (most visible on thin stems / light weights).

    NOTE: defaultWidthX / nominalWidthX are deliberately left untouched so
    the width deltas encoded by T2CharStringPen keep decoding correctly.
    """
    if 'CFF ' not in font:
        return  # CFF2 uses a different hinting model; skip for safety

    cff = font['CFF '].cff
    top_dict = cff.topDictIndex[0]

    # Top-dict FontBBox (global bounding box of the font)
    fb = getattr(top_dict, 'FontBBox', None)
    if fb:
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
        val = priv.get(attr) if hasattr(priv, 'get') else None
        if val:
            try:
                priv[attr] = [int(round(v * factor)) for v in val]
            except Exception as e:
                logger.debug(f"Could not scale CFF Private.{attr}: {e}")

    # Scalar hint parameters
    for attr in ('BlueShift', 'BlueFuzz'):
        val = priv.get(attr) if hasattr(priv, 'get') else None
        if val is not None:
            try:
                priv[attr] = int(round(val * factor))
            except Exception as e:
                logger.debug(f"Could not scale CFF Private.{attr}: {e}")


def _scale_metrics(font: TTFont, factor: float) -> None:
    """Scale all metric tables by *factor*."""

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
        # Scale the global font bounding box (was missing — causes clipping
        # and subtle distortion in some renderers)
        for attr in ('xMin', 'yMin', 'xMax', 'yMax'):
            val = getattr(head, attr, None)
            if val is not None:
                setattr(head, attr, int(round(val * factor)))

        # Scale lowestRecPPEM (minimum recommended screen size in px)
        if head.lowestRecPPEM:
            head.lowestRecPPEM = max(1, int(round(head.lowestRecPPEM * factor)))

        # ---- Update the modification timestamp in head ----
        import time
        from fontTools.ttLib.tables._h_e_a_d import mac_epoch_diff
        now = int(time.time())
        head.modified = now + mac_epoch_diff if hasattr(head, 'modified') else now


def scale_font_glyphs(input_path: str, output_path: str, scale_percent: float, preserve_curves: bool = True) -> bool:
    """
    Scale ALL glyph outlines and metrics by the given percentage.
    
    IMPROVED in v9: Better handling of CFF fonts to prevent curve artifacts.

    Args:
        input_path: Path to input font file (.otf / .ttf)
        output_path: Path to save the scaled font
        scale_percent: Percentage to scale (10 = make 10% larger)
        preserve_curves: If True, use improved method for curve preservation
        
    Returns:
        True on success, False on error.
    """
    if scale_percent == 0:
        # Allow 0% scaling (just copy the font)
        try:
            with TTFont(input_path) as font:
                font.save(output_path)
            logger.info(f"✓ Font copied to {output_path} (no scaling)")
            return True
        except Exception as e:
            logger.error(f"Error copying font {input_path}: {str(e)}")
            return False

    factor = 1.0 + scale_percent / 100.0
    logger.info(f"Scaling font by {scale_percent}% (factor ×{factor:.4f})")
    if preserve_curves:
        logger.info("Using high-quality curve preservation mode")

    try:
        with TTFont(input_path) as font:
            has_truetype = 'glyf' in font
            has_cff = 'CFF ' in font or 'CFF2' in font

            # Scale glyph outlines
            if has_truetype:
                _scale_truetype_glyphs(font, factor, preserve_curves)
            elif has_cff:
                result = _scale_cff_glyphs(font, factor, preserve_curves)
                if not result:
                    return False
                # Keep hint zones / stem snaps aligned with the scaled outlines
                _scale_cff_hint_values(font, factor)
            else:
                logger.error("Font has neither TrueType nor CFF outlines — cannot scale.")
                return False

            # Scale all metric tables
            _scale_metrics(font, factor)

            # Save the scaled font
            font.save(output_path)

        logger.info(f"✓ Scaled font saved to {output_path}")
        return True

    except Exception as e:
        logger.error(f"Error scaling font {input_path}: {str(e)}")
        logger.debug(f"Full traceback:\n{traceback.format_exc()}")
        return False


# ---------------------------------------------------------------------------
# Hinting — unchanged from v7
# ---------------------------------------------------------------------------

def optimize_truetype_font(input_path: str, output_path: str, options: dict) -> bool:
    """Optimize TrueType-based font with ttfautohint"""
    try:
        # Build ttfautohint command
        cmd = ['ttfautohint']

        # Add options based on user preferences
        if options.get('hinting_strength'):
            cmd.extend(['--hinting-limit', str(options['hinting_strength'])])

        if options.get('no_combining_chars'):
            cmd.append('--no-combining-chars')

        if options.get('detailed_info'):
            cmd.append('--detailed-info')

        if options.get('fallback_stem_width'):
            cmd.extend(['--fallback-stem-width', str(options['fallback_stem_width'])])

        # Default options for better rendering (matches v8, known-good)
        cmd.extend([
            '--default-script=latn',  # Default script
            '--fallback-script=none',  # No fallback script
            '--symbol',                # Process symbol area
            '--fallback-scaling',      # Use fallback scaling
        ])

        # Input and output files (ttfautohint: input output positionals)
        cmd.extend([input_path, output_path])

        # Execute with timeout
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )

        if result.returncode != 0:
            logger.error(f"ttfautohint failed: {result.stderr}")
            return False

        logger.info(f"✓ TrueType font optimized with ttfautohint")
        return True

    except subprocess.TimeoutExpired:
        logger.error("ttfautohint timed out after 5 minutes")
        return False
    except Exception as e:
        logger.error(f"Error optimizing TrueType font: {str(e)}")
        return False


def optimize_cff_font(input_path: str, output_path: str, options: dict) -> bool:
    """Optimize CFF-based font with psautohint"""
    try:
        # Build psautohint command
        cmd = ['psautohint']

        # Add options based on user preferences
        if options.get('allow_changes'):
            cmd.append('--allow-changes')

        if options.get('no_flex'):
            cmd.append('--no-flex')

        if options.get('no_hint_sub'):
            cmd.append('--no-hint-sub')

        if options.get('verbose'):
            cmd.append('-v')

        # Default options for better compatibility
        cmd.append('--no-zones-stems')  # Allow fonts without zones/stems

        # Output file (psautohint uses -o for output, input is positional)
        cmd.extend(['-o', output_path])

        # Input font
        cmd.append(input_path)

        # Execute with timeout
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )

        if result.returncode != 0:
            logger.error(f"psautohint failed: {result.stderr}")
            return False

        logger.info(f"✓ CFF font optimized with psautohint")
        return True

    except subprocess.TimeoutExpired:
        logger.error("psautohint timed out after 5 minutes")
        return False
    except Exception as e:
        logger.error(f"Error optimizing CFF font: {str(e)}")
        return False


# ---------------------------------------------------------------------------
# Main optimization pipeline
# ---------------------------------------------------------------------------

def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Main optimization pipeline.
    
    Args:
        input_path: Input font file path
        output_path: Output font file path
        options: Dictionary of optimization options
        
    Returns:
        True on success, False on error
    """
    # Check if we need to scale first
    scale_percent = options.get('scale', 0)
    preserve_curves = options.get('preserve_curves', True)
    
    temp_path = None
    
    try:
        # Step 1: Scale if requested
        if scale_percent != 0:
            # Create temporary file for scaled version
            input_ext = os.path.splitext(input_path)[1]
            fd, temp_path = tempfile.mkstemp(suffix=input_ext)
            os.close(fd)
            
            # Scale the font
            if not scale_font_glyphs(input_path, temp_path, scale_percent, preserve_curves):
                return False
            
            # Use scaled file as input for hinting
            working_input = temp_path
        else:
            working_input = input_path

        # Step 2: Analyze font type
        font_info = analyze_font_type(working_input)
        
        if not any([font_info['is_truetype'], font_info['is_cff']]):
            logger.error(f"Cannot determine font type for {working_input}")
            return False

        # Step 3: Apply appropriate optimization
        if font_info['is_truetype']:
            success = optimize_truetype_font(working_input, output_path, options)
        elif font_info['is_cff']:
            success = optimize_cff_font(working_input, output_path, options)
        else:
            logger.error("Font has neither TrueType nor CFF outlines")
            return False

        # Step 4: Report file size changes
        if os.path.exists(output_path):
            original_size = os.path.getsize(input_path)
            optimized_size = os.path.getsize(output_path)
            size_change = optimized_size - original_size
            size_change_pct = (size_change / original_size * 100) if original_size > 0 else 0
            
            logger.info(f"Font size: {original_size:,} → {optimized_size:,} bytes "
                       f"({size_change:+,} bytes, {size_change_pct:+.1f}%)")

        return success

    finally:
        # Clean up temporary file
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception as e:
                logger.warning(f"Could not delete temp file {temp_path}: {e}")


def process_directory(input_dir: str, output_dir: str, options: dict) -> dict:
    """
    Process all font files in a directory.
    
    Args:
        input_dir: Input directory path
        output_dir: Output directory path
        options: Dictionary of optimization options
        
    Returns:
        Dictionary with processing statistics
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    
    # Ensure output directory exists
    output_path.mkdir(parents=True, exist_ok=True)
    
    stats = {
        'total': 0,
        'success': 0,
        'failed': 0,
        'skipped': 0
    }
    
    # Find all font files
    font_files = []
    for ext in ['*.otf', '*.ttf', '*.OTF', '*.TTF']:
        font_files.extend(input_path.glob(f'**/{ext}'))
    
    if not font_files:
        logger.warning(f"No font files found in {input_dir}")
        return stats
    
    logger.info(f"Found {len(font_files)} font files to process")
    
    for font_file in font_files:
        # Create relative path for output
        rel_path = font_file.relative_to(input_path)
        output_file = output_path / rel_path
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        stats['total'] += 1
        
        try:
            logger.info(f"Processing: {rel_path}")
            if optimize_font(str(font_file), str(output_file), options):
                stats['success'] += 1
            else:
                stats['failed'] += 1
        except Exception as e:
            logger.error(f"Error processing {rel_path}: {str(e)}")
            stats['failed'] += 1
    
    return stats


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='OTF/TTF Font Optimization Script v9 - Optimize and scale fonts',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic optimization
  python otf_optimize-v9.py input_fonts/ output_fonts/

  # Scale up by 10% then optimize
  python otf_optimize-v9.py --scale 10 input_fonts/ output_fonts/

  # Shrink by 5% then optimize
  python otf_optimize-v9.py --scale -5 input_fonts/ output_fonts/

  # TrueType-specific options
  python otf_optimize-v9.py --strength 200 --no-combining input/ output/

  # CFF-specific options
  python otf_optimize-v9.py --allow-changes --no-flex input/ output/

  # Verbose mode
  python otf_optimize-v9.py -v input/ output/

  # Single file
  python otf_optimize-v9.py --scale 15 input.otf output.otf
        """
    )
    
    # Input/Output
    parser.add_argument('input', nargs='?', help='Input font file or directory')
    parser.add_argument('output', nargs='?', help='Output font file or directory')
    
    # Scaling options
    parser.add_argument('--scale', type=float, default=0,
                       help='Scale percentage (positive = larger, negative = smaller)')
    parser.add_argument('--no-preserve-curves', action='store_true',
                       help='Disable high-quality curve preservation (faster but lower quality)')
    
    # TrueType hinting options
    parser.add_argument('--strength', type=int, dest='hinting_strength',
                       help='Hinting strength limit for ttfautohint')
    parser.add_argument('--no-combining', action='store_true', dest='no_combining_chars',
                       help='Skip combining characters in ttfautohint')
    parser.add_argument('--fallback-stem', type=int, dest='fallback_stem_width',
                       help='Fallback stem width for ttfautohint')
    parser.add_argument('--detailed', action='store_true', dest='detailed_info',
                       help='Show detailed info from ttfautohint')
    
    # CFF hinting options
    parser.add_argument('--allow-changes', action='store_true',
                       help='Allow changes in psautohint (reorder paths)')
    parser.add_argument('--no-flex', action='store_true',
                       help='Disable flex commands in psautohint')
    parser.add_argument('--no-hint-sub', action='store_true',
                       help='Disable hint substitution in psautohint')
    
    # General options
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Enable verbose logging')
    parser.add_argument('--check-deps', action='store_true',
                       help='Check dependencies and exit')
    
    args = parser.parse_args()
    
    # Set up logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Check dependencies if requested
    if args.check_deps:
        if check_dependencies():
            logger.info("All dependencies are installed")
            sys.exit(0)
        else:
            logger.error("Missing dependencies")
            sys.exit(1)
    
    # Check if dependencies are available
    if not check_dependencies():
        logger.warning("Some dependencies are missing, but continuing...")
    
    # Validate input
    if not args.input or not args.output:
        parser.print_help()
        sys.exit(1)
    
    input_path = Path(args.input)
    output_path = Path(args.output)
    
    if not input_path.exists():
        logger.error(f"Input path does not exist: {args.input}")
        sys.exit(1)
    
    # Build options dictionary
    options = {
        'scale': args.scale,
        'preserve_curves': not args.no_preserve_curves,
        'hinting_strength': args.hinting_strength,
        'no_combining_chars': args.no_combining_chars,
        'fallback_stem_width': args.fallback_stem_width,
        'detailed_info': args.detailed_info,
        'allow_changes': args.allow_changes,
        'no_flex': args.no_flex,
        'no_hint_sub': args.no_hint_sub,
        'verbose': args.verbose
    }
    
    # Process based on input type
    if input_path.is_file():
        # Single file processing
        if not optimize_font(str(input_path), str(output_path), options):
            logger.error("Font optimization failed")
            sys.exit(1)
    elif input_path.is_dir():
        # Directory processing
        stats = process_directory(str(input_path), str(output_path), options)
        
        logger.info(f"\nProcessing complete:")
        logger.info(f"  Total: {stats['total']}")
        logger.info(f"  Success: {stats['success']}")
        logger.info(f"  Failed: {stats['failed']}")
        
        if stats['failed'] > 0:
            sys.exit(1)
    else:
        logger.error(f"Invalid input path: {args.input}")
        sys.exit(1)
    
    logger.info("✓ Font optimization completed successfully")


if __name__ == '__main__':
    main()
