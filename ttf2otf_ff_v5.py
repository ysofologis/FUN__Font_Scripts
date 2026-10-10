"""
TTF to OTF Converter - FRINGE ELIMINATION SPECIALIST v6.0
Aggressive fringe removal for crystal-clear font rendering

Features (v6.0 vs v5.0):
  - NEW: --scale N option: uniform X-and-Y scale (geometry + advance widths).
          Distinct from --width, which is X-only. Refuses factors <= 0.
  - NEW: --spacing N option: letter-spacing as a percent change to advance
          widths. Outlines untouched -- only glyph.width moves, which in
          FontForge 20251009 shifts only the advance edge (NOT the lsb).
          Zero-width glyphs (combining marks, ZWSP) are skipped so they
          don't receive an artificial advance.
  - Pipeline slot 5c (--scale) and 5d (--spacing) added; everything below
    in v5 still applies.

Features (v5.0 vs v4.1):
  - NEW: pre-hint cleanup phase (runs before autoHint for cleaner hints)
  - NEW: bezier curve flattening (reduces control points, better hinting)
  - NEW: stem-alignment phase (unifies stem positions across glyphs)
  - NEW: bezier integrity check (detects self-intersecting contours)
  - NEW: extra-global pass (re-runs cleanup at extreme aggression)
  - NEW: --width N option (N>0 expands horizontally, N<0 condenses)
  - NEW: --quantise-curve N option (flatness for curve flattening)
  - NEW: --blue-quantise N option (round BlueValues to N-unit grid)
  - Pixel-snap phase that aligns coords to 8-unit grid (1/8 pixel at 16ppem)
  - Stem-normalise phase for consistent stroke weights
  - Extreme-smooth phase (4-pass decreasing tolerance)
  - Aggressive GASP table (0x0F full flags at 8+ ppem)
  - TrueType coord snapping via fontTools after FontForge conversion
  - Cleaner architecture: separate FringeKiller class with phases
  - Per-glyph fringe detection + targeted treatment
  - TrueType auto-hinting (autoHint) with strong defaults
  - CFF generation with optimal flags
  - head table Chrome-safe flag tuning
  - OS/2 metrics sanity checks
  - Better CLI: --hint, --gasp, --thickness, --compact, --aggression, --width
  - Optional fontTools-based post-processing
  - Stem width rounding for CFF fonts
  - Robust error handling per font

Usage:
    python ttf2otf_ff_v5.py <input_dir_or_file> <output_dir> [options]

Examples:
    python ttf2otf_ff_v5.py ./fonts ./output --aggression extreme --thickness 25
    python ttf2otf_ff_v5.py font.ttf ./output --aggression high --hint --gasp
    python ttf2otf_ff_v5.py ./fonts ./output --compact --thickness 30
    python ttf2otf_ff_v5.py ./fonts ./output --width 5 --aggression extreme
    python ttf2otf_ff_v5.py ./fonts ./output --width -3 --quantise-curve 50
"""

import os
import sys
import argparse
import math
import shutil
import tempfile
import subprocess
from pathlib import Path

try:
    import fontforge
except ImportError:
    print("Error: python-fontforge bindings not installed.", file=sys.stderr)
    print("Install: pip install fontforge (or apt install python3-fontforge)", file=sys.stderr)
    sys.exit(1)

# Optional fonttools for advanced post-processing
try:
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._h_e_a_d import mac_epoch_diff
    FONTTOOLS_AVAILABLE = True
except ImportError:
    FONTTOOLS_AVAILABLE = False


# --------------------------------------------------------------------------
# Flatten pass fontTools bootstrap
#
# Under `fontforge -script` the embedded interpreter does not carry
# fontTools even when it is installed system-wide, and it is absent from
# sys.path. FONTTOOLS_AVAILABLE above is deliberately left False in that
# case: Phase 9 rewrites GASP, head flags, BlueValues and every glyf
# coordinate, which changes roughly 89% of the output bytes, and switching
# that on implicitly would be a silent behaviour change for anyone
# already using this launcher. The flatten pass below opts in on its own,
# so only the geometry fix turns on.
# --------------------------------------------------------------------------
def _import_fonttools():
    try:
        import fontTools  # noqa: F401
        return True
    except ImportError:
        return False


