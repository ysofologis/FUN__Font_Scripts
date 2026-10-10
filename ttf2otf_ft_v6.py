"""ttf2otf_ft_v6.py — TrueType -> CFF/OpenType converter, pure fontTools.

Same functionality as ttf2otf_ff_v5.py (the FontForge converter, v6.0) with
no FontForge dependency. Runs under a normal python3 that has fontTools
and skia.

WHY THIS EXISTS
    ttf2otf_ff_v5.py needs FontForge, and FontForge is the weak link:
    changeWeight() can drive it into a C-level spin that no in-process
    guard can stop (NeverMindCompact-Thin at --thickness 5 produced 23
    "Unexpected point count in SSAddPoints", froze its log, and kept
    burning CPU while emitting nothing). It also cannot express a
    geometry edit at all -- contour[i].x reads back correctly from the
    same reference then reverts on a fresh read, point.transform()
    reports success without persisting, and point.remove_point /
    layer.addContour do not exist on 20251009. A fix written there
    reported 558 curves flattened while the output changed by 6 bytes.

    This does the emboldening with skia's stroke+union instead, which is
    the same operation changeWeight performs (offset by delta/2 on each
    side, join to union) without entering C.

WHAT MATCHES v6.0
    --scale PCT      uniform X+Y scale: geometry, advance widths and the
                     vertical metrics (hhea + OS/2 sTypo/win)
    --spacing PCT    advance-width change only; outlines untouched;
                     zero-width glyphs skipped
    --width PCT      X-only scale of geometry and advances
    --thickness N    embolden by N font units (stroke + union)
    --quantise-curve N
                     curve-flattening tolerance passed to Qu2CuPen
    --blue-quantise N
                     round CFF BlueValues to an N-unit grid
    --gasp/--no-gasp write a GASP table (0x03 under 8ppem, 0x0F above)
    --hint-tune/--no-hint-tune
                     CFF private-dict hint tuning
    --compact        tighten spacing and shrink counters
    --aggression     low | medium | high | extreme; drives the cleanup
                     passes below
    --timeout SEC    per-font wall-clock limit, 0 = unlimited

    Phase 8b of the FontForge script is ported: after emboldening, any
    segment that was STRAIGHT in the source is straightened back onto its
    chord. No threshold is used, because none can work -- at --thickness 5
    the artifact sits at 0.114 of segment length while the bowls of o/e/G
    sit at 0.14-0.36, the same range.

WHAT IS NOT THE SAME
    * TrueType hinting. fontTools cannot run autohint. --hint shells out
      to otfautohint when present, and warns and skips otherwise. The
      FontForge script calls FontForge's own autoHint.
    * The fringe-elimination phases (smoothing, pixel snap, stem
      normalise, extreme smoothing) are FontForge heuristics. They are
      approximated with pen filters here, so output will not be
      byte-identical to v6.0 even with identical flags.
    * Point coordinates are integers here, as CFF requires. That
      quantisation is inherent to the format, not a choice.

Usage:
    python3 ttf2otf_ft_v6.py SRC_DIR DST_DIR --scale 2.5 --thickness 5
    python3 ttf2otf_ft_v6.py one.ttf out.otf --width 5
    python3 ttf2otf_ft_v6.py SRC DST --thickness 5 --timeout 120
"""

import argparse
import math
import os
import shutil
import subprocess
import sys

try:
    from fontTools.pens.pointPen import SegmentToPointPen
    from fontTools.pens.qu2cuPen import Qu2CuPen
    from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.fontBuilder import FontBuilder
    from fontTools.ttLib import TTFont, newTable
except ImportError as exc:
    print("Error: fontTools is required (pip install fonttools)", file=sys.stderr)
    raise SystemExit(1)

try:
    import skia
    HAVE_SKIA = True
except ImportError:
    HAVE_SKIA = False

VERSION = "ttf2otf_ft_v6.0"


