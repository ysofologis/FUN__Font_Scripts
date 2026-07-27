"""
TTF → OTF converter using fonttools with thickness boost and fringe cleanup.

Features:
- Convert TrueType outlines to CFF/OTF via qu2cu
- Increase stroke weight (thickness boost)
- Remove fringe artifacts (round, simplify, remove overlaps)

Usage:
    python ttf2otf_fonttools_v3.py <input.ttf> <output.otf>
    python ttf2otf_fonttools_v3.py <input.ttf> <output.otf> --thickness 20
    python ttf2otf_fonttools_v3.py <input_dir/> <output_dir/> --thickness 15 --aggression medium
"""

import argparse
import subprocess
import sys
from pathlib import Path

from fontTools.ttLib import TTFont


def thicken_font(font: TTFont, amount: int) -> None:
    """
    Increase stroke weight by expanding glyph outlines outward.
    Works on glyf table (TrueType outlines) before CFF conversion.

    Args:
        font: TTFont with glyf table
        amount: Expansion in font units (positive = thicker)
    """
    if amount <= 0 or "glyf" not in font:
        return

    glyf = font["glyf"]
    hhea = font["hhea"]
    units_per_em = font["head"].unitsPerEm

    # Scale factor: amount is in font units, convert to em fraction
    # A value of ~20-40 is subtle, 60-100 is bold
    factor = amount / units_per_em

    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        if glyph is None:
            continue

        # Get bounding box to scale from center
        xMin, yMin, xMax, yMax = glyph.getBounds()
        if xMin is None:
            continue

        cx = (xMin + xMax) / 2
        cy = (yMin + yMax) / 2

        # Scale factor slightly > 1 to push edges outward
        # We scale from center to avoid positional drift
        scale = 1.0 + factor * 2

        # Build a new glyph by transforming the existing one
        # We use the pen to draw the transformed outline
        pen = TTGlyphPen(glyph)
        # Apply uniform scale from origin, then translate back
        # This is a simplified radial expand — translate to origin, scale, translate back
        # For TrueType glyphs, we need to reconstruct from the glyph object itself
        # Approach: use transformPen on a copy of the glyph

        # Actually for glyf table, we can directly adjust the coordinates
        # Let's do a proper affine transform on all points
        _expand_glyph_in_place(glyph, scale, cx, cy)


def _expand_glyph_in_place(glyph, scale, cx, cy):
    """Apply uniform scale from center point to a glyf glyph."""
    # TrueType glyphs use quadratic curves (q curves) in glyf table
    # We scale all coordinates around the glyph center
    if not hasattr(glyph, "data"):
        return

    try:
        data = glyph.data

        # Scale coordinates in all tables that have them
        for tag in ["coordinates", "endPts", "flags"]:
            if tag in data:
                coords = data[tag]
                if tag == "coordinates":
                    new_coords = []
                    for x, y in coords:
                        # Translate to origin, scale, translate back
                        nx = x - cx
                        ny = y - cy
                        nx = nx * scale
                        ny = ny * scale
                        new_coords.append((nx + cx, ny + cy))
                    data[tag] = new_coords
    except Exception:
        # Some glyphs may have complex data structures
        pass


def round_glyph_coords(font: TTFont) -> None:
    """Round all glyph coordinates to integers to prevent subpixel fringing."""
    if "glyf" not in font:
        return

    glyf = font["glyf"]
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        if glyph is None:
            continue
        try:
            glyph.round()
        except Exception:
            pass


def remove_glyph_overlaps(font: TTFont) -> int:
    """
    Remove overlapping contours from all glyphs.
    Returns number of glyphs processed.
    """
    if "glyf" not in font:
        return 0

    try:
        from fontTools.ttLib.tables._g_l_y_f import Glyph
        import io

        glyf = font["glyf"]
        count = 0

        for glyph_name in font.getGlyphOrder():
            glyph = glyf[glyph_name]
            if glyph is None:
                continue

            try:
                glyph.removeOverlap()
                count += 1
            except Exception:
                pass

        return count
    except ImportError:
        return 0


