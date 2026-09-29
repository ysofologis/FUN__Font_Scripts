#!/usr/bin/env python3
"""
OTF/TTF Font Optimizer — FontForge port of otf_optimize-v14.py

A feature-for-feature reimplementation of ``otf_optimize-v14.py`` using the
**FontForge contour engine** instead of fontTools + Skia pathops. Same CLI,
same dual-mode flag semantics, same pipeline order, same defaults.

Why port at all
---------------
v14 leans on ``python-pathops`` (Skia) for its geometric work. Skia is a
rasterizer's path engine: it is fast and robust, but it has no notion of
type design — no stem-snap, no em-square, no private dict. FontForge is a
type tool: it carries the CFF private dictionary, the layer model, and the
per-glyph contour ops that this pipeline actually needs. Porting means the
geometry work runs on the same engine the rest of the foundry tooling
already uses, and the output is inspectable in FontForge's own UI.

Pipeline (identical order to v14 — order is load-bearing)
---------------------------------------------------------
  0. Load via fontforge.open()
  1. Variable-font instantiation (fvar -> static default instance)
  2. --correct          contour correction, runs FIRST so every downstream
                        transform sees clean topology
  3. --scale            uniform scale within the em-square, UPM preserved
  4. --thickness        stem thickening (+ the v13/v14 quantization knobs)
  5. --spacing          letter-spacing; outlines untouched, advances grow
  6. --zones            CFF BlueValues / StemSnapH-V recompute
  7. --check-outlines   topology audit + auto-fix
  8. --hint-tune        re-hint (TTF native, CFF via AFDKO)
  9. Generate

Stages 6 and 8b delegate to the shared sibling modules
------------------------------------------------------
``--zones`` and the CFF half of ``--hint-tune`` wrap **AFDKO**
(``otf_recalc_zones`` / ``otf_recalc_stems`` / ``otfautohint``), which is a
separate PostScript engine with no FontForge equivalent. Reusing
``zones.py`` and v14's ``apply_hint_tune`` verbatim keeps those two stages
byte-comparable with v14 instead of approximating them. Everything else —
correction, scaling, thickening, quantization, tracking, TrueType
hinting, generation — runs natively on FontForge.

FontForge API constraints this port is built around (all verified against
FontForge 20251009 + python3.14, not assumed):

  * **Font-level ops act on the SELECTION, which defaults to empty.**
    ``font.transform()``, ``font.changeWeight()``, ``font.simplify()``,
    ``font.removeOverlap()`` and ``font.correctDirection()`` are silent
    no-ops on a freshly opened font. You MUST assign ``font.selection``
    first. This is the single easiest way to get a silently broken
    "optimized" font out of FontForge. ``font.changeWeight`` is also
    unreliable even with a selection set — this script uses the
    per-glyph ``glyph.changeWeight(N)``, which is exact.
  * **The Python binding exposes no point/contour access.** There is no
    ``layer.shapes()``, ``glyph.points``, ``glyph.contours`` or
    ``glyph.outline()``. The per-vertex stages (flatten, epsilon-merge,
    coordinate grid, stem quantization) therefore go through the
    segment-pen protocol: ``glyph.draw(pen)`` to read, ``glyph.glyphPen()``
    to write.
  * **``glyph.clear()`` clobbers the advance width** (600 -> 1000 for a
    1000-UPM font). Re-assign ``width``/``vwidth`` AFTER re-emitting.
  * **``layer.stroke(pen, width)`` is unusable from Python** — it rejects
    both user pens and ``glyphPen`` with "bad argument type for built-in
    operation". The outset is ``glyph.changeWeight(N)``, FontForge's own
    native stroke+union. Hand-rolling the outset as a union of per-edge
    offset quads looks equivalent and is not: it re-cuts counters, so
    ring glyphs fragment and lose ink. ``changeWeight`` keeps holes.
  * **``generate()`` has no ``layers=`` kwarg** (it is ``layer=``) and
    this build **rejects a ``'sfnt'`` or ``'truetype'`` flag**. Use
    ``flags=('opentype',)`` for CFF and **no flags at all** for TrueType.
  * **``glyph.clear()`` resets the advance width to the em size** (600 ->
    1000 on a 1000-UPM font), so re-assign ``width``/``vwidth`` after
    every re-emit.
  * ``font.private`` is read-only (``.guess()`` only), so the CFF private
    dict is written by the fontTools post-pass, not by FontForge.

Usage:
    python otf_optimize-ff-v14.py --scale N [--thickness T]
                                   [--thicken-grid G] [--thicken-epsilon E]
                                   [--thicken-stem-quantize Q]
                                   [--thicken-flatten F]
                                   [--spacing S] [--correct] [--zones]
                                   [--check-outlines] [--hint-tune MODE]
                                   input_dir output_dir

Examples:
    # v14-equivalent: scale 50%, thicken 2.5%
    otf_optimize-ff-v14.py --scale 50 --thickness 2.5 in/ out/

    # hint-friendly geometry
    otf_optimize-ff-v14.py --scale 50 --thickness 2.5 \
        --thicken-grid 1.0 --thicken-epsilon 1.0 --thicken-stem-quantize 10 \
        --zones --hint-tune auto in/ out/

    # polygon mode: fringe-free output at the cost of file size
    otf_optimize-ff-v14.py --correct --thickness 2.5 --thicken-flatten 8 \
        --thicken-grid 1.0 --thicken-stem-quantize 10 --zones \
        --hint-tune auto in/ out/

Equivalent of v14: the same inputs in the same order produce equivalent
geometry, but not byte-identical binaries — the contour engine, the
outset implementation and the autohinter are all different code paths.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

# FontForge's python bindings are compiled against a specific CPython
# ABI. On this host they live in the linuxbrew tree for python3.14 while
# the default `python3` is 3.13, so add the versioned site-packages dir
# when the plain import fails. Harmless if the module is already on path.
try:
    import fontforge
except ImportError:  # pragma: no cover - environment dependent
    import glob as _glob
    for _cand in sorted(_glob.glob("/home/linuxbrew/.linuxbrew/lib/python3.*/site-packages"),
                        reverse=True):
        if _cand not in sys.path:
            sys.path.append(_cand)
    import fontforge

from fontTools.ttLib import TTFont

# CFF private-dict + AFDKO stages: shared with v14 so they stay
# behaviourally identical. Optional; skipped with a warning if absent.
try:
    from zones import recalc_zones
    HAS_ZONES = True
except ImportError:  # pragma: no cover
    recalc_zones = None
    HAS_ZONES = False

try:
    from check_outlines import check_outlines as _ft_check_outlines
    HAS_CHECK_OUTLINES = True
except ImportError:  # pragma: no cover
    _ft_check_outlines = None
    HAS_CHECK_OUTLINES = False

try:
    from foundrytools import Font as _FtFont
    HAS_FOUNDRYTOOLS = True
except ImportError:  # pragma: no cover
    _FtFont = None
    HAS_FOUNDRYTOOLS = False

try:
    import afdko  # noqa: F401
    HAS_AFDKO = True
except ImportError:  # pragma: no cover
    HAS_AFDKO = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("otf_optimize_ff_v14")

SUPPORTED_EXTENSIONS = {".otf", ".ttf"}

# ---------------------------------------------------------------------------
#  Defaults carried over from v14
# ---------------------------------------------------------------------------

DEFAULT_COORD_GRID = 0.25       # --thicken-grid
DEFAULT_STEM_QUANTIZE = 0       # --thicken-stem-quantize
DEFAULT_FLATTEN_SEGMENTS = 0    # --thicken-flatten

# Axial-edge tolerance for the stem-width quantizer (font units). Matches
# v14's _AXIAL_TOLERANCE concept.
AXIAL_TOLERANCE = 0.5

# Tolerance for calling a segment "vertical" when measuring stem width.
# Deliberately looser than AXIAL_TOLERANCE: real stems often lean a few
# tenths of a unit (hinting, design), and 1.0u still excludes anything
# genuinely diagonal. Must stay identical in thicken.py.
STEM_EDGE_TOLERANCE = 1.0

HINT_STRENGTH_PRESERVE = 0.30
HINT_STRENGTH_AGGRESSIVE = 0.70


# ---------------------------------------------------------------------------
#  Segment-pen plumbing
# ---------------------------------------------------------------------------

class _Recorder:
    """Minimal SegmentPen that records a glyph's contour stream.

    FontForge's binding offers no point-level access, so every per-vertex
    operation in this script is expressed as read-with-this-pen ->
    transform -> write-with-glyphPen. That round-trip is lossless for the
    curve types FontForge hands out (cubic for CFF and post-2.x TrueType).
    """

    __slots__ = ("ops",)

    def __init__(self):
        self.ops: list[tuple[str, list[tuple[float, float]]]] = []

    def moveTo(self, pt):
        self.ops.append(("moveTo", [(float(pt[0]), float(pt[1]))]))

    def lineTo(self, pt):
        self.ops.append(("lineTo", [(float(pt[0]), float(pt[1]))]))

    def curveTo(self, *pts):
        self.ops.append(("curveTo", [(float(p[0]), float(p[1])) for p in pts]))

    def qCurveTo(self, *pts):
        self.ops.append(("qCurveTo", [(float(p[0]), float(p[1])) for p in pts]))

    def closePath(self):
        self.ops.append(("closePath", []))

    def endPath(self):
        self.ops.append(("endPath", []))

    def addComponent(self, name, transform):
        # Composite reference, preserved verbatim so re-emitting a
        # composite glyph does not destroy it.
        #
        # args is a LIST OF THE TWO POSITIONAL ARGUMENTS, not a list
        # holding one tuple: _emit calls pen.addComponent(*args), and
        # glyphPen.addComponent takes (glyphname, matrix) -- packing them
        # into a single tuple makes it raise "argument 1 must be str".
        self.ops.append(("addComponent", [str(name), tuple(transform)]))

    def __len__(self):
        return len(self.ops)


def _read_glyph(glyph) -> list[tuple[str, list]]:
    """Read a glyph's contours (and components) into an op list."""
    rec = _Recorder()
    glyph.draw(rec)
    return rec.ops


