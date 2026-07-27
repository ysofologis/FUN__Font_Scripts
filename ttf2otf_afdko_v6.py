#!/usr/bin/env python3
"""
ttf2otf_afdko_v6.py — TTF → OTF (CFF) using AFDKO tools where available.

AFDKO tools used:
  tx                — exact TTF→CFF conversion via -x flag
  checkoutlinesufo  — outline validation and overlap removal

Pipeline A  (best quality — needs AFDKO tx):
    TTF → tx(-ufo) → UFO → checkoutlinesufo → fonttools assemble → OTF

Pipeline B  (fast — needs AFDKO tx):
    TTF → tx(-cff -x +F +W -Z) → fonttools assemble → OTF

Pipeline C  (fallback — no AFDKO required):
    TTF → fonttools (qu2cu conversion) → OTF

Usage:
    python ttf2otf_afdko_v6.py <input_dir> <output_dir>
    python ttf2otf_afdko_v6.py <input_dir> <output_dir> --recursive
    python ttf2otf_afdko_v6.py <input_dir> <output_dir> --mode full        # Pipeline A
    python ttf2otf_afdko_v6.py <input_dir> <output_dir> --mode direct       # Pipeline B
    python ttf2otf_afdko_v6.py <input_dir> <output_dir> --mode fonttools   # Pipeline C

Requirements:
    pip install afdko fonttools brotli

Note on hinting:
    otfautohint is intentionally not used because it requires BlueValues
    (stem zones) in the CFF private dict — data that tx does not produce
    when converting from TTF. Without BlueValues, otfautohint errors out.
    CFF hinting is best applied at the Type 1 / design source stage, not
    after TTF→CFF conversion. The font renders correctly without it.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.ttLib.tables._g_a_s_p import table__g_a_s_p, GASP_GRIDFIT, GASP_DOGRAY


# ─────────────────────────────────────────────────────────────────────────────
# AFDKO tool detection
# ─────────────────────────────────────────────────────────────────────────────

def _tool(name: str) -> Path | None:
    """Return Path to AFDKO binary if found in PATH, else None."""
    for dir_ in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(dir_) / name
        if p.is_file():
            return p
    return None


TX    = _tool("tx")
CHKO  = _tool("checkoutlinesufo")


def _run(cmd: list[str], *, cwd: Path | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, raising RuntimeError on non-zero exit."""
    merged_env = {**os.environ, **(env or {})}
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=merged_env,
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"  stdout: {e.stdout[:300]}\n"
            f"  stderr: {e.stderr[:500]}"
        )


def _banner() -> str:
    parts = []
    if TX:   parts.append(f"tx={TX}")
    if CHKO: parts.append(f"checkoutlinesufo={CHKO}")
    if not parts:
        return "AFDKO: NONE (fonttools fallback)"
    parts.insert(0, "AFDKO:")
    return "  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Assembler: build OTF from a UFO directory (Pipeline A)
# ─────────────────────────────────────────────────────────────────────────────

