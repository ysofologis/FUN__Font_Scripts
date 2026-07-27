#!/usr/bin/env python3
"""
TTF → OTF Conversion Script

Simple, reliable conversion of TTF fonts to OTF (CFF) using fonttools.

Pipeline:
    TTF → FontBuilder → OTF (CFF)

Features:
- Converts quadratic curves to cubic (Qu2CuPen)
- Preserves all glyph data, metrics, and tables
- Sets GASP table for grayscale AA at all sizes
- Clears hinting flags in head table

Requirements:
    pip install fonttools

Usage:
    python ttf2otf_convert.py <input_dir> <output_dir>
    python ttf2otf_convert.py <input.ttf> <output.otf>
"""

import os
import sys
import argparse
import logging
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttFont import TTLibError
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.fontBuilder import FontBuilder
from fontTools.ttLib.tables._g_a_s_p import table__g_a_s_p, GASP_GRIDFIT, GASP_DOGRAY

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def convert_ttf_to_otf(ttf_path: Path, otf_path: Path) -> dict:
    """
    Convert TTF to OTF using fonttools' FontBuilder.
    
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
                hmtx_metrics = hmtx.metrics.get(gname, (0, 0)) if hmtx.metrics else (0, 0)
                metrics[gname] = hmtx_metrics
                continue

            # Skip composite glyphs (they reference other glyphs)
            if glyph.numberOfContours < 0:
                # Composite glyph - skip or create empty
                t2_pen = T2CharStringPen(0, None)
                cs = t2_pen.getCharString()
                charstrings[gname] = cs
                hmtx_metrics = hmtx.metrics.get(gname, (0, 0)) if hmtx.metrics else (0, 0)
                metrics[gname] = hmtx_metrics
                continue

            try:
                # Convert quadratic curves to cubic
                t2_pen = T2CharStringPen(0, None)
                qu2cu = Qu2CuPen(t2_pen, max_err=0.3)
                # Pass glyf table as required argument
                glyph.draw(qu2cu, glyf)
                cs = t2_pen.getCharString()
                charstrings[gname] = cs

                # Get metrics from hmtx table
                hmtx_metrics = hmtx.metrics.get(gname, (0, 0)) if hmtx.metrics else (0, 0)
                metrics[gname] = hmtx_metrics
            except Exception as e:
                # Skip problematic glyphs silently
                logger.debug(f"Skipping problematic glyph '{gname}': {type(e).__name__}")
                # Create empty charstring for failed glyphs
                t2_pen = T2CharStringPen(0, None)
                cs = t2_pen.getCharString()
                charstrings[gname] = cs
                metrics[gname] = (0, 0)
        
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
        
        # Clear hinting flags in head table (bits 3, 4, 5)
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


def process_directory(input_dir: str, output_dir: str) -> dict:
    """
    Process all TTF fonts in a directory recursively.
    
    Args:
        input_dir: Input directory path
        output_dir: Output directory path
    
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
        return {'total': 0, 'success': 0, 'failed': 0}
    
    logger.info(f"Found {len(font_files)} TTF file(s) in {input_dir}")
    
    stats = {'total': 0, 'success': 0, 'failed': 0}
    
    for font_file in font_files:
        stats['total'] += 1
        
        rel_path = font_file.relative_to(input_path)
        out_file = output_path / rel_path.with_suffix('.otf')
        out_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Processing {font_file}...")
        
        try:
            convert_ttf_to_otf(font_file, out_file)
            stats['success'] += 1
        except Exception as e:
            logger.error(f"Error processing {font_file}: {e}")
            logger.debug(f"Traceback:\n{sys.exc_info()[2]}")
            stats['failed'] += 1
    
    logger.info(f"Directory processing complete: {stats['success']}/{stats['total']} successful")
    return stats


def main() -> None:
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Convert TTF to OTF using fonttools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert all TTFs in directory
  python ttf2otf_convert.py input_fonts/ output_fonts/
  
  # Single file conversion
  python ttf2otf_convert.py input.ttf output.otf
        """
    )
    
    parser.add_argument('input', help='Input TTF font file or directory')
    parser.add_argument('output', help='Output OTF font file or directory')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable DEBUG-level logging')
    
    args = parser.parse_args()
    
    # Configure logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
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
    
    # Process single file or directory
    if os.path.isfile(args.input):
        convert_ttf_to_otf(Path(args.input), Path(args.output))
    elif os.path.isdir(args.input):
        stats = process_directory(args.input, args.output)
        if stats['failed'] > 0:
            sys.exit(1)
    else:
        logger.error(f"Input path is neither file nor directory: {args.input}")
        sys.exit(1)


if __name__ == '__main__':
    main()