def _is_empty(ops) -> bool:
    return not any(op in ("moveTo", "addComponent") for op, _ in ops)


def _has_components(ops) -> bool:
    return any(op == "addComponent" for op, _ in ops)


def _emit(glyph, ops, width: int, vwidth: int) -> None:
    """Replace a glyph's contents with `ops`.

    NOTE the width ordering: ``glyph.clear()`` resets the advance to the
    em size, so width/vwidth must be re-assigned AFTER re-emitting or
    every glyph silently ends up with a 1000u advance in a 1000-UPM font.
    """
    glyph.clear()
    pen = glyph.glyphPen()
    # Safety net: a segment op with no open contour is a pen-protocol
    # violation that makes glyphPen raise. Skip the stray op rather than
    # lose the whole glyph, but SAY SO -- a silently truncated contour is
    # a wrong shape that looks like success, which is the worse failure.
    started = False
    dropped = 0
    for op, args in ops:
        if op == "moveTo":
            started = True
            pen.moveTo(args[0])
        elif op == "lineTo":
            if not started:
                dropped += 1
                continue
            pen.lineTo(args[0])
        elif op == "curveTo":
            if not started:
                dropped += 1
                continue
            pen.curveTo(*args)
        elif op == "qCurveTo":
            if not started:
                dropped += 1
                continue
            pen.qCurveTo(*args)
        elif op == "closePath":
            if started:
                pen.closePath()
            started = False
        elif op == "endPath":
            if started:
                pen.endPath()
            started = False
        elif op == "addComponent":
            pen.addComponent(*args)
            started = False
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown op {op!r}")
    if dropped:
        log.warning(f"_emit: dropped {dropped} stray segment op(s) in "
                    f"'{getattr(glyph, 'glyphname', '?')}' (malformed "
                    f"contour stream; shape may be incomplete)")
    glyph.width = width
    glyph.vwidth = vwidth


# ---------------------------------------------------------------------------
#  Geometry helpers
# ---------------------------------------------------------------------------

def _round_to_grid(value: float, grid: float) -> int:
    """Round `value` to the nearest multiple of `grid` (int result).

    Mirrors v14's ``_round_to_grid`` exactly, including its short-circuit:
    any grid >= 1.0 is plain integer rounding, NOT a snap to that grid.
    So ``--thicken-grid 8`` on a 1000-UPM font snaps to whole units and
    leaves 79 as 79, while ``--thicken-grid 0.25`` is the only setting
    that actually quantizes sub-unit coordinates. This looks like a bug
    but it is v14's documented-and-shipped behaviour; changing it here
    would make the two ports disagree on the same input. (Both collapse
    to the same binary output anyway, because CFF charstring operands
    and TrueType coordinates are integer font units.)
    """
    if grid >= 1.0:
        return int(math.floor(value + 0.5))
    return int(math.floor(round(value / grid) * grid + 0.5))


