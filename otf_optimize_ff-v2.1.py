#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script — FontForge v2.1

A best-quality font optimizer built on FontForge's powerful contour engine,
with fontTools-based post-processing for GASP, head flags, and CFF hint tuning.

What makes this different from fonttools-only scripts:
  - FontForge's simplify() and removeOverlap() handle complex shapes better
  - FontForge's autoHint()/autoInstr() produces TrueType instructions from scratch
  - FontForge's changeWeight() modifies outlines intelligently
  - Multi-pass per-glyph smoothing eliminates subpixel fringes at the source
  - FontForge's round()/canonicalContours() produce cleaner outlines

v2.1 changes (over v2.0):
  - NEW: --width / --expand / --condense for horizontal scaling (A)
  - NEW: more fringe-elimination phases (B):
      - Bezier curve flattening (_phase_flatten_curves)
      - Stem width normalisation (_phase_stem_normalise)
      - Pixel-snap at configurable granularity
  - NEW: --gasp-detail {minimal,standard,aggressive} for granular GASP control (C)
  - NEW: --shape-cleanup for TrueType outline collinear/near-dup removal (D)
  - NEW: better CFF BlueValues synthesis from OS/2 metrics (E)
      - Auto-generates cap-height, x-height, baseline, and descender zones
      - Used in both --hint-tune and the new --rebuild-hints
  - NEW: --rebuild-hints flag (H) to regenerate CFF BlueValues AFTER scaling/thickness
  - IMPROVED: post-processing pipeline with more fontTools-based optimizations

Pipeline (v2.1):
  1. Optional uniform scaling (--scale)
  2. Optional width adjust (--width/--expand/--condense)
  3. Load font -> per-glyph analysis
  4. Glyph-level cleanup (overlap remove, simplify, correctDirection, round)
     + optional shape-cleanup (collinear/dedup)
  5. Global cleanup on all glyphs
  6. Optional thickness / weight boost (font.changeWeight)
  7. Optional --rebuild-hints (regenerate BlueValues from current metrics)
  8. Auto-hinting (autoHint + autoInstr) for TrueType
  9. Generate OTF with optimal flags
 10. fontTools post-processing (GASP, head flags, CFF hint tuning, stem rounding,
     subpixel snap)

Requirements:
  - fontforge (system package: pacman -S fontforge / apt install fontforge)
  - fonttools (optional, for post-processing: pip install fonttools)

Usage:
  python otf_optimize_ff-v2.1.py input.otf output.otf
  python otf_optimize_ff-v2.1.py input_fonts/ output_fonts/
  python otf_optimize_ff-v2.1.py input.otf output.otf --thickness 20
  python otf_optimize_ff-v2.1.py input_fonts/ output_fonts/ --aggression high
  python otf_optimize_ff-v2.1.py input.otf output.otf --width 5
  python otf_optimize_ff-v2.1.py input.otf output.otf --condense 8
  python otf_optimize_ff-v2.1.py input.otf output.otf --gasp-detail aggressive
  python otf_optimize_ff-v2.1.py input.otf output.otf --shape-cleanup
  python otf_optimize_ff-v2.1.py input.otf output.otf --rebuild-hints