def _build_otf_from_ufo(
    ufo_dir: Path,
    ttf_ref: Path,          # reference font for metrics / tables
    otf_path: Path,
    max_err: float = 0.3,
) -> dict:
    """
    Read glyph outlines from a UFO directory (produced by tx -ufo) and
    assemble an OTF using fonttools + qu2cu for curve conversion.

    We deliberately DO NOT use makeotf here — it requires a features.fea
    file that most UFOs don't carry.  Instead, fonttools handles the OTF
    table assembly, giving us full control over every table.
    """
    import plistlib

    ttf = TTFont(str(ttf_ref))
    glyph_order = ttf.getGlyphOrder()
    cmap        = ttf.getBestCmap()
    glyph_set   = ttf.getGlyphSet()
    head        = ttf["head"]
    hhea        = ttf["hhea"]
    os2         = ttf["OS/2"]
    post        = ttf["post"]
    name_table  = ttf["name"]

    # ── load UFO outlines ─────────────────────────────────────────────────
    ufo_glyphs_dir = ufo_dir / "glyphs"
    ufo_fontinfo    = plistlib.loads((ufo_dir / "fontinfo.plist").read_bytes())

    # Parse .glif files into glyph objects using fontTools' UFO reader
    try:
        from fontTools.ufoLib import UFOReader
        from fontTools.designspaceLib import DesignSpaceDocument
    except ImportError:
        # fallback: manually load glyphs from .glif
        pass

    # Read glyph outlines via UFOReader
    ufo_reader = UFOReader(str(ufo_dir))
    ufo_glyph_set = ufo_reader.getGlyphSet()

    # Remove hinting from reference glyf table
    glyf_table = ttf["glyf"]
    for gname in glyph_order:
        if gname in glyf_table:
            glyf_table[gname].removeHinting()

    fb = FontBuilder(head.unitsPerEm, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)

    charstrings, metrics = {}, {}

    for gname in glyph_order:
        if gname not in ufo_glyph_set:
            if gname in glyph_set:
                metrics[gname] = (glyph_set[gname].width, 0)
            continue

        # Draw the UFO glyph into a T2CharStringPen via qu2cu
        t2_pen = T2CharStringPen(0, None)  # no glyphSet needed for UFO
        qu2cu  = Qu2CuPen(t2_pen, max_err=max_err)
        ufo_glyph_set[gname].draw(qu2cu)
        cs = t2_pen.getCharString()
        charstrings[gname] = cs

        # Width from the reference TTF glyph
        if gname in glyph_set:
            metrics[gname] = (glyph_set[gname].width, 0)
        else:
            metrics[gname] = (ufo_glyph_set[gname].width, 0)

    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(
        ascent=hhea.ascent, descent=hhea.descent,
        lineGap=hhea.lineGap,
        advanceWidthMax=getattr(hhea, "advanceWidthMax", None) or 0,
        minLeftSideBearing=getattr(hhea, "minLeftSideBearing", None) or 0,
        minRightSideBearing=getattr(hhea, "minRightSideBearing", None) or 0,
        xMaxExtent=getattr(hhea, "xMaxExtent", None) or 0,
    )

    ps_name   = name_table.getDebugName(6) or ttf_ref.stem.replace(" ", "-")
    font_info = {
        "FullName":   name_table.getDebugName(4) or ttf_ref.stem,
        "FamilyName": name_table.getDebugName(1) or ttf_ref.stem,
        "Weight":     os2.usWeightClass or 400,
        "ItalicAngle": post.italicAngle or 0,
    }
    private_dict = {
        "StdHW": post.underlineThickness or 50,
        "StdVW": post.underlineThickness or 80,
    }
    fb.setupCFF(ps_name, font_info, charstrings, private_dict)
    fb.setupNameTable({
        "familyName": name_table.getDebugName(1) or ttf_ref.stem,
        "styleName":  name_table.getDebugName(2) or "Regular",
    })
    # fsSelection bits 7-9 are only valid in OS/2 v4+; mask them for v3
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

    # Clear hinting-related head flags for CFF output
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
    # Note: no GSUB stub needed; optional for fonts without OT features

    otf_path.parent.mkdir(parents=True, exist_ok=True)
    fb.font.save(str(otf_path))
    ttf.close()
    return _get_stats(otf_path, ttf_ref)


# ─────────────────────────────────────────────────────────────────────────────
# Assembler: build OTF from a raw CFF table (Pipeline B)
# ─────────────────────────────────────────────────────────────────────────────