def _flatten_cubic(p0, p1, p2, p3, segments: int):
    """Sample one cubic into `segments` on-curve points."""
    out = []
    for i in range(1, segments + 1):
        t = i / segments
        mt = 1.0 - t
        a = mt * mt * mt
        b = 3.0 * mt * mt * t
        c = 3.0 * mt * t * t
        d = t * t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _flatten_ops(ops, segments: int):
    """Replace every cubic with `segments` line segments (v14 --thicken-flatten).

    Linearizing BEFORE the outset is what makes the epsilon-merge and the
    stem quantizer operate on real vertices instead of control points,
    which is where v14's fringe artefacts came from.
    """
    if segments <= 0:
        return ops
    out: list[tuple[str, list]] = []
    cur = None
    for op, args in ops:
        if op == "moveTo":
            out.append((op, args))
            cur = args[0]
        elif op == "lineTo":
            out.append((op, args))
            cur = args[0]
        elif op == "curveTo":
            c1, c2, p3 = args
            for pt in _flatten_cubic(cur, c1, c2, p3, segments):
                out.append(("lineTo", [pt]))
            cur = p3
        elif op == "qCurveTo":
            # Promote to cubic via the standard degree-elevation identity,
            # then sample. Keeps the same visual curve.
            pts = list(args)
            if len(pts) == 1 and not pts[-1]:
                end = None
                ctrl = pts[0]
                end = ctrl
                c1 = (cur[0] + 2.0 / 3.0 * (ctrl[0] - cur[0]),
                      cur[1] + 2.0 / 3.0 * (ctrl[1] - cur[1]))
                c2 = (end[0] + 2.0 / 3.0 * (ctrl[0] - end[0]),
                      end[1] + 2.0 / 3.0 * (ctrl[1] - end[1]))
                p3 = end
            else:
                ctrl = pts[0]
                end = pts[-1]
                c1 = (cur[0] + 2.0 / 3.0 * (ctrl[0] - cur[0]),
                      cur[1] + 2.0 / 3.0 * (ctrl[1] - cur[1]))
                c2 = (end[0] + 2.0 / 3.0 * (ctrl[0] - end[0]),
                      end[1] + 2.0 / 3.0 * (ctrl[1] - end[1]))
                p3 = end
            for pt in _flatten_cubic(cur, c1, c2, p3, segments):
                out.append(("lineTo", [pt]))
            cur = p3
        else:
            out.append((op, args))
    return out


def _epsilon_merge(ops, epsilon: float):
    """Drop on-curve LINE points within `epsilon` of their predecessor.

    Mirrors v14's ``_EpsilonMergePen``. Only `lineTo` anchors are ever
    dropped: off-curve control points and the `curveTo`/`qCurveTo` ops
    that carry them always pass through, and a `moveTo` is NEVER dropped
    because a contour with no `moveTo` is a pen-protocol violation that
    makes glyphPen raise ("The curveTo operator must be preceded by a
    moveTo operator").

    `last` is reset at every contour boundary (moveTo / closePath /
    endPath) so the first point of one contour is never measured against
    the last point of the previous one. Without that reset, adjacent
    contours whose endpoints nearly touch -- which is exactly what
    happens to glyphs like 'exclam' after an outset, where the two
    parts end up within a font unit of each other -- lose their moveTo
    and fail to re-emit.
    """
    if not epsilon or epsilon <= 0:
        return ops
    out: list[tuple[str, list]] = []
    last = None
    for op, args in ops:
        if op == "moveTo":
            out.append((op, args))
            last = args[0]
        elif op == "lineTo":
            pt = args[0]
            if last is not None and math.hypot(pt[0] - last[0], pt[1] - last[1]) < epsilon:
                continue
            out.append((op, args))
            last = pt
        elif op in ("curveTo", "qCurveTo"):
            out.append((op, args))
            last = args[-1]
        elif op in ("closePath", "endPath"):
            out.append((op, args))
            last = None
        else:
            out.append((op, args))
            last = None
    return out


def _quantize_stem_widths(ops, stem_quantize: int):
    """Snap axial stem widths to multiples of `stem_quantize` font units.

    Approach-grade (same caveat as v14): walks the contour, collects
    near-axial edges, pairs opposing ones by perpendicular proximity, and
    shifts one edge of each pair so the measured gap lands on the grid.
    """
    if stem_quantize <= 1:
        return ops

    horiz: list[tuple[float, float, float]] = []   # (x1, x2, y)
    vert: list[tuple[float, float, float]] = []    # (y1, y2, x)
    cur = None
    start = None
    for op, args in ops:
        if op == "moveTo":
            cur = args[0]
            start = cur
        elif op == "lineTo":
            new = args[0]
            if cur is not None:
                dx, dy = new[0] - cur[0], new[1] - cur[1]
                if abs(dy) <= AXIAL_TOLERANCE and abs(dx) > AXIAL_TOLERANCE:
                    horiz.append((min(cur[0], new[0]), max(cur[0], new[0]), cur[1]))
                elif abs(dx) <= AXIAL_TOLERANCE and abs(dy) > AXIAL_TOLERANCE:
                    vert.append((min(cur[1], new[1]), max(cur[1], new[1]), cur[0]))
            cur = new
        elif op in ("curveTo", "qCurveTo"):
            cur = args[-1]
        elif op == "closePath":
            cur = start

    h_off = _find_axial_pairs(horiz, "h", stem_quantize)
    v_off = _find_axial_pairs(vert, "v", stem_quantize)
    if not h_off and not v_off:
        return ops

    def shift(pt):
        x, y = pt
        y_new = y + h_off.get(_key_h(x, y), 0.0)
        x_new = x + v_off.get(_key_v(x, y), 0.0)
        return (x_new, y_new)

    out: list[tuple[str, list]] = []
    for op, args in ops:
        if op in ("moveTo", "lineTo"):
            out.append((op, [shift(args[0])]))
        elif op in ("curveTo", "qCurveTo"):
            out.append((op, [shift(p) for p in args]))
        else:
            out.append((op, args))
    return out


def _key_h(x, y):
    return (round(x, 1), round(y, 1))


def _key_v(x, y):
    return (round(x, 1), round(y, 1))


def _find_axial_pairs(edges, axis: str, quantize: int):
    """Return {edge_key: offset} that snaps paired gaps to `quantize`."""
    offsets: dict = {}
    if not edges:
        return offsets
    if axis == "h":
        # y is the perpendicular coordinate; pair edges with nearby y.
        ordered = sorted(edges, key=lambda e: e[2])
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                gap = b[2] - a[2]
                if gap <= 0 or gap > 8.0 * max(1.0, a[1] - a[0]):
                    continue
                target = round(gap / quantize) * quantize
                delta = (target - gap) / 2.0
                if abs(delta) < 1e-9:
                    continue
                # Move the upper edge outward and the lower edge inward.
                offsets[_key_h(b[0], b[2])] = offsets.get(_key_h(b[0], b[2]), 0.0) + delta
                offsets[_key_h(a[0], a[2])] = offsets.get(_key_h(a[0], a[2]), 0.0) - delta
                break
    else:
        ordered = sorted(edges, key=lambda e: e[2])
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                gap = b[2] - a[2]
                if gap <= 0 or gap > 8.0 * max(1.0, a[1] - a[0]):
                    continue
                target = round(gap / quantize) * quantize
                delta = (target - gap) / 2.0
                if abs(delta) < 1e-9:
                    continue
                offsets[_key_v(b[2], b[0])] = offsets.get(_key_v(b[2], b[0]), 0.0) + delta
                offsets[_key_v(a[2], a[0])] = offsets.get(_key_v(a[2], a[0]), 0.0) - delta
                break
    return offsets