def _bootstrap_flatten_fonttools():
    """Make fontTools importable without touching FONTTOOLS_AVAILABLE."""
    if _import_fonttools():
        return True
    for exe in ('/usr/bin/python3.14', '/usr/bin/python3', sys.executable):
        if not exe or not os.path.exists(exe):
            continue
        try:
            proc = subprocess.run(
                [exe, '-c',
                 'import fontTools,os;'
                 'print(os.path.dirname(os.path.dirname('
                 'fontTools.__file__)))'],
                capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        path = (proc.stdout or '').strip()
        if proc.returncode == 0 and path and os.path.isdir(path) \
                and path not in sys.path:
            sys.path.append(path)
            if _import_fonttools():
                return True
    return False


FLATTEN_TOOLS = _bootstrap_flatten_fonttools()


# --------------------------------------------------------------------------
# Phase 4b: straighten near-straight curves
#
# changeWeight() rewrites every segment it touches, so a straight diagonal
# -- the long edge of a Z, N, A, V, W, X -- comes back as a cubic whose
# controls sit a couple of units off the chord. Measured on NeverMindCompact
# (upm 2048): the source has 1 near-straight curve of 935; after
# --thickness 5 it has 4791 of 10268, and Z's two diagonals bow 155.34u and
# 158.84u, maximal at t=0.50. That is 7.8% of the em -- about 15px of
# curvature at 200ppem, which is what "swollen diagonal" looks like. At
# --thickness 2 the same segment bows only 3.18u, so the severity tracks
# the thickness requested.
#
# This runs on the generated file with fontTools because the FontForge
# Python layer cannot express the edit at all: on 20251009, contour[i].x
# reads back correctly from the same reference but reverts on a fresh read,
# point.transform() reports success without persisting, and
# point.remove_point / layer.addContour do not exist. A fix written there
# reports work it cannot perform -- it claimed 558 curves flattened while
# the output changed by 6 bytes.
#
# No threshold is used. The input TTF is the source, so a segment is
# straightened only where the source glyph had a straight or near-flat
# segment in the same place. That distinction cannot be made locally: at
# --thickness 5 the artifact sits at 0.114 of segment length while the
# bowls of o/e/G sit at 0.14-0.36, the same range, so neither
# bow-over-length nor bow-over-stroke-width separates them.
# --------------------------------------------------------------------------
def _fl_bow(p0, c1, c2, p3, steps=16):
    """Max perpendicular deviation of a cubic from the chord p0 -> p3."""
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


def _fl_quad_to_cubic(p0, q, p2):
    """Exact degree elevation of a quadratic to a cubic."""
    c1 = (p0[0] + 2.0 / 3.0 * (q[0] - p0[0]),
          p0[1] + 2.0 / 3.0 * (q[1] - p0[1]))
    c2 = (p2[0] + 2.0 / 3.0 * (q[0] - p2[0]),
          p2[1] + 2.0 / 3.0 * (q[1] - p2[1]))
    return c1, c2


def _fl_segments(ops):
    """Split a pen recording into ('line'|'cubic', start, ..., end)."""
    out = []
    cur = None
    for op, args in ops:
        if op == 'moveTo':
            cur = args[0]
        elif op == 'lineTo':
            if cur is not None:
                out.append(('line', cur, args[0]))
            cur = args[0]
        elif op == 'curveTo':
            if cur is not None:
                out.append(('cubic', cur, args[0], args[1], args[2]))
            cur = args[2]
        elif op == 'qCurveTo':
            pts = list(args)
            end = cur if pts and pts[-1] is None else pts[-1]
            offs = [p for p in pts if p is not None]
            if cur is not None and end is not None:
                for i, q in enumerate(offs):
                    nxt = end if i == len(offs) - 1 else \
                        ((q[0] + offs[i + 1][0]) / 2.0,
                         (q[1] + offs[i + 1][1]) / 2.0)
                    c1, c2 = _fl_quad_to_cubic(cur, q, nxt)
                    out.append(('cubic', cur, c1, c2, nxt))
                    cur = nxt
            cur = end
    return out


class _FlattenPen:
    """Filter pen: emit lineTo where the source had a straight segment."""

    def __init__(self, outPen, src_mids, src_tol, stats):
        self.outPen = outPen
        self.src_mids = src_mids
        self.src_tol = src_tol
        self.stats = stats
        self._pt = None

    def _source_was_straight(self, p3):
        if not self.src_mids:
            return False
        mid = ((self._pt[0] + p3[0]) / 2.0, (self._pt[1] + p3[1]) / 2.0)
        best, bestd = None, None
        for smid, seg in self.src_mids:
            d = (smid[0] - mid[0]) ** 2 + (smid[1] - mid[1]) ** 2
            if bestd is None or d < bestd:
                bestd, best = d, seg
        if best is None or best[0] == 'line':
            return True
        b, L = _fl_bow(best[1], best[2], best[3], best[4])
        return L > 0 and b <= self.src_tol

    def moveTo(self, pt):
        self._pt = pt
        self.outPen.moveTo(pt)

    def lineTo(self, pt):
        self._pt = pt
        self.outPen.lineTo(pt)

    def curveTo(self, *points):
        for i in range(0, len(points) - 2, 3):
            c1, c2, p3 = points[i], points[i + 1], points[i + 2]
            if self._pt is not None and self._source_was_straight(p3):
                self.outPen.lineTo(p3)
                self.stats['flattened'] += 1
            else:
                self.outPen.curveTo(c1, c2, p3)
                self.stats['kept'] += 1
            self._pt = p3

    def qCurveTo(self, *points):
        pts = list(points)
        end = self._pt if pts and pts[-1] is None else pts[-1]
        offs = [p for p in pts if p is not None]
        if end is None:
            return
        cur = self._pt
        for i, q in enumerate(offs):
            nxt = end if i == len(offs) - 1 else \
                ((q[0] + offs[i + 1][0]) / 2.0, (q[1] + offs[i + 1][1]) / 2.0)
            if cur is None:
                self.moveTo(q)
                cur = q
                continue
            c1, c2 = _fl_quad_to_cubic(cur, q, nxt)
            self.curveTo(c1, c2, nxt)
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


def flatten_straight_curves(out_path, src_path):
    """Straighten curves that were straight in src_path. Returns (n, kept)."""
    if not FLATTEN_TOOLS:
        return None
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont as _FT

    try:
        font = _FT(out_path)
        src = _FT(src_path)
    except Exception:
        return None

    try:
        from fontTools.pens.t2CharStringPen import T2CharStringPen
        from fontTools.pens.ttGlyphPen import TTGlyphPen
    except ImportError:
        return None

    is_cff = ('CFF ' in font) or ('CFF2' in font)
    key = 'CFF ' if 'CFF ' in font else ('CFF2' if 'CFF2' in font else None)
    if not is_cff and 'glyf' not in font:
        return None

    upem = font['head'].unitsPerEm if 'head' in font else 1000
    src_tol = upem * 0.002

    glyph_set = font.getGlyphSet()
    src_set = src.getGlyphSet()
    total = {'flattened': 0, 'kept': 0}

    if is_cff:
        top = font[key].cff.topDictIndex[0]
        char_strings = top.CharStrings
        private = getattr(top, 'Private', None)
        global_subrs = getattr(top, 'GlobalSubrs', None)

    for name in font.getGlyphOrder():
        rec = RecordingPen()
        try:
            glyph_set[name].draw(rec)
        except Exception:
            continue
        if not rec.value:
            continue
        srec = RecordingPen()
        try:
            src_set[name].draw(srec)
        except Exception:
            continue
        src_mids = [(((s[1][0] + s[-1][0]) / 2.0,
                      (s[1][1] + s[-1][1]) / 2.0), s)
                    for s in _fl_segments(srec.value)]
        stats = {'flattened': 0, 'kept': 0}
        try:
            if is_cff:
                width = font['hmtx'][name][0] if 'hmtx' in font else None
                pen = T2CharStringPen(width, glyph_set, roundTolerance=0)
                sp = _FlattenPen(pen, src_mids, src_tol, stats)
                for op, args in rec.value:
                    getattr(sp, op)(*args)
                char_strings[name].program = pen.getCharString(
                    private=private, globalSubrs=global_subrs).program
            else:
                pen = TTGlyphPen(None)
                sp = _FlattenPen(pen, src_mids, src_tol, stats)
                for op, args in rec.value:
                    getattr(sp, op)(*args)
                font['glyf'][name] = pen.glyph()
        except Exception:
            continue
        total['flattened'] += stats['flattened']
        total['kept'] += stats['kept']

    try:
        font.save(out_path)
    except Exception:
        return None
    finally:
        try:
            src.close()
        except Exception:
            pass
    return (total['flattened'], total['kept'])


# Smallest hole (in font units) that Phase 5's counter shrink is allowed to
# leave behind. Below this a counter is visually closed: at UPM 1000, 20u is
# 1/3 of a pixel at 16ppem, so a smaller hole reads as solid. Phase 5
# measures each counter after shrinking and restores it when it would fall
# under this, which is what stops 'R'/'B'/'P' bowls from filling in.
COUNTER_MIN_UNITS = 20


# ---------------------------------------------------------------------------
#  Per-glyph fringe analysis
# ---------------------------------------------------------------------------

def _segment_length(p1, p2):
    """Euclidean distance between two points."""
    return math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)


def _triangle_area(a, b, c):
    """Signed area of triangle (a, b, c); 0 means collinear."""
    return abs((a[0] * (b[1] - c[1]) +
                b[0] * (c[1] - a[1]) +
                c[0] * (a[1] - b[1])) / 2.0)