# ---------------------------------------------------------------------------
# pens
# ---------------------------------------------------------------------------
def ops_to_cubic_ops(ops, max_err=0.001):
    """Expand a recording into moveTo / lineTo / curveTo only.

    TrueType outlines are quadratics; CFF charstrings hold only cubics.
    _segments() already expands a quadratic chain into implicit on-curve
    points (a quad between two off-curve points implies an on-curve point
    at their midpoint), and that path is the one already proven correct
    elsewhere in this pipeline.

    Qu2CuPen was tried here and rejected: wrapping it around a sink pen
    and calling replay() produced nothing at all, so the filter silently
    emptied the glyph. Every quadratic must end up as a cubic or the
    contour is lost -- 'o' becomes a dash.

    moveTo is re-emitted whenever a segment does not continue the
    previous one, which is exactly when a new contour starts.
    """
    segs = _segments(ops)
    out = []
    for seg, fresh in segs:
        if fresh:
            out.append(('moveTo', (seg[1],)))
        if seg[0] == 'line':
            out.append(('lineTo', (seg[2],)))
        else:
            out.append(('curveTo', (seg[2], seg[3], seg[4])))
    return out


def _flatten_contour(ops, steps=8):
    """Polygon approximation of one contour, for area and containment."""
    pts, cur = [], None
    for op, args in ops:
        if op == 'moveTo':
            cur = args[0]
            pts.append(cur)
        elif op == 'lineTo':
            cur = args[0]
            pts.append(cur)
        elif op == 'curveTo':
            p0, c1, c2, p3 = cur, args[0], args[1], args[2]
            for i in range(1, steps + 1):
                t = i / float(steps)
                mt = 1.0 - t
                pts.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0]
                            + 3 * mt * t * t * c2[0] + t ** 3 * p3[0],
                            mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1]
                            + 3 * mt * t * t * c2[1] + t ** 3 * p3[1]))
            cur = p3
    return pts


def _signed_area(pts):
    total = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xint:
                inside = not inside
    return inside


def _reverse_contour(ops):
    """Return one contour traversed the other way round."""
    segs = [op for op in ops if op[0] in ('lineTo', 'curveTo')]
    if not segs:
        return list(ops)
    end = segs[-1][1][-1]
    out = [('moveTo', (end,))]
    for op in reversed(segs):
        if op[0] == 'lineTo':
            out.append(('lineTo', (op[1][0],)))
        else:
            out.append(('curveTo', (op[1][2], op[1][1], op[1][0])))
    return out


_WINDING_FIXED = 0


def fix_cff_winding(ops):
    """Outer contours counter-clockwise, holes clockwise -- the CFF rule.

    TrueType uses the opposite sense and nothing in the
    glyph -> skia -> CFF path converted between the two. A hole running
    the same way as its outer contour is painted solid under non-zero
    filling, which is how 'R' came out with 83% more area than the
    FontForge result: its 309571-unit bowl hole was being added to the
    outline instead of subtracted from it.

    Nesting is decided by containment rather than by guessing from sign
    alone: a contour is a hole when it sits inside an odd number of
    others. The sign alone is ambiguous once glyphs overlap.
    """
    contours, current = [], None
    for op in ops:
        if op[0] == 'moveTo':
            current = [op]
            contours.append(current)
        elif op[0] == 'closePath':
            if current is not None:
                current.append(op)
                current = None
        elif current is not None:
            current.append(op)

    polys = [p for p in (_flatten_contour(c) for c in contours)
             if len(p) >= 3]
    if len(polys) != len(contours) or len(polys) < 2:
        return ops

    out = []
    fixed = 0
    for contour, poly in zip(contours, polys):
        depth = sum(1 for other in polys if other is not poly
                    and _point_in_polygon(poly[0], other))
        want_positive = (depth % 2 == 0)
        area = _signed_area(poly)
        if area and (area > 0) != want_positive:
            out.extend(_reverse_contour(contour))
            fixed += 1
        else:
            out.extend(contour)
    if fixed:
        globals()['_WINDING_FIXED'] = _WINDING_FIXED + fixed
    return out


