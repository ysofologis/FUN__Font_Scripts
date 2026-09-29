#!/usr/bin/env python3
"""
Fix Sadi Sans OS/2 usWeightClass.

CONCRETE CASE — kept specific on purpose. Sadi Sans ships every .otf in the
family declaring usWeightClass = 400 (Regular), regardless of the file's
actual style. The `style` name says "Bold" but the OS/2 table says "Regular",
so fontconfig scores every file as an exact match for a Regular request and
the tie breaks arbitrarily. Symptom: `fc-match "Sadi Sans"` returns Bold.

Fixes: rewrite OS/2.usWeightClass to match each file's style name.
Siblings that are already correct are copied through untouched, so the output
directory is always a complete, ready-to-install family.

Template for similar cases: the shape is (1) audit every file's declared
weight, (2) map style name -> canonical usWeightClass, (3) rewrite only the
mismatches. Swap EXPECTED / NAME_RE for another family and it works as-is.

Usage:
    python fix_sadi_sans_weights.py INPUT_DIR OUTPUT_DIR

Requires: fonttools  (pip install fonttools)

Verify:
    fc-scan OUTPUT_DIR/SadiSans-Bold.otf | grep weight   # expect 700
    fc-match -f '%{weight} %{style[0]}' "Sadi Sans"      # expect 80 Regular

Known remaining issue: OS/2.fsSelection is 0x0040 (USE_TYPO_METRICS only) in
every file — bit 0 (ITALIC) is unset on italic cuts and bit 5 (BOLD) is unset
on bold cuts. fontconfig infers slant/style from the `style` name so fc-match
works, but consumers reading fsSelection directly will see no italic flag.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

# Canonical CSS-style usWeightClass values, keyed by the style token that
# appears in the Sadi Sans filenames.
EXPECTED = {
    "Thin":       100,
    "ExtraLight": 200,
    "Light":      300,
    "Regular":    400,
    "Medium":     500,
    "SemiBold":   600,
    "Bold":       700,
    "ExtraBold":  800,
    "Heavy":      900,
}

# SadiSans-Bold, SadiSans-BoldItalic, SadiSans-ExtraLightItalic, ...
NAME_RE = re.compile(
    r"^SadiSans-?"
    r"(Thin|ExtraLight|Light|Regular|Medium|SemiBold|Bold|ExtraBold|Heavy)"
    r"(Italic)?$"
)

# The variable cuts span the full wght axis; 400 is the conventional
# usWeightClass for a variable font default instance.
VARIABLE_STEMS = ("SadiSansVariable", "SadiSansVariableItalic")


def expected_weight_for(filename: str) -> tuple[int | None, str | None]:
    """Return (expected_usWeightClass, style_label), or (None, None) if unknown."""
    stem = Path(filename).stem

    if stem in VARIABLE_STEMS:
        return 400, stem

    m = NAME_RE.match(stem)
    if not m:
        return None, None

    name = m.group(1)
    label = name + ("Italic" if m.group(2) else "")
    return EXPECTED[name], label


def fix_one(src: Path, dst: Path) -> tuple[str, bool]:
    """Copy src -> dst, rewriting OS/2.usWeightClass when it disagrees.

    Returns (report_line, changed).
    """
    expected, label = expected_weight_for(src.name)
    if expected is None:
        shutil.copy2(src, dst)
        return f"  {src.name:34s} SKIP  (unrecognized name)", False

    font = TTFont(str(src))
    actual = font["OS/2"].usWeightClass

    if actual == expected:
        shutil.copy2(src, dst)
        return f"  {src.name:34s} OK    ({label:>16s} = {actual})", False

    font["OS/2"].usWeightClass = expected
    font.save(str(dst))
    return f"  {src.name:34s} FIX   ({label:>16s} {actual} -> {expected})", True


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    inp = Path(sys.argv[1]).expanduser()
    outp = Path(sys.argv[2]).expanduser()

    if not inp.is_dir():
        print(f"error: not a directory: {inp}", file=sys.stderr)
        return 2

    otfs = sorted(inp.glob("*.otf"))
    if not otfs:
        print(f"error: no .otf files in {inp}", file=sys.stderr)
        return 2

    outp.mkdir(parents=True, exist_ok=True)

    print(f"input : {inp}")
    print(f"output: {outp}\n")

    fixed = 0
    for src in otfs:
        try:
            line, changed = fix_one(src, outp / src.name)
        except Exception as e:  # noqa: BLE001 - report and keep going
            line = f"  {src.name:34s} ERROR {e!r}"
            changed = False
        print(line)
        fixed += changed

    copied = len(otfs) - fixed
    print(f"\npatched {fixed}, copied {copied} unchanged, total {len(otfs)}")
    print("\ninstall:  cp -f <output>/* ~/.local/share/fonts/<family>/ && fc-cache -fv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
