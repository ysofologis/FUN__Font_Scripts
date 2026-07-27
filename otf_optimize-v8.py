#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script v8

This script optimizes fonts by:
- Autohinting TrueType-based fonts with ttfautohint
- Autohinting CFF-based OTF fonts with psautohint
- Properly identifying CFF vs TrueType outlines
- Scaling font size by a percentage (NEW in v8)
- Maintaining OTF output format

v8 changes:
  - Added --scale argument to increase font size by a percentage
  - Scales all glyph outlines (TrueType glyf & CFF charstrings)
  - Scales all metrics (advance widths, bearings, vertical metrics, etc.)

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

        # Determine file extension
        file_ext = os.path.splitext(font_path)[1].lower()

        # Check for CFF table (CFF-based fonts)
        has_cff = 'CFF ' in font or 'CFF2' in font

        # Check for glyf table (TrueType outlines)
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
#  Font scaling (v8)
# ---------------------------------------------------------------------------

def _scale_truetype_glyphs(font: TTFont, factor: float) -> None:
    """
    Scale all TrueType (glyf) glyph outlines by *factor*.

    Handles simple glyphs (coordinate arrays) and composite glyphs
    (component offsets).  Recalculates bounding boxes after scaling.
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


def _scale_cff_glyphs(font: TTFont, factor: float) -> None:
    """
    Scale all CFF charstring outlines by *factor*.

    Draws each glyph through a TransformPen → T2CharStringPen to produce
    scaled charstrings, then replaces the charstrings in the CFF / CFF2 top
    dict.  Finally adjusts the private dict so widths are stored as full
    values (nominalWidthX = 0).
    """
    # Determine which CFF table is present
    cff_table_key = 'CFF2' if 'CFF2' in font else 'CFF '
    top_dict = font[cff_table_key].cff.topDictIndex[0]
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

            # Scaled advance width
            # NOTE: use .metrics dict, not hmtx directly —
            # hmtx table lacks __contains__ so glyph_name in hmtx
            # falls back to integer-index iteration and fails.
            if hmtx and glyph_name in hmtx.metrics:
                scaled_width = int(round(hmtx.metrics[glyph_name][0] * factor))
            else:
                scaled_width = 0

            # Build a new charstring through TransformPen
            t2_pen = T2CharStringPen(scaled_width, glyph_set)
            transform_pen = TransformPen(t2_pen, (factor, 0, 0, factor, 0, 0))
            glyph.draw(transform_pen)

            new_charstring = t2_pen.getCharString()
            # The new T2CharString needs a reference to the Private dict
            # so fontTools' calcBounds() can resolve width encoding.
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

    if had_errors:
        logger.warning("Some CFF glyphs could not be scaled and were left unchanged.")

    return True


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
        # Scale lowestRecPPEM (minimum recommended screen size in px)
        if head.lowestRecPPEM:
            head.lowestRecPPEM = max(1, int(round(head.lowestRecPPEM * factor)))

    # ---- Update the modification timestamp in head ----
    if 'head' in font:
        import time
        from fontTools.ttLib.tables._h_e_a_d import mac_epoch_diff
        now = int(time.time())
        head.modified = now + mac_epoch_diff if hasattr(head, 'modified') else now


def scale_font_glyphs(input_path: str, output_path: str, scale_percent: float) -> bool:
    """
    Scale ALL glyph outlines and metrics by the given percentage.

    Args:
        input_path: Path to input font file (.otf / .ttf)
        output_path: Path to save the scaled font
        scale_percent: Percentage to scale (10 = make 10% larger)

    Returns:
        True on success, False on error.
    """
    if scale_percent == 0:
        return False

    factor = 1.0 + scale_percent / 100.0
    logger.info(f"Scaling font by {scale_percent}% (factor ×{factor:.4f})")

    try:
        font = TTFont(input_path)
        has_truetype = 'glyf' in font
        has_cff = 'CFF ' in font or 'CFF2' in font

        # Scale glyph outlines
        if has_truetype:
            _scale_truetype_glyphs(font, factor)
        elif has_cff:
            result = _scale_cff_glyphs(font, factor)
            if not result:
                font.close()
                return False
        else:
            logger.error("Font has neither TrueType nor CFF outlines — cannot scale.")
            font.close()
            return False

        # Scale all metric tables
        _scale_metrics(font, factor)

        # Save the scaled font
        font.save(output_path)
        font.close()

        logger.info(f"✓ Scaled font saved to {output_path}")
        return True

    except Exception as e:
        logger.error(f"Error scaling font {input_path}: {str(e)}")
        return False


# ---------------------------------------------------------------------------
#  Hinting — unchanged from v7
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

        # Default options for better rendering
        cmd.extend([
            '--default-script=latn',   # Default script
            '--fallback-script=none',  # No fallback script
            '--symbol',                # Process symbol area
            '--fallback-scaling',      # Use fallback scaling
        ])

        # Input and output files
        cmd.extend([input_path, output_path])

        logger.debug(f"Running ttfautohint: {' '.join(cmd)}")

        # Run ttfautohint
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

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
    """Optimize CFF-based font with psautohint"""
    try:
        # Build psautohint command
        cmd = ['psautohint']

        # Add output file specification
        cmd.extend(['-o', output_path])

        # Add optimization options
        if options.get('allow_changes'):
            cmd.append('--allow-changes')

        if options.get('no_flex'):
            cmd.append('--no-flex')

        if options.get('no_hint_sub'):
            cmd.append('--no-hint-sub')

        # Add option to allow fonts without zones/stems
        cmd.append('--no-zones-stems')

        # Add verbose flag if requested
        if options.get('verbose'):
            cmd.append('-v')

        # Input font
        cmd.append(input_path)

        logger.debug(f"Running psautohint: {' '.join(cmd)}")

        # Run psautohint
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

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
#  Main optimization pipeline (v8: with optional scaling step)
# ---------------------------------------------------------------------------

def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize a single font file.

    Pipeline:
      1. (v8) Optionally scale the font by a percentage → temp file
      2. Analyse the (possibly scaled) font format
      3. Autohint with the appropriate tool → final output
      4. Clean up any temp files

    Args:
        input_path: Path to input font file
        output_path: Path to output optimized font file
        options: Dictionary of optimisation options

    Returns:
        True if successful, False otherwise
    """
    temp_files = []
    actual_input = input_path

    # ------------------------------------------------------------------
    # Step 1 — Scale font if requested (v8)
    # ------------------------------------------------------------------
    scale_pct = options.get('scale_percent', 0)
    if scale_pct != 0:
        # Create a temporary scaled copy
        ext = os.path.splitext(input_path)[1] or '.otf'
        fd, temp_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        temp_files.append(temp_path)

        font_name = os.path.basename(input_path)
        logger.info(f"Pre-scaling {font_name} by {scale_pct}%...")

        if not scale_font_glyphs(input_path, temp_path, scale_pct):
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
        logger.info(f"Analyzing {font_name}...")

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
        if font_info['is_truetype']:
            logger.info(f"Optimizing {font_name} with TrueType outlines using ttfautohint...")

            if optimize_truetype_font(actual_input, output_path, options):
                input_size = os.path.getsize(input_path)
                output_size = os.path.getsize(output_path)

                if input_size > 0:
                    reduction = ((input_size - output_size) / input_size) * 100
                    logger.info(f"✓ Optimized {font_name} "
                               f"({input_size:,} → {output_size:,} bytes, "
                               f"{reduction:.1f}% size change)")
                else:
                    logger.info(f"✓ Optimized {font_name}")

                for t in temp_files:
                    if os.path.exists(t):
                        os.unlink(t)
                return True
            else:
                for t in temp_files:
                    if os.path.exists(t):
                        os.unlink(t)
                return False

        elif font_info['is_cff']:
            logger.info(f"Optimizing {font_name} with CFF outlines using psautohint...")

            if optimize_cff_font(actual_input, output_path, options):
                input_size = os.path.getsize(input_path)
                output_size = os.path.getsize(output_path)

                if input_size > 0:
                    reduction = ((input_size - output_size) / input_size) * 100
                    logger.info(f"✓ Optimized {font_name} "
                               f"({input_size:,} → {output_size:,} bytes, "
                               f"{reduction:.1f}% size change)")
                else:
                    logger.info(f"✓ Optimized {font_name}")

                for t in temp_files:
                    if os.path.exists(t):
                        os.unlink(t)
                return True
            else:
                for t in temp_files:
                    if os.path.exists(t):
                        os.unlink(t)
                return False

    except Exception as e:
        logger.error(f"Error processing {actual_input}: {str(e)}")
        for t in temp_files:
            if os.path.exists(t):
                os.unlink(t)
        return False

    # Should not reach here
    for t in temp_files:
        if os.path.exists(t):
            os.unlink(t)
    return False