def _build_otf_from_cff(
    cff_path: Path,
    ttf_ref: Path,
    otf_path: Path,
    do_hinting: bool = False,  # unused: otfautohint requires BlueValues
) -> dict:
    """
    Load a CFF file produced by tx -cff and build a proper OTF with all
    required tables via fonttools.

    tx -cff outputs a raw CFF font (not an OpenType wrapper), so we use
    fontTools.cffLib.CFFFontSet to read it.
    """
    from fontTools.cffLib import CFFFontSet
    from io import BytesIO

    ttf = TTFont(str(ttf_ref))
    glyph_order = ttf.getGlyphOrder()
    cmap        = ttf.getBestCmap()
    glyph_set   = ttf.getGlyphSet()
    head        = ttf["head"]
    hhea        = ttf["hhea"]
    os2         = ttf["OS/2"]
    post        = ttf["post"]
    name_table  = ttf["name"]

    # Read raw CFF via CFFFontSet (TTFont can't open raw CFF files)
    with open(str(cff_path), "rb") as f:
        raw_cff = f.read()
    cff_fs = CFFFontSet()
    cff_fs.decompile(BytesIO(raw_cff), None)

    # Get the first (and usually only) font in the CFF FontSet
    cff_font   = cff_fs[0]
    cff_chars  = cff_font.CharStrings  # fontTools.cffLib.CharStrings
    # charStrings is a dict: glyphName → charsetID
    # charStringsIndex is the actual INDEX of charstrings (T2CharString objects)
    charstrings_index = cff_chars.charStringsIndex
    charset_map       = cff_chars.charStrings  # {glyphName: charsetID}

    fb = FontBuilder(head.unitsPerEm, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)

    charstrings, metrics = {}, {}
    for gname in glyph_order:
        if gname in charset_map:
            cs_id = charset_map[gname]
            charstrings[gname] = charstrings_index[cs_id]
        if gname in glyph_set:
            metrics[gname] = (glyph_set[gname].width, 0)

    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(
        ascent=hhea.ascent, descent=hhea.descent,
        lineGap=hhea.lineGap,
        advanceWidthMax=getattr(hhea, "advanceWidthMax", None) or 0,
        minLeftSideBearing=getattr(hhea, "minLeftSideBearing", None) or 0,
        minRightSideBearing=getattr(hhea, "minRightSideBearing", None) or 0,
        xMaxExtent=getattr(hhea, "xMaxExtent", None) or 0,
    )

    ps_name   = name_table.getDebugName(6) or ttf_ref.stem.replace(" ", "-")
    font_info = {
        "FullName":   name_table.getDebugName(4) or ttf_ref.stem,
        "FamilyName": name_table.getDebugName(1) or ttf_ref.stem,
        "Weight":     os2.usWeightClass or 400,
        "ItalicAngle": post.italicAngle or 0,
    }
    private_dict = {
        "StdHW": post.underlineThickness or 50,
        "StdVW": post.underlineThickness or 80,
    }
    fb.setupCFF(ps_name, font_info, charstrings, private_dict)
    fb.setupNameTable({
        "familyName": name_table.getDebugName(1) or ttf_ref.stem,
        "styleName":  name_table.getDebugName(2) or "Regular",
    })
    # fsSelection bits 7-9 are only valid in OS/2 v4+; mask them for v3
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

    gasp = table__g_a_s_p()
    gasp.gaspRange = {0: GASP_GRIDFIT | GASP_DOGRAY}
    fb.font["gasp"] = gasp
    # Note: GSUB stub intentionally omitted. Most OTF renderers don't need it
    # for fonts without OpenType feature (ligatures, alternates, etc.).

    otf_path.parent.mkdir(parents=True, exist_ok=True)
    fb.font.save(str(otf_path))
    ttf.close()
    return _get_stats(otf_path, ttf_ref)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline A: tx(-ufo) → checkoutlinesufo → fonttools assemble
# ─────────────────────────────────────────────────────────────────────────────

