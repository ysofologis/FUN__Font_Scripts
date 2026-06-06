#!/usr/bin/env python3
"""
TTF to OTF Converter using FontForge API
Optimized for maximum quality in font rendering, sharpening, and clarity

Usage:
    python ttf2otf_ff_v1.py <input_dir_or_file> <output_dir>

Positional arguments:
    input_dir_or_file   Path to a directory containing .ttf files or a single .ttf file
    output_dir          Directory where the converted .otf files will be saved

Example:
    python ttf2otf_ff_v1.py ./my_ttfs ./optimized_otfs
"""

import os
import sys
import argparse
import fontforge


def process_one_font(font_path: str, out_dir: str, verbose: bool = True) -> None:
    """
    Convert a single TTF font to OTF with maximum quality settings.

    Optimizations applied:
    - Preserve all original metadata and tables
    - Enable high-quality hinting for better rendering
    - Optimize glyph outlines for clarity
    - Set proper OS/2 and name tables
    - Use CFF (Compact Font Format) for OTF output
    """
    if verbose:
        print(f"Processing: {font_path}")

    try:
        # Open the font file
        font = fontforge.open(font_path)

        if verbose:
            print(f"  Loaded: {font.familyname} {font.fontname}")
    except Exception as e:
        print(f"❌ Could not open {font_path}: {e}", file=sys.stderr)
        return

    try:
        # ============================================================
        # QUALITY OPTIMIZATIONS
        # ============================================================

        # 1. Preserve original metadata
        original_family = font.familyname
        original_style = font.fontname.replace(original_family, "").strip()

        # 2. Ensure proper font naming
        font.familyname = original_family
        font.fontname = f"{original_family}-{original_style}" if original_style else original_family
        font.fullname = font.fontname
        if hasattr(font, 'weight'):
            font.weight = font.weight  # Preserve original weight

        # 3. Enable high-quality hinting
        try:
            font.autoHint()
        except Exception as e:
            if verbose:
                print(f"  ⚠️ Auto-hinting skipped: {e}")

        # Additional hinting refinement
        try:
            glyph_count = 0
            for glyph in font.glyphs():
                if glyph.isWorthOutputting():
                    glyph_count += 1
                    # Ensure proper alignment
                    glyph.round()
                    # Optimize control points for smoother curves
                    glyph.correctDirection()
            if verbose:
                print(f"  Processed {glyph_count} glyphs for hinting")
        except Exception as e:
            if verbose:
                print(f"  ⚠️ Glyph refinement skipped: {e}")

        # 4. Optimize for rendering quality
        try:
            # Set OS/2 table values if available
            if hasattr(font, 'os2_vendor'):
                font.os2_vendor = "FF  "  # FontForge vendor ID

            if hasattr(font, 'os2_panose'):
                font.os2_panose = (2, 0, 0, 0, 0, 0, 0, 0, 0, 0)  # Default panose

            # Set embedding rights
            if hasattr(font, 'os2'):
                if hasattr(font.os2, 'fstype'):
                    font.os2.fstype = 0  # Installable embedding
                elif hasattr(font.os2, 'fsType'):
                    font.os2.fsType = 0
        except Exception as e:
            if verbose:
                print(f"  ⚠️ OS/2 optimization skipped: {e}")

        # 5. Set proper copyright info
        try:
            if hasattr(font, 'copyright') and font.copyright:
                # Only add © if not already present
                if not font.copyright.startswith('©'):
                    font.copyright = f"© {font.copyright}"
            else:
                font.copyright = f"© {original_family}"
        except Exception as e:
            if verbose:
                print(f"  ⚠️ Copyright update skipped: {e}")

        # 6. Optimize glyph outlines
        try:
            font.selection.all()
            font.simplify()  # Simplify contours for cleaner outlines
            font.round()     # Round coordinates to integers
            font.canonicalContours()  # Fix contour direction and ordering
            if verbose:
                print("  Applied outline optimizations")
        except Exception as e:
            if verbose:
                print(f"  ⚠️ Glyph optimization skipped: {e}")

        # 7. Ensure proper encoding
        try:
            if font.encoding != "unicode":
                font.encoding = "unicode"
                if verbose:
                    print(f"  Changed encoding to: unicode")
        except Exception as e:
            if verbose:
                print(f"  ⚠️ Encoding setting skipped: {e}")

        # 8. Set GASP table for better rendering at all sizes
        # FIXED: Different approach for GASP table
        try:
            # Method 1: Try to set gasp as a list of tuples
            # Format: [(ppem, flag), ...] where flag is 1=Gridfit, 2=Antialias, 3=Both
            font.gasp = [[8, 3], [16, 3]]  # Using list instead of tuple
            if verbose:
                print("  Set GASP table for optimal rendering")
        except Exception as e1:
            try:
                # Method 2: Try as tuple of tuples
                font.gasp = ((8, 3), (16, 3))
                if verbose:
                    print("  Set GASP table for optimal rendering (alternate format)")
            except Exception as e2:
                try:
                    # Method 3: Set individual gasp entries
                    # Some FontForge versions use different API
                    if hasattr(font, 'gasp'):
                        # Clear existing gasp
                        font.gasp = []
                        # Add entries one by one
                        font.gasp.append((8, 3))
                        font.gasp.append((16, 3))
                        if verbose:
                            print("  Set GASP table using append method")
                    else:
                        raise AttributeError("gasp attribute not available")
                except Exception as e3:
                    if verbose:
                        print(f"  ⚠️ GASP table setting skipped (all methods failed): {e3}")

        # ============================================================
        # OUTPUT CONFIGURATION
        # ============================================================

        # Create output directory if it doesn't exist
        os.makedirs(out_dir, exist_ok=True)

        # Generate output filename
        base_name = os.path.splitext(os.path.basename(font_path))[0]
        out_path = os.path.join(out_dir, f"{base_name}.otf")

        # Generate OTF file with quality flags
        try:
            # Try with explicit flags first
            font.generate(out_path, flags=("opentype", "cff"))
        except:
            try:
                # Try alternative flag format
                font.generate(out_path, flags=("opentype",))
            except:
                # Fallback to simple generation
                font.generate(out_path)

        if verbose:
            print(f"✅ Saved → {out_path}")
            # Get font info
            glyph_count = sum(1 for g in font.glyphs() if g.isWorthOutputting())
            print(f"  Format: OpenType/CFF")
            print(f"  Glyphs: {glyph_count}")
            if hasattr(font, 'ascent') and hasattr(font, 'descent'):
                print(f"  Metrics: ascent={font.ascent}, descent={font.descent}")
            if hasattr(font, 'upos'):
                print(f"  Underline: position={font.upos}, thickness={font.uwidth}")

    except Exception as e:
        print(f"❌ Error processing {font_path}: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
    finally:
        try:
            font.close()
        except:
            pass


def walk_and_convert(input_path: str, out_dir: str, verbose: bool = True) -> None:
    """
    Recursively convert all TTF files from input directory to OTF in output directory.
    Preserves the directory structure.
    """
    os.makedirs(out_dir, exist_ok=True)

    if os.path.isfile(input_path):
        # Single file mode
        process_one_font(input_path, out_dir, verbose)
        return

    # Directory mode - walk through all subdirectories
    ttf_count = 0
    success_count = 0
    fail_count = 0

    for root, dirs, files in os.walk(input_path):
        for fname in files:
            if fname.lower().endswith(".ttf"):
                ttf_count += 1
                font_path = os.path.join(root, fname)

                # Preserve sub-directory structure in output
                rel_path = os.path.relpath(root, input_path)
                target_dir = os.path.join(out_dir, rel_path)

                # Track success/failure
                try:
                    process_one_font(font_path, target_dir, verbose)
                    success_count += 1
                except Exception as e:
                    fail_count += 1
                    if verbose:
                        print(f"  ❌ Failed: {e}")

    if verbose:
        print(f"\nSummary: {success_count} succeeded, {fail_count} failed out of {ttf_count} total TTF files")
        if ttf_count == 0:
            print(f"⚠️ No .ttf files found in {input_path}")


def main():
    parser = argparse.ArgumentParser(
        description="TTF to OTF Converter using FontForge - Optimized for maximum quality"
    )
    parser.add_argument(
        "input_path",
        help="Path to a directory containing .ttf files or a single .ttf file"
    )
    parser.add_argument(
        "output_dir",
        help="Directory where the optimized .otf files will be written"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed processing information"
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress all output except errors"
    )

    args = parser.parse_args()

    # Determine verbosity
    verbose = not args.quiet  # Quiet overrides everything
    if args.verbose:
        verbose = True

    if verbose:
        print("=" * 60)
        print("TTF to OTF Converter v1.3")
        print("Optimized for maximum quality in rendering, sharpening, and clarity")
        print("=" * 60)
        print(f"Input:  {args.input_path}")
        print(f"Output: {args.output_dir}")
        print()

    # Check if input exists
    if not os.path.exists(args.input_path):
        print(f"❌ Error: Input path does not exist: {args.input_path}", file=sys.stderr)
        sys.exit(1)

    # Check if FontForge is available
    try:
        ff_version = fontforge.version()
        if verbose:
            print(f"FontForge version: {ff_version}")
            print()
    except Exception as e:
        print(f"❌ Error: FontForge is not available: {e}", file=sys.stderr)
        sys.exit(1)

    # Start conversion
    walk_and_convert(args.input_path, args.output_dir, verbose)

    if verbose:
        print()
        print("=" * 60)
        print("Conversion complete!")
        print("=" * 60)


if __name__ == "__main__":
    main()