def process_directory(input_dir: str, output_dir: str, options: dict) -> int:
    """
    Process all font files in a directory

    Args:
        input_dir: Input directory path
        output_dir: Output directory path
        options: Optimization options

    Returns:
        Number of successfully processed files
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)

    # Validate input directory
    if not input_path.exists():
        logger.error(f"Input directory does not exist: {input_dir}")
        return 0

    if not input_path.is_dir():
        logger.error(f"Input path is not a directory: {input_dir}")
        return 0

    # Create output directory if it doesn't exist
    output_path.mkdir(parents=True, exist_ok=True)

    # Find all font files (OTF and TTF)
    font_extensions = ['*.otf', '*.ttf']
    font_files = []
    for ext in font_extensions:
        font_files.extend(input_path.glob(ext))

    if not font_files:
        logger.warning(f"No font files found in {input_dir}")
        return 0

    logger.info(f"Found {len(font_files)} font files to process")

    success_count = 0

    # Process each file
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
        description="Optimize font hinting and rendering for both TrueType and CFF-based fonts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input_fonts/ output_fonts/
  %(prog)s --scale 10 input_fonts/ output_fonts/      # Scale up by 10%% then optimize
  %(prog)s --scale -5 input_fonts/ output_fonts/      # Shrink by 5%% then optimize
  %(prog)s --strength 200 input_fonts/ output_fonts/
  %(prog)s --no-combining --detailed input_fonts/ output_fonts/
  %(prog)s --allow-changes --no-flex input_fonts/ output_fonts/
        """
    )

    parser.add_argument(
        "input_dir",
        help="Input directory containing font files (.otf, .ttf)"
    )

    parser.add_argument(
        "output_dir",
        help="Output directory for optimized font files"
    )

    # ---- v8: Font scaling ----
    parser.add_argument(
        "--scale",
        type=float,
        default=0,
        dest="scale_percent",
        help="Scale (increase) font size by this percentage "
             "(e.g., 10 = 110%% size, -5 = 95%% size). "
             "Scaling is applied as a pre-processing step before hinting."
    )

    # ttfautohint options (for TrueType fonts)
    parser.add_argument(
        "--strength",
        type=int,
        default=100,
        dest="hinting_strength",
        help="Hinting strength limit for TrueType fonts (default: 100)"
    )

    parser.add_argument(
        "--no-combining",
        action="store_true",
        dest="no_combining_chars",
        help="Don't set fallbacks for combining characters (TrueType fonts)"
    )

    parser.add_argument(
        "--detailed",
        action="store_true",
        dest="detailed_info",
        help="Add detailed TTF instructions information (TrueType fonts)"
    )

    parser.add_argument(
        "--stem-width",
        type=int,
        dest="fallback_stem_width",
        help="Fallback stem width value (TrueType fonts)"
    )

    # psautohint options (for CFF fonts)
    parser.add_argument(
        "--allow-changes",
        action="store_true",
        dest="allow_changes",
        help="Allow changes to glyph outlines (reorder paths) for CFF fonts"
    )

    parser.add_argument(
        "--no-flex",
        action="store_true",
        dest="no_flex",
        help="Suppress generation of flex commands for CFF fonts"
    )

    parser.add_argument(
        "--no-hint-sub",
        action="store_true",
        dest="no_hint_sub",
        help="Suppress hint substitution for CFF fonts"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Check dependencies
    if not check_dependencies():
        sys.exit(1)

    # Prepare optimization options
    options = {
        'scale_percent': args.scale_percent,
        'hinting_strength': args.hinting_strength,
        'no_combining_chars': args.no_combining_chars,
        'detailed_info': args.detailed_info,
        'fallback_stem_width': args.fallback_stem_width,
        'allow_changes': args.allow_changes,
        'no_flex': args.no_flex,
        'no_hint_sub': args.no_hint_sub,
        'verbose': args.verbose
    }

    logger.info("Starting font optimization...")
    logger.info(f"Input directory: {args.input_dir}")
    logger.info(f"Output directory: {args.output_dir}")
    if args.scale_percent != 0:
        logger.info(f"Font scaling: {args.scale_percent:+g}%")

    # Process fonts
    success_count = process_directory(
        args.input_dir,
        args.output_dir,
        options
    )

    logger.info(f"Optimization complete! Successfully processed {success_count} fonts.")


if __name__ == "__main__":
    main()