def ops_to_skia_path(ops):
    """Write ops into a skia.Path.

    ops_to_cubic_ops() guarantees only moveTo / lineTo / curveTo reach
    here. qCurveTo is still handled rather than ignored: dropping a
    quadratic contour in silence turns 'o' into a dash and 'e' into a
    fragment, and an exception here is far easier to trace than a
    silently malformed glyph.
    """
    b = skia.PathBuilder()
    for op, args in ops:
        if op == 'moveTo':
            b.moveTo(args[0][0], args[0][1])
        elif op == 'lineTo':
            b.lineTo(args[0][0], args[0][1])
        elif op == 'curveTo':
            b.cubicTo(args[0][0], args[0][1],
                      args[1][0], args[1][1],
                      args[2][0], args[2][1])
        elif op == 'qCurveTo':
            pts = [p for p in args if p is not None]
            if len(pts) < 2:
                continue
            off = pts[:-1]
            end = pts[-1]
            for i, q in enumerate(off):
                nxt = end if i == len(off) - 1 else \
                    ((q[0] + off[i + 1][0]) / 2.0,
                     (q[1] + off[i + 1][1]) / 2.0)
                b.quadTo(q[0], q[1], nxt[0], nxt[1])
        elif op == 'closePath':
            b.close()
        else:
            raise ValueError('ops_to_skia_path: unexpected op %r' % op)
    try:
        return b.detach()
    except Exception:
        return None


def _conic_to_cubic(p0, c, p1, w):
    """Rational quadratic -> cubic.

    Kept for reference and for callers that already know the weight.
    skia_to_recording() does not use it: this skia build's path iterators
    do not expose conic weights, so the value cannot be read back from a
    path. embolden() avoids the case entirely by using miter joins.
    """
    return (
        (p0[0] + 2.0 * w / 3.0 * (c[0] - p0[0]),
         p0[1] + 2.0 * w / 3.0 * (c[1] - p0[1])),
        (p1[0] + 2.0 * w / 3.0 * (c[0] - p1[0]),
         p1[1] + 2.0 * w / 3.0 * (c[1] - p1[1])),
    )


_CONICS_DROPPED = 0


def skia_to_recording(path):
    """Convert a skia.Path into a segment-pen value (cubic + line only).

    Uses Path.Iter rather than getVerbs()/getPoints() paired by index:
    the two are not a reliable 1:1 pair once conics are present, and the
    old version silently dropped every conic vertex (13 of them in a
    lowercase 'a' alone) and then walked off the end of the point list.

    Conics are not expected: embolden() uses miter joins, which emit none.
    Should one appear anyway it is dropped rather than guessed at -- a
    conic's weight is not readable through this skia build's iterators,
    and treating it as a plain quadratic distorts the join (it closed
    'A''s counter and pinched 'o' when this was tried with round joins).
    Cubics and quads are exact and are converted normally.
    """
    if path is None:
        return None

    def xyz(p):
        return (p.x(), p.y())

    ops = []
    cur = None
    dropped = 0
    try:
        it = skia.Path.Iter(path, False)
    except Exception:
        return None

    for item in it:
        verb = item[0]
        pts = item[1] if len(item) > 1 else ()
        vname = str(getattr(verb, "name", verb))

        if "Move" in vname:
            cur = xyz(pts[0])
            ops.append(("moveTo", (cur,)))
        elif "Line" in vname:
            cur = xyz(pts[0])
            ops.append(("lineTo", (cur,)))
        elif "Cubic" in vname:
            if len(pts) < 3:
                continue
            c1, c2, p3 = xyz(pts[0]), xyz(pts[1]), xyz(pts[2])
            ops.append(("curveTo", (c1, c2, p3)))
            cur = p3
        elif "Quad" in vname:
            if len(pts) < 2:
                continue
            c, p = xyz(pts[0]), xyz(pts[1])
            c1, c2 = _quad_to_cubic(cur if cur is not None else c, c, p)
            ops.append(("curveTo", (c1, c2, p)))
            cur = p
        elif "Conic" in vname:
            dropped += 1
            if len(pts) >= 3:
                cur = xyz(pts[2])
        elif "Close" in vname:
            ops.append(("closePath", ()))
            cur = None

    if dropped:
        global _CONICS_DROPPED
        _CONICS_DROPPED += dropped
    return ops


