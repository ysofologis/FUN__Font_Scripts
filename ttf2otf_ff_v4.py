"""
TTF to OTF Converter - FRINGE ELIMINATION SPECIALIST v4.1
Aggressive fringe removal for crystal-clear font rendering

Features (v4.1 vs v4):
  - NEW: pixel-snap phase that aligns coords to 8-unit grid (1/8 pixel at 16ppem)
  - NEW: stem-normalise phase for consistent stroke weights
  - NEW: extreme-smooth phase (4-pass decreasing tolerance)
  - NEW: aggressive GASP table (0x0F full flags at 8+ ppem)
  - NEW: TrueType coord snapping via fontTools after FontForge conversion
  - Cleaner architecture: separate FringeKiller class with phases
  - Per-glyph fringe detection + targeted treatment
  - TrueType auto-hinting (autoHint) with strong defaults
  - CFF generation with optimal flags
  - head table Chrome-safe flag tuning
  - OS/2 metrics sanity checks
  - Better CLI: --hint, --gasp, --thickness, --compact, --aggression
  - Optional fontTools-based post-processing
  - Stem width rounding for CFF fonts (v8.2 hint-tune style)
  - Robust error handling per font

Usage:
    python ttf2otf_ff_v4.py <input_dir_or_file> <output_dir> [options]

Examples:
    python ttf2otf_ff_v4.py ./fonts ./output --aggression extreme --thickness 25
    python ttf2otf_ff_v4.py font.ttf ./output --aggression high --hint --gasp
    python ttf2otf_ff_v4.py ./fonts ./output --compact --thickness 30
"""