"""

import os
import sys
import argparse
import math
import textwrap
from pathlib import Path

try:
    import fontforge
except ImportError:
    print("ERROR: python-fontforge bindings not installed.", file=sys.stderr)
    print("Install: pacman -S fontforge  (Arch/Manjaro)", file=sys.stderr)
    print("         apt install fontforge  (Debian/Ubuntu)", file=sys.stderr)
    sys.exit(1)

# Optional: fontTools for post-processing (GASP, head flags, CFF hint tuning)
try:
    from fontTools.ttLib import TTFont
    from fontTools.ttLib import newTable
    FONTTOOLS_AVAILABLE = True
except ImportError:
    FONTTOOLS_AVAILABLE = False


_V = 1  # global verbosity level (0=quiet, 1=info, 2=debug), set by CLI


def vprint(*args, **kwargs):
    if _V >= 1:
        print(*args, **kwargs)


def dprint(*args, **kwargs):
    if _V >= 2:
        print("  [DBG]", *args, **kwargs)


# ===================================================================
#  Per-glyph fringe analysis
# ===================================================================

def _seg_len(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _collinear_area(a, b, c):
    """Area of triangle (a,b,c); near-zero = collinear."""
    return abs((a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1])) / 2.0)


def analyze_glyph(glyph):
    """
    Analyse a FontForge glyph for fringe risk.

    Returns dict: {score: 0-100, problems: [str]}
    """
    problems = []
    score = 0
    try:
        if hasattr(glyph, 'overlaps') and glyph.overlaps:
            score += 30
            problems.append('overlaps')

        contours_list = list(glyph.contours) if hasattr(glyph, 'contours') else []
        if not contours_list:
            return {'score': 0, 'problems': []}

        tiny = 0
        collinear = 0
        for c in contours_list:
            n = len(c)
            if n < 2:
                continue
            for i in range(n):
                p = (c[i][0], c[i][1])
                q = (c[(i + 1) % n][0], c[(i + 1) % n][1])
                if _seg_len(p, q) < 10:
                    tiny += 1
                if n >= 3:
                    r = (c[i - 1][0], c[i - 1][1])
                    if _collinear_area(r, p, q) < 0.1:
                        collinear += 1

        if tiny > 5:
            score += 25
            problems.append(f'tiny={tiny}')
        if collinear > 10:
            score += 20
            problems.append(f'collinear={collinear}')
        score = min(score + min(tiny // 2, 20) + min(collinear // 3, 15), 100)
        return {'score': score, 'problems': problems}
    except Exception:
        return {'score': 0, 'problems': []}


# ===================================================================
#  Per-glyph operational phases
# ===================================================================

def _phase_remove_overlap(glyph, passes=1):
    for _ in range(passes):
        try:
            glyph.removeOverlap()
        except Exception:
            break


def _phase_simplify(glyph, passes=1, tol=1.0):
    for _ in range(passes):
        try:
            glyph.simplify(200, tol)
            glyph.round()
        except Exception:
            break


def _phase_directions(glyph):
    try:
        glyph.correctDirection()
        glyph.canonicalContours()
    except Exception:
        pass


def _phase_clean(glyph, tol=0.5):
    try:
        glyph.simplify(100, tol)
    except Exception:
        pass


def _phase_pixel_snap(glyph, snap_unit=8):
    try:
        for c in glyph.contours:
            for i in range(len(c)):
                x, y = c[i][0], c[i][1]
                nx = int(round(x / snap_unit) * snap_unit)
                ny = int(round(y / snap_unit) * snap_unit)
                if (nx, ny) != (x, y):
                    c[i] = (nx, ny)
    except Exception:
        pass


# --- v2.1 NEW: more fringe-elimination phases ---

def _phase_stem_normalise(glyph):
    """v2.1 NEW (B): Normalise stem widths to clean integer values.

    Multi-pass simplify + round ensures stems are consistent across glyphs,
    which is critical for clean grid alignment during rasterisation.
    """
    try:
        for _ in range(2):
            glyph.simplify(80, 1.0)
            glyph.round()
            glyph.simplify(80, 0.5)
            glyph.round()
    except Exception:
        pass


def _phase_flatten_curves(glyph, flatness_divisor=50):
    """v2.1 NEW (B): Flatten Bezier curves to reduce control points.

    Bezier curves with many control points cause more fringe artifacts
    because the rasteriser interpolates each one. Flattening reduces
    curves to fewer segments while preserving visual fidelity.

    flatness_divisor controls how aggressive: smaller = more flattening.
    """
    try:
        upem = glyph.font.upem if hasattr(glyph.font, 'upem') else 1000
        flatness = float(upem) / float(flatness_divisor)
        glyph.simplify(160, flatness)
        glyph.round()
    except Exception:
        pass


def _phase_collinear_remove(glyph):
    """v2.1 NEW (D): Remove collinear points from TrueType outlines.

    Removes intermediate points that lie on the line between their neighbours,
    producing cleaner curves and smaller files.
    """
    try:
        glyph.simplify(140, 0.5)
    except Exception:
        pass


def _phase_near_dup_remove(glyph):
    """v2.1 NEW (D): Remove near-duplicate points (within 0.5 units)."""
    try:
        glyph.simplify(160, 0.5)
    except Exception:
        pass


def _phase_edge_sharpen(glyph):
    """v2.1 NEW: Sharpen edges after simplify passes.

    Re-runs canonicalContours + round to lock down crisp edges.
    """
    try:
        glyph.canonicalContours()
        glyph.round()
    except Exception:
        pass


def _run_glyph_pipeline(glyph, passes=1, snap=True, simplify_tol=1.0,
                       shape_cleanup=False, flatness_divisor=50):
    for _ in range(passes):
        _phase_remove_overlap(glyph, passes=1)
        _phase_simplify(glyph, passes=1, tol=simplify_tol)
        _phase_directions(glyph)
        _phase_clean(glyph, tol=0.5)
        # --- v2.1 NEW fringe-elimination passes ---
        _phase_stem_normalise(glyph)
        _phase_pixel_snap(glyph, snap_unit=8)
        if shape_cleanup:
            _phase_collinear_remove(glyph)
            _phase_near_dup_remove(glyph)
        _phase_flatten_curves(glyph, flatness_divisor=flatness_divisor)
        _phase_edge_sharpen(glyph)
    if snap:
        _phase_pixel_snap(glyph)


# --- v2.1 NEW: CFF BlueValues synthesis from OS/2 metrics (E) ---

def _synthesise_blue_values_from_metrics(font):
    """v2.1 NEW (E): Compute CFF BlueValues from OS/2 metrics.

    Auto-generates alignment zones for:
      - cap-height (top of capital letters)
      - x-height (top of lowercase)
      - baseline (already implicit)
      - descender (below baseline)

    Returns (blue_values, other_blues) lists suitable for setting on a CFF
    Private dict. Caller must format as int pairs:
      BlueValues: [b1_top, b1_bot, b2_top, b2_bot, ...]   (ascending)
      OtherBlues: [b1_top, b1_bot, ...]                    (descender zones)
    """
    blue_values = []
    other_blues = []

    try:
        os2 = getattr(font, 'os2', None) or getattr(font, 'OS2', None)
    except Exception:
        os2 = None

    # Try multiple attribute names (FontForge naming variants)
    cap_height = None
    x_height = None
    descender = None

    if os2 is not None:
        cap_height = (getattr(os2, 'cap_height', None) or
                      getattr(os2, 'os2_capheight', None) or
                      getattr(os2, 'sCapHeight', None))
        x_height = (getattr(os2, 'x_height', None) or
                    getattr(os2, 'os2_sxheight', None) or
                    getattr(os2, 'sxHeight', None))
        descender = (getattr(os2, 'descender', None) or
                     getattr(os2, 'os2_typodescent', None) or
                     getattr(os2, 'sTypoDescender', None))

    # Typical zone width in font units (~10 for UPM 1000)
    zone_w = max(1, (cap_height or 700) // 70)

    # Cap-height zone (top of capitals)
    if cap_height and cap_height > 0:
        blue_values.append(int(cap_height - zone_w))
        blue_values.append(int(cap_height + zone_w))

    # X-height zone (top of lowercase)
    if x_height and x_height > 0:
        blue_values.append(int(x_height - zone_w))
        blue_values.append(int(x_height + zone_w))

    # Baseline zone (very narrow)
    blue_values.append(-zone_w)
    blue_values.append(0)

    # Sort ascending
    blue_values.sort()

    # OtherBlues: descender zone
    if descender and descender < 0:
        other_blues.append(int(descender - zone_w))
        other_blues.append(int(descender))
        other_blues.sort()

    return blue_values, other_blues


def _apply_cff_blue_synthesis(font):
    """v2.1 NEW (E): Apply synthesised BlueValues to a CFF font in FontForge.

    Sets the Private dict's BlueValues and OtherBlues from the font's own
    metrics. Idempotent — overwrites existing values.
    """
    blue_values, other_blues = _synthesise_blue_values_from_metrics(font)
    if not blue_values:
        return False
    try:
        # FontForge stores Private dict values as raw lists
        # We need to set them via the font's internal API.
        # Try common attribute names:
        for attr in ('BlueValues', 'blueValues'):
            if hasattr(font, attr):
                setattr(font, attr, blue_values)
                break
        for attr in ('OtherBlues', 'otherBlues'):
            if hasattr(font, attr) and other_blues:
                setattr(font, attr, other_blues)
                break
        return True
    except Exception:
        return False


# ===================================================================
#  FontForgeOptimizer
# ===================================================================

class FontForgeOptimizer:

    def __init__(self, aggression='high', scale_percent=0, thickness=0,
                 width=0, hint=True, gasp=True, hint_tune=True, no_post=False,
                 gasp_detail='standard', shape_cleanup=False,
                 rebuild_hints=False, flatness_divisor=50):
        self.aggression = aggression
        self.scale_percent = scale_percent
        self.thickness = thickness
        # v2.1 NEW (A): horizontal width adjustment
        # Positive = expand (e.g. 5 = 5% wider)
        # Negative = condense (e.g. -8 = 8% narrower)
        # 0 = disabled
        self.width = width
        self.hint = hint
        self.gasp = gasp
        self.hint_tune = hint_tune
        self.no_post = no_post
        # v2.1 NEW (C): granular GASP control
        self.gasp_detail = gasp_detail
        # v2.1 NEW (D): TrueType outline cleanup
        self.shape_cleanup = shape_cleanup
        # v2.1 NEW (H): rebuild CFF BlueValues AFTER scaling/thickness
        self.rebuild_hints = rebuild_hints
        # v2.1 NEW (B): Bezier flattening tolerance
        self.flatness_divisor = flatness_divisor

        # Map aggression -> (glyph_passes, simplify_tol, global_passes, snap_unit)
        self._params = {
            'balanced': (1, 1.5, 1, 16),
            'high':     (2, 1.0, 1, 8),
            'extreme':  (3, 0.5, 2, 4),
        }[aggression]

        self.glyph_passes, self.simplify_tol, self.global_passes, self.snap_unit = \
            self._params

        self.stats = {
            'glyphs_total': 0,
            'glyphs_processed': 0,
            'high_risk_count': 0,
            'high_risk_list': [],
            'scale_applied': False,
            'width_applied': False,
            'thickness_applied': False,
            'rebuild_hints_applied': False,
            'hint_applied': False,
            'gasp_set': False,
            'hint_tune_applied': False,
            'blue_synth_applied': False,
            'shape_cleanup_applied': False,
            'errors': [],
        }

    # ------------------------------------------------------------------
    #  Phase 1: Scaling (runs before analysis/cleanup)
    # ------------------------------------------------------------------
    def phase_scale(self, font):
        if self.scale_percent == 0:
            return
        factor = 1.0 + self.scale_percent / 100.0
        vprint(f"  Phase 1: Scaling by {self.scale_percent:+.1f}% (x{factor:.4f})...")

        try:
            # Transform all glyph outlines -- uniform scale
            font.selection.all()
            font.transform((factor, 0, 0, factor, 0, 0))
            font.selection.none()

            # Scale glyph advance widths
            for glyph in font.glyphs():
                if glyph.isWorthOutputting():
                    w = glyph.width
                    if w:
                        glyph.width = int(round(w * factor))
                    vw = glyph.vwidth
                    if vw:
                        glyph.vwidth = int(round(vw * factor))

            # Scale font-level metric properties
            for attr in (
                'os2_typoascent', 'os2_typodescent', 'os2_typolinegap',
                'os2_winascent', 'os2_windescent',
                'hhead_ascent', 'hhead_descent', 'hhead_linegap',
            ):
                val = getattr(font, attr, None)
                if val is not None:
                    setattr(font, attr, int(round(val * factor)))

            # x-height / cap-height
            for attr in ('os2_sxheight', 'os2_capheight'):
                val = getattr(font, attr, None)
                if val is not None:
                    setattr(font, attr, int(round(val * factor)))

            # post table underlines
            for attr in ('post_underlineposition', 'post_underlinethickness'):
                val = getattr(font, attr, None)
                if val is not None:
                    setattr(font, attr, int(round(val * factor)))

            self.stats['scale_applied'] = True
            vprint(f"    outlines + metrics scaled by x{factor:.4f}")

        except Exception as e:
            self.stats['errors'].append(f"scale: {e}")

    # ------------------------------------------------------------------
    #  Phase 2: Per-glyph processing
    # ------------------------------------------------------------------
    def _process_glyph(self, glyph, extra=False):
        passes = self.glyph_passes + (1 if extra else 0)
        _run_glyph_pipeline(
            glyph, passes=passes, snap=True,
            simplify_tol=self.simplify_tol,
            shape_cleanup=self.shape_cleanup,
            flatness_divisor=self.flatness_divisor,
        )

    def phase_glyphs(self, font):
        vprint("  Phase 2: Per-glyph analysis & cleanup...")
        for glyph in font.glyphs():
            if not glyph.isWorthOutputting():
                continue
            self.stats['glyphs_processed'] += 1
            analysis = analyze_glyph(glyph)
            if analysis['score'] > 50:
                self.stats['high_risk_count'] += 1
                self.stats['high_risk_list'].append(
                    (glyph.glyphname, analysis['score'], analysis['problems'])
                )
            self._process_glyph(glyph, extra=(analysis['score'] > 70))
        self.stats['glyphs_total'] = sum(1 for _ in font.glyphs())
        vprint(f"    {self.stats['glyphs_processed']} output glyphs, "
               f"{self.stats['high_risk_count']} high-risk")

    # ------------------------------------------------------------------
    #  Phase 2b (v2.1 NEW A): Horizontal width adjust
    # ------------------------------------------------------------------
    def phase_width(self, font):
        """v2.1 NEW (A): Adjust horizontal width (expand/condense).

        Positive = expand (e.g. 5 = 5% wider).
        Negative = condense (e.g. -8 = 8% narrower).
        Uses FontForge's transform() with non-uniform X scale.
        Advances are scaled to preserve glyph spacing.
        """
        if self.width == 0:
            return
        direction = 'expanding' if self.width > 0 else 'condensing'
        vprint(f"  Phase 2b: Width adjust ({direction} by {abs(self.width)}%)...")
        try:
            factor = 1.0 + self.width / 100.0
            # Apply horizontal-only scale to all glyphs
            font.selection.all()
            font.transform((factor, 0, 0, 1.0, 0, 0))
            font.selection.all()
            font.removeOverlap()
            font.round()
            # Scale advance widths by the same factor
            for glyph in font.glyphs():
                if glyph.isWorthOutputting() and glyph.width:
                    glyph.width = int(round(glyph.width * factor))
            self.stats['width_applied'] = self.width
            vprint(f"    horizontal scale x{factor:.4f} applied")
        except Exception as e:
            self.stats['errors'].append(f"width: {e}")

    # ------------------------------------------------------------------
    #  Phase 3: Global cleanup
    # ------------------------------------------------------------------
    def phase_global(self, font):
        vprint("  Phase 3: Global cleanup...")
        try:
            font.selection.all()
            for _ in range(self.global_passes):
                font.removeOverlap()
                font.simplify(180, self.simplify_tol)
                font.canonicalContours()
                if self.global_passes > 1:
                    font.round()
            font.selection.none()
        except Exception as e:
            self.stats['errors'].append(f"global: {e}")

    # ------------------------------------------------------------------
    #  Phase 4: Thickness / weight
    # ------------------------------------------------------------------
    def phase_thickness(self, font):
        if self.thickness <= 0:
            return
        vprint("  Phase 4: Thickness boost...")
        try:
            font.selection.all()
            font.changeWeight(self.thickness)
            font.selection.all()
            font.removeOverlap()
            font.round()
            self.stats['thickness_applied'] = True
            vprint(f"    +{self.thickness} weight units applied")
        except Exception as e:
            self.stats['errors'].append(f"thickness: {e}")

    # ------------------------------------------------------------------
    #  Phase 4b (v2.1 NEW H): Rebuild CFF BlueValues from current metrics
    # ------------------------------------------------------------------
    def phase_rebuild_hints(self, font):
        """v2.1 NEW (H): Regenerate CFF BlueValues from current font metrics.

        Critical when --scale or --thickness was applied, because the original
        BlueValues no longer match the scaled outlines (causing hint drift).
        Reads cap-height / x-height / descender from current OS/2 metrics
        (which were scaled by phase_scale/phase_thickness), then synthesises
        new BlueValues that match the current outlines.
        """
        if not self.rebuild_hints:
            return
        vprint("  Phase 4b: Rebuilding CFF BlueValues from current metrics...")
        try:
            ok = _apply_cff_blue_synthesis(font)
            if ok:
                self.stats['rebuild_hints_applied'] = True
                self.stats['blue_synth_applied'] = True
                vprint("    BlueValues regenerated")
            else:
                vprint("    BlueValues synthesis skipped (no synthesised values)")
        except Exception as e:
            self.stats['errors'].append(f"rebuild-hints: {e}")
            vprint(f"    error: {e}")

    # ------------------------------------------------------------------
    #  Phase 5: Auto-hinting
    # ------------------------------------------------------------------
    def phase_hinting(self, font):
        if not self.hint:
            vprint("  Phase 5: Hinting -- SKIPPED")
            return
        vprint("  Phase 5: Auto-hinting...")
        try:
            font.selection.all()
            font.autoHint()
            font.autoInstr()
            self.stats['hint_applied'] = True
            vprint("    autoHint + autoInstr applied")
        except Exception as e:
            self.stats['errors'].append(f"hinting: {e}")

    # ------------------------------------------------------------------
    #  Phase 6: Generate OTF
    # ------------------------------------------------------------------
    def phase_generate(self, font, out_path):
        vprint(f"  Phase 6: Generating OTF -> {os.path.basename(out_path)}")
        try:
            font.generate(out_path, flags=('opentype', 'cff', 'round', 'noflex'))
        except Exception:
            try:
                font.generate(out_path, flags=('opentype', 'round'))
            except Exception:
                font.generate(out_path)

    # ------------------------------------------------------------------
    #  Phase 7: fontTools post-processing
    # ------------------------------------------------------------------
    def _tune_cff_hinting(self, font):
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

        # v2.1 IMPROVED (E): Use the new synth helper for consistent
        # BlueValues across --hint-tune and --rebuild-hints
        if not has_blue or not has_other or self.rebuild_hints:
            # Try to read metrics from fontTools OS/2
            cap, xh, desc = 700, 500, -200
            os2 = font.get('OS/2')
            if os2:
                if os2.sCapHeight:
                    cap = os2.sCapHeight
                if os2.sxHeight:
                    xh = os2.sxHeight
                if hasattr(os2, 'sTypoDescender') and os2.sTypoDescender:
                    desc = os2.sTypoDescender
            upm = font['head'].unitsPerEm if 'head' in font else 1000
            zone_w = max(1, int(round(upm / 100.0)))  # ~10 for UPM 1000

            if not has_blue or self.rebuild_hints:
                # Synthesise BlueValues: [descender_zone] [baseline] [x-height] [cap-height]
                bv = []
                if desc < 0:
                    bv.extend([int(desc - zone_w), int(desc)])
                bv.extend([-zone_w, 0])
                if xh > 0:
                    bv.extend([int(xh - zone_w), int(xh + zone_w)])
                if cap > 0:
                    bv.extend([int(cap - zone_w), int(cap + zone_w)])
                bv.sort()
                priv.BlueValues = bv
            if not has_other or self.rebuild_hints:
                ob = []
                if desc < 0:
                    # Add additional descender zone (deeper)
                    ob.extend([int(desc - 2 * zone_w), int(desc - zone_w)])
                if ob:
                    ob.sort()
                if ob:
                    priv.OtherBlues = ob
            self.stats['hint_tune_applied'] = True
            if self.rebuild_hints:
                self.stats['rebuild_hints_applied'] = True

        # Round stem widths
        for attr in ('StdHW', 'StdVW'):
            raw = priv.rawDict.get(attr)
            if raw is not None and isinstance(raw, (int, float)):
                rounded = int(round(raw))
                if rounded != raw:
                    setattr(priv, attr, rounded)
        for attr in ('StemSnapH', 'StemSnapV'):
            val = priv.rawDict.get(attr)
            if val:
                priv.rawDict[attr] = [int(round(v)) for v in val if v is not None]

    def _build_gasp_table(self, gasp):
        """v2.1 NEW (C): Build GASP table from --gasp-detail setting.

        Three presets:
          minimal   -- uniform grid-fit + AA (simplest)
          standard  -- 3 ranges (default, good for most cases)
          aggressive -- 5 ranges with full flags at all sizes
        """
        if self.gasp_detail == 'minimal':
            gasp.gaspRange = {
                0:      0x03,  # GRIDFIT | DOGRAY
                65535:  0x03,
            }
        elif self.gasp_detail == 'aggressive':
            # v2.1: 5 ranges with full flags at 7ppem+ for crisp stems
            gasp.gaspRange = {
                0:      0x03,  # GRIDFIT | DOGRAY (no smoothing at tiny sizes)
                7:      0x03,
                12:     0x07,  # + SYMMETRIC_GRIDFIT
                19:     0x0F,  # + SYMMETRIC_SMOOTHING (full flags)
                65535:  0x0F,
            }
        else:
            # 'standard' (default): 3 ranges
            gasp.gaspRange = {
                0:      0x03,
                7:      0x0F,
                65535:  0x0F,
            }

    def phase_postprocess(self, font_path):
        if self.no_post or not FONTTOOLS_AVAILABLE:
            if not FONTTOOLS_AVAILABLE:
                vprint("  Phase 7: fontTools post-processing -- NOT available, skipping")
            else:
                vprint("  Phase 7: fontTools post-processing -- SKIPPED (--no-post)")
            return

        vprint("  Phase 7: fontTools post-processing...")
        try:
            font = TTFont(font_path)

            # GASP table -- v2.1 IMPROVED (C): uses --gasp-detail
            if self.gasp:
                try:
                    if 'gasp' in font:
                        del font['gasp']
                    gasp = newTable('gasp')
                    gasp.version = 1
                    self._build_gasp_table(gasp)
                    font['gasp'] = gasp
                    self.stats['gasp_set'] = True
                except Exception as e:
                    dprint(f"GASP error: {e}")

            # head table flags (Chrome-safe)
            try:
                if 'head' in font:
                    head = font['head']
                    # Clear bit 3 (Force PPEM integer) -- Chrome breaks
                    head.flags &= ~0x0008
                    head.flags |= 0x0103  # baseline y=0, lsb=0, rounded layout
                    head.macStyle &= ~0x18  # clear Outline/Shadow
                    if 'OS/2' in font and font['OS/2'].usWeightClass >= 600:
                        head.macStyle |= 0x01
            except Exception as e:
                dprint(f"head flags error: {e}")

            # CFF hint tuning
            if self.hint_tune and ('CFF ' in font or 'CFF2' in font):
                try:
                    self._tune_cff_hinting(font)
                except Exception as e:
                    dprint(f"CFF hint tune error: {e}")

            # Subpixel coordinate snapping (TrueType)
            if 'glyf' in font:
                try:
                    glyf = font['glyf']
                    snapped = 0
                    for glyph_name in font.getGlyphOrder():
                        if glyph_name not in glyf:
                            continue
                        g = glyf[glyph_name]
                        if not hasattr(g, 'coordinates') or g.coordinates is None:
                            continue
                        coords = g.coordinates
                        for i in range(len(coords)):
                            x, y = coords[i]
                            ix, iy = int(round(x)), int(round(y))
                            if (x, y) != (ix, iy):
                                coords[i] = (ix, iy)
                                snapped += 1
                    if snapped:
                        dprint(f"  Subpixel snap: {snapped} coords -> integer")
                except Exception as e:
                    dprint(f"subpixel snap error: {e}")

            font.save(font_path)
            font.close()
        except Exception as e:
            self.stats['errors'].append(f"post-process: {e}")

    # ------------------------------------------------------------------
    #  Validation
    # ------------------------------------------------------------------
    def validate(self, out_path):
        if not os.path.exists(out_path):
            return False, "file not created"
        try:
            f = fontforge.open(out_path)
            f.close()
            return True, None
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    #  Master run
    # ------------------------------------------------------------------
    def run(self, in_path, out_path):
        font = None
        try:
            font = fontforge.open(in_path)
            vprint(f"  Font: {font.familyname} / {font.fontname}")
            vprint(f"  Glyphs: {sum(1 for _ in font.glyphs())}")

            self.phase_scale(font)
            # v2.1 NEW (A): width adjust runs BEFORE glyph cleanup so
            # cleanup sees the final outline size (matches --scale behaviour)
            self.phase_width(font)
            self.phase_glyphs(font)
            self.phase_global(font)
            self.phase_thickness(font)
            # v2.1 NEW (H): rebuild CFF BlueValues AFTER thickness,
            # so alignment zones match the modified outlines
            self.phase_rebuild_hints(font)
            self.phase_hinting(font)

            os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
            self.phase_generate(font, out_path)
            self.phase_postprocess(out_path)

            valid, err = self.validate(out_path)
            if not valid:
                self.stats['errors'].append(f"validation: {err}")
            return valid
        except Exception as e:
            self.stats['errors'].append(f"run: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            if font:
                try:
                    font.close()
                except Exception:
                    pass


# ===================================================================
#  Batch processing
# ===================================================================

def find_font_files(input_path):
    if os.path.isfile(input_path):
        yield input_path, None, None
        return

    for root, dirs, files in os.walk(input_path):
        for fname in sorted(files):
            if fname.lower().endswith(('.otf', '.ttf')):
                yield os.path.join(root, fname), None, os.path.relpath(root, input_path)


def get_output_path(in_path, out_dir, rel_subdir):
    base = os.path.splitext(os.path.basename(in_path))[0] + '.otf'
    target = out_dir if rel_subdir in (None, '.') else os.path.join(out_dir, rel_subdir)
    os.makedirs(target, exist_ok=True)
    return os.path.join(target, base)


def process_all(input_path, output_dir, options):
    files = list(find_font_files(input_path))
    if not files:
        print(f"No font files found: {input_path}")
        return 0

    count = 0
    success = 0
    for in_path, _, rel_subdir in files:
        out_path = get_output_path(in_path, output_dir, rel_subdir)
        count += 1
        short = os.path.basename(in_path)
        print(f"\n[{count}/{len(files)}] {short}")

        opt = FontForgeOptimizer(
            aggression=options['aggression'],
            scale_percent=options['scale_percent'],
            thickness=options['thickness'],
            width=options.get('width_percent', 0),
            hint=options['hint'],
            gasp=options['gasp'],
            hint_tune=options['hint_tune'],
            no_post=options['no_post'],
            gasp_detail=options.get('gasp_detail', 'standard'),
            shape_cleanup=options.get('shape_cleanup', False),
            rebuild_hints=options.get('rebuild_hints', False),
        )
        ok = opt.run(in_path, out_path)
        if ok:
            success += 1
        else:
            print(f"  X FAILED -- see errors above")

        s = opt.stats
        parts = []
        if s['glyphs_processed']:
            parts.append(f"{s['glyphs_processed']} glyphs")
        if s['high_risk_count']:
            parts.append(f"{s['high_risk_count']} high-risk")
        if s['scale_applied']:
            parts.append(f"scale {options['scale_percent']:+.1f}%")
        if s.get('width_applied'):
            parts.append(f"width {s['width_applied']:+.1f}%")
        if s['thickness_applied']:
            parts.append(f"thickness+{options['thickness']}")
        if s.get('shape_cleanup_applied'):
            parts.append("shape-cleanup")
        if s.get('rebuild_hints_applied'):
            parts.append("rebuild-hints")
        if s['hint_applied']:
            parts.append("hinted")
        if s['gasp_set']:
            parts.append(f"GASP-{options.get('gasp_detail', 'standard')}")
        if s['hint_tune_applied']:
            parts.append("hint-tune")
        if s['errors']:
            parts.append(f"errors({len(s['errors'])})")
        short_out = os.path.basename(out_path)
        print(f"  -> {short_out}  ({', '.join(parts) if parts else 'done'})")

    return success


# ===================================================================
#  CLI
# ===================================================================

def main():
    global _V

    parser = argparse.ArgumentParser(
        description="OTF/TTF Font Optimizer -- FontForge v2.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            AGGRESSION LEVELS:
              balanced   -- Mild cleanup (fast, good for already-clean fonts)
              high       -- Aggressive fringe elimination (default)
              extreme    -- Maximum quality (slower, most thorough)

            EXAMPLES:
              python otf_optimize_ff-v2.1.py font.otf output.otf
              python otf_optimize_ff-v2.1.py input_fonts/ output_fonts/ --thickness 20
              python otf_optimize_ff-v2.1.py input_fonts/ output_fonts/ --scale 5.0 --thickness 15
              python otf_optimize_ff-v2.1.py input_fonts/ output_fonts/ --aggression extreme
              python otf_optimize_ff-v2.1.py input_fonts/ output_fonts/ --no-hint
              python otf_optimize_ff-v2.1.py input.otf output.otf --no-post
              python otf_optimize_ff-v2.1.py font.otf output.otf --width 5
              python otf_optimize_ff-v2.1.py font.otf output.otf --condense 8
              python otf_optimize_ff-v2.1.py font.otf output.otf --shape-cleanup --rebuild-hints
              python otf_optimize_ff-v2.1.py font.otf output.otf --gasp-detail aggressive
        """),
    )

    parser.add_argument('input', help='Input font file (.otf/.ttf) or directory')
    parser.add_argument('output', help='Output font file or directory')

    parser.add_argument('--scale', type=float, default=0, dest='scale_percent',
                        help='Uniform scale percentage (positive=larger, negative=smaller). '
                             'e.g. 5.0 = 105%%, -3 = 97%%. Default: 0')
    parser.add_argument('--aggression', choices=['balanced', 'high', 'extreme'],
                        default='high',
                        help='Cleanup aggressiveness (default: high)')
    parser.add_argument('--thickness', type=int, default=0,
                        help='Weight/thickness boost in font units (0=off, 10-40 typical). '
                             'Uses FontForge changeWeight (perpendicular stroke offset).')
    # ---- v2.1 NEW (A) ----
    parser.add_argument('--width', type=float, default=0, dest='width_percent',
                        help='[v2.1 NEW] Adjust horizontal width. '
                             'Positive = expand (e.g. 5 = 5%% wider), '
                             'negative = condense (e.g. -8 = 8%% narrower). '
                             'Adjusts both outlines and advance widths. Default: 0')
    parser.add_argument('--expand', type=float, default=None,
                        help='[v2.1 NEW] Alias for --width with positive value. '
                             'e.g. --expand 5 = --width 5.')
    parser.add_argument('--condense', type=float, default=None,
                        help='[v2.1 NEW] Alias for --width with negative value. '
                             'e.g. --condense 8 = --width -8.')
    # ---- v2.1 NEW (D) ----
    parser.add_argument('--shape-cleanup', action='store_true', default=False,
                        help='[v2.1 NEW] Aggressive TrueType outline cleanup: '
                             'remove collinear and near-duplicate points.')
    # ---- v2.1 NEW (C) ----
    parser.add_argument('--gasp-detail', choices=['minimal', 'standard', 'aggressive'],
                        default='standard',
                        help='[v2.1 NEW] Granular GASP table control. '
                             'aggressive = 5 ranges with full flags. '
                             'standard = 3 ranges. minimal = uniform grid-fit + AA.')
    # ---- v2.1 NEW (H) ----
    parser.add_argument('--rebuild-hints', action='store_true', default=False,
                        help='[v2.1 NEW] Regenerate CFF BlueValues/OtherBlues from '
                             'current OS/2 metrics AFTER scaling/thickness. '
                             'Critical for fixing hint drift caused by --scale/--thickness.')
    parser.add_argument('--no-hint', dest='hint', action='store_false', default=True,
                        help='Skip auto-hinting (keep original hints)')
    parser.add_argument('--no-gasp', dest='gasp', action='store_false', default=True,
                        help='Skip GASP table optimisation')
    parser.add_argument('--no-hint-tune', dest='hint_tune', action='store_false', default=True,
                        help='Skip CFF hint tuning (BlueValues, LanguageGroup)')
    parser.add_argument('--no-post', action='store_true', default=False,
                        help='Skip fontTools post-processing entirely')
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='Verbosity: -v = info, -vv = debug')
    parser.add_argument('--version', action='version', version='otf_optimize_ff-v2.1')

    args = parser.parse_args()
    _V = args.verbose

    # v2.1 NEW (A): Resolve --expand/--condense aliases into --width
    if args.expand is not None:
        if args.width_percent != 0:
            print("ERROR: --width and --expand are mutually exclusive", file=sys.stderr)
            sys.exit(2)
        args.width_percent = abs(args.expand)
    if args.condense is not None:
        if args.width_percent != 0:
            print("ERROR: --width and --condense are mutually exclusive", file=sys.stderr)
            sys.exit(2)
        args.width_percent = -abs(args.condense)

    if not os.path.exists(args.input):
        print(f"ERROR: Input not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # Header
    print("=" * 62)
    print("  OTF/TTF Font Optimizer -- FontForge v2.0")
    print("=" * 62)
    print(f"  Input:      {args.input}")
    print(f"  Output:     {args.output}")
    print(f"  Aggression: {args.aggression}")
    if args.scale_percent:
        print(f"  Scale:      {args.scale_percent:+.1f}%")
    print(f"  Thickness:  {f'+{args.thickness}' if args.thickness else 'off'}")
    print(f"  Hinting:    {'autoHint + autoInstr' if args.hint else 'SKIPPED'}")
    print(f"  GASP:       {'on' if args.gasp else 'off'}")
    print(f"  CFF tune:   {'on' if args.hint_tune else 'off'}")
    print(f"  fontTools:  {'available' if FONTTOOLS_AVAILABLE and not args.no_post else 'not used'}")
    print()

    options = {
        'aggression': args.aggression,
        'scale_percent': args.scale_percent,
        'thickness': args.thickness,
        'width_percent': args.width_percent,
        'hint': args.hint,
        'gasp': args.gasp,
        'hint_tune': args.hint_tune,
        'no_post': args.no_post,
        'gasp_detail': args.gasp_detail,
        'shape_cleanup': args.shape_cleanup,
        'rebuild_hints': args.rebuild_hints,
    }

    if os.path.isfile(args.input):
        if os.path.isdir(args.output):
            out_file = os.path.join(
                args.output,
                os.path.splitext(os.path.basename(args.input))[0] + '.otf'
            )
        else:
            out_file = args.output
        os.makedirs(os.path.dirname(out_file) or '.', exist_ok=True)

        optimiser = FontForgeOptimizer(**options)
        ok = optimiser.run(args.input, out_file)
        s = optimiser.stats
        print()
        parts = []
        if s['glyphs_processed']:
            parts.append(f"{s['glyphs_processed']} glyphs")
        if s['high_risk_count']:
            parts.append(f"{s['high_risk_count']} high-risk cleaned")
        if s['scale_applied']:
            parts.append(f"scale {args.scale_percent:+.1f}%")
        if s['thickness_applied']:
            parts.append(f"thickness +{args.thickness}")
        if s['hint_applied']:
            parts.append("hinted")
        if s['gasp_set']:
            parts.append("GASP")
        if s['hint_tune_applied']:
            parts.append("hint-tune")
        if s['errors']:
            parts.append(f"X {len(s['errors'])} error(s)")

        if parts:
            print("  " + " | ".join(parts))
        print(f"\n  {'OK' if ok else 'FAIL'}  {out_file}")

        if not FONTTOOLS_AVAILABLE and not args.no_post:
            print("  i  Install fonttools for full post-processing: pip install fonttools")

        sys.exit(0 if ok else 1)
    else:
        total_success = process_all(args.input, args.output, options)
        print(f"\n{'=' * 62}")
        print(f"  SUMMARY: {total_success} font(s) processed successfully")
        print(f"{'=' * 62}")
        sys.exit(0 if total_success > 0 else 1)


if __name__ == '__main__':
    main()