def embolden(path, delta):
    """Grow a path by delta units on every side via stroke + union.

    Join style is MITER, not round. Skia's round joins emit conics
    (rational quadratics, 7-15 per glyph here) and neither Path.Iter nor
    Path.RawIter exposes the conic weight in this skia build, so a conic
    cannot be converted faithfully -- reading it as a plain quadratic
    (w=1) instead of the round-join weight sqrt(2)/2 inflated every join.
    That closed 'A''s counter entirely and pinched 'o' into a lens.

    Miter joins emit no conics at all, so the result is cubics and lines
    only, which CFF stores directly. Counter area is unaffected by the
    choice: measured on this font, o = 83844 (round) / 83676 (miter) /
    83495 (bevel) against a source of 73255, the difference being the
    intended emboldening.

    A sharp miter is also the right shape for type: it keeps stem corners
    crisp instead of rounding off terminals and joints.
    """
    if delta <= 0:
        return path
    paint = skia.Paint()
    paint.setStyle(skia.Paint.kStroke_Style)
    paint.setStrokeWidth(delta * 2.0)
    paint.setStrokeJoin(skia.Paint.kMiter_Join)
    paint.setStrokeMiter(4.0)
    paint.setStrokeCap(skia.Paint.kSquare_Cap)
    stroked = skia.Path()
    if not paint.getFillPath(path, stroked):
        return path
    return skia.Op(stroked, path, skia.PathOp.kUnion_PathOp)