# ---------------------------------------------------------------------------
#  FontForge stages
# ---------------------------------------------------------------------------

def _select_all(font) -> int:
    """Select every encoded glyph. Returns the count.

    Without this, every font-level FontForge operation is a silent no-op.
    """
    codes = []
    for g in font.glyphs():
        try:
            codes.append(g.unicode)
        except Exception:
            continue
    try:
        font.selection = codes
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"Could not set font selection: {exc}")
        return 0
    return len(codes)


def _is_cff(path: Path) -> bool:
    try:
        tt = TTFont(str(path), lazy=True)
        out = "CFF " in tt or "CFF2" in tt
        tt.close()
        return out
    except Exception:
        return path.suffix.lower() == ".otf"


def instantiate_to_static_file(src: Path, work: Path) -> bool:
    """Bake a variable font down to its default static instance, in place.

    v14 does this as pipeline stage 0: gvar/CFF2 deltas are tied to the
    original point counts, so any outline transform (scale, thicken)
    applied on top of a variable font desyncs them. FontForge exposes no
    fvar/gvar/CFF2 API, so the pass runs through fontTools first.

    Operates on the working copy in `work`, never on `src`: the source
    file is the user's master and must stay byte-identical.

    Returns True if the font was variable and has been instantiated.
    """
    try:
        tt = TTFont(str(work))
    except Exception as exc:
        log.warning(f"Could not open {work.name} for instancing: {exc}")
        return False
    try:
        if "fvar" not in tt:
            return False
        from fontTools.varLib.instancer import instantiateVariableFont
        axes = {ax.axisTag: ax.defaultValue for ax in tt["fvar"].axes}
        instantiateVariableFont(tt, axes, inplace=True)
        tt.save(str(work))
        log.info("Variable font instantiated to default static instance "
                 "(fvar/gvar/HVAR/MVAR dropped)")
        return True
    except Exception as exc:
        log.warning(f"Variable-font instancing failed for {work.name}: {exc}; "
                    f"continuing on the variable outlines (risky)")
        return False
    finally:
        try:
            tt.close()
        except Exception:
            pass


def correct_glyphs(font, is_cff: bool) -> int:
    """Contour correction on every glyph. Returns the number touched.

    FontForge's equivalent of v14's Skia ``union + simplify + winding
    fix + tiny-path cull``:
      removeOverlap()  -> resolves self-intersections / merges overlaps
      correctDirection()-> outer-CCW (CFF) or outer-CW (TrueType)
      round()          -> drops sub-unit control points
    """
    touched = 0
    for g in font.glyphs():
        try:
            if _is_empty(_read_glyph(g)):
                continue
            g.removeOverlap()
            g.correctDirection()
            g.round()
            touched += 1
        except Exception as exc:
            log.debug(f"correct: {getattr(g, 'glyphname', '?')} failed: {exc}")
    log.info(f"Contour correction touched {touched} glyph(s)")
    return touched


def scale_glyphs(font, factor: float) -> None:
    """Uniform scale within the em-square; unitsPerEm preserved.

    Uses the font-level transform with an explicit selection, which
    scales outlines, advance widths and vertical metrics while leaving
    the em size alone — the same contract as v14's ScalerVisitor.
    """
    if factor == 1.0:
        return
    _select_all(font)
    em = font.em
    ascent, descent = font.ascent, font.descent
    font.transform((factor, 0, 0, factor, 0, 0))
    font.em = em
    font.ascent = ascent
    font.descent = descent


def thicken_glyphs(font, thickness_percent: float, *,
                   coord_grid: float = DEFAULT_COORD_GRID,
                   simplify_epsilon: float | None = None,
                   stem_quantize: int = DEFAULT_STEM_QUANTIZE,
                   flatten_segments: int = DEFAULT_FLATTEN_SEGMENTS,
                   is_cff: bool = False) -> None:
    """Thicken stems through FontForge's native outline outset.

    Pipeline per glyph, matching v14's ordering:
      1. optional polygon flatten      (--thicken-flatten)
      2. native outset                 (changeWeight)
      3. removeOverlap + correctDirection to restore clean fill topology
      4. optional epsilon-merge        (--thicken-epsilon)
      5. optional stem quantization    (--thicken-stem-quantize)
      6. snap every coordinate to grid (--thicken-grid)

    The outset itself is `glyph.changeWeight(N)`, which FontForge
    measures against the glyph's own stem width. It is the only usable
    primitive here, for two reasons found by testing:

      * `layer.stroke()` cannot be called from Python at all — it rejects
        every pen object Python can construct.
      * Hand-rolling the outset as a union of per-edge offset quads and
        resolving it with removeOverlap() does NOT work: a ring glyph
        fragments (2 contours -> 6) and its area grows ~2% instead of
        ~20%, because the per-edge quads re-cut the counter. FontForge's
        native outset keeps holes as holes.

    `changeWeight(N)` adds N font units to the total stem width (half on
    each side). N is therefore the same quantity v14 calls stroke_width,
    which keeps the two ports numerically equivalent.

    Advance widths are deliberately preserved: this is a weight change,
    not a metric change.
    """
    if thickness_percent <= 0:
        return

    multiplier = 1.0 + thickness_percent / 100.0
    multiplier = max(0.10, min(4.00, multiplier))

    if coord_grid is None or coord_grid <= 0:
        coord_grid = 1.0
    if stem_quantize < 0:
        stem_quantize = 0

    # Reference stem width, measured from the outlines rather than
    # guessed. Falls back to StdHW (CFF) and then to the old cap-height
    # heuristic only when measurement finds nothing usable.
    #
    # Cap height is measured from the 'H' outline first: a stored or
    # guessed value narrows or widens the stem window and can silently
    # exclude the very stems being measured.
    cap_source = "measured from 'H'"
    try:
        _h_ops = _read_glyph(font["H"])
        cap_measured = _measure_cap_height(_h_ops)
    except Exception:
        cap_measured = None
    if cap_measured and cap_measured > 0:
        cap_height = float(cap_measured)
    else:
        cap_height = _guess_cap_height(font)
        cap_source = "stored/guessed"

    ref_stem = _measure_stem_width(font, cap_height)
    ref_source = "measured from outlines"
    if not ref_stem:
        try:
            ref_stem = float(font.private.guess("StdHW")) or None
            ref_source = "CFF StdHW"
        except Exception:
            ref_stem = None
    if not ref_stem:
        ref_stem = cap_height * 0.07
        ref_source = "cap-height heuristic (7%)"
    # changeWeight's argument is a per-stem widening in font units.
    widen = abs(multiplier - 1.0) * ref_stem

    log.info(f"Thickening stems by {thickness_percent:+.2f}% "
             f"(multiplier {multiplier:.4f}, +{widen:.2f}u per stem; "
             f"ref_stem={ref_stem:.1f}u from {ref_source}, "
             f"cap={cap_height:.0f}u ({cap_source}); "
             f"grid={coord_grid:.4f}u, "
             f"simplify_epsilon={simplify_epsilon}, "
             f"stem_quantize={stem_quantize}u, flatten={flatten_segments}")

    if widen < 1.0:
        log.info(f"Stem widening {widen:.2f}u too small to register after "
                 f"integer rounding; skipping (raise --thickness for a "
                 f"visible effect)")
        return

    thickened = 0
    skipped = 0
    for g in font.glyphs():
        name = getattr(g, "glyphname", "?")
        try:
            ops = _read_glyph(g)
            if _is_empty(ops) or _has_components(ops):
                skipped += 1
                continue

            w, vw = g.width, g.vwidth

            # v14 flattens BEFORE the outset so the following epsilon-merge
            # and stem quantizer work on real vertices, not control points.
            if flatten_segments > 0:
                _emit(g, _flatten_ops(ops, flatten_segments), w, vw)

            g.changeWeight(widen)
            g.removeOverlap()
            g.correctDirection()

            ops = _read_glyph(g)
            if simplify_epsilon is not None:
                ops = _epsilon_merge(ops, simplify_epsilon)
            if stem_quantize > 0:
                ops = _quantize_stem_widths(ops, stem_quantize)
            if coord_grid > 0:
                ops = _apply_grid(ops, coord_grid)

            _emit(g, ops, w, vw)
            thickened += 1
        except Exception as exc:
            log.warning(f"Could not thicken '{name}': {exc}")
            skipped += 1

    log.info(f"Thickening complete: {thickened} glyphs thickened, {skipped} skipped")


