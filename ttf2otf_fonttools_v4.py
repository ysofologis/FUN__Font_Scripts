#!/usr/bin/env python3
"""
ttf2otf_fonttools_v4.py — TTF → OTF (CFF) converter using fonttools.

Converts TrueType (.ttf) fonts to OpenType/CFF (.otf) fonts while
preserving metrics and maximizing clarity via proper quadratic→cubic conversion.

Usage:
    python ttf2otf_fonttools_v4.py <input_dir> <output_dir>
    python ttf2otf_fonttools_v4.py <input_dir> <output_dir> --recursive

Requirements:
    pip install fonttools brotli
"""

import argparse
import sys
import time
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.qu2cuPen import Qu2CuPen


def convert_ttf_to_otf(ttf_path: Path, otf_path: Path, max_err: float = 0.5) -> dict:
    """
    Convert a single TTF file to OTF (CFF).

    Args:
        ttf_path:  Source .ttf path
        otf_path:  Destination .otf path
        max_err:   Max quadratic→cubic error in font units. Lower = sharper, larger file.

    Returns:
        dict with conversion stats (glyphs, chars).
    """
    ttf = TTFont(str(ttf_path))
    glyph_order = ttf.getGlyphOrder()
    cmap = ttf.getBestCmap()
    glyph_set = ttf.getGlyphSet()
    head = ttf["head"]
    hhea = ttf["hhea"]
    os2 = ttf["OS/2"]
    post = ttf["post"]
    name_table = ttf["name"]

    # --- Build OTF via FontBuilder ---
    fb = FontBuilder(head.unitsPerEm, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)

    # Convert each glyph: TrueType quadratic → CFF cubic
    charstrings = {}
    metrics = {}

    for gname in glyph_order:
        if gname not in glyph_set:
            continue

        # T2CharStringPen(width, glyphSet)
        t2_pen = T2CharStringPen(0, glyph_set)
        # Qu2CuPen wraps it: intercepts quadratic calls, converts to cubic
        qu2cu = Qu2CuPen(t2_pen, max_err=max_err)
        glyph_set[gname].draw(qu2cu)
        charstrings[gname] = t2_pen.getCharString()

        metrics[gname] = (glyph_set[gname].width, 0)

    # Horizontal metrics & header
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=hhea.ascent, descent=hhea.descent)

    # CFF table
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

    # Name table
    fb.setupNameTable({
        "familyName": name_table.getDebugName(1) or ttf_path.stem,
        "styleName": name_table.getDebugName(2) or "Regular",
    })

    # OS/2 — preserve all metrics from original
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
        fsSelection=os2.fsSelection,
        sFamilyClass=os2.sFamilyClass,
        panose=os2.panose,
        usWeightClass=os2.usWeightClass,
        usWidthClass=os2.usWidthClass,
        usDefaultChar=os2.usDefaultChar,
        usBreakChar=os2.usBreakChar,
        usMaxContext=os2.usMaxContext,
    )

    # Head table
    fb.setupHead(
        unitsPerEm=head.unitsPerEm,
        created=head.created,
        modified=head.modified,
        flags=head.flags,
        macStyle=head.macStyle,
        lowestRecPPEM=head.lowestRecPPEM,
        glyphDataFormat=head.glyphDataFormat,
    )

    # Post table
    fb.setupPost(
        isFixedPitch=post.isFixedPitch,
        underlinePosition=post.underlinePosition,
        underlineThickness=post.underlineThickness,
    )

    # Save
    otf_path.parent.mkdir(parents=True, exist_ok=True)
    fb.font.save(str(otf_path))
    ttf.close()

    return {"glyphs": len(charstrings), "chars": len(cmap) if cmap else 0}


def process_directory(input_dir: Path, output_dir: Path, recursive: bool = False,
                      max_err: float = 0.5) -> None:
    """Process all TTF files in input_dir, writing OTF files to output_dir."""
    pattern = "**/*.ttf" if recursive else "*.ttf"
    ttf_files = sorted(input_dir.glob(pattern))

    if not ttf_files:
        print(f"No TTF files found in {input_dir}")
        sys.exit(1)

    total = len(ttf_files)
    success = 0
    failed = 0
    skipped = 0
    t0 = time.time()

    print(f"Found {total} TTF file(s) in {input_dir}")
    print(f"Max quadratic→cubic error: {max_err} font units\n")

    for i, ttf_path in enumerate(ttf_files, 1):
        rel = ttf_path.relative_to(input_dir)
        otf_path = output_dir / rel.with_suffix(".otf")

        print(f"[{i}/{total}] {rel}  →  ", end="", flush=True)

        if otf_path.exists():
            print("SKIP (exists)")
            skipped += 1
            continue

        try:
            stats = convert_ttf_to_otf(ttf_path, otf_path, max_err=max_err)
            size_in = ttf_path.stat().st_size
            size_out = otf_path.stat().st_size
            ratio = size_out / size_in if size_in else 0
            print(f"OK  ({stats['glyphs']} glyphs, {size_in:,} → {size_out:,} bytes, {ratio:.1%})")
            success += 1
        except Exception as e:
            print(f"FAIL: {e}")
            failed += 1

    elapsed = time.time() - t0
    print(f"\n--- Done in {elapsed:.1f}s ---")
    print(f"    Success: {success}  Failed: {failed}  Skipped: {skipped}  Total: {total}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert TTF fonts to OTF (CFF) using fonttools, preserving clarity."
    )
    parser.add_argument("input_dir", type=Path, help="Directory containing TTF fonts")
    parser.add_argument("output_dir", type=Path, help="Output directory for OTF fonts")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="Process subdirectories recursively")
    parser.add_argument("--max-err", type=float, default=0.5,
                        help="Max quadratic→cubic error in font units (default: 0.5). "
                             "Lower = sharper but larger files. Higher = smaller but less precise.")

    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"Error: {args.input_dir} is not a directory")
        sys.exit(1)

    process_directory(args.input_dir, args.output_dir, recursive=args.recursive,
                      max_err=args.max_err)


if __name__ == "__main__":
    main()