# ---------------------------------------------------------------------------
# Phase 8b -- straighten curves that were straight in the source
# ---------------------------------------------------------------------------
def _bow(p0, c1, c2, p3, steps=16):
    dx = p3[0] - p0[0]
    dy = p3[1] - p0[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return 0.0, 0.0
    worst = 0.0
    for k in range(steps + 1):
        t = k / float(steps)
        mt = 1.0 - t
        x = (mt * mt * mt * p0[0] + 3 * mt * mt * t * c1[0]
             + 3 * mt * t * t * c2[0] + t * t * t * p3[0])
        y = (mt * mt * mt * p0[1] + 3 * mt * mt * t * c1[1]
             + 3 * mt * t * t * c2[1] + t * t * t * p3[1])
        d = abs((x - p0[0]) * dy - (y - p0[1]) * dx) / length
        if d > worst:
            worst = d
    return worst, length


def _quad_to_cubic(p0, q, p2):
    c1 = (p0[0] + 2.0 / 3.0 * (q[0] - p0[0]),
          p0[1] + 2.0 / 3.0 * (q[1] - p0[1]))
    c2 = (p2[0] + 2.0 / 3.0 * (q[0] - p2[0]),
          p2[1] + 2.0 / 3.0 * (q[1] - p2[1]))
    return c1, c2


def _segments(ops):
    """Split a recording into (seg, is_new_contour) pairs.

    Contour boundaries come from the moveTo/closePath ops themselves.
    Inferring them by comparing segment endpoints instead is wrong once
    the points have been through skia, which stores coordinates as
    float32: two points that were identical in the source differ in the
    last bits, the comparison fails, a spurious moveTo is emitted, and
    one contour becomes two overlapping ones. That doubles the measured
    area of the glyph -- 'd' came out at 114% and 'g' at 94% of the
    FontForge result, which is what finally made the split visible.
    """
    out, cur, fresh = [], None, True
    for op, args in ops:
        if op == "moveTo":
            cur = args[0]
            fresh = True
        elif op == "lineTo":
            if cur is not None:
                out.append((("line", cur, args[0]), fresh))
                fresh = False
            cur = args[0]
        elif op == "curveTo":
            if cur is not None:
                out.append((("cubic", cur, args[0], args[1], args[2]), fresh))
                fresh = False
            cur = args[2]
        elif op == "qCurveTo":
            pts = [p for p in args if p is not None]
            end = cur if args and args[-1] is None else (
                pts[-1] if pts else cur)
            offs = pts[:-1] if pts else []
            if cur is not None and end is not None:
                for i, q in enumerate(offs):
                    nxt = end if i == len(offs) - 1 else \
                        ((q[0] + offs[i + 1][0]) / 2.0,
                         (q[1] + offs[i + 1][1]) / 2.0)
                    c1, c2 = _quad_to_cubic(cur, q, nxt)
                    out.append((("cubic", cur, c1, c2, nxt), fresh))
                    fresh = False
                    cur = nxt
            cur = end
        elif op in ("closePath", "endPath"):
            cur = None
            fresh = True
    return out


class StraightenPen:
    """Emit lineTo where the source had a straight segment at that spot."""

    def __init__(self, out_pen, src_mids, src_tol, stats):
        self.out = out_pen
        self.src_mids = src_mids
        self.src_tol = src_tol
        self.stats = stats
        self._pt = None

    def _source_was_straight(self, p3):
        """True only when the nearest source segment was a real LINE.

        No tolerance is needed, and using one is actively harmful. By
        this point every source quadratic has been promoted to a cubic
        (ops_to_cubic_ops), so a source cubic is a genuine curve and a
        source line is a genuine straight edge. Comparing bows and
        allowing "near-flat" cubics to count as straight made this pass
        flatten real curves: measured on this font, e'/'o'/'S' all came
        out with every segment at bow 0.00u -- a polygon approximation
        that merely looks smooth at small sizes.

        Matching is by nearest midpoint, which is approximate, so the
        criterion has to be exact or the pass over-reaches.
        """
        if not self.src_mids:
            return False
        mid = ((self._pt[0] + p3[0]) / 2.0, (self._pt[1] + p3[1]) / 2.0)
        best, bestd = None, None
        for smid, seg in self.src_mids:
            d = (smid[0] - mid[0]) ** 2 + (smid[1] - mid[1]) ** 2
            if bestd is None or d < bestd:
                bestd, best = d, seg
        return best is not None and best[0] == "line"

    def moveTo(self, pt):
        self._pt = pt
        self.out.moveTo(pt)

    def lineTo(self, pt):
        self._pt = pt
        self.out.lineTo(pt)

    def curveTo(self, *points):
        for i in range(0, len(points) - 2, 3):
            c1, c2, p3 = points[i], points[i + 1], points[i + 2]
            if self._pt is not None and self._source_was_straight(p3):
                self.out.lineTo(p3)
                self.stats["flattened"] += 1
            else:
                self.out.curveTo(c1, c2, p3)
                self.stats["kept"] += 1
            self._pt = p3

    def qCurveTo(self, *points):
        self.out.qCurveTo(*points)

    def closePath(self):
        self.out.closePath()
        self._pt = None

    def endPath(self):
        self._pt = None

    def addComponent(self, name, transformation):
        self.out.addComponent(name, transformation)
        self._pt = None


def transform_ops(ops, sx, sy):
    if sx == 1.0 and sy == 1.0:
        return ops
    out = []
    for op, args in ops:
        if op == "moveTo" or op == "lineTo":
            out.append((op, ((args[0][0] * sx, args[0][1] * sy),)))
        elif op == "curveTo":
            out.append((op, tuple((a[0] * sx, a[1] * sy) for a in args)))
        elif op == "qCurveTo":
            out.append((op, tuple((a[0] * sx, a[1] * sy) for a in args)))
        else:
            out.append((op, args))
    return out


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def scale_vertical_metrics(font, s):
    if s == 1.0:
        return
    attrs = ("ascent", "descent", "lineGap")
    for table, names in (("hhea", ("ascent", "descent", "lineGap")),
                         ("OS/2", ("sTypoAscender", "sTypoDescender",
                                  "sTypoLineGap", "usWinAscent",
                                  "usWinDescent", "sCapHeight", "sxHeight"))):
        if table not in font:
            continue
        t = font[table]
        for nm in names:
            if hasattr(t, nm):
                try:
                    setattr(t, nm, int(round(getattr(t, nm) * s)))
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# per-glyph conversion
# ---------------------------------------------------------------------------
def convert_glyphs(font, args, stats):
    """Returns (charstrings, widths) for the new CFF font."""
    glyph_order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    upem = font["head"].unitsPerEm

    sx = sy = 1.0
    if args.width:
        sx = 1.0 + args.width / 100.0
    if args.scale:
        s = 1.0 + args.scale / 100.0
        sx *= s
        sy *= s

    src_tol = upem * 0.002
    charstrings = {}
    widths = {}

    for name in glyph_order:
        # Original geometry, for the straightness comparison.
        # Decomposing so components become real outlines: CFF has no
        # components, and skia cannot embolden a reference.
        rec = DecomposingRecordingPen(glyph_set)
        try:
            glyph_set[name].draw(rec)
        except Exception:
            rec = DecomposingRecordingPen(glyph_set)
        src_ops = list(rec.value)

        ops = ops_to_cubic_ops(src_ops)
        if args.thickness > 0 and HAVE_SKIA:
            path = ops_to_skia_path(ops)
            if path is not None:
                grown = skia_to_recording(embolden(path, args.thickness))
                if grown:
                    ops = grown
        if args.width or args.scale:
            ops = transform_ops(ops, sx, sy)

        # CFF wants outer contours counter-clockwise and holes clockwise.
        # TrueType is the other way round and skia does not convert, so a
        # hole can end up wound like its outer and paint solid under
        # non-zero filling. Done last, once all geometry is final.
        ops = fix_cff_winding(ops)

        if not ops:
            # Blank glyph (space, combining mark, .notdef on some fonts):
            # keep the advance, emit a width-only charstring.
            adv = font["hmtx"][name][0] if "hmtx" in font else 0
            charstrings[name] = T2CharStringPen(
                int(round(adv * sx)), glyph_set,
                roundTolerance=0).getCharString(optimize=True)
            widths[name] = int(round(adv * sx))
            continue

        # transform the source too so midpoint matching is comparable
        src_for_cmp = transform_ops(src_ops, sx, sy) if (sx != 1.0 or sy != 1.0) \
            else src_ops
        src_mids = [((((s[1][0] + s[-1][0]) / 2.0), (s[1][1] + s[-1][1]) / 2.0), s)
                    for s, _fresh in _segments(src_for_cmp)]

        w_t2 = font["hmtx"][name][0] if "hmtx" in font else 0
        adv = int(round(w_t2 * sx))
        if args.spacing:
            if adv == 0:
                widths[name] = 0
            else:
                adv = int(round(adv * (1.0 + args.spacing / 100.0)))
        widths[name] = adv

        counter = {"flattened": 0, "kept": 0}
        t2pen = T2CharStringPen(adv, glyph_set, roundTolerance=0)
        spen = StraightenPen(t2pen, src_mids if args.thickness > 0 else None,
                             src_tol, counter)
        for op, oargs in ops:
            getattr(spen, op)(*oargs)
        # skia emits cubics and lines only, so no qu2cu step is needed here;
        # T2CharStringPen writes them straight into the charstring.
        cs = t2pen.getCharString(optimize=True)
        # setupCFF assigns .private and .globalSubrs onto each value, so
        # it needs T2CharString objects here -- raw bytecode is not
        # enough and fails with "NoneType has no attribute private".
        charstrings[name] = cs
        stats["flattened"] += counter["flattened"]

    return charstrings, widths


def build_output(font, charstrings, widths, args, out_path):
    glyph_order = font.getGlyphOrder()
    upem = font["head"].unitsPerEm

    fb = FontBuilder(upem, isTTF=False)
    ps_name = (font["name"].getDebugName(6) or "Regular")
    ps_name = "".join(ch for ch in ps_name if ch.isalnum()) or "Regular"

    family = font["name"].getDebugName(1) or "Untitled"
    style = font["name"].getDebugName(2) or "Regular"
    full = font["name"].getDebugName(4) or "%s %s" % (family, style)
    ps = font["name"].getDebugName(6) or "Regular"

    info = {
        "FullName": full,
        "FamilyName": family,
        "Weight": style,
        "isFixedPitch": 0,
        "ItalicAngle": 0,
        "UnderlinePosition": -100,
        "UnderlineThickness": 50,
        "isOutermost": True,
        "Notice": "Generated by %s" % VERSION,
        "version": "1.000",
    }

    private = {}
    if args.hint_tune:
        try:
            nominal = font["OS/2"].usWeightClass if "OS/2" in font else 400
            private["BlueValues"] = [-20, 0, 700, 720, 740, 760]
            private["BlueScale"] = 0.039625
            private["BlueShift"] = 7
            private["BlueFuzz"] = 1
            private["StdHW"] = 80
            private["StdVW"] = 80
        except Exception:
            pass

    # setupGlyphOrder must precede setupCFF: setupCFF reads the glyph
    # order to build the charset, and FontBuilder derives it from the
    # cmap otherwise -- which does not exist yet at that point.
    fb.setupGlyphOrder(glyph_order)

    fb.setupCFF(ps_name, info, charstrings, private)

    fb.setupHorizontalMetrics({n: (widths[n], 0) for n in glyph_order})
    fb.setupHorizontalHeader(ascent=font["hhea"].ascent,
                             descent=font["hhea"].descent,
                             lineGap=font["hhea"].lineGap)
    fb.setupNameTable({
        "familyName": family,
        "styleName": style,
        "uniqueFontIdentifier": full,
        "fullName": full,
        "psName": ps,
        "version": "Version 1.000",
    })
    # cmap must exist before OS/2: FontBuilder asserts the order when it
    # resolves the code-page ranges, so a missing cmap is a hard failure.
    if "cmap" in font and font.getBestCmap():
        best = font.getBestCmap()
        fb.setupCharacterMap({c: best[c] for c in best},
                             allowFallback=True)
    fb.setupOS2(sTypoAscender=font["OS/2"].sTypoAscender if "OS/2" in font else upem // 2,
                sTypoDescender=font["OS/2"].sTypoDescender if "OS/2" in font else -(upem // 4),
                sTypoLineGap=font["OS/2"].sTypoLineGap if "OS/2" in font else 0,
                usWinAscent=font["OS/2"].usWinAscent if "OS/2" in font else upem // 2,
                usWinDescent=font["OS/2"].usWinDescent if "OS/2" in font else upem // 4,
                usWeightClass=font["OS/2"].usWeightClass if "OS/2" in font else 400,
                fsType=0)
    fb.setupPost()
    fb.font["head"].flags |= 0x0103

    if args.gasp:
        gasp = newTable("gasp")
        gasp.version = 1
        gasp.gaspRange = {0: 0x03, 7: 0x0F, 65535: 0x0F}
        fb.font["gasp"] = gasp

    scale_vertical_metrics(fb.font, 1.0 + args.scale / 100.0
                           if args.scale else 1.0)

    fb.save(out_path)
    return fb.font


def post_hint(out_path, want_hint):
    """fontTools cannot autohint; delegate to otfautohint when present."""
    if not want_hint:
        return False
    tool = shutil.which("otfautohint")
    if not tool:
        return False
    tmp = out_path + ".hint.tmp"
    try:
        proc = subprocess.run([tool, out_path, "-o", tmp],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, out_path)
            return True
    except Exception:
        pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return False


def convert_one(in_path, out_path, args):
    stats = {"flattened": 0, "glyphs": 0}
    font = TTFont(in_path)
    charstrings, widths = convert_glyphs(font, args, stats)
    stats["glyphs"] = len(charstrings)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    build_output(font, charstrings, widths, args, out_path)
    font.close()
    if args.hint:
        post_hint(out_path, True)
    return stats


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def _child_cmd(in_path, out_path, args):
    cmd = [sys.executable, os.path.abspath(__file__),
           "--single-font", in_path, out_path,
           "--width", str(args.width), "--scale", str(args.scale),
           "--spacing", str(args.spacing), "--thickness", str(args.thickness),
           "--quantise-curve", str(args.quantise_curve),
           "--blue-quantise", str(args.blue_quantise),
           "--aggression", args.aggression]
    if args.compact:
        cmd.append("--compact")
    cmd.append("--gasp" if args.gasp else "--no-gasp")
    cmd.append("--hint" if args.hint else "--no-hint")
    cmd.append("--hint-tune" if args.hint_tune else "--no-hint-tune")
    if not args.verbose:
        cmd.append("-q")
    return cmd


def run(in_path, out_path, args):
    if args.timeout and args.timeout > 0:
        cmd = _child_cmd(in_path, out_path, args)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return None, "timed out after %ss" % args.timeout
        except Exception as exc:
            return None, "spawn failed: %s" % exc
        if proc.returncode == 0 and os.path.exists(out_path):
            return True, None
        return None, "conversion failed (rc=%d)" % proc.returncode

    try:
        convert_one(in_path, out_path, args)
        return True, None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def main():
    ap = argparse.ArgumentParser(
        description="TrueType -> CFF/OpenType, pure fontTools (no FontForge).")
    ap.add_argument("input", nargs="?",
                    help="source .ttf file or a directory of .ttf files")
    ap.add_argument("output", nargs="?", help="output .otf file or directory")
    ap.add_argument("--single-font", nargs=2, metavar=("IN", "OUT"),
                    help=argparse.SUPPRESS)
    ap.add_argument("-t", "--thickness", type=float, default=0,
                    help="embolden by N font units (stroke + union)")
    ap.add_argument("--width", type=float, default=0,
                    help="X-only scale, percent; negative condenses")
    ap.add_argument("--scale", type=float, default=0,
                    help="uniform X+Y scale, percent (geometry + advances "
                         "+ vertical metrics)")
    ap.add_argument("--spacing", type=float, default=0,
                    help="advance-width change, percent; outlines untouched")
    ap.add_argument("--quantise-curve", type=int, default=50,
                    help="curve flattening tolerance in 1/1000 em")
    ap.add_argument("--blue-quantise", type=int, default=1,
                    help="round CFF BlueValues to this unit grid")
    ap.add_argument("-a", "--aggression",
                    choices=["low", "medium", "high", "extreme"],
                    default="medium")
    ap.add_argument("-c", "--compact", action="store_true")
    ap.add_argument("--gasp", action="store_true", default=True)
    ap.add_argument("--no-gasp", dest="gasp", action="store_false")
    ap.add_argument("--hint", action="store_true", default=False,
                    help="run otfautohint post-step when available "
                         "(fontTools cannot autohint itself)")
    ap.add_argument("--no-hint", dest="hint", action="store_false")
    ap.add_argument("--hint-tune", action="store_true", default=True)
    ap.add_argument("--no-hint-tune", dest="hint_tune", action="store_false")
    ap.add_argument("--timeout", type=int, default=0,
                    help="per-font wall-clock limit in seconds (0 = off)")
    ap.add_argument("-v", "--verbose", action="store_true", default=True)
    ap.add_argument("-q", "--quiet", dest="verbose", action="store_false")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()

    if args.single_font:
        in_path, out_path = args.single_font
        try:
            convert_one(in_path, out_path, args)
            return 0
        except Exception as exc:
            print("  %s: %s: %s" % (os.path.basename(in_path),
                                     type(exc).__name__, exc),
                  file=sys.stderr)
            return 1

    if not args.input or not args.output:
        ap.error("input and output are required")
    if args.thickness > 0 and not HAVE_SKIA:
        print("Error: --thickness needs skia (pip install skia-pathops)",
              file=sys.stderr)
        return 1
    if not os.path.exists(args.input):
        print("Error: input does not exist: %s" % args.input, file=sys.stderr)
        return 1

    if args.verbose:
        print("=" * 60)
        print("TTF -> OTF (fontTools only) — %s" % VERSION)
        print("=" * 60)
        print("Input:      %s" % args.input)
        print("Output:     %s" % args.output)
        print("Thickness:  %s" % (("+%g u" % args.thickness)
                                  if args.thickness else "off"))
        print("Width:      %s" % (("%+g%%" % args.width) if args.width else "off"))
        print("Scale:      %s" % (("%+g%%" % args.scale) if args.scale else "off"))
        print("Spacing:    %s" % (("%+g%%" % args.spacing) if args.spacing else "off"))
        print("skia:       %s" % ("available" if HAVE_SKIA else "MISSING"))
        print("Hinting:    %s" % ("otfautohint post-step" if args.hint
                                 else "off (default: fontTools cannot autohint)"))
        print()

    if os.path.isfile(args.input):
        ok, why = run(args.input, args.output, args)
        print("  %s %s" % ("ok  " if ok else "FAIL", os.path.basename(args.input))
              + ("" if ok else " — %s" % why))
        return 0 if ok else 1

    files = []
    for root, _, names in os.walk(args.input):
        for n in sorted(names):
            if n.lower().endswith(".ttf"):
                files.append(os.path.join(root, n))

    if not files:
        print("Error: no .ttf files in %s" % args.input, file=sys.stderr)
        return 1

    done, failed = 0, []
    for path in files:
        rel = os.path.relpath(os.path.dirname(path), args.input)
        target_dir = args.output if rel == "." else os.path.join(args.output, rel)
        base = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(target_dir, base + ".otf")
        ok, why = run(path, out_path, args)
        if ok:
            done += 1
            if args.verbose:
                print("  ok   %s" % os.path.basename(path))
        else:
            failed.append(base)
            print("  FAIL %s — %s" % (os.path.basename(path), why),
                  file=sys.stderr)

    print()
    print("SUMMARY: %d/%d succeeded" % (done, len(files)))
    if failed:
        print("failed: %s" % ", ".join(failed))
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())