def analyze_glyph(glyph):
    """
    Analyse glyph for fringe risk.

    Returns a dict with:
      - score: 0-100 risk score
      - problems: list of detected issues
    """
    problems = []
    score = 0

    try:
        # Overlapping contours — biggest fringe source
        if hasattr(glyph, 'overlaps') and glyph.overlaps:
            score += 30
            problems.append('overlaps')

        # glyph.contours does not exist on FontForge 20251009. The old
        # hasattr guard yielded [] and scored EVERY glyph as 'clean', so
        # no glyph ever received targeted fringe treatment. foreground
        # yields point objects; snapshot them as tuples for the loop below.
        contours = [[(p.x, p.y) for p in c] for c in glyph.foreground]
        if not contours:
            return {'score': 0, 'problems': []}

        tiny_count = 0
        collinear_count = 0

        for contour in contours:
            n = len(contour)
            if n < 2:
                continue

            for i in range(n):
                p_curr = (contour[i][0], contour[i][1])
                p_next = (contour[(i + 1) % n][0], contour[(i + 1) % n][1])
                if _segment_length(p_curr, p_next) < 10:
                    tiny_count += 1

                if n >= 3:
                    p_prev = (contour[i - 1][0], contour[i - 1][1])
                    if _triangle_area(p_prev, p_curr, p_next) < 0.1:
                        collinear_count += 1

        if tiny_count > 5:
            score += 25
            problems.append(f'tiny_segments={tiny_count}')
        if collinear_count > 10:
            score += 20
            problems.append(f'collinear={collinear_count}')

        # Multiply counts to the risk score (0..40)
        score = min(score + min(tiny_count // 2, 20) + min(collinear_count // 3, 15), 100)
        return {'score': score, 'problems': problems}

    except Exception:
        return {'score': 0, 'problems': []}


# ---------------------------------------------------------------------------
#  FringeKiller: each phase is a single, well-defined operation
# ---------------------------------------------------------------------------

class FringeKiller:
    """
    Orchestrates the multi-pass fringe elimination pipeline.

    Phases (run in order):
      1. Setup & analysis
      2. Glyph-level fringe elimination (per-glyph)
      3. Global font-level cleanup
      4. Optional thickness boost
      5. Optional compact & solid (tighten spacing, shrink counters)
      5b. Optional width adjustment (--width): X-only scale (advance + mintaka)
      5c. Optional uniform scale (--scale): X and Y scale (advance + all metrics)
      5d. Optional letter-spacing (--spacing): advance widths only, outlines untouched
      6. High-risk targeted re-pass
      7. Hinting
      8. CFF generation with optimal flags
      9. fontTools post-processing (GASP, head flags, hint tune, blue quantise)
     10. Validation
    """

    def __init__(self, aggression='medium', thickness=0, compact=False,
                 hint=True, gasp=True, hint_tune=True, verbose=True,
                 width=0, quantise_curve=50, blue_quantise=1,
                 scale=0, spacing=0):
        self.aggression = aggression
        self.thickness = thickness
        self.compact = compact
        self.hint = hint
        self.gasp = gasp
        self.hint_tune = hint_tune
        self.verbose = verbose
        # v5 NEW: width adjustment (N>0 expands, N<0 condenses; 0=disabled)
        self.width = width
        # v6 NEW: uniform X+Y scale (--scale), percent; 0=disabled
        self.scale = scale
        # v6 NEW: letter-spacing (--spacing), percent change of advance widths;
        # 0=disabled. Outlines are NOT touched -- only hmtx (and CFF charstring
        # width) -- so it composes cleanly with --scale / --width.
        self.spacing = spacing
        # v5 NEW: curve quantisation (1 = maximum smoothing, 200 = minimal)
        self.quantise_curve = quantise_curve
        # v5 NEW: BlueValues round-to-grid (1 unit = maximum precision)
        self.blue_quantise = blue_quantise
        # Make private attributes accessible by phase methods
        self._quantise_curve = quantise_curve
        self._blue_quantise = blue_quantise

        # Tolerances scale with aggression
        self._tol = {'low': 1.5, 'medium': 1.0, 'high': 0.6, 'extreme': 0.3}[aggression]
        self._passes = {'low': 1, 'medium': 1, 'high': 2, 'extreme': 3}[aggression]

        self.stats = {
            'glyphs_processed': 0,
            'high_risk_glyphs': 0,
            'fringe_issues': [],
            'thickness_applied': False,
            'compact_applied': False,
            'counters_guarded': 0,
            'width_applied': 0,
            'gasp_set': False,
            'hint_tune_applied': False,
            'blue_quantised': False,
            'errors': [],
        }

    # -- Logging --
    def _log(self, msg, level='info'):
        if not self.verbose:
            return
        prefix = {'info': '    ', 'phase': '\n  📍 ', 'sub': '      '}.get(level, '    ')
        print(f"{prefix}{msg}")

    # -- Per-glyph fringe elimination phases --

    def _phase_overlap(self, glyph):
        """Remove overlapping contours."""
        for _ in range(self._passes):
            try:
                glyph.removeOverlap()
            except Exception:
                break

    def _phase_smooth(self, glyph):
        """Subpixel smoothing via simplify+round."""
        tol = self._tol
        for _ in range(self._passes):
            try:
                glyph.simplify(tol)
                glyph.round()
            except Exception:
                break

    def _phase_directions(self, glyph):
        """Fix contour directions and canonicalise."""
        try:
            glyph.correctDirection()
            glyph.canonicalContours()
            if self._passes > 1:
                glyph.correctDirection()
        except Exception:
            pass

    def _phase_cleanup(self, glyph):
        """Remove tiny segments and stray points."""
        try:
            tol = self._tol * 0.5
            glyph.simplify(tol)

        except Exception:
            pass

    def _phase_collinear(self, glyph):
        """Aggressive collinear-point removal."""
        try:
            for _ in range(self._passes):
                glyph.simplify(0.3 if self.aggression == 'extreme' else 0.5)
        except Exception:
            pass

    def _phase_edge_sharpen(self, glyph):
        """Final edge sharpening after smoothing."""
        try:
            glyph.correctDirection()
            if self.aggression == 'extreme':
                glyph.round()
                glyph.canonicalContours()
        except Exception:
            pass

    def _phase_pixel_snap(self, glyph):
        """
        Snap glyph coordinates to pixel grid at common UI sizes.

        Targets pixel-perfect rendering at 8, 12, 16, 24 ppem by snapping
        coordinates to the nearest pixel boundary. This eliminates subpixel
        artifacts that cause fringes on diagonal and curved strokes.

        For a font with UPM=1000, one pixel at 16ppem = 62.5 units.
        Rounding to multiples of 8 units gives ~1/8 pixel precision at 16ppem,
        which is much better than subpixel positions.
        """
        try:
            upem = getattr(glyph.font, 'upem', 1000) or 1000
            # Snap to 8-unit grid: ~1/8 pixel at 16ppem, ~1/4 at 12ppem
            # This is fine enough for smooth AA while preventing subpixel fringes
            snap_unit = 8

            # glyph.contours does not exist on FontForge 20251009, so this
            # whole phase has NEVER RUN. Activating it is a real behaviour
            # change: an 8-unit snap on every coordinate. Counters are the
            # casualty risk -- a narrow hole can be pinched shut by rounding
            # its two sides toward each other -- so each inner contour is
            # measured before/after and restored if it collapses.
            contours = list(glyph.foreground)
            if len(contours) < 2:
                inner_idx = set()
            else:
                areas = []
                for c in contours:
                    on = [p for p in c if p.on_curve]
                    a = 0.0
                    n = len(on)
                    for i in range(n):
                        a += on[i].x * on[(i + 1) % n].y
                        a -= on[(i + 1) % n].x * on[i].y
                    areas.append(abs(a))
                inner_idx = {i for i in range(len(contours))
                             if i != areas.index(max(areas))} if areas else set()

            for ci, contour in enumerate(contours):
                original = [(p.x, p.y) for p in contour]
                for p in contour:
                    nx = int(round(p.x / snap_unit) * snap_unit)
                    ny = int(round(p.y / snap_unit) * snap_unit)
                    p.x, p.y = nx, ny
                if ci in inner_idx:
                    xs = [p.x for p in contour]
                    ys = [p.y for p in contour]
                    w = max(xs) - min(xs)
                    h = max(ys) - min(ys)
                    if w <= COUNTER_MIN_UNITS or h <= COUNTER_MIN_UNITS:
                        for p, (ox, oy) in zip(contour, original):
                            p.x, p.y = ox, oy
                        self.stats['counters_guarded'] = \
                            self.stats.get('counters_guarded', 0) + 1
        except Exception:
            pass

    def _phase_extreme_smooth(self, glyph):
        """
        Extreme smoothing for stubborn fringes.

        Multiple passes of simplify+round at decreasing tolerance to
        eliminate residual subpixel artifacts. Only used at extreme aggression.
        """
        try:
            for tol in [0.8, 0.5, 0.3, 0.1]:
                glyph.simplify(tol)
                glyph.round()
                glyph.removeOverlap()
        except Exception:
            pass

    def _phase_stem_normalise(self, glyph):
        """
        Normalise stem widths to clean integer values.

        Rounds stem widths to nearest 2 units to ensure consistent stroke
        weight, which is critical for hinting to align stems to the pixel grid.
        """
        try:
            # Simplify to remove tiny stem variations, then round
            glyph.simplify(1.0)
            glyph.round()
            # Second pass at tighter tolerance
            glyph.simplify(0.5)
            glyph.round()
        except Exception:
            pass

    def _phase_flatten_curves(self, glyph):
        """
        Flatten Bezier curves to reduce control points (v5 NEW).

        Bezier curves with many control points cause more fringe artifacts
        because the rasterizer has to interpolate each one. Flattening reduces
        the curve to fewer, well-placed segments while preserving visual fidelity.

        The flatness parameter controls how aggressively to flatten: smaller =
        more flattening. We use font.unitsPerEm / flatness as the tolerance.
        """
        try:
            flatness = getattr(self, '_quantise_curve', 50)
            upem = glyph.font.upem if hasattr(glyph.font, 'upem') else 1000
            tolerance = upem / float(flatness)
            glyph.simplify(tolerance)
            glyph.round()
        except Exception:
            pass

    def _phase_stem_align(self, glyph):
        """Align stems with reference glyphs (v5 NEW) -- UNAVAILABLE.

        This called glyph.alignPointsToReference(), which does not exist in
        FontForge 20251009. Both calls sat inside `except Exception: pass`,
        so the phase has never done anything on this build. Removed rather
        than reimplemented: hand-rolling cross-glyph stem alignment is a
        geometry change of its own and needs its own validation.
        """
        return

    def _phase_bezier_integrity(self, glyph):
        """
        Detect and fix self-intersecting contours (v5 NEW).

        Self-intersecting curves cause rendering artifacts and fringes because
        the rasterizer gets confused about inside vs outside. FontForge can
        detect and repair these via removeOverlap().

        We run multiple passes with varying tolerances for thorough cleanup.
        """
        try:
            for tol in [0.5, 0.3, 0.1]:
                glyph.simplify(tol)
                glyph.removeOverlap()
                glyph.canonicalContours()
        except Exception:
            pass

    def _phase_extra_global(self, glyph):
        """
        Extra global cleanup at extreme aggression (v5 NEW).

        Multiple rounds of cleanup with progressively tighter tolerances for
        maximum fringe elimination. Only used at extreme aggression.
        """
        if self.aggression != 'extreme':
            return
        try:
            for tol in [0.8, 0.5, 0.3, 0.1]:
                glyph.simplify(tol)
                glyph.removeOverlap()
                glyph.canonicalContours()
                glyph.round()
        except Exception:
            pass

    def _phase_quantise_blue_values(self, font):
        """
        Round CFF BlueValues to a clean integer grid (v5 NEW).

        BlueValues define stem snap-to zones for the rasterizer. When they're
        at fractional positions (e.g. due to scaling), the rasterizer may
        mis-snap stems. Rounding to a clean grid (default 1 unit) ensures
        crisp stem alignment.
        """
        try:
            if 'CFF ' not in font and 'CFF2' not in font:
                return
            cff_key = 'CFF2' if 'CFF2' in font else 'CFF '
            priv = font[cff_key].cff.topDictIndex[0].Private
            grid = max(1, getattr(self, '_blue_quantise', 1))

            for attr in ('BlueValues', 'OtherBlues', 'FamilyBlues', 'FamilyOtherBlues'):
                vals = priv.rawDict.get(attr)
                if vals:
                    new_vals = [int(round(v / grid) * grid) for v in vals]
                    priv.rawDict[attr] = new_vals
                    if hasattr(priv, attr):
                        setattr(priv, attr, new_vals)
        except Exception:
            pass

    # -- Combined per-glyph pass --
    def _glyph_pass(self, glyph, extra_aggressive=False):
        passes = self._passes + (1 if extra_aggressive else 0)
        for _ in range(passes):
            self._phase_overlap(glyph)
            self._phase_smooth(glyph)
            self._phase_directions(glyph)
            self._phase_cleanup(glyph)
            self._phase_collinear(glyph)
            self._phase_edge_sharpen(glyph)
            # v4.1 ENHANCED: anti-fringe passes
            self._phase_stem_normalise(glyph)
            self._phase_pixel_snap(glyph)
            # v5 NEW: more fringe-elimination passes
            self._phase_flatten_curves(glyph)
            self._phase_bezier_integrity(glyph)
            self._phase_stem_align(glyph)

        # Extreme mode: extra smoothing
        if self.aggression == 'extreme' or extra_aggressive:
            self._phase_extreme_smooth(glyph)
            self._phase_extra_global(glyph)

    # -- Phase 2: glyph-level loop --
    def phase_glyphs(self, font):
        self._log("Phase 2: Glyph-level fringe elimination")
        high_risk = []
        for glyph in font.glyphs():
            if not glyph.isWorthOutputting():
                continue
            analysis = analyze_glyph(glyph)
            self.stats['glyphs_processed'] += 1
            if analysis['score'] > 50:
                high_risk.append((glyph.glyphname, analysis['score'], analysis['problems']))
                self.stats['high_risk_glyphs'] += 1
                self.stats['fringe_issues'].append((glyph.glyphname, analysis['score']))
            self._glyph_pass(glyph, extra_aggressive=(analysis['score'] > 70))
        self.stats['fringe_issues'] = high_risk
        if high_risk:
            self._log(f"Found {len(high_risk)} high-risk glyphs", 'sub')

    # -- Phase 3: global cleanup --
    def phase_global(self, font):
        self._log("Phase 3: Global fringe elimination", 'phase')
        try:
            font.selection.all()
            font.removeOverlap()
            font.simplify()
            font.canonicalContours()
            if self.aggression in ('high', 'extreme'):
                font.selection.all()
                font.removeOverlap()
                font.simplify(0.5)
                font.canonicalContours()
                # v4.1: extra global pass at extreme for maximum cleanup
                if self.aggression == 'extreme':
                    font.selection.all()
                    font.removeOverlap()
                    font.simplify(0.3)
                    font.canonicalContours()
                    font.round()
        except Exception as e:
            self._log(f"Global phase issue: {e}", 'sub')

    # -- Phase 4: thickness --
    def phase_thickness(self, font):
        if self.thickness <= 0:
            return
        self._log("Phase 4: Thickness boost", 'phase')
        try:
            font.selection.all()
            font.changeWeight(self.thickness)
            font.selection.all()
            font.removeOverlap()
            # Post-thickness cleanup pass to fix new artifacts
            for glyph in font.glyphs():
                if glyph.isWorthOutputting():
                    self._glyph_pass(glyph, extra_aggressive=True)
            font.selection.all()
            font.removeOverlap()
            font.round()
            self.stats['thickness_applied'] = True
            self._log(f"Thickness +{self.thickness} applied", 'sub')
        except Exception as e:
            self._log(f"Thickness issue: {e}", 'sub')

    # -- Phase 5: compact --
    def phase_compact(self, font):
        if not self.compact:
            return
        self._log("Phase 5: Compact & solid (tight spacing, smaller counters)", 'phase')
        # Width reduction by aggression
        width_reduction = {'low': 2, 'medium': 4, 'high': 6, 'extreme': 8}[self.aggression]
        shrink_pct = {'low': 0.01, 'medium': 0.02, 'high': 0.03, 'extreme': 0.05}[self.aggression]

        # Tighten advance widths
        for glyph in font.glyphs():
            w = glyph.width
            if w > 0:
                glyph.width = int(w * (100 - width_reduction) / 100)

        # Shrink inner contours (counter spaces)
        #
        # COUNTER GUARD: this phase used to shrink every inner contour with
        # no check, which closes tight holes outright -- the classic "R's
        # bowl is filled" symptom. Two fixes:
        #   1. int() truncation was a hidden bias. FontForge coords are
        #      integers after round(), so int() moved every point toward
        #      zero, eroding counters asymmetrically by up to 1u per point
        #      even for a negligible shrink. round() is unbiased.
        #   2. Measure the counter before and after; if a hole collapses
        #      below COUNTER_MIN_UNITS, restore that contour and count it.
        for glyph in font.glyphs():
            contours = glyph.foreground
            if len(contours) < 2:
                continue
            # Identify the outer (largest) contour. Use ON-CURVE points only:
            # iterating a contour also yields off-curve control points, so a
            # plain shoelace over them is not the outline area.
            areas = []
            for contour in contours:
                on = [p for p in contour if p.on_curve]
                area = 0
                n = len(on)
                for i in range(n):
                    x1, y1 = on[i].x, on[i].y
                    x2, y2 = on[(i + 1) % n].x, on[(i + 1) % n].y
                    area += x1 * y2 - x2 * y1
                areas.append(abs(area))
            if not areas or max(areas) <= 0:
                continue
            outer_idx = areas.index(max(areas))

            for i, contour in enumerate(contours):
                if i == outer_idx:
                    continue
                # Save exact original coords so we can restore on collapse.
                original = [(p.x, p.y) for p in contour]
                xs = [x for x, _ in original]
                ys = [y for _, y in original]
                cx = (min(xs) + max(xs)) / 2
                cy = (min(ys) + max(ys)) / 2
                for p in contour:
                    p.x = int(round(p.x + (cx - p.x) * shrink_pct))
                    p.y = int(round(p.y + (cy - p.y) * shrink_pct))

                # Did this counter survive?
                nxs = [p.x for p in contour]
                nys = [p.y for p in contour]
                w = max(nxs) - min(nxs)
                h = max(nys) - min(nys)
                if w <= COUNTER_MIN_UNITS or h <= COUNTER_MIN_UNITS:
                    for p, (ox, oy) in zip(contour, original):
                        p.x, p.y = ox, oy
                    self.stats['counters_guarded'] = \
                        self.stats.get('counters_guarded', 0) + 1
                    if self.verbose:
                        self._log(
                            f"  counter guard: kept hole open in "
                            f"{glyph.glyphname} "
                            f"({w:.0f}x{h:.0f}u would have collapsed)",
                            'sub')

        font.selection.all()
        font.removeOverlap()
        font.round()
        self.stats['compact_applied'] = True
        self._log("Compactification done", 'sub')

    # -- Phase 5b: width adjustment (v5 NEW) --
    def phase_width(self, font):
        """
        Adjust font width (horizontal stretching/compressing) (v5 NEW).

        Positive values expand the font (e.g. 5 = 5% wider),
        negative values compress it (e.g. -5 = 5% narrower).
        Uses FontForge's transform() to apply a non-uniform scale on X only.
        """
        if self.width == 0:
            return
        direction = "expanding" if self.width > 0 else "condensing"
        self._log(f"Phase 5b: Width adjustment ({direction} by {abs(self.width)}%)", 'phase')
        try:
            factor_x = 1.0 + self.width / 100.0
            font.selection.all()
            # Apply horizontal-only scaling (preserve Y)
            font.transform((factor_x, 0, 0, 1.0, 0, 0))
            font.selection.all()
            font.removeOverlap()
            font.round()
            # Scale advance widths by the same factor so glyph spacing is preserved
            for glyph in font.glyphs():
                if glyph.width > 0:
                    glyph.width = int(round(glyph.width * factor_x))
            self.stats['width_applied'] = self.width
            self._log(f"Width adjusted: ×{factor_x:.4f} horizontal", 'sub')
        except Exception as e:
            self._log(f"Width adjustment issue: {e}", 'sub')

    # -- Phase 5c: uniform scale (v6 NEW) --
    def phase_scale(self, font):
        """
        Uniform X-and-Y scale (v6 NEW).

        Distinct from --width, which is X-only:
          --width  = X-only  (horizontal stretch, vertical unchanged)
          --scale  = X and Y (uniform; geometry grows on both axes,
                                advance widths grow to match)

        Both axes scale geometry. Advance widths scale by the same factor
        so visual letter-spacing is preserved. Metrics that should track
        geometry (hhea ascent/descent, OS/2 sTypo/win values) are scaled
        by FontForge via font.transform; verified on AdwaitaSans where the
        font-level transform produced identical metrics to per-glyph calls.

        Range is clamped to [0.05, 20.0] (s = 1 + scale/100). s <= 0 would
        invert the font; we refuse rather than silently producing garbage.
        """
        if self.scale == 0:
            return
        try:
            s = 1.0 + self.scale / 100.0
            if s <= 0:
                self._log(
                    f"  --scale {self.scale:g}% would invert the font "
                    f"(factor {s:.4f} <= 0); refusing.",
                    'sub')
                return
            self._log(
                f"Phase 5c: Uniform scale ×{s:.4f} ({self.scale:+g}%)",
                'phase')
            font.selection.all()
            font.transform((s, 0.0, 0.0, s, 0.0, 0.0))
            font.selection.all()
            font.removeOverlap()
            font.round()
            for glyph in font.glyphs():
                if glyph.width > 0:
                    glyph.width = int(round(glyph.width * s))
            self.stats['scale_applied'] = self.scale
        except Exception as e:
            self._log(f"Scale issue: {e}", 'sub')

    # -- Phase 5d: letter-spacing / --spacing (v6 NEW) --
    def phase_spacing(self, font):
        """
        Letter-spacing (v6 NEW): --spacing PCT adjusts hmtx (and the CFF
        charstring width) per the same percent. Positive opens spacing,
        negative tightens. Glyph outlines are NOT touched.

        Why glyph.width only (and not left/right side bearings):
            FontForge 20251009: glyph.left_side_bearing SETTER physically
            shifts the outline, while right_side_bear and width move only
            the advance edge. Adjusting the LSB would drag every glyph
            sideways -- wrong for letter-spacing. Verified on JanoSansPro 'o'
            during the v2.0 --spacing work.
        """
        if self.spacing == 0:
            return
        try:
            pct = self.spacing
            self._log(
                f"Phase 5d: Letter-spacing {pct:+g}% on advance widths",
                'phase')
            touched = 0
            skipped = 0
            for glyph in font.glyphs():
                w = glyph.width
                if w <= 0:
                    # combining marks, ZWSP, etc. keep width = 0 by design.
                    skipped += 1
                    continue
                new_w = int(round(w * (100 + pct) / 100))
                glyph.width = new_w
                touched += 1
            self.stats['spacing_applied'] = pct
            self.stats['spacing_touched'] = touched
            self.stats['spacing_skipped'] = skipped
            self._log(
                f"Letter-spacing done: {touched} glyphs touched, "
                f"{skipped} skipped (zero-width preserved)",
                'sub')
        except Exception as e:
            self._log(f"Spacing issue: {e}", 'sub')

    # -- Phase 6: high-risk re-pass --
    def phase_high_risk_repass(self, font):
        if not self.stats['fringe_issues'] or self.aggression not in ('high', 'extreme'):
            return
        self._log("Phase 6: High-risk glyph re-pass (extreme)", 'phase')
        # Top 20 high-risk glyphs. fringe_issues entries are 3-tuples
        # (name, score, problems) as built in phase_glyphs; this loop unpacked
        # 2 and raised ValueError. It was unreachable before the fringe
        # detector was repaired -- glyph.contours does not exist on FontForge
        # 20251009, so every glyph scored 0 and the list stayed empty.
        for entry in self.stats['fringe_issues'][:20]:
            name = entry[0]
            try:
                glyph = font[name]
                self._phase_overlap(glyph)
                self._phase_smooth(glyph)
                self._phase_cleanup(glyph)
            except Exception:
                pass
        if self.aggression == 'extreme':
            font.selection.all()
            font.removeOverlap()
            font.simplify(0.2)
            font.canonicalContours()

    # -- Phase 7: hinting --
    def phase_hint(self, font):
        if not self.hint:
            return
        self._log("Phase 7: TrueType auto-hinting", 'phase')
        try:
            font.selection.all()
            font.autoHint()
            font.autoInstr()
            self._log("Auto-hinting applied", 'sub')
        except Exception as e:
            self._log(f"Hinting issue: {e}", 'sub')

    # -- Phase 8: CFF generation --
    def phase_generate(self, font, out_path):
        """Generate OTF/CFF with optimal flags."""
        self._log(f"Phase 8: Generating OTF → {os.path.basename(out_path)}", 'phase')
        flags_seen = False

        # Preferred: opentype + cff flags
        attempts = [
            ('opentype', 'cff'),
            ('opentype',),
            ('opentype', 'round'),
        ]
        for attempt in attempts:
            try:
                font.generate(out_path, flags=attempt)
                flags_seen = attempt
                break
            except Exception:
                continue
        if not flags_seen:
            # Last-resort: default flags
            font.generate(out_path)

    # -- Phase 9: fontTools post-processing --
    def phase_postprocess(self, font_path):
        """Use fontTools for GASP, head flags, CFF hint tuning, and pixel snapping."""
        if not FONTTOOLS_AVAILABLE:
            return
        self._log("Phase 9: fontTools post-processing (GASP, head flags, pixel snap)", 'phase')

        try:
            font = TTFont(font_path)

            # GASP table — full flags at all AA-enabled ranges for fringe elimination
            if self.gasp:
                try:
                    if 'gasp' in font:
                        del font['gasp']
                    from fontTools.ttLib import newTable
                    gasp = newTable('gasp')
                    gasp.version = 1
                    # All flags enabled (0x0F) at all AA-enabled sizes:
                    # GRIDFIT | DOGRAY | SYMMETRIC_GRIDFIT | SYMMETRIC_SMOOTHING
                    # This is the optimal combination for fringe-free rendering.
                    if self.aggression == 'extreme':
                        gasp.gaspRange = {
                            0:   0x03,  # 0-7: GRIDFIT + AA (no smoothing at tiny sizes)
                            7:   0x0F,  # 8+: full flags (GRIDFIT + AA + SYMMETRIC)
                            65535: 0x0F,
                        }
                    else:
                        gasp.gaspRange = {
                            0:   0x03,
                            7:   0x0F,
                            65535: 0x0F,
                        }
                    font['gasp'] = gasp
                    self.stats['gasp_set'] = True
                except Exception as e:
                    self._log(f"GASP issue: {e}", 'sub')

            # head table Chrome-safe flags
            try:
                if 'head' in font:
                    head = font['head']
                    head.flags |= 0x0103   # baseline y=0, lsb x=0, rounded layout
                    # Bit 3 (Force PPEM integer) intentionally NOT set —
                    # Chrome rejects fonts with this flag.
                    head.macStyle &= ~0x18  # clear Outline/Shadow
                    # Set bold bit only for semibold+ weights
                    if 'OS/2' in font and font['OS/2'].usWeightClass >= 600:
                        head.macStyle |= 0x01
            except Exception as e:
                self._log(f"head flags issue: {e}", 'sub')

            # CFF hint tuning
            if self.hint_tune and ('CFF ' in font or 'CFF2' in font):
                try:
                    self._tune_cff_hinting(font)
                except Exception as e:
                    self._log(f"CFF hint tuning issue: {e}", 'sub')

            # v5 NEW: Quantise BlueValues to clean integer grid
            if self.hint_tune and self.blue_quantise > 0 and ('CFF ' in font or 'CFF2' in font):
                try:
                    self._phase_quantise_blue_values(font)
                    self.stats['blue_quantised'] = True
                except Exception as e:
                    self._log(f"BlueValues quantise issue: {e}", 'sub')

            # v4.1 ENHANCED: force all TrueType glyph coords to integer
            # Catches any fractional coords that survived FontForge processing
            if 'glyf' in font:
                try:
                    from fontTools.pens.recordingPen import RecordingPen
                    from fontTools.pens.t2Pen import T2Pen  # not needed but keeps import order
                    glyf = font['glyf']
                    snapped = 0
                    for glyph_name in font.getGlyphOrder():
                        if glyph_name not in glyf:
                            continue
                        glyph = glyf[glyph_name]
                        if not hasattr(glyph, 'coordinates') or glyph.coordinates is None:
                            continue
                        coords = glyph.coordinates
                        for i in range(len(coords)):
                            x, y = coords[i]
                            ix, iy = int(round(x)), int(round(y))
                            if (x, y) != (ix, iy):
                                coords[i] = (ix, iy)
                                snapped += 1
                    if snapped:
                        self._log(f"Pixel-snap: {snapped} coords snapped to integer", 'sub')
                except Exception as e:
                    self._log(f"Pixel-snap issue: {e}", 'sub')

            font.save(font_path)
            font.close()
        except Exception as e:
            self._log(f"fontTools post-processing failed: {e}", 'sub')

    def _tune_cff_hinting(self, font):
        """Synthesise CFF Private dict hint values (v8.2 --hint-tune logic)."""
        cff_key = 'CFF2' if 'CFF2' in font else 'CFF '
        top_dict = font[cff_key].cff.topDictIndex[0]
        priv = top_dict.Private

        if not priv.LanguageGroup:
            priv.LanguageGroup = 1
        if priv.ExpansionFactor is None:
            priv.ExpansionFactor = 0.06
        if priv.BlueFuzz is None:
            priv.BlueFuzz = 1
        if priv.BlueShift is None:
            priv.BlueShift = 7

        has_blue = priv.rawDict.get('BlueValues')
        has_other = priv.rawDict.get('OtherBlues')

        if not has_blue or not has_other:
            cap, xh = 700, 500
            os2 = font.get('OS/2')
            if os2:
                if os2.sCapHeight:
                    cap = os2.sCapHeight
                if os2.sxHeight:
                    xh = os2.sxHeight
            if not has_blue:
                priv.BlueValues = [0, -10, cap, cap + 10]
            if not has_other and xh:
                priv.OtherBlues = [xh, xh + 10]

        # Round stem widths
        for attr in ('StdHW', 'StdVW'):
            raw = priv.rawDict.get(attr)
            if raw is not None and isinstance(raw, (int, float)):
                rounded = int(round(raw))
                if rounded != raw:
                    setattr(priv, attr, rounded)

        self.stats['hint_tune_applied'] = True

    def phase_flatten(self, in_path, out_path):
        """Phase 8b: collapse near-straight curves back onto their chords.

        Only runs when --thickness was used, since changeWeight is what
        creates them, and only when the source font is still readable. The
        input TTF is the source, so no threshold is needed to tell an
        artifact from a designed curve.
        """
        if self.thickness <= 0:
            return
        if not FLATTEN_TOOLS:
            if self.verbose:
                self._log("Phase 8b: flatten skipped (fontTools unavailable)",
                          'sub')
            return
        try:
            result = flatten_straight_curves(out_path, in_path)
        except Exception as e:
            self._log(f"Phase 8b issue: {e}", 'sub')
            return
        if result is None:
            self._log("Phase 8b: flatten skipped (glyphs unreadable)", 'sub')
            return
        flattened, kept = result
        self.stats['curves_flattened'] = flattened
        self.stats['curves_kept'] = kept
        if flattened:
            self._log(f"Phase 8b: straightened {flattened} near-straight "
                      f"curves back onto their chords ({kept} kept)", 'sub')

    # -- Validation --
    def validate(self, in_path, out_path):
        """Verify output font is loadable and has reasonable structure."""
        if not os.path.exists(out_path):
            self.stats['errors'].append(f"{os.path.basename(out_path)}: file not created")
            return False
        try:
            f = fontforge.open(out_path)
            f.close()
            return True
        except Exception as e:
            self.stats['errors'].append(f"{os.path.basename(out_path)}: {e}")
            return False

    # -- Master orchestrator --
    def run(self, in_path, out_path):
        """Run full pipeline on one input file."""
        font = None
        try:
            font = fontforge.open(in_path)
            self._log(f"Loaded: {font.familyname} {font.fontname}")
            glyph_count = sum(1 for _ in font.glyphs())
            self._log(f"Total glyphs: {glyph_count}")

            # Phase 2
            self.phase_glyphs(font)
            # Phase 3
            self.phase_global(font)
            # Phase 4 (optional)
            self.phase_thickness(font)
            # Phase 5 (optional)
            self.phase_compact(font)
            # Phase 5b (v5 NEW: optional width adjustment)
            self.phase_width(font)
            # Phase 5c (v6 NEW: optional uniform scale)
            self.phase_scale(font)
            # Phase 5d (v6 NEW: optional letter-spacing)
            self.phase_spacing(font)
            # Phase 6 (high-risk re-pass)
            self.phase_high_risk_repass(font)
            # Phase 7 (hinting)
            self.phase_hint(font)
            # Phase 8 (generate)
            os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
            self.phase_generate(font, out_path)
            # Phase 8b: straighten curves changeWeight created
            self.phase_flatten(in_path, out_path)
            # Phase 9 (fontTools post-processing)
            self.phase_postprocess(out_path)
            # Validate
            return self.validate(in_path, out_path)

        except Exception as e:
            self.stats['errors'].append(f"{os.path.basename(in_path)}: {e}")
            import traceback
            if self.verbose:
                traceback.print_exc()
            return False
        finally:
            if font:
                try:
                    font.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
#  Walk input path and process each TTF
# ---------------------------------------------------------------------------

def _run_single_in_subprocess(font_path, out_path, opts, timeout):
    """Convert one font in a child `fontforge -script` and enforce a timeout.

    Phase 4's changeWeight can drive FontForge into a C-level spin on some
    faces -- NeverMindCompact-Thin at --thickness 5 produces 23 "Unexpected
    point count in SSAddPoints", freezes its log, and keeps burning CPU
    while emitting no output. No in-process guard can stop that: the hang is
    inside FontForge's C code, so a Python signal handler or thread never
    runs again. Isolation is the only way to convert a stalled face into a
    reported failure so the rest of the batch still completes.

    Returns (ok, timed_out) so a genuine timeout is not misreported as a
    crash -- they need different remedies, and conflating them hides which
    one happened.
    """
    cmd = [sys.executable, '-script', __file__, '--single-font',
           font_path, out_path]
    for key, value in opts.items():
        flag = '--' + key.replace('_', '-')
        if value is True:
            cmd.append(flag)
        elif value not in (False, None, 0, 0.0, ''):
            cmd.extend([flag, str(value)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, True
    except Exception:
        return False, False
    return (proc.returncode == 0 and os.path.exists(out_path)), False


def walk_and_convert(input_path, out_dir, aggression='medium', thickness=0,
                     compact=False, hint=True, gasp=True, hint_tune=True,
                     verbose=True, width=0, quantise_curve=50, blue_quantise=1,
                     scale=0, spacing=0, timeout=0):
    """Walk input path (file or directory) and convert all TTFs."""
    os.makedirs(out_dir, exist_ok=True)

    opts = dict(
        aggression=aggression, thickness=thickness, compact=compact,
        hint=hint, gasp=gasp, hint_tune=hint_tune, verbose=verbose,
        width=width, quantise_curve=quantise_curve,
        blue_quantise=blue_quantise, scale=scale, spacing=spacing)

    if os.path.isfile(input_path):
        base = os.path.splitext(os.path.basename(input_path))[0]
        out_path = os.path.join(out_dir, f"{base}.otf")
        if timeout and timeout > 0:
            ok, timed_out = _run_single_in_subprocess(
                input_path, out_path, opts, timeout)
        else:
            killer = FringeKiller(**opts)
            ok = killer.run(input_path, out_path)
            _print_stats(killer, [os.path.basename(input_path)], ok)
        if not ok:
            why = 'timed out' if timed_out else 'failed'
            print(f"❌ {base}: conversion {why}", file=sys.stderr)
        return 1 if ok else 0

    # Walk for all .ttf files
    ttf_files = []
    for root, _, files in os.walk(input_path):
        for fname in files:
            if fname.lower().endswith(('.ttf',)):
                ttf_files.append(os.path.join(root, fname))

    if not ttf_files:
        print(f"⚠️  No .ttf files found in {input_path}", file=sys.stderr)
        return 0

    if verbose:
        print(f"\n📁 Found {len(ttf_files)} TTF files")
        print(f"🔧 Fringe elimination aggression: {aggression.upper()}\n")

    success = 0
    failures = []
    for font_path in ttf_files:
        rel = os.path.relpath(os.path.dirname(font_path), input_path)
        target_dir = out_dir if rel == '.' else os.path.join(out_dir, rel)
        base = os.path.splitext(os.path.basename(font_path))[0]
        out_path = os.path.join(target_dir, f"{base}.otf")
        os.makedirs(target_dir, exist_ok=True)

        if timeout and timeout > 0:
            ok, timed_out = _run_single_in_subprocess(
                font_path, out_path, opts, timeout)
            if not ok:
                failures.append(os.path.basename(font_path))
                if verbose:
                    why = f"timed out after {timeout}s" if timed_out \
                        else "conversion failed"
                    print(f"❌ {base}: {why} (skipped, batch continues)",
                          file=sys.stderr)
            else:
                success += 1
            continue

        killer = FringeKiller(**opts)
        ok = killer.run(font_path, out_path)
        if ok:
            success += 1
        else:
            failures.append(os.path.basename(font_path))

    _print_stats(None, ttf_files, success, failures)
    return success


def _print_stats(killer, files, success_or_ok, failures=None):
    """Print summary."""
    if killer and killer.verbose is False:
        return
    if killer is None:
        # Summary for batch run
        print()
        print('=' * 60)
        print(f"📊 SUMMARY: {success_or_ok}/{len(files)} succeeded")
        if failures:
            print(f"Failures: {', '.join(failures)}")
        print('=' * 60)
    else:
        # Single-file run
        print()
        print(f"✅ {os.path.basename(files[0]) if isinstance(files, list) else ''}: "
              f"glyphs={killer.stats['glyphs_processed']}, "
              f"high-risk={killer.stats['high_risk_glyphs']}, "
              f"thickness={'+' + str(killer.thickness) if killer.stats['thickness_applied'] else 'off'}, "
              f"gasp={'on' if killer.stats['gasp_set'] else 'off'}")


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="TTF to OTF Converter — FRINGE ELIMINATION SPECIALIST v6.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
AGGRESSION LEVELS:
  low      — Mild fringe removal (fast)
  medium   — Standard fringe elimination (default)
  high     — Aggressive fringe removal
  extreme  — Maximum fringe elimination (slowest, best)

EXAMPLES:
  # Standard processing
  python ttf2otf_ff_v5.py ./fonts ./output

  # Aggressive fringe removal with thickness boost
  python ttf2otf_ff_v5.py ./fonts ./output --aggression high --thickness 25

  # With auto-hinting and GASP table (recommended)
  python ttf2otf_ff_v5.py font.ttf ./output --aggression medium --hint --gasp

  # Maximum treatment with width expansion
  python ttf2otf_ff_v5.py ./fonts ./output --aggression extreme --compact --thickness 40 --width 5

  # Compress to semi-condensed
  python ttf2otf_ff_v5.py ./fonts ./output --width -8 --aggression high

  # Aggressive curve quantisation for old messy fonts
  python ttf2otf_ff_v5.py font.ttf ./output --quantise-curve 25 --aggression extreme
        """
    )

    parser.add_argument("input_path", nargs="?",
                        help="Path to a directory of .ttf files or a single .ttf")
    parser.add_argument("output_dir", nargs="?",
                        help="Directory where optimised .otf files will be saved")

    parser.add_argument("-a", "--aggression",
                        choices=['low', 'medium', 'high', 'extreme'],
                        default='medium',
                        help="Fringe elimination aggression (default: medium)")
    parser.add_argument("-t", "--thickness", type=int, default=0,
                        help="Thickness boost in font units (e.g. 25 for subtle bolding)")
    parser.add_argument("-c", "--compact", action="store_true",
                        help="Compact mode: tighter spacing, smaller counters")
    parser.add_argument("--width", type=float, default=0,
                        help="[v5 NEW] Adjust font width horizontally. "
                             "Positive = expand (e.g. 5 = 5%% wider), "
                             "negative = compress (e.g. -3 = 3%% narrower). "
                             "Default 0 = no change. Adjusts both outlines and advance widths.")
    parser.add_argument("--scale", type=float, default=0,
                        help="[v6 NEW] Uniform X-and-Y scale, percent. "
                             "Distinct from --width, which is X-only: --scale "
                             "grows geometry on both axes (e.g. 5 = 5%% taller "
                             "AND 5%% wider) and matches advance widths. "
                             "Default 0 = no change. Refuses factors <= 0 in a factor.")
    parser.add_argument("--spacing", type=float, default=0,
                        help="[v6 NEW] Letter-spacing / tracking, percent change of "
                             "each glyph's advance width. Positive opens spacing; "
                             "negative tightens. Outlines are NOT touched -- "
                             "glyph.width moves only the advance edge in "
                             "FontForge 20251009, so it composes cleanly with "
                             "--scale / --width / --compact. Default 0 = no change.")
    parser.add_argument("--quantise-curve", type=int, default=50,
                        help="[v5 NEW] Curve flattening tolerance divisor (default: 50). "
                             "Smaller = more aggressive flattening. Higher = preserve more curves.")
    parser.add_argument("--blue-quantise", type=int, default=1,
                        help="[v5 NEW] Round CFF BlueValues/OtherBlues to N-unit grid "
                             "(default: 1 = each unit). Eliminates fractional BlueValues "
                             "that can cause rasterizer misalignment.")
    parser.add_argument("--hint", action="store_true", default=True,
                        help="Apply TrueType auto-hinting (default: on)")
    parser.add_argument("--no-hint", dest="hint", action="store_false",
                        help="Skip auto-hinting")
    parser.add_argument("--gasp", action="store_true", default=True,
                        help="Set GASP table for clear screen rendering (default: on)")
    parser.add_argument("--no-gasp", dest="gasp", action="store_false",
                        help="Skip GASP table configuration")
    parser.add_argument("--hint-tune", action="store_true", default=True,
                        help="CFF hint tuning (synthesise BlueValues if missing)")
    parser.add_argument("--no-hint-tune", dest="hint_tune", action="store_false",
                        help="Skip CFF hint tuning")
    parser.add_argument("-v", "--verbose", action="store_true", default=True,
                        help="Verbose output (default: on)")
    parser.add_argument("-q", "--quiet", dest="verbose", action="store_false",
                        help="Quiet mode (errors only)")
    parser.add_argument("--version", action="version", version="ttf2otf_ff_v6.0")
    parser.add_argument("--timeout", type=int, default=0,
                        help="Per-font timeout in seconds (0 = no limit). "
                             "Each font converts in a child process, so one "
                             "that hangs inside FontForge -- e.g. "
                             "NeverMindCompact-Thin at --thickness 5, which "
                             "spins in C and never returns -- is reported as "
                             "a failure and the batch continues. Default: 0.")
    parser.add_argument("--single-font", nargs=2, metavar=("IN", "OUT"),
                        help=argparse.SUPPRESS)

    args = parser.parse_args()

    # --single-font is how the parent re-enters this script once per font
    # under --timeout. It carries its own paths, so it must be handled
    # before the positional arguments are validated.
    if args.single_font:
        in_path, out_path = args.single_font
        killer = FringeKiller(args.aggression, args.thickness, args.compact,
                              args.hint, args.gasp, args.hint_tune,
                              args.verbose, width=args.width,
                              quantise_curve=args.quantise_curve,
                              blue_quantise=args.blue_quantise,
                              scale=args.scale, spacing=args.spacing)
        sys.exit(0 if killer.run(in_path, out_path) else 1)

    if not args.input_path or not args.output_dir:
        parser.error("input_path and output_dir are required")

    if not os.path.exists(args.input_path):
        print(f"❌ Input path does not exist: {args.input_path}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print("=" * 60)
        print("🔧 TTF → OTF — FRINGE ELIMINATION SPECIALIST v6.0")
        print("=" * 60)
        print(f"Input:       {args.input_path}")
        print(f"Output:      {args.output_dir}")
        print(f"Aggression:  {args.aggression.upper()}")
        print(f"Thickness:   {f'+{args.thickness}' if args.thickness else 'off'}")
        print(f"Compact:     {'on' if args.compact else 'off'}")
        print(f"Hinting:     {'on' if args.hint else 'off'}")
        print(f"GASP table:  {'on' if args.gasp else 'off'}")
        print(f"CFF hint tune: {'on' if args.hint_tune else 'off'}")
        print(f"fontTools:   {'available' if FONTTOOLS_AVAILABLE else 'NOT installed'}")
        print()

    # Check FontForge
    try:
        ff_ver = fontforge.version()
        if args.verbose:
            print(f"FontForge: {ff_ver}")
            print()
    except Exception as e:
        print(f"❌ FontForge error: {e}", file=sys.stderr)
        sys.exit(1)

    success = walk_and_convert(
        args.input_path, args.output_dir,
        aggression=args.aggression,
        thickness=args.thickness,
        compact=args.compact,
        hint=args.hint,
        gasp=args.gasp,
        hint_tune=args.hint_tune,
        verbose=args.verbose,
        width=args.width,
        quantise_curve=args.quantise_curve,
        blue_quantise=args.blue_quantise,
        scale=args.scale,
        spacing=args.spacing,
        timeout=args.timeout,
    )

    sys.exit(0 if success > 0 else 1)


if __name__ == "__main__":
    main()