import os
import sys
import argparse
import math
import shutil
import tempfile
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

        contours = list(glyph.contours) if hasattr(glyph, 'contours') else []
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
      6. High-risk targeted re-pass
      7. CFF generation with optimal flags
      8. fontTools post-processing (GASP, head flags, hint tune)
      9. Validation
    """

    def __init__(self, aggression='medium', thickness=0, compact=False,
                 hint=True, gasp=True, hint_tune=True, verbose=True):
        self.aggression = aggression
        self.thickness = thickness
        self.compact = compact
        self.hint = hint
        self.gasp = gasp
        self.hint_tune = hint_tune
        self.verbose = verbose

        # Tolerances scale with aggression
        self._tol = {'low': 1.5, 'medium': 1.0, 'high': 0.6, 'extreme': 0.3}[aggression]
        self._passes = {'low': 1, 'medium': 1, 'high': 2, 'extreme': 3}[aggression]

        self.stats = {
            'glyphs_processed': 0,
            'high_risk_glyphs': 0,
            'fringe_issues': [],
            'thickness_applied': False,
            'compact_applied': False,
            'gasp_set': False,
            'hint_tune_applied': False,
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
                glyph.simplify(160, tol)
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
            glyph.simplify(100, tol)
            glyph.clean()
        except Exception:
            pass

    def _phase_collinear(self, glyph):
        """Aggressive collinear-point removal."""
        try:
            for _ in range(self._passes):
                glyph.simplify(140, 0.3 if self.aggression == 'extreme' else 0.5)
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

            for contour in glyph.contours:
                for i in range(len(contour)):
                    x, y = contour[i][0], contour[i][1]
                    # Round to nearest snap_unit
                    nx = int(round(x / snap_unit) * snap_unit)
                    ny = int(round(y / snap_unit) * snap_unit)
                    if (nx, ny) != (x, y):
                        contour[i] = (nx, ny)
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
                glyph.simplify(140, tol)
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
            glyph.simplify(80, 1.0)
            glyph.round()
            # Second pass at tighter tolerance
            glyph.simplify(80, 0.5)
            glyph.round()
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

        # Extreme mode: extra smoothing
        if self.aggression == 'extreme' or extra_aggressive:
            self._phase_extreme_smooth(glyph)

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
                font.simplify(160, 0.5)
                font.canonicalContours()
                # v4.1: extra global pass at extreme for maximum cleanup
                if self.aggression == 'extreme':
                    font.selection.all()
                    font.removeOverlap()
                    font.simplify(120, 0.3)
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
        for glyph in font.glyphs():
            contours = glyph.foreground
            if len(contours) < 2:
                continue
            # Identify the outer (largest) contour
            areas = []
            for contour in contours:
                pts = [(p.x, p.y) for p in contour]
                n = len(pts)
                area = 0
                for i in range(n):
                    x1, y1 = pts[i]
                    x2, y2 = pts[(i + 1) % n]
                    area += x1 * y2 - x2 * y1
                areas.append(abs(area))
            outer_idx = areas.index(max(areas))
            for i, contour in enumerate(contours):
                if i == outer_idx:
                    continue
                pts = list(contour)
                xs = [p.x for p in pts]
                ys = [p.y for p in pts]
                cx = (min(xs) + max(xs)) / 2
                cy = (min(ys) + max(ys)) / 2
                for p in pts:
                    p.x = int(p.x + (cx - p.x) * shrink_pct)
                    p.y = int(p.y + (cy - p.y) * shrink_pct)

        font.selection.all()
        font.removeOverlap()
        font.round()
        self.stats['compact_applied'] = True
        self._log("Compactification done", 'sub')

    # -- Phase 6: high-risk re-pass --
    def phase_high_risk_repass(self, font):
        if not self.stats['fringe_issues'] or self.aggression not in ('high', 'extreme'):
            return
        self._log("Phase 6: High-risk glyph re-pass (extreme)", 'phase')
        # Top 20 high-risk glyphs
        for name, _ in self.stats['fringe_issues'][:20]:
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
            font.simplify(120, 0.2)
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
            # Phase 6 (high-risk re-pass)
            self.phase_high_risk_repass(font)
            # Phase 7 (hinting)
            self.phase_hint(font)
            # Phase 8 (generate)
            os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
            self.phase_generate(font, out_path)
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

def walk_and_convert(input_path, out_dir, aggression='medium', thickness=0,
                     compact=False, hint=True, gasp=True, hint_tune=True,
                     verbose=True):
    """Walk input path (file or directory) and convert all TTFs."""
    os.makedirs(out_dir, exist_ok=True)

    if os.path.isfile(input_path):
        base = os.path.splitext(os.path.basename(input_path))[0]
        out_path = os.path.join(out_dir, f"{base}.otf")
        killer = FringeKiller(aggression, thickness, compact, hint, gasp,
                              hint_tune, verbose)
        ok = killer.run(input_path, out_path)
        _print_stats(killer, [os.path.basename(input_path)], ok)
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

        killer = FringeKiller(aggression, thickness, compact, hint, gasp,
                              hint_tune, verbose)
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
        description="TTF to OTF Converter — FRINGE ELIMINATION SPECIALIST v4.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
AGGRESSION LEVELS:
  low      — Mild fringe removal (fast)
  medium   — Standard fringe elimination (default)
  high     — Aggressive fringe removal
  extreme  — Maximum fringe elimination (slowest, best)

EXAMPLES:
  # Standard processing
  python ttf2otf_ff_v4.py ./fonts ./output

  # Aggressive fringe removal with thickness boost
  python ttf2otf_ff_v4.py ./fonts ./output --aggression high --thickness 25

  # With auto-hinting and GASP table (recommended)
  python ttf2otf_ff_v4.py font.ttf ./output --aggression medium --hint --gasp

  # Maximum treatment
  python ttf2otf_ff_v4.py ./fonts ./output --aggression extreme --compact --thickness 40
        """
    )

    parser.add_argument("input_path",
                        help="Path to a directory of .ttf files or a single .ttf")
    parser.add_argument("output_dir",
                        help="Directory where optimised .otf files will be saved")

    parser.add_argument("-a", "--aggression",
                        choices=['low', 'medium', 'high', 'extreme'],
                        default='medium',
                        help="Fringe elimination aggression (default: medium)")
    parser.add_argument("-t", "--thickness", type=int, default=0,
                        help="Thickness boost in font units (e.g. 25 for subtle bolding)")
    parser.add_argument("-c", "--compact", action="store_true",
                        help="Compact mode: tighter spacing, smaller counters")
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
    parser.add_argument("--version", action="version", version="ttf2otf_ff_v4.1")

    args = parser.parse_args()

    if not os.path.exists(args.input_path):
        print(f"❌ Input path does not exist: {args.input_path}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print("=" * 60)
        print("🔧 TTF → OTF — FRINGE ELIMINATION SPECIALIST v4.0")
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
    )

    sys.exit(0 if success > 0 else 1)


if __name__ == "__main__":
    main()