def _pipeline_ufo(ttf_path: Path, otf_path: Path, max_err: float) -> dict:
    """
    Best-quality AFDKO path — use only when explicitly requested via --mode full:
      TTF → tx(-ufo) → checkoutlinesufo → fonttools → OTF

    WARNING: checkoutlinesufo is slow on large fonts (~20s for 4k-glyph fonts)
    and the overlap-removal (-e) is of marginal value after qu2cu conversion.
    Prefer --mode direct for batch conversion.

    checkoutlinesufo options used:
      -q        quiet
      -e        error-correction (overlap removal)
      --no-basic-checks  skip flat-curve / colinear checks (noise after qu2cu)
      -w        write results to default glyphs layer (not a separate layer)
    """
    if not TX:
        raise RuntimeError("tx not found — cannot use UFO pipeline")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp     = Path(tmpdir)
        ufo_dir = tmp / "font.ufo"

        # TTF → UFO  (exact conversion, no curve approximation)
        _run([str(TX), "-ufo", str(ttf_path), str(ufo_dir)])

        # Outline validation + overlap removal (slow; ~20s for 4k-glyph fonts)
        if CHKO:
            _run([
                str(CHKO), "-q", "-e",
                "--no-basic-checks", "-w",
                str(ufo_dir),
            ])

        # Assemble OTF from UFO glyphs
        _build_otf_from_ufo(ufo_dir, ttf_path, otf_path, max_err=max_err)

    return _get_stats(otf_path, ttf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline B: tx(-cff) → fonttools assemble
# ─────────────────────────────────────────────────────────────────────────────

def _pipeline_direct_cff(ttf_path: Path, otf_path: Path) -> dict:
    """
    Fast AFDKO path:
      TTF → tx(-cff -x +F +W -Z) → CFF → fonttools assemble → OTF

    tx options:
      -cff   output raw CFF table
      -x     exact conversion (no quadratic→cubic approximation)
      +F     optimize Family blues
      +W     optimize widths
      -Z     decompose SEAC / dotsection
    """
    if not TX:
        raise RuntimeError("tx not found — cannot use direct CFF pipeline")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp      = Path(tmpdir)
        cff_path = tmp / "font.cff"

        _run([
            str(TX), "-cff", "-x", "+F", "+W", "-Z",
            str(ttf_path), str(cff_path),
        ])

        _build_otf_from_cff(cff_path, ttf_path, otf_path)

    return _get_stats(otf_path, ttf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline C: pure fonttools (no AFDKO required)
# ─────────────────────────────────────────────────────────────────────────────

def _pipeline_fonttools(ttf_path: Path, otf_path: Path, max_err: float) -> dict:
    """
    fonttools-only fallback when AFDKO is unavailable.
    Converts TrueType outlines to CFF via qu2cu pen.
    """
    ttf = TTFont(str(ttf_path))
    glyph_order = ttf.getGlyphOrder()
    cmap        = ttf.getBestCmap()
    glyph_set   = ttf.getGlyphSet()
    head        = ttf["head"]
    hhea        = ttf["hhea"]
    os2         = ttf["OS/2"]
    post        = ttf["post"]
    name_table  = ttf["name"]

    # Remove hinting from glyf table glyphs (not glyphset wrappers)
    glyf_table = ttf["glyf"]
    for gname in glyph_order:
        if gname in glyf_table:
            glyf_table[gname].removeHinting()

    fb = FontBuilder(head.unitsPerEm, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)

    charstrings, metrics = {}, {}

    for gname in glyph_order:
        if gname not in glyph_set:
            continue
        t2_pen = T2CharStringPen(0, glyph_set)
        qu2cu  = Qu2CuPen(t2_pen, max_err=max_err)
        glyph_set[gname].draw(qu2cu)
        charstrings[gname] = t2_pen.getCharString()
        metrics[gname] = (glyph_set[gname].width, 0)

    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(
        ascent=hhea.ascent, descent=hhea.descent,
        lineGap=getattr(hhea, "lineGap", None) or 0,
        advanceWidthMax=getattr(hhea, "advanceWidthMax", None) or 0,
        minLeftSideBearing=getattr(hhea, "minLeftSideBearing", None) or 0,
        minRightSideBearing=getattr(hhea, "minRightSideBearing", None) or 0,
        xMaxExtent=getattr(hhea, "xMaxExtent", None) or 0,
    )

    ps_name   = name_table.getDebugName(6) or ttf_path.stem.replace(" ", "-")
    font_info = {
        "FullName":   name_table.getDebugName(4) or ttf_path.stem,
        "FamilyName": name_table.getDebugName(1) or ttf_path.stem,
        "Weight":     os2.usWeightClass or 400,
        "ItalicAngle": post.italicAngle or 0,
    }
    private_dict = {
        "StdHW": post.underlineThickness or 50,
        "StdVW": post.underlineThickness or 80,
    }
    fb.setupCFF(ps_name, font_info, charstrings, private_dict)
    fb.setupNameTable({
        "familyName": name_table.getDebugName(1) or ttf_path.stem,
        "styleName":  name_table.getDebugName(2) or "Regular",
    })
    # fsSelection bits 7-9 are only valid in OS/2 v4+; mask them for v3
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
    gasp = table__g_a_s_p()
    gasp.gaspRange = {0: GASP_GRIDFIT | GASP_DOGRAY}
    fb.font["gasp"] = gasp
    # Note: GSUB stub intentionally omitted. Most OTF renderers don't need it
    # for fonts without OpenType feature (ligatures, alternates, etc.).

    otf_path.parent.mkdir(parents=True, exist_ok=True)
    fb.font.save(str(otf_path))
    ttf.close()
    return _get_stats(otf_path, ttf_path)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_stats(otf_path: Path, ttf_path: Path) -> dict:
    try:
        f = TTFont(str(otf_path))
        n = len(f.getGlyphOrder())
        f.close()
    except Exception:
        n = 0
    size_in  = ttf_path.stat().st_size
    size_out = otf_path.stat().st_size
    return {"glyphs": n, "size_in": size_in, "size_out": size_out}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def convert_ttf_to_otf(
    ttf_path: Path,
    otf_path: Path,
    *,
    mode: str = "auto",
    max_err: float = 0.3,
) -> dict:
    """
    Convert one TTF to OTF.

    mode:
      auto      — Pipeline A if tx+checkoutlinesufo available,
                  else Pipeline B if tx available,
                  else Pipeline C (fonttools).
      full      — Pipeline A (best quality).
      direct    — Pipeline B (fast, tx only).
      fonttools — Pipeline C (no AFDKO required).
    """
    if mode == "fonttools":
        return _pipeline_fonttools(ttf_path, otf_path, max_err)

    if mode == "full":
        if not TX:
            raise RuntimeError("--mode full requires AFDKO tx")
        return _pipeline_ufo(ttf_path, otf_path, max_err)

    if mode == "direct":
        if not TX:
            raise RuntimeError("--mode direct requires AFDKO tx")
        return _pipeline_direct_cff(ttf_path, otf_path)

    # auto: use Pipeline B (direct CFF) when tx is available — faster and
    #       equally accurate for TTF→OTF conversion. Use --mode full for
    #       Pipeline A (UFO) when maximum quality is needed.
    if not TX:
        return _pipeline_fonttools(ttf_path, otf_path, max_err)
    return _pipeline_direct_cff(ttf_path, otf_path)


def process_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    recursive: bool = False,
    mode: str = "auto",
    max_err: float = 0.3,
) -> None:
    pattern = "**/*.ttf" if recursive else "*.ttf"
    ttf_files = sorted(input_dir.glob(pattern))

    if not ttf_files:
        print(f"No TTF files found in {input_dir}")
        sys.exit(1)

    total = len(ttf_files)
    success = failed = skipped = 0
    t0 = time.time()

    print(f"Found {total} TTF file(s) in {input_dir}")
    print(f"Mode: {mode}  |  max_err: {max_err}")
    print(f"{_banner()}\n")

    for i, ttf_path in enumerate(ttf_files, 1):
        rel      = ttf_path.relative_to(input_dir)
        otf_path = output_dir / rel.with_suffix(".otf")

        print(f"[{i}/{total}] {rel}  →  ", end="", flush=True)

        if otf_path.exists():
            print("SKIP (exists)")
            skipped += 1
            continue

        try:
            stats = convert_ttf_to_otf(
                ttf_path, otf_path,
                mode=mode, max_err=max_err,
            )
            ratio = stats["size_out"] / stats["size_in"] if stats["size_in"] else 0
            print(f"OK  ({stats['glyphs']} glyphs, "
                  f"{stats['size_in']:,} → {stats['size_out']:,} bytes, "
                  f"{ratio:.1%})")
            success += 1
        except Exception as e:
            print(f"FAIL: {e}")
            failed += 1

    elapsed = time.time() - t0
    print(f"\n--- Done in {elapsed:.1f}s ---")
    print(f"    Success: {success}  Failed: {failed}  "
          f"Skipped: {skipped}  Total: {total}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert TTF fonts to OTF (CFF) using AFDKO tools."
    )
    parser.add_argument("input_dir",  type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("-r", "--recursive", action="store_true")
    parser.add_argument(
        "--mode", choices=["auto", "full", "direct", "fonttools"],
        default="auto",
        help="auto: use best available AFDKO pipeline  "
             "full: tx -ufo → checkoutlinesufo → fonttools (best)  "
             "direct: tx -cff → fonttools (fast)  "
             "fonttools: pure fonttools fallback",
    )
    parser.add_argument("--max-err", type=float, default=0.3,
                        help="Max quadratic→cubic error (default: 0.3). "
                             "Lower = sharper, larger files.")

    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"Error: {args.input_dir} is not a directory")
        sys.exit(1)

    process_directory(
        args.input_dir, args.output_dir,
        recursive=args.recursive,
        mode=args.mode,
        max_err=args.max_err,
    )


if __name__ == "__main__":
    main()
