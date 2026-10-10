"""flatten_straight_curves.py — collapse near-straight curves back onto their chords.

    Why this exists
      FontForge's changeWeight() rewrites every segment it touches. A
      straight diagonal — the long edge of a Z, N, A, V, X — comes back
      as a cubic whose control points sit a couple of units off the
      chord. The stroke keeps its nominal weight, but the outline no
      longer matches the designer's intent: in the source font a shape
      either needs a curve or it gets a line, and now it gets a curve
      that is a line.

        Measured on NeverMindCompact (upm=2048):
          source          1 near-straight curve of   935
          after changeWeight   4791 of 10268
          Z diagonal bow 3.18u / 2.89u  (0.155% / 0.141% of em)

      This pass rewrites such a segment as the single straight line it
      effectively already was. Geometry changes by at most the
      tolerance; nothing else in the outline moves.

    Why it is a separate script
      The FontForge Python layer cannot express this edit. On
      FontForge 20251009, assigning contour[i].x reads back correctly
      from the same reference but reverts on a fresh read, and
      point.transform() reports success without persisting;
      point.remove_point and layer.addContour do not exist. A fix
      inside ttf2otf_ff_v5.py would report work it cannot perform, so
      this runs after v5 instead, under an interpreter that has
      fontTools:

          python3 flatten_straight_curves.py IN.otf -o OUT.otf

    Components and metrics
      Composite glyphs keep their components — the pass filters the
      pen stream rather than decomposing it. Advance widths, bearings
      and every non-outline table are untouched; only curve segments
      are affected.

    Tolerance
      Two tests, combined: a segment is straightened when EITHER says
      it is straight.

        --max-bow    absolute cap in font units, default 0.2% of the em
                     (4.1u at upm=2048). Catches artifacts on
                     medium-length segments that the ratio test waves
                     through: a 3.6u bow on a 643u segment is 0.0057 of
                     its length but only 0.018% of the em, invisible at
                     any size.
        --rel-tol    bow as a fraction of the segment's own length,
                     default 0.02. Catches artifacts on long segments,
                     where an em-relative cap is too loose, and is what
                     protects genuinely curved joins.

      Both are needed. Measured on NeverMindCompact the artifacts sit
      at 0.005-0.013 of segment length while real joins -- an N's
      shoulder (123u over 1107u), a Thin's near-semicircular terminal
      (43u over 50u) -- sit at 0.09-0.86. The gap is wide enough that
      any setting between them is safe.

Usage:
    python3 flatten_straight_curves.py IN.otf -o OUT.otf [--rel-tol F]
    python3 flatten_straight_curves.py IN.otf --in-place [--dry-run]
"""

import argparse
import math
import sys

try:
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: fontTools is required (pip install fonttools)", file=sys.stderr)
    sys.exit(1)

try:
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    _HAVE_T2 = True
except ImportError:
    _HAVE_T2 = False

try:
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    _HAVE_TTG = True
except ImportError:
    _HAVE_TTG = False


def _bow(p0, c1, c2, p3, steps=16):
    """Max perpendicular deviation of a cubic from the chord p0 -> p3.

    Measuring the curve rather than the raw control offset matters: a
    cubic's control offset is roughly twice its bow, so comparing
    control offset against a bow tolerance silently skips every
    segment it should catch.
    """
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
    """Exact degree elevation of a quadratic to a cubic."""
    c1 = (p0[0] + 2.0 / 3.0 * (q[0] - p0[0]),
          p0[1] + 2.0 / 3.0 * (q[1] - p0[1]))
    c2 = (p2[0] + 2.0 / 3.0 * (q[0] - p2[0]),
          p2[1] + 2.0 / 3.0 * (q[1] - p2[1]))
    return c1, c2


class StraightenPen:
    """Filter pen: emit lineTo for near-straight curves, pass the rest.

    Implements the segment protocol directly rather than subclassing
    BasePen, so it does not depend on BasePen internals for the current
    point. addComponent is forwarded so composites survive intact.
    """

    def __init__(self, outPen, rel_tol=0.02, max_bow=None, stats=None):
        self.outPen = outPen
        self.rel_tol = rel_tol
        self.max_bow = max_bow
        self.stats = stats if stats is not None else {"flattened": 0, "kept": 0}
        self._pt = None

    # -- straightness test --
    def _is_straight(self, p0, c1, c2, p3):
        """Straighten when EITHER test passes; both must be satisfied
        to keep the curve.

        The absolute cap catches artifacts on medium-length segments
        that the ratio test would wave through: on NeverMindCompact a
        3.6u bow on a 643u segment is 0.0057 of its length, but only
        0.018% of the em, so it is invisible at any size and should go.

        The ratio test catches artifacts on long segments, where an
        absolute cap scaled to the em would be too loose. It is also
        what protects genuinely curved joins, which is why the two
        tests are combined rather than either one alone: on this font
        the artifacts sit near 0.005-0.013 of length while the real
        joins (an N's shoulder, a Thin's near-semicircular terminal)
        sit at 0.09-0.86, so any threshold in that wide gap is safe.
        """
        bow, length = _bow(p0, c1, c2, p3)
        if length == 0:
            return False
        if self.max_bow and bow <= self.max_bow:
            return True
        return bow <= self.rel_tol * length

    def _emit_cubic(self, c1, c2, p3):
        p0 = self._pt
        if p0 is not None and self._is_straight(p0, c1, c2, p3):
            self.outPen.lineTo(p3)
            self.stats["flattened"] += 1
        else:
            self.outPen.curveTo(c1, c2, p3)
            self.stats["kept"] += 1
        self._pt = p3

    # -- segment protocol --
    def moveTo(self, pt):
        self._pt = pt
        self.outPen.moveTo(pt)

    def lineTo(self, pt):
        self._pt = pt
        self.outPen.lineTo(pt)

    def curveTo(self, *points):
        for i in range(0, len(points) - 2, 3):
            self._emit_cubic(points[i], points[i + 1], points[i + 2])

    def qCurveTo(self, *points):
        # A trailing None means an all-off-curve closed contour; treat the
        # contour start as the final on-curve point and close it.
        end = points[-1] if points[-1] is not None else self._pt
        pts = list(points[:-1]) if points[-1] is not None else list(points)
        if end is None:
            return
        cur = self._pt
        for idx, q in enumerate(pts):
            if idx == len(pts) - 1:
                nxt = end
            else:
                nxt = (pts[idx + 1][0], pts[idx + 1][1])
            if cur is None:
                self.moveTo(q)
                cur = q
                continue
            c1, c2 = _quad_to_cubic(cur, q, nxt)
            self._emit_cubic(c1, c2, nxt)
            cur = nxt

    def closePath(self):
        self.outPen.closePath()
        self._pt = None

    def endPath(self):
        self.outPen.endPath()
        self._pt = None

    def addComponent(self, name, transformation):
        self.outPen.addComponent(name, transformation)
        self._pt = None

    def beginPath(self, *args, **kwargs):
        if hasattr(self.outPen, "beginPath"):
            self.outPen.beginPath(*args, **kwargs)

    def skipCurrentPath(self):
        pass


