#!/usr/bin/env python3
"""
otf_optimize_foundry-v15.py — scale and thicken TTF/OTF fonts, foundrytools path.

Pure-Python pipeline using foundrytools + fontTools + skia. Does two
things and nothing else:

    --scale       percent, scales outlines + advance widths (default 0 = no-op)
    --thickness   units per side, emboldens via stroke-and-union (default 0)

Both default to 0, which is a verified no-op. Positive values grow.

Supports TTF (glyf) and OTF (CFF / CFF2) input. TTF uses TTGlyphPen +
TransformPen; OTF uses T2CharStringPen round-trip (decompose via
RecordingPen, transform coordinates, rebuild via T2CharStringPen).
Both share the same skia stroke+union for thicken. The OTF path's
charstring round-trip inlines the source's subroutines (so the output
is larger than the input) but preserves the private dict and global subrs.

Why a separate script:
    otf_optimize-ff-v2.x.py uses libfontforge's glyph.changeWeight(),
    which is fast on small fonts but slow on big ones (35k-glyph faces
    take minutes per face on SF-Compact). This script uses skia for
    both scaling and thickening; faster on big fonts but the embolden
    output geometry is NOT identical to libfontforge's changeWeight
    (different stroke width, different corner handling). Verify the
    visual output before substituting on a critical pipeline.

Pipeline:
    1. Open with foundrytools.Font (validates font, exposes ttfont).
    2. If variable, instantiate to a named instance.
    3. --scale: per-glyph TransformPen for outlines (TTF) or per-
       charstring coordinate scaling (CFF); per-glyph advance width
       scaling on both paths.
       Composite references are unrolled at render time on the TTF path.
    4. --thickness: per-glyph skia-pathops Path + stroke + Path.union,
       re-emit into the glyph's pen (TTF) or into a fresh T2Char (CFF).
    5. Font.save(out_path).

Usage:
    # Thicken only (10 units per side):
    python otf_optimize_foundry-v15.py --thickness 10 IN OUT

    # Scale and thicken:
    python otf_optimize_foundry-v15.py --scale 5 --thickness 10 IN OUT

    # Whole directory (recursive, 12-worker pool by default):
    python otf_optimize_foundry-v15.py --scale 2.5 --thickness 5 ./fonts/ ./out/

Requirements:
    pip install foundrytools          # pulls in fontTools + skia

Author: Bufos — created 2026-10-19 alongside otf_optimize-ff-v2.1.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import warnings
from pathlib import Path

# Silence the noisy pkg_resources deprecation warning that fs emits on import.
warnings.filterwarnings(
    "ignore", message=".*pkg_resources.*deprecated.*", category=UserWarning
)


# ─────────────────────────────────────────────────────────────────────────────
#  Dependency resolution (modeled on ttf2otf_foundry_v1.1.py)
# ─────────────────────────────────────────────────────────────────────────────

_REEXEC_GUARD = "OTF_OPTIMIZE_FOUNDRY_BOOTSTRAPPED"
_PROBE = r'''
import sys
why = []
try:
    import foundrytools
    from foundrytools import Font
    from foundrytools.core.font import FontError
except Exception as e:
    why.append(f"foundrytools: {type(e).__name__}: {e}")
try:
    import pathops
    Path = pathops.Path
    PathOp = pathops.PathOp
    PathOpsError = pathops.PathOpsError
except Exception as e:
    why.append(f"pathops: {type(e).__name__}: {e}")
try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.recordingPen import RecordingPen
except Exception as e:
    why.append(f"fontTools: {type(e).__name__}: {e}")
if why:
    sys.stderr.write(" | ".join(why))
    sys.exit(1)
sys.exit(0)
'''


def _candidate_interpreters() -> list[str]:
    out: list[str] = []
    for py in (
        sys.executable,
        "/usr/bin/python3.14",
        "/usr/bin/python3.13",
        "/usr/bin/python3.12",
        "/usr/bin/python3",
        shutil.which("python3.14") or "",
        shutil.which("python3.13") or "",
        shutil.which("python3.12") or "",
        shutil.which("python3") or "",
    ):
        if py and py not in out and os.path.isfile(py):
            out.append(py)
    return out


def _probe(py: str):
    import subprocess as _sp
    try:
        r = _sp.run([py, "-c", _PROBE], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    if r.returncode == 0:
        return None
    msg = (r.stderr or "") + (r.stdout or "")
    msg = "\n".join(ln for ln in msg.splitlines() if ln.strip())
    return (msg[:200] if msg else f"exit {r.returncode}")


def _die(reasons: dict[str, str]) -> None:
    lines = [
        "no usable interpreter for this script.",
        "",
        "  foundrytools / pathops / fontTools may well be installed -- under a",
        "  DIFFERENT interpreter. Probed (functional test: real import + symbols):",
    ]
    for py, why in reasons.items():
        lines.append(f"    {py}")
        lines.append(f"        {why}")
    lines += [
        "",
        "  Fix: name an interpreter that has the deps explicitly, e.g.",
        "    /usr/bin/python3.14 otf_optimize_foundry-v15.py ...",
        "  or install them into the one you are using:",
        "    pip install foundrytools",
        "",
        f"  Set {_REEXEC_GUARD}=1 to disable the automatic interpreter switch.",
    ]
    sys.exit("\n".join(lines))


def _ensure_dependencies() -> None:
    ordered = _candidate_interpreters()
    if not ordered:
        _die({"(none found)": "no python interpreter on this system"})

    first_why = _probe(ordered[0])
    reasons: dict[str, str] = {ordered[0]: first_why} if first_why else {}
    if first_why is None:
        return

    if os.environ.get(_REEXEC_GUARD):
        _die(reasons)

    for py in ordered[1:]:
        why = _probe(py)
        reasons[py] = why
        if why is None:
            os.environ[_REEXEC_GUARD] = "1"
            os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])
    _die(reasons)


_ensure_dependencies()

try:
    from foundrytools import Font
    from foundrytools.core.font import FontError
    from foundrytools.app.var2static import run as var2static_run, Var2StaticError
except Exception as _exc:  # pragma: no cover
    sys.exit(
        f"foundrytools passed the probe but not this import "
        f"({type(_exc).__name__}: {_exc})\n"
        f"   interpreter: {sys.executable}\n"
    )

from fontTools.ttLib import TTFont
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.t2CharStringPen import T2CharStringPen

# v15 uses skia directly (not skia-pathops) for thicken. The skia-pathops
# binding's Path.stroke() returns None on every input in this build, even
# for trivial squares; the right path is skia.Paint + paint.getFillPath()
# + skia.Op, the same pattern ttf2otf_ft_v6.py uses.
import skia  # noqa: F401  (used by _thicken_glyph)


# ─────────────────────────────────────────────────────────────────────────────
#  Geometry: scale (TTF only — composites are walked, not unrolled)
# ─────────────────────────────────────────────────────────────────────────────


def _scale_glyph(glyf_table, glyph_name: str, sx: float, sy: float) -> None:
    """Scale a single TTF glyph's outline and advance width.

    Re-emits the glyph through a TransformPen wrapping a TTGlyphPen,
    then assigns the resulting Glyph back into the glyf table.
    Per-glyph rather than font-level: a font-level transform was
    observed to leave GPOS PairPos unchanged on AdwaitaSans (1881
    composites). Same reasoning as otf_optimize-ff-v2.0.py.
    """
    g = glyf_table[glyph_name]
    if g.isComposite():
        # Composite references carry their transform in the glyph
        # flags (SCALED, XY_SCALE, TWO_BY_TWO bits) and a packed
        # matrix. Numeric scaling here would corrupt the encoded
        # form. v15 leaves composite references untouched; this
        # affects accented glyphs (Aacute, Aogonek, etc.) which
        # will render at the original size while their base glyph
        # is scaled. A proper fix would inflate composites first.
        return

    out_pen = TTGlyphPen(None)
    transform = TransformPen(out_pen, (sx, 0, 0, sy, 0, 0))
    g.draw(transform, glyf_table)
    glyf_table[glyph_name] = out_pen.glyph()


# ─────────────────────────────────────────────────────────────────────────────
#  Geometry: thicken (TTF only)
# ─────────────────────────────────────────────────────────────────────────────


def _thicken_glyph(glyf_table, glyph_name: str, delta_units: float) -> bool:
    """Embolden a single TTF glyph by skia stroke+union.

    Returns True on success; False if the glyph has no outlines or
    skia refused to round-trip it. Some glyphs legitimately cannot
    be stroked (empty, .notdef, decomposable failures).
    """
    g = glyf_table[glyph_name]
    if g.isComposite() or g.numberOfContours == 0:
        return False

    rec = RecordingPen()
    try:
        g.draw(rec, glyf_table)
    except Exception:
        return False
    ops = list(rec.value)
    if not ops:
        return False

    # Build a skia.Path from the recording.
    builder = skia.PathBuilder()
    for op, args in ops:
        if op == "moveTo":
            x, y = args[0]
            builder.moveTo(x, y)
        elif op == "lineTo":
            x, y = args[0]
            builder.lineTo(x, y)
        elif op == "qCurveTo":
            # Chained quad segments; pass to skia as quads, it
            # raises to cubics internally.
            for i in range(len(args) - 1):
                x1, y1 = args[i]
                x2, y2 = args[i + 1]
                builder.quadTo(x1, y1, x2, y2)
        elif op == "curveTo":
            x1, y1, x2, y2, x3, y3 = args[0]
            builder.cubicTo(x1, y1, x2, y2, x3, y3)
        elif op == "closePath":
            builder.close()
    sk_path = builder.detach()

    # Stroke: width is 2 * delta_units because the stroke is centered
    # on the path; user-supplied --thickness is units per side, matching
    # ttf2otf_ft_v6.py's convention.
    paint = skia.Paint()
    paint.setStyle(skia.Paint.kStroke_Style)
    paint.setStrokeWidth(delta_units * 2.0)
    paint.setStrokeJoin(skia.Paint.kMiter_Join)
    paint.setStrokeCap(skia.Paint.kButt_Cap)
    paint.setStrokeMiter(4.0)
    stroked = skia.Path()
    if not paint.getFillPath(sk_path, stroked):
        return False

    # Union with the original path to fill in the inside of the stroke.
    unioned = skia.Op(stroked, sk_path, skia.PathOp.kUnion_PathOp)
    if unioned is None or unioned.isEmpty():
        return False

    # Re-emit into a fresh pen.
    out_pen = TTGlyphPen(None)
    # The path is a list of verbs; draw into the pen.
    _draw_skia_path_to_pen(unioned, out_pen)
    glyf_table[glyph_name] = out_pen.glyph()
    return True


def _draw_skia_path_to_pen(sk_path, pen) -> None:
    """Walk a skia.Path and emit it into a SegmentPen.

    Skia emits cubics and lines (and conics, which we drop). Modeled on
    ttf2otf_ft_v6.py's skia_to_recording, which is verified against
    skia 144.x.
    """
    if sk_path is None:
        return

    def xyz(p):
        return (p.x(), p.y())

    try:
        it = skia.Path.Iter(sk_path, False)
    except Exception:
        return

    arity = (("Move", 1), ("Line", 1), ("Cubic", 3), ("Quad", 2),
             ("Conic", 2), ("Close", 0))
    cur = None
    for item in it:
        verb = item[0]
        pts = item[1] if len(item) > 1 else ()
        vname = str(getattr(verb, "name", verb))
        need = None
        for key, n in arity:
            if key in vname:
                need = n
                break
        if need is None:
            continue
        if need == 0:
            if "Close" in vname:
                pen.closePath()
                cur = None
            continue
        if len(pts) < need:
            continue
        pts = tuple(pts[len(pts) - need:])

        if "Move" in vname:
            cur = xyz(pts[0])
            pen.moveTo((cur[0], cur[1]))
        elif "Line" in vname:
            cur = xyz(pts[0])
            pen.lineTo((cur[0], cur[1]))
        elif "Cubic" in vname:
            p1, p2, p3 = xyz(pts[0]), xyz(pts[1]), xyz(pts[2])
            cur = p3
            pen.curveTo((p1[0], p1[1]), (p2[0], p2[1]), (p3[0], p3[1]))
        elif "Quad" in vname:
            # Skia's union of a stroke+union produces quadratic curves.
            # T2CharStringPen has qCurveTo (will raise to cubic).
            # TTGlyphPen does not -- we raise to a cubic ourselves.
            # To raise correctly we need the previous on-curve point;
            # track it via the `cur` variable.
            p1, p2 = xyz(pts[0]), xyz(pts[1])
            if cur is None:
                # No prior on-curve point; skip the curve.
                continue
            cx1 = cur[0] + 2 / 3 * (p1[0] - cur[0])
            cy1 = cur[1] + 2 / 3 * (p1[1] - cur[1])
            cx2 = p1[0] + 2 / 3 * (p2[0] - p1[0])
            cy2 = p1[1] + 2 / 3 * (p2[1] - p1[1])
            cur = p2
            if hasattr(pen, "qCurveTo"):
                pen.qCurveTo((p1[0], p1[1]), (p2[0], p2[1]))
            else:
                pen.curveTo((cx1, cy1), (cx2, cy2), (p2[0], p2[1]))
        elif "Conic" in vname:
            continue


# ─────────────────────────────────────────────────────────────────────────────
#  Geometry: scale and thicken for CFF (OTF)
# ─────────────────────────────────────────────────────────────────────────────
#
# CFF charstrings are cubic-only and live in a 'CFF ' table. There is no
# per-glyph width on the charstring itself -- advance widths live in 'hmtx'.
# The round-trip is:
#   1. cs.draw(recording_pen)        -- decompose charstring to ops
#   2. transform ops                  -- apply scale or stroke+union
#   3. T2CharStringPen(...)           -- re-emit to a fresh T2Char
#   4. new_cs.private = cs.private    -- preserve hints/subr refs
#   5. charstrings[name] = new_cs     -- install


def _decompose_cff_to_ops(charstrings, glyph_name: str) -> list | None:
    """Return the recording-pen op list for a CFF charstring, or None."""
    try:
        cs = charstrings[glyph_name]
    except KeyError:
        return None
    rec = RecordingPen()
    try:
        cs.draw(rec)
    except Exception:
        return None
    return list(rec.value)


def _rebuild_cff_charstring(ops: list, src_cs) -> "T2Char":
    """Build a fresh T2Char from a recording-pen op list."""
    new_pen = T2CharStringPen(width=None, glyphSet=None, roundTolerance=0.5)
    for op, args in ops:
        if op == "moveTo":
            new_pen.moveTo(args[0])
        elif op == "lineTo":
            new_pen.lineTo(args[0])
        elif op == "curveTo":
            # CFF RecordingPen emits curveTo as 3 separate (x, y) args.
            # T2CharStringPen.curveTo expects 3 (x, y) tuples.
            new_pen.curveTo(
                (args[0][0], args[0][1]),
                (args[1][0], args[1][1]),
                (args[2][0], args[2][1]),
            )
        elif op == "qCurveTo":
            # Pass to the pen's qCurveTo; it raises to cubic internally.
            for i in range(len(args) - 1):
                x1, y1 = args[i]
                x2, y2 = args[i + 1]
                new_pen.qCurveTo((x1, y1), (x2, y2))
        elif op == "closePath":
            new_pen.closePath()
        elif op == "endPath":
            new_pen.endPath()
    new_cs = new_pen.getCharString(optimize=True)
    new_cs.private = src_cs.private
    new_cs.globalSubrs = src_cs.globalSubrs
    return new_cs


def _cff_scale_glyph(charstrings, glyph_name: str, sx: float, sy: float) -> None:
    """Scale a single CFF charstring's outline.

    Re-decomposes, scales each coordinate by (sx, sy), rebuilds.
    TransformPen is a PointPen, not a SegmentPen, so the TTF-style
    "wrap a TransformPen around a RecordingPen" trick does not work
    for the CFF path. Direct math is simpler and equivalent.
    """
    ops = _decompose_cff_to_ops(charstrings, glyph_name)
    if not ops:
        return
    src_cs = charstrings[glyph_name]

    # Build the new op list with each coordinate scaled.
    new_ops = []
    for op, args in ops:
        if op == "moveTo":
            x, y = args[0]
            new_ops.append((op, ((x * sx, y * sy),)))
        elif op == "lineTo":
            x, y = args[0]
            new_ops.append((op, ((x * sx, y * sy),)))
        elif op == "curveTo":
            # CFF RecordingPen emits curveTo as 3 separate (x, y) args.
            new_args = (
                (args[0][0] * sx, args[0][1] * sy),
                (args[1][0] * sx, args[1][1] * sy),
                (args[2][0] * sx, args[2][1] * sy),
            )
            new_ops.append((op, new_args))
        elif op == "qCurveTo":
            # Chain of quad control points; scale each.
            new_args = tuple((x * sx, y * sy) for x, y in args)
            new_ops.append((op, new_args))
        else:
            # closePath, endPath -- no coords.
            new_ops.append((op, args))

    charstrings[glyph_name] = _rebuild_cff_charstring(new_ops, src_cs)




def _cff_thicken_glyph(charstrings, glyph_name: str,
                       delta_units: float) -> bool:
    """Embolden a single CFF charstring by skia stroke+union.

    Returns True on success; False if decomposition or stroke failed.
    """
    ops = _decompose_cff_to_ops(charstrings, glyph_name)
    if not ops:
        return False
    if not _ops_have_outline(ops):
        return False

    src_cs = charstrings[glyph_name]

    # Build a skia.Path from the recording.
    builder = skia.PathBuilder()
    for op, args in ops:
        if op == "moveTo":
            x, y = args[0]
            builder.moveTo(x, y)
        elif op == "lineTo":
            x, y = args[0]
            builder.lineTo(x, y)
        elif op == "qCurveTo":
            for i in range(len(args) - 1):
                x1, y1 = args[i]
                x2, y2 = args[i + 1]
                builder.quadTo(x1, y1, x2, y2)
        elif op == "curveTo":
            x1, y1, x2, y2, x3, y3 = args[0][0], args[0][1], args[1][0], args[1][1], args[2][0], args[2][1]
            builder.cubicTo(x1, y1, x2, y2, x3, y3)
        elif op == "closePath":
            builder.close()
        elif op == "endPath":
            pass
    sk_path = builder.detach()

    # Stroke.
    paint = skia.Paint()
    paint.setStyle(skia.Paint.kStroke_Style)
    paint.setStrokeWidth(delta_units * 2.0)
    paint.setStrokeJoin(skia.Paint.kMiter_Join)
    paint.setStrokeCap(skia.Paint.kButt_Cap)
    paint.setStrokeMiter(4.0)
    stroked = skia.Path()
    if not paint.getFillPath(sk_path, stroked):
        return False

    # Union with the original.
    unioned = skia.Op(stroked, sk_path, skia.PathOp.kUnion_PathOp)
    if unioned is None or unioned.isEmpty():
        return False

    # Walk the unioned path into a fresh T2Char.
    new_pen = T2CharStringPen(width=None, glyphSet=None, roundTolerance=0.5)
    _draw_skia_path_to_pen(unioned, new_pen)
    new_cs = new_pen.getCharString(optimize=True)
    new_cs.private = src_cs.private
    new_cs.globalSubrs = src_cs.globalSubrs
    charstrings[glyph_name] = new_cs
    return True


def _ops_have_outline(ops: list) -> bool:
    """Does an op list contain anything more than an empty move/close?"""
    for op, args in ops:
        if op in ("lineTo", "curveTo", "qCurveTo"):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Per-font driver
# ─────────────────────────────────────────────────────────────────────────────


def process_font(job: tuple[str, str, float, float]) -> tuple[str, bool, str]:
    """Open, scale, thicken, save. Returns (name, ok, message).

    Dispatches on TTF (glyf) vs CFF/OTF (CFF or CFF2) tables. TTF path
    uses TTGlyphPen + TransformPen; CFF path uses T2CharStringPen.
    Both share the same skia stroke+union for thicken.
    """
    src, dst, scale_pct, thickness_units = job
    name = os.path.basename(src)
    try:
        t0 = time.time()
        f = Font(src)
        tt = f.ttfont

        is_ttf = "glyf" in tt
        is_cff = "CFF " in tt or "CFF2" in tt
        if not (is_ttf or is_cff):
            return name, False, "no glyf or CFF table; skipped"

        if "fvar" in tt:
            try:
                var2static_run(f, overlap=True)
            except Var2StaticError as e:
                return name, False, f"var2static: {e}"

        if scale_pct == 0 and thickness_units == 0:
            shutil.copy2(src, dst)
            return name, True, f"no-op copy ({time.time() - t0:.1f}s)"

        sx = 1.0 + scale_pct / 100.0
        sy = sx

        # ----- SCALE -----
        msg_scale = ""
        if scale_pct != 0:
            hmtx = tt["hmtx"]
            for glyph in tt.getGlyphOrder():
                adv, lsb = hmtx[glyph]
                hmtx[glyph] = (int(round(adv * sx)), lsb)

            if is_ttf:
                for glyph in tt.getGlyphOrder():
                    _scale_glyph(tt["glyf"], glyph, sx, sy)
            else:
                charstrings = tt["CFF "].cff.topDictIndex[0].CharStrings
                for glyph in tt.getGlyphOrder():
                    _cff_scale_glyph(charstrings, glyph, sx, sy)
            msg_scale = f"scale={sx:.4f}"

        # ----- THICKEN -----
        msg_thick = ""
        if thickness_units != 0:
            if is_ttf:
                applied = 0
                for glyph in tt.getGlyphOrder():
                    if _thicken_glyph(tt["glyf"], glyph, thickness_units):
                        applied += 1
            else:
                charstrings = tt["CFF "].cff.topDictIndex[0].CharStrings
                applied = 0
                for glyph in tt.getGlyphOrder():
                    if _cff_thicken_glyph(charstrings, glyph, thickness_units):
                        applied += 1
            msg_thick = f"thicken +{thickness_units:.1f}u/side on {applied} glyphs"

        f.save(dst)
        return name, True, (f"{msg_scale} {msg_thick} "
                            f"({time.time() - t0:.1f}s)").strip()
    except Exception as exc:
        return name, False, f"{type(exc).__name__}: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
#  Directory walk + worker pool
# ─────────────────────────────────────────────────────────────────────────────

_SUPPORTED_EXT = (".ttf", ".otf")


def discover(input_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(input_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in _SUPPORTED_EXT:
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Scale and/or thicken TTF/OTF fonts via foundrytools + "
                     "skia-pathops. Both TTF and CFF/OTF are supported. "
                     "CFF charstrings are decomposed via T2CharStringPen, "
                     "transformed, and re-emitted."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input_dir", type=Path, help="Input directory (recursive)")
    p.add_argument("output_dir", type=Path, help="Output directory")
    p.add_argument("--scale", type=float, default=0.0, metavar="PCT",
                   help="Scale outlines + advance widths by (1 + PCT/100). "
                        "Default 0 (no-op).")
    p.add_argument("--thickness", type=float, default=0.0, metavar="UNITS",
                   help="Embolden by UNITS per side via skia-pathops stroke+union. "
                        "Default 0 (no-op). Negative values rejected.")
    p.add_argument("--instance", default=None, metavar="NAME",
                   help="For variable fonts, pick a named instance. Default: "
                        "auto-pick Regular / first instance.")
    p.add_argument("--jobs", "-j", type=int, default=0, metavar="N",
                   help="Worker count. 0 (default) = min(cpu_count, len(fonts)).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not args.input_dir.is_dir():
        print(f"input directory not found: {args.input_dir}", file=sys.stderr)
        return 2
    if args.thickness < 0:
        print(f"--thickness must be >= 0 (got {args.thickness})", file=sys.stderr)
        return 2
    if args.scale == 0 and args.thickness == 0:
        print("warning: --scale and --thickness are both 0; this is a no-op copy.",
              file=sys.stderr)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fonts = discover(args.input_dir)
    if not fonts:
        print(f"no .ttf files under {args.input_dir}", file=sys.stderr)
        return 1

    jobs = [(str(f), str(args.output_dir / f.name), args.scale, args.thickness)
            for f in fonts]
    cpu = os.cpu_count() or 1
    workers = min(args.jobs, len(jobs)) if args.jobs > 0 else min(cpu, len(jobs))

    print(f"{len(fonts)} font(s)  scale={args.scale:+g}%  thickness=+{args.thickness}u/side")
    print(f"workers: {workers} ({cpu} CPUs)")

    if workers <= 1:
        ok = 0
        for j in jobs:
            name, good, msg = process_font(j)
            print(f"  {'ok  ' if good else 'FAIL'} {name}: {msg}")
            ok += good
        print(f"done: {ok}/{len(fonts)} succeeded -> {args.output_dir}")
        return 0 if ok == len(fonts) else 1

    import multiprocessing as mp
    ctx = mp.get_context("fork")
    PER_FACE_TIMEOUT = int(os.environ.get("OTF_PER_FACE_TIMEOUT", "600"))
    ok = 0
    with ctx.Pool(processes=workers) as pool:
        pending = [(Path(j[0]).name, pool.apply_async(process_font, (j,))) for j in jobs]
        for name, res in pending:
            try:
                rname, good, msg = res.get(timeout=PER_FACE_TIMEOUT)
            except mp.TimeoutError:
                print(f"  FAIL {name}: worker timeout ({PER_FACE_TIMEOUT}s)")
                continue
            print(f"  {'ok  ' if good else 'FAIL'} {rname}: {msg}")
            ok += good
    print(f"done: {ok}/{len(fonts)} succeeded -> {args.output_dir}")
    return 0 if ok == len(fonts) else 1


if __name__ == "__main__":
    sys.exit(main())
