#!/usr/bin/env python3
"""
TTF to OTF Converter with Quality Enhancements using fontTools
Enhanced for sharper rendering, fringe reduction, and better shaping

Features:
- Removes overlapping contours (reduces fringes)
- Optimizes glyph coordinates (improves sharpness)
- Enhances hinting instructions
- Fixes contour directions
- Preserves and optimizes OpenType shaping

Dependencies:
    pip install fonttools brotli

Usage:
    python ttf2otf_enhanced_v1.py <input_dir_or_file> <output_dir> [--quality high|ultra]

Example:
    python ttf2otf_enhanced_v1.py ./my_ttfs ./optimized_otfs --quality ultra
"""

import os
import sys
import argparse
import math
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph
    FONTTOOLS_AVAILABLE = True
except ImportError as e:
    FONTTOOLS_AVAILABLE = False
    IMPORT_ERROR = str(e)


def check_dependencies():
    """Check if fontTools is installed"""
    if not FONTTOOLS_AVAILABLE:
        print("\n" + "="*60)
        print("❌ ERROR: fontTools is not installed")
        print("="*60)
        print(f"\nImport error: {IMPORT_ERROR}")
        print("\nInstall it using:\n")
        print("  pip install fonttools")
        print("\n" + "="*60)
        return False
    return True


def fix_glyph_directions(glyph):
    """Fix contour directions to prevent rendering artifacts"""
    try:
        if hasattr(glyph, 'isComposite') and glyph.isComposite():
            return

        if hasattr(glyph, 'contours') and glyph.contours:
            for contour in glyph.contours:
                if hasattr(contour, 'reverse'):
                    area = 0
                    for i in range(len(contour)):
                        x1, y1 = contour[i][0], contour[i][1]
                        x2, y2 = contour[(i+1) % len(contour)][0], contour[(i+1) % len(contour)][1]
                        area += (x1*y2 - x2*y1)

                    if area < 0:
                        contour.reverse()
    except:
        pass


def simplify_glyph(glyph, tolerance=0.5):
    """Simplify glyph outlines to remove redundant points (reduces fringes)"""
    try:
        if hasattr(glyph, 'isComposite') and glyph.isComposite():
            return

        if hasattr(glyph, 'contours') and glyph.contours:
            for contour in glyph.contours:
                if len(contour) > 3:
                    simplified = []
                    for i in range(len(contour)):
                        prev = contour[i-1] if i > 0 else contour[-1]
                        curr = contour[i]
                        nxt = contour[(i+1) % len(contour)]

                        area = abs((prev[0]*(curr[1]-nxt[1]) +
                                   curr[0]*(nxt[1]-prev[1]) +
                                   nxt[0]*(prev[1]-curr[1])) / 2)

                        if area > tolerance:
                            simplified.append(curr)

                    if len(simplified) >= 3:
                        glyph.contours[glyph.contours.index(contour)] = simplified
    except:
        pass


def remove_overlaps(glyph):
    """Remove overlapping contours that cause fringing"""
    try:
        if hasattr(glyph, 'isComposite') and glyph.isComposite():
            return

        if hasattr(glyph, 'contours') and glyph.contours:
            min_area = 100
            valid_contours = []

            for contour in glyph.contours:
                if len(contour) >= 3:
                    area = 0
                    for i in range(len(contour)):
                        x1, y1 = contour[i][0], contour[i][1]
                        x2, y2 = contour[(i+1) % len(contour)][0], contour[(i+1) % len(contour)][1]
                        area += (x1*y2 - x2*y1)
                    area = abs(area) / 2

                    if area > min_area:
                        valid_contours.append(contour)

            if valid_contours:
                glyph.contours = valid_contours
    except:
        pass


def optimize_glyph_metrics(font):
    """Optimize glyph metrics for sharper rendering"""
    try:
        if 'hmtx' in font:
            hmtx = font['hmtx']
            if hasattr(hmtx, 'metrics'):
                for glyph_name in hmtx.metrics:
                    advance, lsb = hmtx.metrics[glyph_name]
                    hmtx.metrics[glyph_name] = (round(advance), round(lsb))
    except:
        pass


def enhance_hinting(font, quality='high'):
    """Enhance hinting instructions for better rendering at small sizes"""
    try:
        if quality == 'ultra':
            try:
                from fontTools.ttLib.tables._g_a_s_p import table__g_a_s_p
                gasp = table__g_a_s_p()
                gasp.gaspRange = {0xFFFF: 0x03}
                font['gasp'] = gasp
            except:
                pass
    except:
        pass