def flatten_font(font, rel_tol=0.02, max_bow=None, dry_run=False, verbose=True):
    """Straighten near-straight curves in every glyph. Returns stats."""
    glyph_order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    upem = font["head"].unitsPerEm if "head" in font else 1000
    if max_bow is None:
        max_bow = upem * 0.002
    total = {"flattened": 0, "kept": 0}

    is_cff = ("CFF " in font) or ("CFF2" in font)

    if is_cff:
        if not _HAVE_T2:
            raise RuntimeError("fontTools T2CharStringPen unavailable")
        top = font["CFF "].cff.topDictIndex[0] if "CFF " in font \
            else font["CFF2"].cff.topDictIndex[0]
        char_strings = top.CharStrings
        private = getattr(top, "Private", None)
        global_subrs = getattr(top, "GlobalSubrs", None)

    for name in glyph_order:
        rec = RecordingPen()
        try:
            glyph_set[name].draw(rec)
        except Exception:
            continue

        if not rec.value:
            continue

        stats = {"flattened": 0, "kept": 0}
        try:
            if is_cff:
                width = font["hmtx"][name][0] if "hmtx" in font else None
                t2 = T2CharStringPen(width, glyph_set, roundTolerance=0)
                sp = StraightenPen(t2, rel_tol, max_bow, stats)
                for op, args in rec.value:
                    getattr(sp, op)(*args)
                if not dry_run:
                    cs = char_strings[name]
                    cs.program = t2.getCharString(
                        private=private, globalSubrs=global_subrs).program
            else:
                if not _HAVE_TTG:
                    raise RuntimeError("fontTools TTGlyphPen unavailable")
                tt = TTGlyphPen(None)
                sp = StraightenPen(tt, rel_tol, max_bow, stats)
                for op, args in rec.value:
                    getattr(sp, op)(*args)
                if not dry_run:
                    font["glyf"][name] = tt.glyph()
        except Exception as e:
            if verbose:
                print("  %s: skipped (%s: %s)"
                      % (name, type(e).__name__, e), file=sys.stderr)
            continue

        total["flattened"] += stats["flattened"]
        total["kept"] += stats["kept"]

    total["upem"] = upem
    total["glyphs"] = len(glyph_order)
    return total


def main():
    ap = argparse.ArgumentParser(
        description="Collapse near-straight curves back onto their chords.")
    ap.add_argument("input", help="source .otf or .ttf")
    ap.add_argument("-o", "--out", help="output path (default: in-place with --in-place)")
    ap.add_argument("--in-place", action="store_true",
                    help="overwrite the input file")
    ap.add_argument("--rel-tol", type=float, default=0.02,
                    help="straighten when bow <= this fraction of segment "
                         "length (default 0.02 = 2%%)")
    ap.add_argument("--max-bow", type=float, default=0.0,
                    help="absolute bow cap in font units; 0 uses 0.2%% of "
                         "the em, -1 disables the cap")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if not args.out and not args.in_place:
        ap.error("give -o OUT, or --in-place")

    if args.rel_tol < 0:
        ap.error("--rel-tol must be >= 0")

    font = TTFont(args.input)
    stats = flatten_font(font, rel_tol=args.rel_tol,
                         max_bow=(None if args.max_bow < 0 else args.max_bow),
                         dry_run=args.dry_run, verbose=not args.quiet)

    if not args.quiet:
        verb = "would straighten" if args.dry_run else "straightened"
        print("flatten_straight_curves %s" % args.input)
        print("  upem=%d  glyphs=%d" % (stats["upem"], stats["glyphs"]))
        print("  %s %d near-straight curves" % (verb, stats["flattened"]))
        print("  kept %d genuinely curved segments" % stats["kept"])

    if not args.dry_run:
        dest = args.input if args.in_place else args.out
        font.save(dest)
        if not args.quiet:
            print("  wrote %s" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())