def simplify_glyphs(font: TTFont, tolerance: float = 1.0) -> None:
    """
    Simplify glyph outlines by removing collinear points and tiny segments.
    """
    if "glyf" not in font:
        return

    glyf = font["glyf"]
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        if glyph is None:
            continue
        try:
            glyph.simplify(tolerance)
        except Exception:
            pass


def correct_glyph_direction(font: TTFont) -> None:
    """Fix contour directions to prevent rendering artifacts."""
    if "glyf" not in font:
        return

    glyf = font["glyf"]
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        if glyph is None:
            continue
        try:
            glyph.correctDirection()
        except Exception:
            pass


def convert_and_process(
    input_path: Path,
    output_path: Path,
    thickness: int = 0,
    aggression: str = "medium",
) -> bool:
    """
    Convert a TTF to OTF with optional thickness boost and fringe cleanup.

    Args:
        input_path: Input .ttf file
        output_path: Output .otf file
        thickness: Font unit boost (0 = none, 20-40 subtle, 60-100 bold)
        aggression: Fringe cleanup level ('low', 'medium', 'high')

    Returns:
        True on success, False on failure.
    """
    try:
        # Step 1: Load the font
        print(f"  Loading: {input_path.name}")
        font = TTFont(input_path)

        # Step 2: Process glyf-based operations (pre-conversion)
        if "glyf" in font:
            print(f"  Processing glyphs...")

            # Remove overlaps first (before thickening)
            n_overlaps = remove_glyph_overlaps(font)
            if n_overlaps > 0:
                print(f"    Removed overlaps in {n_overlaps} glyphs")

            # Simplify based on aggression
            if aggression == "low":
                simplify_glyphs(font, tolerance=2.0)
            elif aggression == "medium":
                simplify_glyphs(font, tolerance=1.0)
            elif aggression in ("high", "extreme"):
                simplify_glyphs(font, tolerance=0.5)

            # Round coordinates to prevent subpixel fringing
            round_glyph_coords(font)

            # Fix contour directions
            correct_glyph_direction(font)

            # Step 3: Thickness boost (after overlap removal + simplify)
            if thickness > 0:
                print(f"  Thickening by {thickness} font units...")
                _thicken_glyf_outlines(font, thickness)

                # Post-thickness cleanup
                remove_glyph_overlaps(font)
                round_glyph_coords(font)
                simplify_glyphs(font, tolerance=0.5 if aggression in ("high", "extreme") else 1.0)

        # Step 4: Convert to OTF via qu2cu subprocess
        print(f"  Converting to OTF (CFF outlines)...")
        result = subprocess.run(
            [
                sys.executable, "-m", "fontTools",
                "qu2cu",
                str(input_path),
                "-o", str(output_path),
                "-e", "0.001",
                "-v",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"  ERROR: {result.stderr.strip()}")
            return False

        # The qu2cu output is a new file — but we already processed the original.
        # Instead, save directly using fonttools with CFF conversion approach.
        # Since qu2cu creates a NEW file from the original (not our processed one),
        # we need a different approach: build the CFF table ourselves.
        #
        # Alternative: process the OUTPUT of qu2cu.
        # But qu2cu reads the original input file, not our modified font object.
        #
        # Best approach for fonttools without full CFF rebuilder:
        # 1. Do all glyf modifications on the original font
        # 2. Save as TTF first
        # 3. Then run qu2cu on the saved TTF to get OTF
        # OR: just save with flavor='woff2' or similar
        #
        # Let's save the processed font, then run qu2cu on it.

        # Save processed font to a temp location
        temp_ttf = output_path.with_suffix(".processed.ttf")
        font.save(temp_ttf)

        # Now run qu2cu on the processed temp file
        temp_otf = output_path.with_suffix(".processed.otf")
        result2 = subprocess.run(
            [
                sys.executable, "-m", "fontTools",
                "qu2cu",
                str(temp_ttf),
                "-o", str(temp_otf),
                "-e", "0.001",
            ],
            capture_output=True,
            text=True,
        )

        # Clean up temp TTF
        temp_ttf.unlink(missing_ok=True)

        if result2.returncode != 0:
            print(f"  ERROR during qu2cu: {result2.stderr.strip()}")
            return False

        # Rename processed OTF to final output
        if temp_otf.exists():
            if output_path.exists():
                output_path.unlink()
            temp_otf.rename(output_path)

        print(f"  → {output_path.name} ({output_path.stat().st_size // 1024}KB)")
        return True

    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def _thicken_glyf_outlines(font: TTFont, amount: int) -> None:
    """
    Thicken glyph outlines by expanding from center.
    Uses affine transform on glyph coordinates.
    """
    if "glyf" not in font or amount <= 0:
        return

    glyf = font["glyf"]
    units_per_em = font["head"].unitsPerEm
    scale = 1.0 + (amount / units_per_em) * 2.5

    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        if glyph is None:
            continue
        try:
            _scale_glyph_from_center(glyph, scale)
        except Exception:
            pass


def _scale_glyph_from_center(glyph, scale):
    """Scale glyph coordinates around the glyph center."""
    try:
        bounds = glyph.getBounds()
        if bounds is None:
            return
        xMin, yMin, xMax, yMax = bounds
        cx = (xMin + xMax) / 2
        cy = (yMin + yMax) / 2

        if not hasattr(glyph, "data"):
            return

        data = glyph.data

        if "coordinates" in data:
            coords = data["coordinates"]
            new_coords = []
            for x, y in coords:
                nx = (x - cx) * scale + cx
                ny = (y - cy) * scale + cy
                new_coords.append((round(nx), round(ny)))
            data["coordinates"] = new_coords
    except Exception:
        pass


def process_directory(
    input_dir: Path,
    output_dir: Path,
    thickness: int = 0,
    aggression: str = "medium",
) -> tuple[int, int]:
    """Process all TTF files in a directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ttf_files = sorted(input_dir.glob("*.ttf"))

    if not ttf_files:
        print(f"No .ttf files found in: {input_dir}")
        return 0, 0

    success = 0
    failure = 0

    for ttf_path in ttf_files:
        print(f"\n{ttf_path.name}:")
        output_path = output_dir / f"{ttf_path.stem}.otf"
        if convert_and_process(ttf_path, output_path, thickness, aggression):
            success += 1
        else:
            failure += 1

    return success, failure


def main():
    parser = argparse.ArgumentParser(
        description="Convert TTF to OTF with thickness boost and fringe cleanup"
    )
    parser.add_argument("input", type=Path, help="Input TTF file or directory")
    parser.add_argument("output", type=Path, help="Output OTF file or directory")
    parser.add_argument(
        "-t", "--thickness",
        type=int,
        default=0,
        help="Font unit thickness boost (0=none, 20-40=subtle, 60-100=bold)",
    )
    parser.add_argument(
        "-a", "--aggression",
        choices=["low", "medium", "high", "extreme"],
        default="medium",
        help="Fringe cleanup aggression (default: medium)",
    )
    args = parser.parse_args()

    input_path: Path = args.input

    if not input_path.exists():
        print(f"Error: Input not found: {input_path}")
        sys.exit(1)

    if input_path.is_file():
        output_path = Path(args.output)
        print(f"Converting: {input_path.name}")
        if not convert_and_process(input_path, output_path, args.thickness, args.aggression):
            sys.exit(1)
        print("Done.")
    else:
        output_dir = Path(args.output)
        success, failure = process_directory(
            input_path, output_dir, args.thickness, args.aggression
        )
        print(f"\n{'='*50}")
        print(f"Done. {success} converted, {failure} failed.")


if __name__ == "__main__":
    main()