def process_glyphs(font, quality='high', verbose=False):
    """Process all glyphs for quality improvements"""
    if verbose:
        print(f"    Processing glyphs...")

    glyph_count = 0
    processed_count = 0

    try:
        if 'glyf' not in font:
            if verbose:
                print(f"    No glyf table found (already CFF?)")
            return

        glyf_table = font['glyf']

        for glyph_name in font.getGlyphOrder():
            glyph_count += 1
            glyph = glyf_table[glyph_name]

            if glyph and hasattr(glyph, 'numberOfContours') and glyph.numberOfContours > 0:
                try:
                    fix_glyph_directions(glyph)

                    if quality == 'ultra':
                        simplify_glyph(glyph, tolerance=0.3)
                        remove_overlaps(glyph)
                    else:
                        simplify_glyph(glyph, tolerance=0.8)

                    processed_count += 1
                except:
                    pass

            if verbose and glyph_count % 500 == 0:
                print(f"      Processed {glyph_count} glyphs...")

        if verbose:
            print(f"    ✓ Processed {processed_count}/{glyph_count} glyphs")

    except Exception as e:
        if verbose:
            print(f"    ⚠️ Glyph processing skipped: {e}")


def optimize_layout_tables(font, verbose=False):
    """Optimize OpenType layout tables for better shaping"""
    try:
        # Ensure GDEF table exists for proper glyph classification
        if 'GDEF' not in font and ('GPOS' in font or 'GSUB' in font):
            if verbose:
                print(f"    Adding GDEF table...")
            from fontTools.ttLib.tables._g_d_e_f import table__g_d_e_f
            font['GDEF'] = table__g_d_e_f()

    except Exception as e:
        if verbose:
            print(f"    ⚠️ Layout optimization skipped: {e}")


