#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script — FontForge v2.0

A best-quality font optimizer built on FontForge's powerful contour engine,
with fontTools-based post-processing for GASP, head flags, and CFF hint tuning.

What makes this different from fonttools-only scripts:
  - FontForge's simplify() and removeOverlap() handle complex shapes better
  - FontForge's autoHint()/autoInstr() produces TrueType instructions from scratch
  - FontForge's changeWeight() modifies outlines intelligently
  - Multi-pass per-glyph smoothing eliminates subpixel fringes at the source
  - FontForge's round()/canonicalContours() produce cleaner outlines

Pipeline:
  1. Optional uniform scaling (--scale, runs BEFORE cleanup so cleaner sees
     the final outline size)
  2. Load font -> per-glyph analysis
  3. Glyph-level cleanup (overlap remove, simplify, correctDirection, round)
  4. Global cleanup on all glyphs
  5. Optional thickness / weight boost
  6. Auto-hinting (autoHint + autoInstr) for TrueType
  7. Generate OTF with optimal flags
  8. fontTools post-processing (GASP, head flags, CFF hint tuning, stem rounding,
     subpixel snap)

Requirements:
  - fontforge (system package: pacman -S fontforge / apt install fontforge)
  - fonttools (optional, for post-processing: pip install fonttools)

Usage:
  python otf_optimize_ff-v2.0.py input.otf output.otf
  python otf_optimize_ff-v2.0.py input_fonts/ output_fonts/
  python otf_optimize_ff-v2.0.py input.otf output.otf --thickness 20
  python otf_optimize_ff-v2.0.py input_fonts/ output_fonts/ --aggression high
  python otf_optimize_ff-v2.0.py input.otf output.otf --scale 5.0 --thickness 15
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


def _run_glyph_pipeline(glyph, passes=1, snap=True):
    for _ in range(passes):
        _phase_remove_overlap(glyph, passes=1)
        _phase_simplify(glyph, passes=1, tol=1.0)
        _phase_directions(glyph)
        _phase_clean(glyph, tol=0.5)
    if snap:
        _phase_pixel_snap(glyph)


# ===================================================================
#  FontForgeOptimizer
# ===================================================================

class FontForgeOptimizer:

    def __init__(self, aggression='high', scale_percent=0, thickness=0,
                 hint=True, gasp=True, hint_tune=True, no_post=False):
        self.aggression = aggression
        self.scale_percent = scale_percent
        self.thickness = thickness
        self.hint = hint
        self.gasp = gasp
        self.hint_tune = hint_tune
        self.no_post = no_post

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
            'thickness_applied': False,
            'hint_applied': False,
            'gasp_set': False,
            'hint_tune_applied': False,
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
        _run_glyph_pipeline(glyph, passes=passes, snap=True)

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
            self.stats['hint_tune_applied'] = True

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

            # GASP table
            if self.gasp:
                try:
                    if 'gasp' in font:
                        del font['gasp']
                    gasp = newTable('gasp')
                    gasp.version = 1
                    gasp.gaspRange = {
                        0:     0x03,
                        7:     0x0F,
                        65535: 0x0F,
                    }
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
            self.phase_glyphs(font)
            self.phase_global(font)
            self.phase_thickness(font)
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
            hint=options['hint'],
            gasp=options['gasp'],
            hint_tune=options['hint_tune'],
            no_post=options['no_post'],
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
        if s['thickness_applied']:
            parts.append(f"thickness+{options['thickness']}")
        if s['hint_applied']:
            parts.append("hinted")
        if s['gasp_set']:
            parts.append("GASP")
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
        description="OTF/TTF Font Optimizer -- FontForge v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            AGGRESSION LEVELS:
              balanced   -- Mild cleanup (fast, good for already-clean fonts)
              high       -- Aggressive fringe elimination (default)
              extreme    -- Maximum quality (slower, most thorough)

            EXAMPLES:
              python otf_optimize_ff-v2.0.py font.otf output.otf
              python otf_optimize_ff-v2.0.py input_fonts/ output_fonts/ --thickness 20
              python otf_optimize_ff-v2.0.py input_fonts/ output_fonts/ --scale 5.0 --thickness 15
              python otf_optimize_ff-v2.0.py input_fonts/ output_fonts/ --aggression extreme
              python otf_optimize_ff-v2.0.py input_fonts/ output_fonts/ --no-hint
              python otf_optimize_ff-v2.0.py input.otf output.otf --no-post
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
                        help='Weight/thickness boost in font units (0=off, 10-40 typical)')
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
    parser.add_argument('--version', action='version', version='otf_optimize_ff-v2.0')

    args = parser.parse_args()
    _V = args.verbose

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
        'hint': args.hint,
        'gasp': args.gasp,
        'hint_tune': args.hint_tune,
        'no_post': args.no_post,
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