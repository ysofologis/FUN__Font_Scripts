#!/usr/bin/env python3
"""Batch UPM increase for a directory of OTF/CFF/TTF fonts.

Scales every font from its current unitsPerEm to a target UPM using
fontTools' scaleUpem visitor, which correctly updates the CFF FontMatrix
(these are CFF/OTF fonts, not just glyf/TTF).

IMPORTANT: this is a pure coordinate-rescaling operation. Rendered output is
visually identical -- it only buys finer quantization resolution for
subsequent passes (e.g. a --thicken-grid measured in font units).

Fonts already at the target UPM are copied through unchanged, so the output
directory is always a complete, installable set.

Usage:
    python3 scale_upem_batch.py INPUT_DIR OUTPUT_DIR [--upm 2048]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.ttLib.scaleUpem import scale_upem

SUFFIXES = {".otf", ".ttf"}


def cff_matrix(font: TTFont) -> str | None:
    try:
        top = font["CFF "].cff.topDictIndex[0]
        return f"{top.FontMatrix[0]:g}"
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_dir", type=Path)
    ap.add_argument("output_dir", type=Path)
    ap.add_argument(
        "--upm",
        type=int,
        default=2048,
        help="target unitsPerEm (default 2048 -- the modern convention; "
             "1000 is the old TrueType-era default)",
    )
    args = ap.parse_args()

    src_dir: Path = args.input_dir
    out_dir: Path = args.output_dir
    if not src_dir.is_dir():
        print(f"error: input dir not found: {src_dir}", file=sys.stderr)
        return 1

    fonts = sorted(p for p in src_dir.iterdir() if p.suffix.lower() in SUFFIXES)
    if not fonts:
        print(f"error: no .otf/.ttf files in {src_dir}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"input   : {src_dir}  ({len(fonts)} fonts)")
    print(f"output  : {out_dir}")
    print(f"target  : UPM {args.upm}")
    print()

    hdr = (
        f"{'file':<34} {'upm':>5} {'x':>5} {'glyphs':>7} "
        f"{'asc':>6} {'desc':>6} {'CFF mtx':>9} {'size':>9}"
    )
    print(hdr)
    print("-" * len(hdr))

    total_in = total_out = 0
    passthrough = 0
    failures: list[tuple[str, str]] = []

    for src in fonts:
        dst = out_dir / src.name
        try:
            font = TTFont(src)
            old_upm = font["head"].unitsPerEm
            old_glyphs = len(font.getGlyphOrder())
            old_asc = font["OS/2"].sTypoAscender
            old_desc = font["OS/2"].sTypoDescender
            old_mtx = cff_matrix(font)
            had_cff = "CFF " in font
            font.close()

            if old_upm == args.upm:
                # Already at target: copy through so the output dir stays complete.
                shutil.copy2(src, dst)
                passthrough += 1
                print(
                    f"{src.name:<34} {old_upm:>5} {'--':>5} {old_glyphs:>7} "
                    f"{old_asc:>6} {old_desc:>6} {(old_mtx or '-'):>9} "
                    f"{dst.stat().st_size:>9,}  (already at target)"
                )
                total_in += src.stat().st_size
                total_out += dst.stat().st_size
                continue

            font = TTFont(src)
            scale_upem(font, args.upm)

            new_glyphs = len(font.getGlyphOrder())
            new_asc = font["OS/2"].sTypoAscender
            new_desc = font["OS/2"].sTypoDescender
            new_mtx = cff_matrix(font)

            if new_glyphs != old_glyphs:
                raise RuntimeError(
                    f"glyph count changed {old_glyphs} -> {new_glyphs}"
                )
            if had_cff and old_mtx is not None and new_mtx == old_mtx:
                raise RuntimeError(
                    "CFF FontMatrix did not change -- outlines unscaled"
                )

            font.save(dst)
            font.close()

            factor = args.upm / old_upm
            total_in += src.stat().st_size
            total_out += dst.stat().st_size
            print(
                f"{src.name:<34} {old_upm:>5} {factor:>5.1f} {new_glyphs:>7} "
                f"{new_asc:>6} {new_desc:>6} {(new_mtx or '-'):>9} "
                f"{dst.stat().st_size:>9,}"
            )
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures.append((src.name, f"{type(exc).__name__}: {exc}"))
            print(f"{src.name:<34} FAILED: {exc}")

    print()
    if total_in:
        print(f"size before : {total_in:,} bytes")
        print(f"size after  : {total_out:,} bytes")
    if passthrough:
        print(f"passthrough : {passthrough} font(s) already at UPM {args.upm}")

    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for name, err in failures:
            print(f"  {name}: {err}", file=sys.stderr)
        return 1

    written = len(list(out_dir.glob("*.otf"))) + len(list(out_dir.glob("*.ttf")))
    print(f"\nOK: {len(fonts)} font(s) processed, {written} file(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