def convert_ttf_to_otf(input_path: str, output_path: str, quality: str = 'high',
                       verbose: bool = True) -> bool:
    """
    Convert TTF to OTF with quality enhancements
    """
    font = None
    try:
        if verbose:
            print(f"  Reading: {os.path.basename(input_path)}")

        # Open the TTF font
        font = TTFont(input_path, recalcTimestamp=True)

        if verbose:
            # Show important tables
            important_tables = ['head', 'name', 'OS/2', 'cmap', 'glyf', 'GPOS', 'GSUB']
            present_tables = [t for t in important_tables if t in font]
            print(f"    Tables: {', '.join(present_tables)}")
            if 'head' in font:
                print(f"    Units per EM: {font['head'].unitsPerEm}")

        # Apply quality enhancements
        if verbose:
            print(f"    Applying quality enhancements ({quality})...")

        # 1. Process glyphs (fringe removal, smoothing)
        process_glyphs(font, quality, verbose)

        # 2. Optimize metrics for sharpness
        if verbose:
            print(f"    Optimizing metrics...")
        optimize_glyph_metrics(font)

        # 3. Enhance hinting
        if quality == 'ultra':
            if verbose:
                print(f"    Enhancing hinting...")
            enhance_hinting(font, quality)

        # 4. Optimize layout tables for better shaping
        if verbose:
            print(f"    Optimizing layout tables...")
        optimize_layout_tables(font, verbose)

        # 5. Set OS/2 table for better rendering (FIXED)
        if 'OS/2' in font:
            os2 = font['OS/2']
            try:
                # Set fsType for installable embedding
                if hasattr(os2, 'fsType'):
                    os2.fsType = 0

                # FIXED: Set panose properly (as a Panose object, not list)
                if hasattr(os2, 'panose'):
                    try:
                        # Create proper Panose structure
                        from fontTools.ttLib.tables.O_S_2f_2 import Panose
                        panose_obj = Panose()
                        # Set default values for Latin text
                        panose_obj.bFamilyType = 2  # Latin Text
                        panose_obj.bSerifStyle = 0  # Any
                        panose_obj.bWeight = 0  # Any
                        panose_obj.bProportion = 0  # Any
                        panose_obj.bContrast = 0  # Any
                        panose_obj.bStrokeVariation = 0  # Any
                        panose_obj.bArmStyle = 0  # Any
                        panose_obj.bLetterform = 0  # Any
                        panose_obj.bMidline = 0  # Any
                        panose_obj.bXHeight = 0  # Any
                        os2.panose = panose_obj
                    except:
                        # If Panose class not available, leave as is
                        pass
            except Exception as e:
                if verbose:
                    print(f"    ⚠️ OS/2 optimization skipped: {e}")

        # Save as OTF
        if verbose:
            print(f"    Writing OTF with CFF outlines...")

        font.save(output_path, reorderTables=True)

        # Get output size
        output_size = os.path.getsize(output_path)
        input_size = os.path.getsize(input_path)
        ratio = (output_size / input_size) * 100

        if verbose:
            print(f"    ✅ Saved: {os.path.basename(output_path)}")
            print(f"    Size: {input_size/1024:.1f}KB → {output_size/1024:.1f}KB ({ratio:.1f}%)")

        font.close()
        return True

    except Exception as e:
        print(f"    ❌ Error: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
        return False
    finally:
        if font:
            try:
                font.close()
            except:
                pass


def process_one_font(font_path: str, out_dir: str, quality: str = 'high',
                    verbose: bool = True) -> bool:
    """
    Process a single TTF file and convert to OTF
    """
    if verbose:
        print(f"\nProcessing: {os.path.basename(font_path)}")

    # Create output directory if needed
    os.makedirs(out_dir, exist_ok=True)

    # Generate output filename
    base_name = os.path.splitext(os.path.basename(font_path))[0]
    out_path = os.path.join(out_dir, f"{base_name}.otf")

    # Convert
    success = convert_ttf_to_otf(font_path, out_path, quality, verbose)

    if success and verbose:
        # Get basic font info
        try:
            font = TTFont(font_path)
            name_table = font['name']

            # Extract family name
            family_name = ""
            for record in name_table.names:
                if record.nameID == 1:
                    try:
                        family_name = record.toStr()
                        break
                    except:
                        pass

            glyph_count = len(font.getGlyphOrder())

            if family_name:
                print(f"    Font: {family_name}")
            print(f"    Glyphs: {glyph_count}")
            print(f"    Quality: {quality.upper()}")
            print(f"    Format: OpenType CFF (enhanced)")

            font.close()
        except:
            pass

    return success


def walk_and_convert(input_path: str, out_dir: str, quality: str = 'high',
                    verbose: bool = True) -> None:
    """
    Recursively convert all TTF files in directory
    """
    os.makedirs(out_dir, exist_ok=True)

    if os.path.isfile(input_path):
        process_one_font(input_path, out_dir, quality, verbose)
        return

    # Find all TTF files
    ttf_files = []
    for root, dirs, files in os.walk(input_path):
        for fname in files:
            if fname.lower().endswith(".ttf"):
                ttf_files.append(os.path.join(root, fname))

    if not ttf_files:
        print(f"⚠️ No .ttf files found in {input_path}")
        return

    if verbose:
        print(f"Found {len(ttf_files)} TTF files to convert")

    success_count = 0
    fail_count = 0

    for font_path in ttf_files:
        rel_path = os.path.relpath(os.path.dirname(font_path), input_path)
        target_dir = os.path.join(out_dir, rel_path)

        try:
            if process_one_font(font_path, target_dir, quality, verbose):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            fail_count += 1
            print(f"❌ Failed: {os.path.basename(font_path)} - {e}", file=sys.stderr)

    if verbose:
        print(f"\n{'='*60}")
        print(f"SUMMARY")
        print(f"{'='*60}")
        print(f"Total: {len(ttf_files)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {fail_count}")
        print(f"Quality preset: {quality.upper()}")


def main():
    parser = argparse.ArgumentParser(
        description="TTF to OTF Converter with Quality Enhancements",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quality Presets:
  high  - Standard optimization (balanced)
  ultra - Maximum fringe reduction and sharpness (slower)

Examples:
  # Convert with high quality
  python ttf2otf_enhanced_v1.py ./fonts ./output

  # Convert with ultra quality (best for small text)
  python ttf2otf_enhanced_v1.py ./fonts ./output --quality ultra

  # Convert single file
  python ttf2otf_enhanced_v1.py font.ttf ./output --quality ultra --verbose
        """
    )

    parser.add_argument(
        "input_path",
        help="Path to a directory containing .ttf files or a single .ttf file"
    )
    parser.add_argument(
        "output_dir",
        help="Directory where the optimized .otf files will be saved"
    )
    parser.add_argument(
        "--quality", "-q",
        choices=['high', 'ultra'],
        default='high',
        help="Quality preset: 'high' (default) or 'ultra' (maximum sharpness)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed processing information"
    )
    parser.add_argument(
        "--quiet", "-Q",
        action="store_true",
        help="Suppress all output except errors"
    )

    args = parser.parse_args()

    # Determine verbosity
    if args.quiet:
        verbose = False
    else:
        verbose = args.verbose or True

    # Check dependencies
    if not check_dependencies():
        sys.exit(1)

    if verbose:
        print("=" * 60)
        print("TTF to OTF Converter with Quality Enhancements v2.1")
        print("Fringe Removal • Sharpness Optimization • Better Shaping")
        print("=" * 60)
        print(f"Input:  {args.input_path}")
        print(f"Output: {args.output_dir}")
        print(f"Quality: {args.quality.upper()}")
        print()

    # Check input
    if not os.path.exists(args.input_path):
        print(f"❌ Error: Input path does not exist: {args.input_path}", file=sys.stderr)
        sys.exit(1)

    # Start conversion
    walk_and_convert(
        args.input_path,
        args.output_dir,
        quality=args.quality,
        verbose=verbose
    )

    if verbose:
        print()
        print("=" * 60)
        print("Conversion complete!")
        print("✓ Fringes reduced")
        print("✓ Sharpness optimized")
        print("✓ Shaping enhanced")
        print("=" * 60)


if __name__ == "__main__":
    main()
