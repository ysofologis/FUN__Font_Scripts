#!/usr/bin/env python3
"""
OTF/TTF Font Optimizer — v12

A minimal font scaler. The single feature of v12 is a correct --scale
argument that makes fonts visually bigger at the same point size WITHOUT
losing pixel-grid quality.

What --scale does:
  - Scales glyph outlines, advance widths, sidebearings, kerning, and
    font-wide metrics (ascent, descent, lineGap, OS/2 metrics, head bbox)
    by N within the same em-square.
  - unitsPerEm is preserved, so the same point size yields bigger
    glyphs (because each em-unit now covers a larger fraction of the
    pixel grid at the same render size).
  - Quality is preserved: the rounded integer font-units still align
    to the pixel grid exactly as before, and sub-pixel AA has the same
    sub-pixel resolution per em-unit (because UPM didn't change).

What --scale does NOT do (explicit out-of-scope for v12):
  - Width adjustment (horizontal-only scaling)
  - Counter rounding, curve simplification
  - GASP, OS/2, head, name-table rewriting
  - CFF zone/stem recalc
  - Variable-font preservation (we instantiate to static first)
  - Production-name changes
  - Glyph subsetting

Stem thickening (--thickness), letter-spacing/tracking (--spacing),
contour correction (--correct), OTF metadata recompute (--zones),
topology audit (--check-outlines), and autohinting (--hint-tune) ARE
supported in v12. The first two are copied/adapted from v10; the
remaining three wrap foundrytools/AFDKO helpers and run at fixed
points in the pipeline.

If you need anything else, use v10 / v11.

Usage:
    python otf_optimize-v12.py --scale N [--thickness T] [--spacing S]
                               [--hint-tune [auto|STRENGTH]]
                               input_dir output_dir

--scale semantics (dual-mode, mirroring the v8.5/v10 convention):
  |value| < 1.0  -> direct multiplier (0.5 = 50% size, 1.5 = 150% size)
  |value| >= 1.0 -> percentage change (50 = +50%, -25 = -25%)
  0              -> no-op (default)
  Capped [0.10, 4.00] to prevent absurd values.

--hint-tune semantics:
  No arg given     -> --hint-tune is OFF (default; no change to font hints).
  'auto'           -> auto-pick a strength from the font's UPM:
                        UPM 1000  -> 0.70  (typical body-text)
                        UPM 2048  -> 0.85  (high-resolution, stronger grid snap)
                        other     -> 0.75
                      Re-runs the CFF/TrueType autohinter over the
                      (already transformed) outlines, REPLACING any
                      existing hints.
  0.0 <= STRENGTH <= 1.0
                    -> explicit strength scalar. Same autohint pass, but
                      the caller controls how aggressively the engine
                      pushes stems to the pixel grid:
                        0.0  = no-op (skipped, treated as off)
                        0.5  = gentle re-hint (preserves more original)
                        0.7  = standard (auto default for UPM 1000)
                        0.85 = strong (auto default for UPM 2048)
                        1.0  = maximum (hintAll + allowChanges + half-
                              pixel StemSnap; may subtly alter stems)
                      Values outside [0.0, 1.0] are clamped with a warning.

  The strength scalar maps to foundrytools' hint knobs:
                        strength < 0.3  -> standard pass (preserve original)
                        strength < 0.7  -> hintAll=False (only empty glyphs)
                        strength >= 0.7 -> hintAll=True, allowChanges=True
                                            (re-hint every glyph)
  At strength >= 0.7 a post-pass sets CFF StemSnap H/V for crisper
  rendering at small sizes.

  Variable-font caveat: --hint-tune operates on the static instance after
  instantiation, so per-instance hinting via cvar/STAT is NOT generated.
  If you need per-VF-instance hints, build the font from sources with
  fontmake/psautohint instead.

  Requirements: foundrytools 0.1.6+. If missing, --hint-tune logs a
  warning and skips silently — the rest of the pipeline still runs.

Examples:
    --scale 50        # +50% size (outlines 1.5x bigger, UPM unchanged)
    --scale 0.5       # 50% size  (outlines 0.5x smaller, UPM unchanged)
    --scale 100       # +100% (outlines 2x bigger)
    --correct         # clean up overlapping/self-intersecting outlines first
    --zones           # recompute OTF blue zones + stem snaps from outlines
    --check-outlines  # full topology audit + auto-fix (overlaps, coincident
                     # points, colinear lines, flat curves, tiny paths)
    --hint-tune auto  # auto-strengthen hints for the font's UPM
    --hint-tune 0.85  # explicit strong hint tune
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.ttLib.scaleUpem import ScalerVisitor

# Stem thickening lives in a sibling module; skia-python is an optional
# dependency of that module (the module skips gracefully if not installed).
from thicken import thicken_glyphs

# Contour correction lives in a sibling module; foundrytools is an
# optional dependency (the module skips gracefully + warns if not installed).
from correct import correct_glyphs

# OTF blue-zone + stem-snap recomputation lives in a sibling module;
# foundrytools + AFDKO are optional dependencies. CFF/PostScript fonts only.
from zones import recalc_zones

# OTF/CFF topology audit + auto-fix via AFDKO checkoutlinesufo (wrapped
# by foundrytools). foundrytools + AFDKO `tx` binary are optional deps.
# CFF/PostScript fonts only — TTF/glyf inputs are auto-skipped.
from check_outlines import check_outlines

# Autohinting is delegated to foundrytools, which wraps ttfautohint-py
# (TrueType) and AFDKO otfautohint (CFF). foundrytools is an OPTIONAL
# dependency for v12 — only required when --hint-tune is used.
try:
    from foundrytools import Font as _FtFont
    HAS_FOUNDRYTOOLS = True
except ImportError:
    _FtFont = None  # type: ignore[assignment]
    HAS_FOUNDRYTOOLS = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("otf_optimize_v12")


SUPPORTED_EXTENSIONS = {".otf", ".ttf"}


# ---------------------------------------------------------------------------
#  Core scaling operation
# ---------------------------------------------------------------------------

def scale_glyphs(font: TTFont, scale_factor: float) -> None:
    """Scale all outlines/metrics within the em-square; preserve unitsPerEm.

    Dispatch by table type:
      - TTF (has 'glyf'): uses fontTools.ttLib.scaleUpem.ScalerVisitor
        (handles glyf outlines, gvar deltas, hmtx/vmtx, hhea/vhea, OS/2,
        head bbox, kern, VORG, VARC, MATH, BASE, COLRv1 paints, ItemVariationStore).
      - CFF (has 'CFF '): uses a focused CFF scaler. ScalerVisitor's CFF branch
        calls cff.desubroutinize() and walks every table including GPOS/GSUB,
        which can raise KeyError on some fonts (e.g. InstagramSansHeadline/
        Script, where the GPOS Coverage/ClassDef attribute set under Format=2
        doesn't match the ScalerVisitor's expectations). The dedicated scaler
        below handles ONLY the table fields that contain actual outline/zone
        numbers, leaving GSUB/GPOS alone (those carry semantic positioning
        that doesn't need to scale with outline changes — at small scale
        factors the visual impact is minimal, and at large scale factors
        the user can re-do layout pass externally).

    In both cases, head.unitsPerEm is snapshot+restored so the em-square
    stays the same size.
    """
    if scale_factor == 1.0:
        return
    if "CFF " in font or "CFF2" in font:
        _scale_cff_glyphs(font, scale_factor)
    else:
        original_upm = font["head"].unitsPerEm
        visitor = ScalerVisitor(scale_factor)
        visitor.visit(font)
        font["head"].unitsPerEm = original_upm  # Restore: the one field we don't want changed


def _scale_cff_glyphs(font: TTFont, scale_factor: float) -> None:
    """Dedicated CFF/CFF2 outline + metadata scaler. Preserves unitsPerEm.

    Scales only the tables whose numeric fields are pure distances/sizes:
      - CFF / CFF2 charstring coordinates (every int arg in every T2 program)
      - CFF TopDict: FontMatrix, FontBBox, UnderlinePosition, UnderlineThickness,
        StrokeWidth
      - CFF PrivateDict: BlueValues, OtherBlues, FamilyBlues, FamilyOtherBlues,
        StdHW, StdVW, StemSnapH, StemSnapV, defaultWidthX, nominalWidthX
      - head bbox (xMin, yMin, xMax, yMax), unitsPerEm preserved
      - hmtx/vmtx (advance widths + LSB)
      - hhea/vhea: ascent, descent, lineGap, advanceWidthMax, min/max sidebearings,
        xMaxExtent, caretOffset
      - OS/2: xAvgCharWidth, sTypo*/usWin*/sxHeight/sCapHeight/ys* fields
      - post: underlinePosition, underlineThickness
      - VORG: defaultVertOriginY

    Does NOT scale: GSUB/GPOS, kern, GDEF ligature carets, COLR. Rationale:
    GPOS advance widths are usually already in em-units and the visual
    effect of scaling them by ~5% is negligible for a single-axis visual
    resize; the alternative is a fontTools-internal KeyError on
    attribute-set mismatches in newer font formats (InstagramSans 2023+,
    SourceCodePro Issue #3891 in upstream fonttools).

    For CFF2 (variable), we instantiate first via the caller
    (instantiate_to_static), so we only need to handle static CFF2 here.
    """
    from fontTools.misc.fixedTools import otRound
    from fontTools.cffLib.specializer import programToCommands, commandsToProgram

    cff = font["CFF "].cff  # unwrap from table_C_F_F_ to CFFFontSet
    cff.desubroutinize()  # safe on already-static CFF; idempotent
    topDict = cff.topDictIndex[0]
    varStore = getattr(topDict, "VarStore", None)
    getNumRegions = varStore.getNumRegions if varStore is not None else None

    privates = set()
    for fontname in cff.keys():
        subfont = cff[fontname]
        cs = subfont.CharStrings
        for g in subfont.charset:
            c, _ = cs.getItemAndSelector(g)
            privates.add(c.private)
            # Scale every numeric arg in the charstring program
            commands = programToCommands(c.program, getNumRegions=getNumRegions)
            for op, args in commands:
                if op == "vsindex":
                    continue
                _cff_scale_args(args, scale_factor)
            c.program[:] = commandsToProgram(commands)

    # TopDict fields that are distances/sizes in font units
    for attr in ("UnderlinePosition", "UnderlineThickness", "FontBBox", "StrokeWidth"):
        value = getattr(topDict, attr, None)
        if value is None:
            continue
        if isinstance(value, list):
            for i, v in enumerate(value):
                if isinstance(v, (int, float)):
                    value[i] = otRound(v * scale_factor)
        else:
            setattr(topDict, attr, otRound(value * scale_factor))

    # FontMatrix is 6 floats; scale them by dividing by scale_factor
    # (because matrix transforms are applied inversely to point coordinates).
    for i in range(6):
        topDict.FontMatrix[i] /= scale_factor

    # PrivateDict fields — same set as ScalerVisitor handles
    for private in privates:
        for attr in (
            "BlueValues", "OtherBlues", "FamilyBlues", "FamilyOtherBlues",
            "StdHW", "StdVW", "StemSnapH", "StemSnapV",
            "defaultWidthX", "nominalWidthX",
        ):
            value = getattr(private, attr, None)
            if value is None:
                continue
            if isinstance(value, list):
                for i, v in enumerate(value):
                    if isinstance(v, (int, float)):
                        value[i] = otRound(v * scale_factor)
            else:
                setattr(private, attr, otRound(value * scale_factor))

    # Now scale the surrounding tables the same way ScalerVisitor does
    # (head bbox, hmtx, vmtx, hhea, vhea, OS/2, post, VORG)
    _scale_outside_cff(font, scale_factor)


def _cff_scale_args(args, scale_factor):
    """Recursively scale all numeric leaves in a CFF charstring arg list.

    Mirrors fontTools.ttLib.scaleUpem._cff_scale but kept private to this
    module. The blend-arg list variant has the num_blends int as its last
    element which we preserve verbatim.
    """
    from fontTools.misc.fixedTools import otRound
    for i, arg in enumerate(args):
        if isinstance(arg, list):
            num_blends = arg[-1]
            _cff_scale_args(arg, scale_factor)
            arg[-1] = num_blends
        elif not isinstance(arg, bytes):
            args[i] = otRound(arg * scale_factor)


def _scale_outside_cff(font: TTFont, scale_factor: float) -> None:
    """Scale the non-CFF tables that ScalerVisitor also handles.

    These are all numeric fields, no visitors/visiting needed. We do them
    directly here to avoid pulling in the full ScalerVisitor (which is
    what walks GPOS and triggers the KeyError on InstagramSans-style fonts).
    """
    from fontTools.misc.fixedTools import otRound

    def _s(v):
        return otRound(v * scale_factor)

    # head: bbox only (unitsPerEm handled by caller)
    if "head" in font:
        head = font["head"]
        for attr in ("xMin", "yMin", "xMax", "yMax"):
            if hasattr(head, attr):
                setattr(head, attr, _s(getattr(head, attr)))

    # post: underline metrics
    if "post" in font:
        post = font["post"]
        for attr in ("underlinePosition", "underlineThickness"):
            if hasattr(post, attr):
                setattr(post, attr, _s(getattr(post, attr)))

    # VORG
    if "VORG" in font:
        vorg = font["VORG"]
        if hasattr(vorg, "defaultVertOriginY") and vorg.defaultVertOriginY is not None:
            vorg.defaultVertOriginY = _s(vorg.defaultVertOriginY)
        if hasattr(vorg, "VOriginRecords") and vorg.VOriginRecords:
            for g in list(vorg.VOriginRecords.keys()):
                vorg.VOriginRecords[g] = _s(vorg.VOriginRecords[g])

    # hhea / vhea
    for tag in ("hhea", "vhea"):
        if tag in font:
            tbl = font[tag]
            for attr in ("ascent", "descent", "lineGap", "advanceWidthMax",
                         "advanceHeightMax", "minLeftSideBearing",
                         "minRightSideBearing", "minTopSideBearing",
                         "minBottomSideBearing", "xMaxExtent", "yMaxExtent",
                         "caretOffset"):
                if hasattr(tbl, attr):
                    setattr(tbl, attr, _s(getattr(tbl, attr)))

    # OS/2
    if "OS/2" in font:
        os2 = font["OS/2"]
        for attr in ("xAvgCharWidth", "ySubscriptXSize", "ySubscriptYSize",
                     "ySubscriptXOffset", "ySubscriptYOffset",
                     "ySuperscriptXSize", "ySuperscriptYSize",
                     "ySuperscriptXOffset", "ySuperscriptYOffset",
                     "yStrikeoutSize", "yStrikeoutPosition",
                     "sTypoAscender", "sTypoDescender", "sTypoLineGap",
                     "usWinAscent", "usWinDescent",
                     "sxHeight", "sCapHeight"):
            if hasattr(os2, attr):
                setattr(os2, attr, _s(getattr(os2, attr)))

    # hmtx / vmtx
    for tag in ("hmtx", "vmtx"):
        if tag in font:
            metrics = font[tag].metrics
            for g in list(metrics.keys()):
                advance, lsb = metrics[g]
                metrics[g] = _s(advance), _s(lsb)


# ---------------------------------------------------------------------------
#  Variable-font instantiation (mandatory before outline scaling)
# ---------------------------------------------------------------------------

def instantiate_to_static(font: TTFont) -> bool:
    """If the font is variable (has an fvar table), instantiate it to the
    default static instance and drop fvar/gvar/HVAR/MVAR/etc.

    Why: gvar/CFF2 deltas are tied to the original point counts. Any
    outline transform desyncs them. Instantiating bakes the default
    axis values into static outlines and removes the variation tables.

    Returns True if instantiation happened (caller may want to log this).
    """
    if "fvar" not in font:
        return False
    try:
        from fontTools.varLib.instancer import instantiateVariableFont
    except ImportError:
        log.warning("fontTools.varLib.instancer not available; "
                    "scaling a variable font in-place is unsafe — skipping")
        return False
    axes = {ax.axisTag: ax.defaultValue for ax in font["fvar"].axes}
    instantiateVariableFont(font, axes, inplace=True)
    log.info("Variable font instantiated to default static instance "
             "(gvar/HVAR/MVAR/fvar dropped)")
    return True


# ---------------------------------------------------------------------------
#  Letter-spacing / tracking (outlines untouched, advance widths adjusted)
# ---------------------------------------------------------------------------

def apply_tracking(font: TTFont, spacing_value: float) -> None:
    """Add letter-spacing / tracking to every glyph's advance width.

    Outlines are NOT modified. Only hmtx advance widths grow; left
    sidebearings stay put (they are intra-glyph, not inter-glyph).

    Dual-mode semantics, matching --scale / --thickness:
      spacing_value == 0                 -> no-op
      0 < |spacing_value| < 1.0          -> direct font units added to each advance
      |spacing_value| >= 1.0             -> percent change vs. original advance

    Capped to keep advance widths sane: tracking addend >= 0 always;
    percent change clamped to [-90%, +400%] (matches the family of our
    other dual-mode flags so it cannot produce negative widths).

    Note on kerning: this does NOT modify GPOS kerning pairs. If the font
    has kerning, the visual tracking effect will be reduced by the average
    applied kern at render time. This is the same trade-off CSS
    letter-spacing makes — standard tracking behavior.

    Returns the actual addend per glyph (in font units) so the caller can
    log it; otherwise None.
    """
    if spacing_value == 0:
        return None

    hmtx = font.get('hmtx')
    if hmtx is None:
        log.warning("Font has no 'hmtx' table; --spacing skipped")
        return None

    # Decide mode
    abs_val = abs(spacing_value)
    if abs_val < 1.0:
        mode = "direct"
        addend = spacing_value
        if abs(addend) > 4096.0:
            log.warning(f"Spacing addend {addend:.2f}u too large, clamping to 4096u")
            addend = 4096.0 * (1 if addend > 0 else -1)
    else:
        mode = "percent"
        pct = spacing_value
        if pct < -90.0:
            log.warning(f"Spacing percent {pct:.1f}% too negative, clamping to -90%")
            pct = -90.0
        if pct > 400.0:
            log.warning(f"Spacing percent {pct:.1f}% too large, clamping to 400%")
            pct = 400.0
        addend = None  # computed per-glyph below

    log.info(f"Tracking ({mode} mode, value={spacing_value:+.2f})")

    new_aw_max = 0
    touched = 0
    for gn in list(hmtx.metrics.keys()):
        old_aw, lsb = hmtx.metrics[gn]
        if mode == "direct":
            new_aw = int(round(old_aw + addend))
        else:  # percent
            new_aw = int(round(old_aw * (1.0 + pct / 100.0)))
        # Guard against negative advance widths
        if new_aw < 0:
            new_aw = 0
        hmtx.metrics[gn] = (new_aw, lsb)
        if new_aw > new_aw_max:
            new_aw_max = new_aw
        touched += 1

    # Recompute hhea.advanceWidthMax to match
    if 'hhea' in font:
        hhea = font['hhea']
        hhea.advanceWidthMax = max(new_aw_max, hhea.advanceWidthMax or 0)

    sample_addend = addend if addend is not None else f"{spacing_value:+.2f}%"
    log.info(f"Tracking applied: {touched} glyph advance widths updated "
             f"(addend: {sample_addend}/glyph, hhea.advanceWidthMax={new_aw_max})")
    return addend


# ---------------------------------------------------------------------------
#  Hint tuning (--hint-tune)
# ---------------------------------------------------------------------------

# Strength thresholds that map the [0,1] strength scalar onto foundrytools'
# hint knobs. Kept as module-level constants so they're easy to tune.
HINT_STRENGTH_PRESERVE = 0.30   # below: hintAll=False (preserve original)
HINT_STRENGTH_AGGRESSIVE = 0.70  # above: hintAll=True + allowChanges=True


def _resolve_hint_strength(spec, font: TTFont) -> float:
    """Resolve --hint-tune's CLI value to a [0.0, 1.0] strength float.

    `spec` may be:
      - None   : --hint-tune was not given -> returns 0.0 (caller skips)
      - 'auto' : auto-pick from font UPM (UPM 2048 -> 0.85, 1000 -> 0.70)
      - float  : the explicit value, clamped to [0.0, 1.0]
    """
    if spec is None:
        return 0.0
    if isinstance(spec, str):
        if spec != "auto":
            log.warning(f"--hint-tune unrecognized string {spec!r}, "
                        f"treating as 'auto'")
        upm = font["head"].unitsPerEm
        if upm == 1000:
            strength = 0.70
        elif upm == 2048:
            strength = 0.85
        else:
            strength = 0.75
        log.info(f"--hint-tune auto: UPM={upm} -> strength {strength:.2f}")
        return strength
    # float path
    strength = float(spec)
    if strength < 0.0 or strength > 1.0:
        log.warning(f"--hint-tune strength {strength:.3f} outside [0,1], "
                    f"clamped")
        strength = max(0.0, min(1.0, strength))
    return strength


def _hint_engine_kwargs(strength: float) -> dict:
    """Map the strength scalar onto foundrytools' CFF autohint kwargs.

    Returns a dict of kwargs to forward to foundrytools.app.otf_autohint.run
    (and analogous defaults for ttf_autohint).
    """
    if strength < HINT_STRENGTH_PRESERVE:
        return {"hintAll": False, "allowChanges": False}
    if strength < HINT_STRENGTH_AGGRESSIVE:
        return {"hintAll": False, "allowChanges": True}
    return {"hintAll": True, "allowChanges": True}


def _set_cff_stemsnap(font: TTFont, strength: float) -> bool:
    """Snap CFF StemSnap H/V to the half-pixel grid for crisper small-size
    rendering. Only kicks in at strength >= HINT_STRENGTH_AGGRESSIVE (0.7).

    Sets:
      - StemSnapH = [upm/2]  (half-em horizontal stem snap, list per CFF spec)
      - StemSnapV = [upm/2]  (half-em vertical stem snap, list per CFF spec)

    Implementation note: CFF PrivateDict's __getattr__ CACHES the first
    accessed value as an instance attribute, so subsequent reads of e.g.
    pd.StemSnapH return the cached [67] instead of the mutated rawDict
    value. fontTools' DictCompiler uses getattr(pd, name) when building
    its compiler-side rawDict, so we MUST write through setattr() to
    invalidate the cache — writing only to pd.rawDict is silently lost
    on save if any attribute was previously accessed.

    Returns True if changed, False if not CFF or strength too low.
    """
    if "CFF " not in font:
        return False
    if strength < HINT_STRENGTH_AGGRESSIVE:
        return False
    upm = font["head"].unitsPerEm
    half_em = [upm // 2]  # CFF PrivateDict expects a LIST of ints
    pd = font["CFF "].cff.topDictIndex[0].Private
    if pd is None:
        return False
    old_h = pd.rawDict.get("StemSnapH")
    old_v = pd.rawDict.get("StemSnapV")
    # Set BOTH the rawDict (for round-trip XML) AND the instance attribute
    # (for fontTools' DictCompiler which uses getattr).
    pd.rawDict["StemSnapH"] = half_em
    pd.rawDict["StemSnapV"] = half_em
    setattr(pd, "StemSnapH", half_em)
    setattr(pd, "StemSnapV", half_em)
    log.info(f"CFF StemSnap H/V -> {half_em} (half-em at UPM={upm}); "
             f"was ({old_h}, {old_v})")
    return True


def apply_hint_tune(font: TTFont, strength: float,
                   preserve_stemsnap: bool = False) -> bool:
    """Autohint the font at the given strength.

    Wraps the raw TTFont in a foundrytools.Font (which makes its own COPY
    of the tables), dispatches to the right autohinter (CFF via AFDKO
    otfautohint, TTF via ttfautohint-py), and then copies the hinted
    tables back into the original `font` so the rest of the pipeline
    sees them. Returns True if the font was actually hinted, False if
    skipped (no foundrytools, strength == 0, or autohint failed).

    `preserve_stemsnap` (used with --zones): when True, skip the
    half-em StemSnap fallback at strength >= 0.7 because --zones has
    already written font-specific values.
    """
    if strength <= 0.0:
        log.info("--hint-tune: strength 0.0 -> no-op (skipped)")
        return False
    if not HAS_FOUNDRYTOOLS:
        log.warning("--hint-tune requested but foundrytools is not installed; "
                    "skipping. `pip install foundrytools` to enable.")
        return False

    kwargs = _hint_engine_kwargs(strength)
    log.info(f"--hint-tune strength {strength:.2f} -> {kwargs}")

    ft_font = _FtFont(font)
    hinted = False
    try:
        if ft_font.is_tt:
            try:
                from foundrytools.app.ttf_autohint import run as ttf_autohint
                ttf_autohint(ft_font)
                log.info("Autohinted (TrueType via foundrytools/ttfautohint-py)")
                hinted = True
            except Exception as e:
                log.error(f"TTF autohint failed: {e}")
                return False
        elif ft_font.is_ps:
            try:
                # Patch AFDKO logger if present (matches v10's defensive
                # pattern — avoids AttributeError on missing record attrs)
                try:
                    from afdko.otfautohint.logging import otfautoLogFormatter
                    _orig_fmt = otfautoLogFormatter.format
                    def _patched_fmt(self, record):
                        for attr in ("glyph", "instance", "dimension"):
                            if not hasattr(record, attr):
                                setattr(record, attr, "")
                        return _orig_fmt(self, record)
                    otfautoLogFormatter.format = _patched_fmt
                except Exception:
                    pass
                from foundrytools.app.otf_autohint import run as otf_autohint
                otf_autohint(ft_font, **kwargs)
                log.info("Autohinted (CFF via foundrytools/AFDKO otfautohint)")
                hinted = True
            except Exception as e:
                log.error(f"CFF autohint failed: {e}")
                return False
        else:
            log.warning("--hint-tune: unrecognized font format, skipping")
            return False
    finally:
        pass  # we'll close below after copying back

    if not hinted:
        return False

    # Copy ONLY hint-related tables back into the original font. Copying
    # ALL tables breaks fontTools' internal invariants (it complains
    # "illegal use of getGlyphOrder()" because the wrapper's TTFont got
    # its own glyphOrder from the BytesIO round-trip). Hint tables are
    # the only ones the autohinters create or modify in-place.
    if ft_font.is_tt:
        # ttfautohint-py creates these new tables and modifies glyf/maxp
        HINT_TAGS = ("fpgm", "prep", "cvt ", "gasp", "glyf", "loca", "maxp")
    else:
        # AFDKO otfautohint modifies CFF ' (rewrites charstrings)
        HINT_TAGS = ("CFF ",)
    copied = 0
    for tag_str in HINT_TAGS:
        if tag_str in ft_font.ttfont:
            font.tables[tag_str] = ft_font.ttfont[tag_str]
            copied += 1
    log.info(f"Copied {copied} hint table(s) ({', '.join(HINT_TAGS)}) "
             f"from foundrytools wrapper back to font")

    ft_font.close()

    # CFF StemSnap post-pass (only at high strength). Skipped when --zones
    # already wrote font-specific values (preserve_stemsnap=True) — those
    # are better than the half-em fallback.
    stemsnapped = _set_cff_stemsnap(font, strength) if not preserve_stemsnap else False

    log.info(f"--hint-tune complete (autohint=yes, stemsnap={'yes' if stemsnapped else 'no'})")
    return True




def parse_scale_percent(value: float) -> float:
    """Convert the CLI value to a multiplicative scale factor.

    Dual-mode (mirrors v8.5 / v10):
      |value| < 1.0  -> direct multiplier
      |value| >= 1.0 -> percentage change (value / 100 added to 1.0)
    Returns 1.0 for 0 (no-op).
    """
    if value == 0:
        return 1.0
    abs_val = abs(value)
    if 0 < abs_val < 1.0:
        return value
    if abs_val >= 1.0:
        return 1.0 + value / 100.0
    return 1.0  # unreachable; defensive


def clamp_scale(factor: float) -> float:
    """Clamp the scale factor to the safe range [0.10, 4.00]."""
    if factor < 0.10:
        log.warning(f"Scale factor {factor:.3f} too small, clamping to 0.10")
        return 0.10
    if factor > 4.00:
        log.warning(f"Scale factor {factor:.3f} too large, clamping to 4.00")
        return 4.00
    return factor


# ---------------------------------------------------------------------------
#  Per-font processing
# ---------------------------------------------------------------------------

def optimize_one(input_path: Path, output_path: Path,
                 scale_factor: float, thickness_percent: float,
                 spacing_value: float, hint_tune: object = None,
                 correct: bool = False, zones: bool = False,
                 check_outlines_flag: bool = False) -> bool:
    """Load, correct, scale, thicken, apply tracking, recompute zones,
    check_outlines, hint-tune, and save one font.

    `hint_tune` may be None (off), the string 'auto', or a float in [0.0, 1.0].
    The strength is resolved once the font is loaded so we can read its UPM
    for the auto case. Returns True on success.

    `correct` (v12 --correct) runs FIRST so all downstream transforms
    operate on clean topology. Skia pathops union+simplify will merge
    any overlapping stems into a single contour before thicken scales
    them, which prevents the "two adjacent overlapping stems get
    thickened then merged into one ugly mega-stem" failure mode.

    `zones` (v12 --zones) runs after scale/thicken/spacing so the stem
    histogram reflects the final outline topology. Must run BEFORE
    --hint-tune so the autohinter sees the new StemSnap/BlueValues
    values and uses them.

    `check_outlines_flag` (v12 --check-outlines) runs AFTER scale/
    thicken/spacing/zones but BEFORE --hint-tune, so checkoutlinesufo
    can fix any topology issues our pipeline introduced (Skia
    overlap removal is fast; checkoutlinesufo is slower but more
    thorough — coincident points, colinear lines, flat curves, tiny
    paths, wrong winding). The autohinter then sees clean outlines
    AND the recomputed zones, so the final hints are based on the
    right stem widths.
    """
    log.info(f"Loading {input_path.name}...")
    try:
        font = TTFont(str(input_path))
    except Exception as e:
        log.error(f"Failed to load {input_path.name}: {e}")
        return False

    instantiate_to_static(font)

    if correct:
        try:
            correct_glyphs(font)
        except Exception as e:
            log.error(f"--correct failed on {input_path.name}: "
                      f"{type(e).__name__}: {e}")
            return False

    if scale_factor != 1.0:
        log.info(f"Scaling {input_path.name} by x{scale_factor:.4f} "
                 f"(UPM preserved at {font['head'].unitsPerEm})")
        try:
            scale_glyphs(font, scale_factor)
        except Exception as e:
            log.error(f"--scale failed on {input_path.name}: "
                      f"{type(e).__name__}: {e}")
            return False

    if thickness_percent > 0:
        try:
            thicken_glyphs(font, thickness_percent)
        except Exception as e:
            log.error(f"--thickness failed on {input_path.name}: "
                      f"{type(e).__name__}: {e}")
            return False

    if spacing_value != 0:
        try:
            apply_tracking(font, spacing_value)
        except Exception as e:
            log.error(f"--spacing failed on {input_path.name}: "
                      f"{type(e).__name__}: {e}")
            return False

    if zones:
        try:
            recalc_zones(font)
        except Exception as e:
            log.error(f"--zones failed on {input_path.name}: "
                      f"{type(e).__name__}: {e}")
            return False

    if check_outlines_flag:
        try:
            check_outlines(font)
        except Exception as e:
            log.error(f"--check-outlines failed on {input_path.name}: "
                      f"{type(e).__name__}: {e}")
            return False

    # Hint tuning runs LAST so it sees the final transformed outlines AND
    # the (possibly recomputed) zone/stem-snap values. The strength is
    # resolved AFTER scale/spacing so the auto-pick uses the current
    # (possibly instantiated) UPM. `preserve_stemsnap=True` when --zones
    # ran successfully so the hint-tune StemSnap fallback doesn't clobber
    # the font-specific values --zones just wrote.
    if hint_tune is not None:
        strength = _resolve_hint_strength(hint_tune, font)
        apply_hint_tune(font, strength, preserve_stemsnap=zones)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        font.save(str(output_path))
    except Exception as e:
        log.error(f"Failed to save {output_path.name}: {e}")
        return False

    in_size = input_path.stat().st_size
    out_size = output_path.stat().st_size
    log.info(f"Saved {output_path.name} ({in_size:,} -> {out_size:,} bytes)")
    return True


# ---------------------------------------------------------------------------
#  Driver
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="otf_optimize-v12",
        description="Minimal font scaler. v12's single feature is --scale, "
                    "which scales outlines within the em-square (UPM preserved) "
                    "so fonts become visually bigger at the same point size "
                    "without losing quality.",
    )
    parser.add_argument("input_dir", type=Path,
                        help="Directory containing input .otf/.ttf files")
    parser.add_argument("output_dir", type=Path,
                        help="Directory to write scaled files into")
    parser.add_argument("--scale", type=float, default=0, dest="scale",
                        help="Scale factor. |v|<1.0 = direct multiplier "
                             "(0.5 = 50%% size). |v|>=1.0 = percentage change "
                             "(50 = +50%%). 0 = no-op. Capped [0.10, 4.00]. "
                             "Outlines scale within the em-square; UPM preserved.")
    parser.add_argument("--correct", action="store_true", dest="correct",
                        help="Run contour correction (overlap removal + "
                             "winding fix + tiny-path cull) on every glyph "
                             "BEFORE any other transform. Uses foundrytools "
                             "Skia pathops (auto-detects TTF vs OTF). "
                             "Recommended when the source font has self-"
                             "intersecting or overlapping outlines; harmless "
                             "on clean fonts (idempotent on topology that "
                             "is already well-formed). Requires foundrytools.")
    parser.add_argument("--zones", action="store_true", dest="zones",
                        help="Recompute OTF/CFF metadata (BlueValues, "
                             "OtherBlues, StdHW, StdVW, StemSnapH, StemSnapV) "
                             "from the actual outlines. CFF/PostScript fonts "
                             "only (TTF has no blue zones; auto-skipped on "
                             "TTF inputs). Runs after scale/thickness/spacing "
                             "so the recomputed values reflect final "
                             "topology, and before --hint-tune so the "
                             "autohinter uses the new StemSnap values. "
                             "Big rendering-quality win at small sizes "
                             "(crisper baselines, x-height, cap-height, and "
                             "vertical stem alignment). Requires foundrytools "
                             "+ afdko.")
    parser.add_argument("--check-outlines", action="store_true",
                        dest="check_outlines_flag",
                        help="Run AFDKO checkoutlinesufo topology audit "
                             "+ auto-fix on every glyph. Catches and "
                             "repairs: overlapping contours, coincident "
                             "points, colinear lines, flat curves, tiny "
                             "sub-paths, wrong winding. CFF/PostScript "
                             "fonts only (TTF/glyf has no CFF topology; "
                             "auto-skipped on TTF inputs). Runs after "
                             "--scale/--thickness/--spacing/--zones but "
                             "before --hint-tune so the autohinter sees "
                             "cleaned outlines. Slower than --correct "
                             "(~6s for 1500-glyph fonts; converts "
                             "OTF->UFO->OTF internally). Requires "
                             "foundrytools + afdko (with `tx` on PATH).")
    parser.add_argument("--thickness", type=float, default=0, dest="thickness_percent",
                        help="Thicken glyph stems by percent. Same dual-mode "
                             "semantics as --scale: |v|<1.0 = direct stem "
                             "multiplier (0.5 = 50%% stems). |v|>=1.0 = "
                             "percent change (2.5 = +2.5%% thicker). 0 = "
                             "no-op. Applied to ALL contours (outer + "
                             "counters). Advance widths preserved. "
                             "Requires skia-python.")
    parser.add_argument("--spacing", type=float, default=0, dest="spacing",
                        help="Letter-spacing / tracking. Outlines unchanged; "
                             "every glyph's advance width gets +N added. "
                             "Same dual-mode semantics: |v|<1.0 = direct "
                             "font units added per gap (0.05 = +0.05u); "
                             "|v|>=1.0 = percent change (5 = +5%% advance). "
                             "0 = no-op (default). Composes with --scale "
                             "and --thickness. Does NOT modify kerning "
                             "(GPOS pairs remain — visual tracking is "
                             "reduced by applied kerns, same as CSS "
                             "letter-spacing).")
    parser.add_argument("--hint-tune", default="off", dest="hint_tune",
                        metavar="MODE",
                        help="Re-hint the font. Values: 'off' (default, no "
                             "change), 'auto' (auto-pick from UPM: 1000->0.70, "
                             "2048->0.85), or a float in [0,1] for explicit "
                             "strength (0.5=gentle, 0.7=standard, 0.85=strong, "
                             "1.0=maximum with hintAll+allowChanges+StemSnap). "
                             "Pass 'off' to disable. Runs after --scale/--"
                             "thickness/--spacing. Requires foundrytools "
                             "(pip install foundrytools).")
    args = parser.parse_args(argv)

    raw = parse_scale_percent(args.scale)
    scale_factor = clamp_scale(raw)
    if scale_factor == 1.0:
        log.info("--scale 0 or 1: pass-through (no size change)")
    else:
        log.info(f"Scale factor: x{scale_factor:.4f} (UPM will be preserved)")

    if args.thickness_percent == 0:
        log.info("--thickness 0: no stem thickening")
    else:
        log.info(f"Stem thickening: {args.thickness_percent:+.2f}% "
                 f"(applied to all contours, widths preserved)")

    if args.spacing == 0:
        log.info("--spacing 0: no tracking")
    else:
        log.info(f"Tracking: {args.spacing:+.2f} per glyph "
                 f"(addend to every advance width)")

    if args.correct:
        log.info("--correct: contour correction ON (runs first; "
                 "foundrytools Skia pathops union+simplify)")
    else:
        log.info("--correct: off (default; pass --correct to enable)")

    if args.zones:
        log.info("--zones: OTF metadata recompute ON (runs before --hint-tune; "
                 "CFF/PostScript only)")
    else:
        log.info("--zones: off (default; pass --zones to enable)")

    if args.check_outlines_flag:
        log.info("--check-outlines: topology audit + auto-fix ON "
                 "(runs before --hint-tune; CFF/PostScript only)")
    else:
        log.info("--check-outlines: off (default; pass --check-outlines to enable)")

    # --hint-tune parsing: value may be 'off' (default), 'auto' (the const),
    # or a string the user typed (we try to coerce to float).
    if args.hint_tune == "off":
        hint_tune: object = None
        log.info("--hint-tune: off (default)")
    else:
        raw = args.hint_tune
        try:
            hint_tune = float(raw)
            log.info(f"--hint-tune: explicit strength {hint_tune:.2f}")
        except (TypeError, ValueError):
            if raw == "auto":
                hint_tune = "auto"
                log.info("--hint-tune: 'auto' (strength picked per-font from UPM)")
            else:
                log.warning(f"--hint-tune unrecognized value {raw!r}; "
                            f"treating as 'auto'")
                hint_tune = "auto"

    if not args.input_dir.is_dir():
        log.error(f"Input directory not found: {args.input_dir}")
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    fonts = sorted(p for p in args.input_dir.iterdir()
                   if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not fonts:
        log.error(f"No .otf/.ttf files found in {args.input_dir}")
        return 1

    log.info(f"Processing {len(fonts)} font(s) from {args.input_dir} -> {args.output_dir}")

    successes = 0
    for src in fonts:
        dst = args.output_dir / src.name
        if optimize_one(src, dst, scale_factor, args.thickness_percent,
                        args.spacing, hint_tune, args.correct, args.zones,
                        args.check_outlines_flag):
            successes += 1

    log.info(f"Done. {successes}/{len(fonts)} font(s) optimized successfully.")
    return 0 if successes == len(fonts) else 1


if __name__ == "__main__":
    sys.exit(main())