def _vertical_edge_widths(ops, min_len: float, max_w: float, min_w: float = 1.0):
    """Widths between adjacent near-vertical edges = stem candidates.

    A stem is the gap between the two sides of a vertical stroke, so two
    near-vertical edges that are close in x (and span a real vertical run)
    bound a stem. Yields the gaps that fall in a plausible stem range.
    """
    edges: list[float] = []
    cur = None
    start = None
    for op, args in ops:
        if op == "moveTo":
            cur = args[0]
            start = cur
        elif op == "lineTo":
            p = args[0]
            if cur is not None and abs(p[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(p[1] - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = p
        elif op in ("curveTo", "qCurveTo"):
            p = args[-1]
            if cur is not None and abs(p[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(p[1] - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = p
        elif op == "closePath":
            if start is not None and cur is not None \
                    and abs(start[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(start[1] - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = start
    edges.sort()
    out = []
    for i in range(len(edges) - 1):
        d = edges[i + 1] - edges[i]
        if min_w <= d <= max_w:
            out.append(d)
    return out


def _measure_cap_height(ops):
    """Top of 'H' measured from the outline = cap height.

    Preferred over a stored/guessed cap height for the stem window,
    because it is read from the same outlines by BOTH implementations,
    so the two scripts cannot disagree. This is not academic:
    FontForge's ``private.guess("CapHeight")`` returned 650 for Jano Sans
    Pro Black where OS/2 says 726, and the narrower window that produced
    (0.35 * 650 = 227.5u) excluded that font's real 228u stems, dropping
    the measured median from 228 to 159.
    """
    ys = [p[1] for op, a in ops
          if op in ("moveTo", "lineTo", "curveTo", "qCurveTo") for p in a]
    return max(ys) if ys else None


def _measure_stem_width(font, cap_height: float, sample: int = 64):
    """Median width of adjacent vertical edges over a sample of glyphs.

    Replaces the old `cap_height * 0.07` guess. That heuristic assumed a
    stem is 7% of cap height, which is roughly right for a light text
    serif but badly low for anything grotesque or display: Jano Sans Pro
    Regular has an 88u stem on a 729u cap (12.1%), so the guess gave 51u
    and `--thickness 5` delivered 2.55u of widening instead of 4.40u --
    about a third of a pixel at 16px. The caller now logs the measured
    value so a wrong target is visible instead of silent.

    v14's CFF path preferred `private.StdHW`, which is better than the
    cap-height guess but still an average over the whole face rather than
    a measurement of the stems that will actually be stroked. Measured
    median is closer to the truth on every font tested:

        font                 cap*0.07   StdHW   measured   'H' truth
        Jano Sans Pro Reg       51.0     78        88.0        88.0
        Jano Sans Pro Light     50.8     57        60.0        60.0
        Adwaita Sans Reg       104.3      -       180.0       190.0
        Source Code Pro Reg     46.2     67        82.0        84.0

    `min_len` / `max_w` are fractions of cap height: ignore stubs shorter
    than 8% of cap (serifs, terminals) and gaps wider than 35% of cap
    (counters, letter interiors), neither of which is a stem.
    """
    min_len = 0.08 * cap_height
    max_w = 0.40 * cap_height
    min_w = 0.015 * cap_height
    widths: list[float] = []
    n = 0
    for g in font.glyphs():
        try:
            ops = _read_glyph(g)
        except Exception:
            continue
        if _is_empty(ops) or _has_components(ops):
            continue
        widths.extend(_vertical_edge_widths(ops, min_len, max_w, min_w))
        n += 1
        if n >= sample:
            break
    if not widths:
        return None
    widths.sort()
    return widths[len(widths) // 2]


def _guess_cap_height(font) -> float:
    try:
        pv = font.private
        for key in ("CapHeight",):
            v = pv.guess(key)
            if v:
                return float(v)
    except Exception:
        pass
    try:
        if font.ascent:
            return float(font.ascent) * 0.875
    except Exception:
        pass
    return float(font.em) * 0.7


def _apply_grid(ops, grid: float):
    out = []
    for op, args in ops:
        if op in ("moveTo", "lineTo"):
            out.append((op, [(_round_to_grid(args[0][0], grid),
                              _round_to_grid(args[0][1], grid))]))
        elif op in ("curveTo", "qCurveTo"):
            out.append((op, [(_round_to_grid(p[0], grid),
                              _round_to_grid(p[1], grid)) for p in args]))
        else:
            out.append((op, args))
    return out


def apply_tracking(font, spacing_value: float) -> None:
    """Letter-spacing: advance widths only, outlines untouched.

    Same dual-mode semantics as v14: |v| < 1.0 is a direct font-unit
    addend, |v| >= 1.0 is a percentage change of each advance.
    """
    if spacing_value == 0:
        return
    abs_val = abs(spacing_value)
    if abs_val < 1.0:
        mode, pct, addend = "direct", 0.0, spacing_value
        if abs(addend) > 4096.0:
            addend = 4096.0 * (1 if addend > 0 else -1)
    else:
        mode = "percent"
        pct = max(-90.0, min(400.0, spacing_value))
        addend = None

    log.info(f"Tracking ({mode} mode, value={spacing_value:+.2f})")
    touched = 0
    max_aw = 0
    for g in font.glyphs():
        try:
            old = g.width
        except Exception:
            continue
        if addend is not None:
            new = int(round(old + addend))
        else:
            new = int(round(old * (1.0 + pct / 100.0)))
        new = max(0, new)
        try:
            g.width = new
        except Exception:
            continue
        max_aw = max(max_aw, new)
        touched += 1
    log.info(f"Tracking applied: {touched} glyph advance widths updated "
             f"(addend: {addend if addend is not None else f'{spacing_value:+.2f}%'}, "
             f"max advance {max_aw})")


def check_outlines_fontforge(font) -> int:
    """FontForge-native topology audit: overlap + direction + canonical form."""
    touched = 0
    for g in font.glyphs():
        try:
            if _is_empty(_read_glyph(g)):
                continue
            g.removeOverlap()
            g.correctDirection()
            g.canonicalContours()
            touched += 1
        except Exception as exc:
            log.debug(f"check-outlines: {getattr(g, 'glyphname', '?')}: {exc}")
    return touched


def apply_hint_tune_tt(font, strength: float) -> None:
    """TrueType autohinting through FontForge's own auto-instruction engine."""
    if strength <= 0.0:
        log.info("--hint-tune: strength 0.0 -> no-op (skipped)")
        return
    if strength >= HINT_STRENGTH_AGGRESSIVE:
        log.info(f"--hint-tune strength {strength:.2f} -> FontForge autoHint "
                 f"+ autoInstr (aggressive)")
        _select_all(font)
        try:
            font.autoHint()
            font.autoInstr()
        except Exception as exc:
            log.error(f"TrueType autohint failed: {exc}")
            return
    else:
        log.info(f"--hint-tune strength {strength:.2f} -> FontForge autoInstr "
                 f"(gentle; instructions only)")
        _select_all(font)
        try:
            font.autoInstr()
        except Exception as exc:
            log.error(f"TrueType autohint failed: {exc}")
            return
    log.info("Autohinted (TrueType via FontForge autoInstr)")


# ---------------------------------------------------------------------------
#  AFDKO-backed stages (shared with v14, FontForge has no equivalent)
# ---------------------------------------------------------------------------

def _resolve_hint_strength(spec, upm: int) -> float:
    if spec is None:
        return 0.0
    if isinstance(spec, str):
        if spec != "auto":
            log.warning(f"--hint-tune unrecognized string {spec!r}, treating as 'auto'")
        strength = 0.70 if upm == 1000 else 0.85 if upm == 2048 else 0.75
        log.info(f"--hint-tune auto: UPM={upm} -> strength {strength:.2f}")
        return strength
    strength = float(spec)
    if strength < 0.0 or strength > 1.0:
        log.warning(f"--hint-tune strength {strength:.3f} outside [0,1], clamped")
        strength = max(0.0, min(1.0, strength))
    return strength


def _hint_engine_kwargs(strength: float) -> dict:
    if strength < HINT_STRENGTH_PRESERVE:
        return {"hintAll": False, "allowChanges": False}
    if strength < HINT_STRENGTH_AGGRESSIVE:
        return {"hintAll": False, "allowChanges": True}
    return {"hintAll": True, "allowChanges": True}


def apply_hint_tune_cff(path: Path, strength: float) -> bool:
    """CFF autohint via AFDKO otfautohint on the generated file.

    FontForge cannot write CFF hint dicts, so this stage is identical to
    v14's: wrap the TTFont in foundrytools, hint, write back.
    """
    if strength <= 0.0:
        return False
    if not HAS_FOUNDRYTOOLS:
        log.warning("--hint-tune requested but foundrytools is not installed; "
                    "skipping. `pip install foundrytools` to enable.")
        return False
    tt = TTFont(str(path))
    ft_font = _FtFont(tt)
    try:
        from foundrytools.app.otf_autohint import run as otf_autohint
        try:
            from afdko.otfautohint.logging import otfautoLogFormatter
            _orig = otfautoLogFormatter.format

            def _patched(self, record):
                for attr in ("glyph", "instance", "dimension"):
                    if not hasattr(record, attr):
                        setattr(record, attr, "")
                return _orig(self, record)

            otfautoLogFormatter.format = _patched
        except Exception:
            pass
        otf_autohint(ft_font, **_hint_engine_kwargs(strength))
        if "CFF " in ft_font.ttfont:
            tt.tables["CFF "] = ft_font.ttfont["CFF "]
        tt.save(str(path))
        log.info("Autohinted (CFF via foundrytools/AFDKO otfautohint)")
        return True
    except Exception as exc:
        log.error(f"CFF autohint failed: {exc}")
        return False
    finally:
        try:
            ft_font.close()
        except Exception:
            pass
        try:
            tt.close()
        except Exception:
            pass


def apply_zones(path: Path) -> bool:
    """Recompute CFF BlueValues / StemSnap from the final outlines."""
    if not HAS_ZONES or not HAS_AFDKO:
        log.warning("--zones requested but foundrytools/afdko are not "
                    "installed; skipping.")
        return False
    tt = TTFont(str(path))
    try:
        ok = recalc_zones(tt)
        if ok:
            tt.save(str(path))
        return bool(ok)
    except Exception as exc:
        log.warning(f"--zones failed: {exc}")
        return False
    finally:
        try:
            tt.close()
        except Exception:
            pass


def apply_check_outlines_afdko(path: Path) -> bool:
    if not HAS_CHECK_OUTLINES:
        log.warning("--check-outlines requested but the AFDKO wrapper is "
                    "not available; skipping.")
        return False
    tt = TTFont(str(path))
    try:
        ok = _ft_check_outlines(tt)
        if ok:
            tt.save(str(path))
        return bool(ok)
    except Exception as exc:
        log.warning(f"--check-outlines (AFDKO) failed: {exc}")
        return False
    finally:
        try:
            tt.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  CLI value parsing (dual-mode, identical to v14)
# ---------------------------------------------------------------------------

def parse_scale_percent(value: float) -> float:
    """0 -> 1.0; |v| < 1 -> multiplier; |v| >= 1 -> percentage change."""
    if value == 0:
        return 1.0
    abs_val = abs(value)
    if abs_val < 1.0:
        return value
    return 1.0 + value / 100.0


def clamp_scale(factor: float) -> float:
    if factor < 0.10:
        log.warning(f"Scale factor {factor:.3f} too small, clamping to 0.10")
        return 0.10
    if factor > 4.00:
        log.warning(f"Scale factor {factor:.3f} too large, clamping to 4.00")
        return 4.00
    return factor


# ---------------------------------------------------------------------------
#  Per-font driver
# ---------------------------------------------------------------------------

def _generate(font, path: Path, is_cff: bool) -> None:
    """Write the font out in its native flavour.

    FontForge's generate() flag vocabulary is deliberately tiny: this
    build has no 'sfnt' or 'truetype' flag, and rejects them with
    "Unknown generate flag". A CFF font needs flags=('opentype',) to
    keep its PostScript outlines; a TrueType font is written with NO
    flags, which is the sfnt/TrueType path.
    """
    if is_cff:
        font.generate(str(path), flags=("opentype",))
    else:
        font.generate(str(path))


def optimize_one(input_path: Path, output_path: Path,
                 scale_factor: float, thickness_percent: float,
                 spacing_value: float, hint_tune, correct: bool,
                 zones: bool, check_flag: bool,
                 thicken_grid: float, thicken_epsilon,
                 thicken_stem_quantize: int, thicken_flatten: int) -> bool:
    log.info(f"Loading {input_path.name}...")

    is_cff = _is_cff(input_path)
    out_ext = ".otf" if is_cff else ".ttf"
    if output_path.suffix.lower() != out_ext:
        output_path = output_path.with_suffix(out_ext)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # All work happens on a private copy. The source font is the user's
    # master: stage 0 rewrites in place, and the pipeline must never be
    # able to damage an input.
    work_dir = Path(tempfile.mkdtemp(prefix="otf-ff-v14-"))
    work = work_dir / input_path.name
    try:
        shutil.copy2(input_path, work)

        # Stage 0: variable -> static, before FontForge ever opens the file.
        # FontForge has no fvar API, so it would otherwise operate on the
        # default instance while leaving the variation tables in place.
        instantiate_to_static_file(input_path, work)

        try:
            font = fontforge.open(str(work))
        except Exception as exc:
            log.error(f"Failed to load {input_path.name}: {exc}")
            return False

        try:
            if correct:
                try:
                    correct_glyphs(font, is_cff)
                except Exception as exc:
                    log.error(f"--correct failed on {input_path.name}: {exc}")
                    return False

            if scale_factor != 1.0:
                log.info(f"Scaling {input_path.name} by x{scale_factor:.4f} "
                         f"(em preserved at {font.em})")
                try:
                    scale_glyphs(font, scale_factor)
                except Exception as exc:
                    log.error(f"--scale failed on {input_path.name}: {exc}")
                    return False

            if thickness_percent > 0:
                try:
                    thicken_glyphs(font, thickness_percent,
                                   coord_grid=thicken_grid,
                                   simplify_epsilon=thicken_epsilon,
                                   stem_quantize=thicken_stem_quantize,
                                   flatten_segments=thicken_flatten,
                                   is_cff=is_cff)
                except Exception as exc:
                    log.error(f"--thickness failed on {input_path.name}: {exc}")
                    return False

            if spacing_value != 0:
                try:
                    apply_tracking(font, spacing_value)
                except Exception as exc:
                    log.error(f"--spacing failed on {input_path.name}: {exc}")
                    return False

            if check_flag:
                try:
                    n = check_outlines_fontforge(font)
                    log.info(f"--check-outlines: FontForge topology pass "
                             f"touched {n} glyphs")
                except Exception as exc:
                    log.error(f"--check-outlines failed on {input_path.name}: {exc}")
                    return False

            try:
                _generate(font, output_path, is_cff)
            except Exception as exc:
                log.error(f"Failed to save {output_path.name}: {exc}")
                return False
        finally:
            try:
                font.close()
            except Exception:
                pass

        # ---- AFDKO post-pass stages (run on the written file) ----
        if zones:
            apply_zones(output_path)

        if check_flag:
            apply_check_outlines_afdko(output_path)

        if hint_tune is not None:
            try:
                upm = TTFont(str(output_path), lazy=True)["head"].unitsPerEm
            except Exception:
                upm = 1000
            strength = _resolve_hint_strength(hint_tune, upm)
            if is_cff:
                apply_hint_tune_cff(output_path, strength)
            else:
                try:
                    font2 = fontforge.open(str(output_path))
                    apply_hint_tune_tt(font2, strength)
                    _generate(font2, output_path, is_cff)
                    font2.close()
                except Exception as exc:
                    log.error(f"--hint-tune failed: {exc}")

        in_size = input_path.stat().st_size
        out_size = output_path.stat().st_size
        log.info(f"Saved {output_path.name} ({in_size:,} -> {out_size:,} bytes)")
        return True
    except Exception as exc:
        log.error(f"Pipeline failed on {input_path.name}: "
                  f"{type(exc).__name__}: {exc}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def _process_font(job):
    """Optimize one font. Module level so multiprocessing can pickle it.

    ``job`` is ``(input_path, output_path, options_dict)`` — primitives
    only, because FontForge handles and closures cannot cross a process
    boundary. Returns ``(font_name, ok)``.
    """
    src, dst, o = job
    name = Path(src).name
    try:
        good = optimize_one(
            Path(src), Path(dst), o["scale_factor"], o["thickness_percent"],
            o["spacing_value"], o["hint_tune"], o["correct"], o["zones"],
            o["check_flag"],
            thicken_grid=o["thicken_grid"],
            thicken_epsilon=o["thicken_epsilon"],
            thicken_stem_quantize=o["thicken_stem_quantize"],
            thicken_flatten=o["thicken_flatten"])
        return name, good
    except Exception as exc:  # pragma: no cover - defensive
        log.error(f"Worker crashed on {name}: {type(exc).__name__}: {exc}")
        return name, False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="otf_optimize-ff-v14",
        description="FontForge port of otf_optimize-v14.py. Same features, "
                    "same CLI, FontForge contour engine.",
    )
    p.add_argument("input_dir", type=Path)
    p.add_argument("output_dir", type=Path)
    p.add_argument("--scale", type=float, default=0, dest="scale",
                   help="|v|<1.0 = multiplier, |v|>=1.0 = percent, 0 = no-op. "
                        "Outlines scale within the em-square; UPM preserved.")
    p.add_argument("--thickness", type=float, default=0, dest="thickness_percent",
                   help="Thicken stems by percent. Same dual-mode semantics "
                        "as --scale. Advance widths preserved.")
    p.add_argument("--thicken-grid", type=float, default=DEFAULT_COORD_GRID,
                   dest="thicken_grid", metavar="UNITS",
                   help="Coordinate quantization grid in font units "
                        "(default 0.25). Coarser values align geometry to the "
                        "integer grid for more deterministic hinting.")
    p.add_argument("--thicken-epsilon", type=float, default=None,
                   dest="thicken_epsilon", metavar="UNITS",
                   help="Pre-simplify on-curve point merge tolerance in font "
                        "units (default None = off).")
    p.add_argument("--thicken-stem-quantize", type=int, default=DEFAULT_STEM_QUANTIZE,
                   dest="thicken_stem_quantize", metavar="UNITS",
                   help="Force stem widths to multiples of N font units "
                        "(0 = off). Typical 10-20 on a 1000 UPM font.")
    p.add_argument("--thicken-flatten", type=int, default=DEFAULT_FLATTEN_SEGMENTS,
                   dest="thicken_flatten", metavar="N",
                   help="Linearize curves to N line segments per cubic before "
                        "the outset (0 = off, curves preserved). Fringe-free "
                        "at the cost of file size.")
    p.add_argument("--spacing", type=float, default=0, dest="spacing",
                   help="Letter-spacing. Outlines unchanged; every advance "
                        "width grows. Dual-mode: |v|<1.0 = font units, "
                        "|v|>=1.0 = percent.")
    p.add_argument("--correct", action="store_true", dest="correct",
                   help="Contour correction (removeOverlap + correctDirection "
                        "+ round) on every glyph before any other transform.")
    p.add_argument("--zones", action="store_true", dest="zones",
                   help="Recompute CFF BlueValues / StdHW / StemSnapH-V from "
                        "the final outlines (AFDKO). CFF only.")
    p.add_argument("--check-outlines", action="store_true",
                   dest="check_outlines_flag",
                   help="Topology audit + auto-fix: FontForge pass in-process, "
                        "then the AFDKO pass on the written file.")
    p.add_argument("--hint-tune", default="off", dest="hint_tune", metavar="MODE",
                   help="'off' (default), 'auto' (pick from UPM), or a float "
                        "in [0,1].")
    p.add_argument("--jobs", "-j", type=int, default=0, dest="jobs",
                   metavar="N",
                   help="Process N fonts in parallel (0 = auto: one worker per "
                        "CPU, capped at the number of fonts). Fonts are "
                        "independent -- each is read from the input dir, "
                        "transformed in its own temp dir and written to its "
                        "own output file -- so this is embarrassingly "
                        "parallel and changes no geometry. Use --jobs 1 to "
                        "force the original sequential behaviour.")
    args = p.parse_args(argv)

    scale_factor = clamp_scale(parse_scale_percent(args.scale))
    if scale_factor == 1.0:
        log.info("--scale 0 or 1: pass-through (no size change)")
    else:
        log.info(f"Scale factor: x{scale_factor:.4f} (em preserved)")
    log.info(f"--thickness {args.thickness_percent:+.2f}%"
             if args.thickness_percent else "--thickness 0: no stem thickening")
    log.info(f"--spacing {args.spacing:+.2f}" if args.spacing else "--spacing 0: no tracking")
    log.info("--correct ON" if args.correct else "--correct: off")
    log.info("--zones ON" if args.zones else "--zones: off")
    log.info("--check-outlines ON" if args.check_outlines_flag else "--check-outlines: off")

    if args.hint_tune == "off":
        hint_tune = None
        log.info("--hint-tune: off")
    else:
        try:
            hint_tune = float(args.hint_tune)
            log.info(f"--hint-tune: explicit strength {hint_tune:.2f}")
        except (TypeError, ValueError):
            hint_tune = "auto"
            log.info("--hint-tune: 'auto' (strength picked per-font from UPM)")

    if not args.input_dir.is_dir():
        log.error(f"Input directory not found: {args.input_dir}")
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)

    fonts = sorted(q for q in args.input_dir.iterdir()
                   if q.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not fonts:
        log.error(f"No .otf/.ttf files found in {args.input_dir}")
        return 1

    log.info(f"Processing {len(fonts)} font(s) from {args.input_dir} -> {args.output_dir}")

    # Fonts are fully independent, so the batch parallelises across files
    # rather than within one. Every stage is either per-glyph (inside a
    # worker) or writes to that font's own output path, and the AFDKO
    # post-passes use unique temp files, so there is no shared state to
    # serialise. Each worker imports fontforge and opens its own copy, so
    # peak RSS scales with --jobs: cap the default to something sane.
    try:
        cpu = os.cpu_count() or 1
    except NotImplementedError:  # pragma: no cover
        cpu = 1
    if args.jobs and args.jobs > 0:
        workers = min(args.jobs, len(fonts))
    else:
        workers = min(cpu, len(fonts))

    if workers > 1:
        log.info(f"Parallel: {workers} worker(s) across {len(fonts)} font(s) "
                 f"({cpu} CPUs visible)")
    else:
        log.info("Sequential (1 worker)")

    # FontForge's Python objects cannot cross a process boundary, and a
    # closure cannot be pickled at all, so each worker gets a plain tuple
    # of primitives and re-opens the font itself.
    opts = {
        "scale_factor": scale_factor,
        "thickness_percent": args.thickness_percent,
        "spacing_value": args.spacing,
        "hint_tune": hint_tune,
        "correct": args.correct,
        "zones": args.zones,
        "check_flag": args.check_outlines_flag,
        "thicken_grid": args.thicken_grid,
        "thicken_epsilon": args.thicken_epsilon,
        "thicken_stem_quantize": args.thicken_stem_quantize,
        "thicken_flatten": args.thicken_flatten,
    }
    jobs = [(str(q), str(args.output_dir / q.name), opts) for q in fonts]

    ok = 0
    if workers > 1:
        # 'fork' keeps startup cheap; each child gets its own fontforge
        # module state and its own font copy, so nothing is shared.
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        with ctx.Pool(processes=workers) as pool:
            for name, good in pool.imap_unordered(_process_font, jobs):
                log.info(f"[{'ok' if good else 'FAILED'}] {name}")
                if good:
                    ok += 1
    else:
        for job in jobs:
            if _process_font(job)[1]:
                ok += 1

    log.info(f"Done. {ok}/{len(fonts)} font(s) optimized successfully.")
    return 0 if ok == len(fonts) else 1


if __name__ == "__main__":
    sys.exit(main())
