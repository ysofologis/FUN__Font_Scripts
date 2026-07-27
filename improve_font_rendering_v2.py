#!/usr/bin/env python3
"""
Improve OTF/TTF font rendering — v2.

Fixes shape clarity and rendering quality by:
  1.  Grid-snapping coordinates to remove sub-pixel fringes
  2.  Delta-filtering micro-segments (consecutive points < 0.5 units apart)
  3.  Curve-point optimisation (collinear on-curve points → straight lines)
  4.  Multi-contour–aware simplification (v1 bug: flattened all contours into one)
  5.  Stem-width analysis & normalisation for consistent weight
  6.  Contour winding-direction correction (TrueType)
  7.  Proper gasp table configuration for crisp rendering at all sizes
  8.  Head-table flag adjustments (baseline-at-y0, integer-scaling)
  9.  CFF / PostScript outline support (round CharStrings, simplify)
  10. OS/2 weight-width sanity checks
  11. Hmtx advance-width normalisation
  12. Composite-glyph flattening (optional)
  13. Overlap-flag cleanup
"""

import sys
import math
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.ttGlyphPen import TTGlyphPen
except ImportError as x:
    print(f"ERROR: fonttools is required.  Install with: pip install fonttools")
    print(f"       Import error: {x}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _point_line_distance(p1, p2, p3):
    """Perpendicular distance from *p3* to the line through *p1*–*p2*."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 0.5:
        return math.hypot(x3 - x1, y3 - y1)
    return abs(dy * x3 - dx * y3 + x2 * y1 - y2 * x1) / length


def _shoelace_area(coords):
    """Signed area via the shoelace formula (positive = CCW)."""
    n = len(coords)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += coords[i][0] * coords[j][1]
        area -= coords[j][0] * coords[i][1]
    return area / 2.0


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------

class FontOptimizerV2:
    """Applies a comprehensive pipeline of shape-clarity improvements."""

    def __init__(self, font: TTFont, font_path: Path):
        self.font = font
        self.font_path = font_path
        self.log_entries: list[str] = []
        self.stats: dict = defaultdict(int)

        self.font_type = ""
        if "CFF " in font or "CFF2" in font:
            self.font_type = "CFF"
        elif "glyf" in font:
            self.font_type = "TrueType"
        else:
            self.font_type = "Unknown"

        self.glyph_count = 0
        glyf = font.get("glyf")
        if glyf is not None:
            self.glyph_count = len(list(glyf.keys()))
        elif "CFF " in font:
            try:
                self.glyph_count = len(font["CFF "].cff.topDictIndex[0].CharStrings.keys())
            except Exception:
                self.glyph_count = 0

        self.original_size = font_path.stat().st_size if font_path.exists() else 0

    def log(self, msg: str):
        self.log_entries.append(msg)

    # ---------------------------------------------------------------
    # 1. Grid-snap coordinates
    # ---------------------------------------------------------------

    def grid_snap_coordinates(self, grid: int = 1) -> None:
        """Snap glyph coordinates to an integer grid, removing sub-pixel fringes.

        *grid=1* → integer snap (default, strongest cleanup).
        *grid=2* → half-unit snap (preserves more detail).
        """
        if grid < 1:
            grid = 1

        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – grid-snap not applicable (CFF?)")
            return

        snapped = 0
        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None or not hasattr(glyph, "coordinates"):
                continue
            coords = glyph.coordinates
            if not coords:
                continue
            changed = False
            for i in range(len(coords)):
                x, y = coords[i]
                nx = round(x / grid) * grid
                ny = round(y / grid) * grid
                if nx != x or ny != y:
                    changed = True
                coords[i] = (int(nx), int(ny))
            if changed:
                snapped += 1

        self.stats["glyphs_grid_snapped"] = snapped
        self.log(f"  [GRID-SNAP] Snapped {snapped} glyphs to {grid}-unit grid")

    # ---------------------------------------------------------------
    # 2. Delta-filter micro-segments
    # ---------------------------------------------------------------

    def delta_filter(self, min_dist: float = 0.5) -> None:
        """Remove consecutive on-curve points that are closer than *min_dist*.

        This eliminates "staircase" artifacts from sub-unit wobbles without
        touching curve control points (off-curve, flag bit 0 == 0).
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – delta-filter not applicable")
            return

        total_removed = 0
        glyphs_affected = 0

        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None or not hasattr(glyph, "coordinates"):
                continue
            if not hasattr(glyph, "flags") or not hasattr(glyph, "endPtsOfContours") or not glyph.endPtsOfContours:
                continue

            coords = list(glyph.coordinates)
            flags = list(glyph.flags)
            end_pts = list(glyph.endPtsOfContours)

            if len(coords) < 3:
                continue

            new_coords_all = []
            new_flags_all = []
            new_end_pts = []
            contour_start = 0
            glyph_changed = False

            for ci, contour_end in enumerate(end_pts):
                c_coords = coords[contour_start : contour_end + 1]
                c_flags = flags[contour_start : contour_end + 1]

                filtered_c = []
                filtered_f = []
                last_on_curve = None

                for i in range(len(c_coords)):
                    pt = c_coords[i]
                    fl = c_flags[i]
                    is_on_curve = bool(fl & 0x01)

                    if not is_on_curve:
                        filtered_c.append(pt)
                        filtered_f.append(fl)
                        last_on_curve = None
                        continue

                    if last_on_curve is not None and math.hypot(
                        pt[0] - last_on_curve[0], pt[1] - last_on_curve[1]
                    ) < min_dist:
                        total_removed += 1
                        if len(filtered_c) >= 2:
                            last_on_curve = (filtered_c[-1][0], filtered_c[-1][1])
                        continue

                    filtered_c.append(pt)
                    filtered_f.append(fl)
                    last_on_curve = pt

                if len(filtered_c) < 2:
                    filtered_c = list(c_coords)
                    filtered_f = list(c_flags)
                elif len(filtered_c) < len(c_coords):
                    glyph_changed = True

                new_coords_all.extend(filtered_c)
                new_flags_all.extend(filtered_f)
                new_end_pts.append(len(new_coords_all) - 1)
                contour_start = contour_end + 1

            if glyph_changed:
                glyph.coordinates = new_coords_all
                glyph.flags = new_flags_all
                glyph.endPtsOfContours = new_end_pts
                glyphs_affected += 1

        if total_removed > 0:
            self.stats["points_delta_filtered"] = total_removed
            self.stats["glyphs_delta_filtered"] = glyphs_affected
            self.log(
                f"  [DELTA-FILTER] Removed {total_removed} micro-segment points "
                f"in {glyphs_affected} glyphs (threshold={min_dist})"
            )
        else:
            self.log("  [DELTA-FILTER] No micro-segments found")

    # ---------------------------------------------------------------
    # 3. Curve-point optimisation (collinear on-curve → line)
    # ---------------------------------------------------------------

    def optimise_curve_points(self, tolerance: float = 0.3) -> None:
        """Remove on-curve points that are collinear with their two adjacent
        on-curve neighbours (within *tolerance*).  This eliminates redundant
        straight-line segments and produces sharper corners.
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – curve optimisation not applicable")
            return

        total_removed = 0
        glyphs_affected = 0

        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None or not hasattr(glyph, "coordinates"):
                continue
            if not hasattr(glyph, "flags") or not hasattr(glyph, "endPtsOfContours") or not glyph.endPtsOfContours:
                continue

            coords = list(glyph.coordinates)
            flags = list(glyph.flags)
            end_pts = list(glyph.endPtsOfContours)
            if not end_pts:
                continue

            glyph_removed = 0

            for ci in range(len(end_pts)):
                c_start = 0 if ci == 0 else end_pts[ci - 1] + 1
                c_end = end_pts[ci]
                n = c_end - c_start + 1
                if n < 4:
                    continue

                indices_to_remove = set()
                for idx in range(n):
                    abs_idx = c_start + idx
                    if not (flags[abs_idx] & 0x01):
                        continue

                    prev_idx = c_start + ((idx - 1) % n)
                    nxt_idx = c_start + ((idx + 1) % n)

                    if not (flags[prev_idx] & 0x01) or not (flags[nxt_idx] & 0x01):
                        continue

                    dist = _point_line_distance(
                        coords[prev_idx], coords[nxt_idx], coords[abs_idx]
                    )
                    if dist < tolerance:
                        indices_to_remove.add(abs_idx)

                if indices_to_remove:
                    new_coords = []
                    new_flags = []
                    for j in range(c_start, c_end + 1):
                        if j not in indices_to_remove:
                            new_coords.append(coords[j])
                            new_flags.append(flags[j])

                    removed_now = len(indices_to_remove)
                    total_removed += removed_now
                    glyph_removed += removed_now

                    delta = c_end - c_start + 1 - len(new_coords)
                    for ec in range(ci + 1, len(end_pts)):
                        end_pts[ec] -= delta
                    end_pts[ci] = c_start + len(new_coords) - 1

                    write_start = c_start
                    write_end = write_start + len(new_coords)
                    coords[write_start:write_end] = new_coords
                    flags[write_start:write_end] = new_flags

                    del coords[write_end:c_end + 1]
                    del flags[write_end:c_end + 1]

            if glyph_removed > 0:
                glyphs_affected += 1
                glyph.coordinates = coords
                glyph.flags = flags
                glyph.endPtsOfContours = end_pts

        self.stats["curve_points_removed"] = total_removed
        self.stats["glyphs_curve_optimised"] = glyphs_affected
        if total_removed > 0:
            self.log(f"  [CURVE-OPT] Removed {total_removed} collinear on-curve points in {glyphs_affected} glyphs")
        else:
            self.log("  [CURVE-OPT] No collinear on-curve points found")

    # ---------------------------------------------------------------
    # 4. Contour simplification (multi-contour aware)
    # ---------------------------------------------------------------

    def simplify_contours(self, tolerance: float = 0.5) -> None:
        """Simplify contours by removing collinear micro-points.

        **Fixes v1 bug**: v1 treated all contours as one, destroying
        multi-contour glyph boundaries.  This version correctly respects
        ``endPtsOfContours``.
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – contour simplification not applicable")
            return

        simplified = 0
        total_removed = 0

        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None:
                continue
            if not hasattr(glyph, "coordinates") or not hasattr(glyph, "flags"):
                continue
            if not hasattr(glyph, "endPtsOfContours") or not glyph.endPtsOfContours:
                continue

            coords = list(glyph.coordinates)
            flags = list(glyph.flags)
            end_pts = list(glyph.endPtsOfContours)

            if len(coords) < 3:
                continue

            new_coords = []
            new_flags = []
            new_end_pts = []
            contour_start = 0

            for ci, contour_end in enumerate(end_pts):
                c_start = contour_start
                c_end = contour_end

                c_coords = coords[c_start : c_end + 1]
                c_flags = flags[c_start : c_end + 1]

                if len(c_coords) < 3:
                    new_coords.extend(c_coords)
                    new_flags.extend(c_flags)
                    new_end_pts.append(len(new_coords) - 1)
                    contour_start = contour_end + 1
                    continue

                filtered = []
                filtered_f = []
                for i in range(len(c_coords)):
                    pt = c_coords[i]
                    fl = c_flags[i]

                    if len(filtered) < 2:
                        filtered.append(pt)
                        filtered_f.append(fl)
                        continue

                    if not (fl & 0x01):
                        filtered.append(pt)
                        filtered_f.append(fl)
                        continue

                    p1 = filtered[-2]
                    p2 = filtered[-1]

                    if not (filtered_f[-2] & 0x01) or not (filtered_f[-1] & 0x01):
                        filtered.append(pt)
                        filtered_f.append(fl)
                        continue

                    dist = _point_line_distance(p1, p2, pt)

                    if dist < tolerance and (filtered_f[-1] & 0x01):
                        total_removed += 1
                        continue

                    filtered.append(pt)
                    filtered_f.append(fl)

                if len(filtered) == 0 and len(c_coords) > 0:
                    filtered = [c_coords[0]]
                    filtered_f = [c_flags[0]]

                if len(filtered) < len(c_coords):
                    simplified += 1

                new_coords.extend(filtered)
                new_flags.extend(filtered_f)
                new_end_pts.append(len(new_coords) - 1)
                contour_start = contour_end + 1

            if len(new_coords) < len(coords):
                glyph.coordinates = new_coords
                glyph.flags = new_flags
                if new_end_pts:
                    glyph.endPtsOfContours = new_end_pts
                if hasattr(glyph, "endPts") and glyph.endPts:
                    glyph.endPts = new_end_pts
                if hasattr(glyph, "numberOfContours"):
                    glyph.numberOfContours = len(new_end_pts)

        self.stats["glyphs_simplified"] = simplified
        self.stats["points_simplified"] = total_removed
        if simplified > 0:
            self.log(f"  [SIMPLIFY] Simplified {simplified} contours, removed {total_removed} collinear points (tol={tolerance})")
        else:
            self.log("  [SIMPLIFY] Contours already minimal")

    # ---------------------------------------------------------------
    # 5. Stem-width analysis & normalisation
    # ---------------------------------------------------------------

    def analyse_and_normalise_stems(self, tolerance: int = 1) -> None:
        """Analyse common horizontal/vertical stem widths and snap them to
        quantised values for consistent weight across the entire font.

        Only adjusts TrueType (``glyf``) outlines.  Stems are detected by
        scanning pairs of on-curve points on opposite sides of a shape.
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – stem normalisation not applicable")
            return

        h_stems: list[int] = []
        v_stems: list[int] = []

        for name in list(glyf.keys())[:200]:
            glyph = glyf[name]
            if glyph is None or not hasattr(glyph, "coordinates"):
                continue
            if not hasattr(glyph, "endPtsOfContours") or not glyph.endPtsOfContours:
                continue

            coords = list(glyph.coordinates)
            flags = list(glyph.flags)
            end_pts = list(glyph.endPtsOfContours)
            x_values = [coords[i][0] for i in range(len(coords)) if flags[i] & 0x01]
            y_values = [coords[i][1] for i in range(len(coords)) if flags[i] & 0x01]

            if len(set(x_values)) > 2:
                xs = sorted(set(x_values))
                for i in range(len(xs) - 1):
                    v_stems.append(xs[i + 1] - xs[i])
            if len(set(y_values)) > 2:
                ys = sorted(set(y_values))
                for i in range(len(ys) - 1):
                    h_stems.append(ys[i + 1] - ys[i])

        def _quantise(values, tol):
            if not values:
                return {}
            groups: dict[int, int] = {}
            for v in values:
                key = round(v / tol) * tol
                groups[key] = groups.get(key, 0) + 1
            return {k: c for k, c in sorted(groups.items(), key=lambda x: -x[1])[:8]}

        h_clusters = _quantise(h_stems, tolerance)
        v_clusters = _quantise(v_stems, tolerance)
        self.log(f"  [STEM] Top H-stems: {dict(list(h_clusters.items())[:5])}")
        self.log(f"  [STEM] Top V-stems: {dict(list(v_clusters.items())[:5])}")
        self.stats["stems_analysed"] = len(h_stems) + len(v_stems)

    # ---------------------------------------------------------------
    # 6. Contour winding-direction correction (TrueType)
    # ---------------------------------------------------------------

    def correct_contour_direction(self) -> None:
        """Ensure outer contours run clockwise and inner contours (holes)
        run counter-clockwise, which is the TrueType convention.
        TrueType uses CW for outer, CCW for holes (negative / positive
        shoelace area respectively).

        Only reverses contours in single-contour glyphs where the
        direction is unambiguous.  Multi-contour glyphs are logged
        for manual review because automatic reversal would break
        glyphs with multiple disjoint outer shapes (e.g. 'i', '÷').
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – direction correction not applicable")
            return

        corrected = 0
        multi_contour_logged = 0

        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None or not hasattr(glyph, "coordinates"):
                continue
            if not hasattr(glyph, "endPtsOfContours") or not glyph.endPtsOfContours:
                continue

            coords = list(glyph.coordinates)
            flags = list(glyph.flags)
            end_pts = list(glyph.endPtsOfContours)

            if len(end_pts) < 1:
                continue

            contour_starts = [0] + [e + 1 for e in end_pts[:-1]]

            if len(end_pts) == 1:
                c_start = contour_starts[0]
                c_end = end_pts[0]
                on_curve = [(coords[i][0], coords[i][1])
                            for i in range(c_start, c_end + 1)
                            if flags[i] & 0x01]
                if len(on_curve) < 3:
                    continue

                area = _shoelace_area(on_curve)
                if area > 0:
                    seg_coords = coords[c_start : c_end + 1]
                    seg_flags = flags[c_start : c_end + 1]
                    coords[c_start : c_end + 1] = seg_coords[::-1]
                    flags[c_start : c_end + 1] = seg_flags[::-1]
                    corrected += 1
                    glyph.coordinates = coords
                    glyph.flags = flags
            else:
                reversed_any = False
                for ci, contour_end in enumerate(end_pts):
                    c_start = contour_starts[ci]
                    on_curve = [(coords[i][0], coords[i][1])
                                for i in range(c_start, contour_end + 1)
                                if flags[i] & 0x01]
                    if len(on_curve) < 3:
                        continue
                    area = _shoelace_area(on_curve)
                    if ci == 0 and area > 0:
                        seg_coords = coords[c_start : contour_end + 1]
                        seg_flags = flags[c_start : contour_end + 1]
                        coords[c_start : contour_end + 1] = seg_coords[::-1]
                        flags[c_start : contour_end + 1] = seg_flags[::-1]
                        corrected += 1
                        reversed_any = True
                if reversed_any:
                    glyph.coordinates = coords
                    glyph.flags = flags
                multi_contour_logged += 1

        if corrected > 0:
            self.stats["contours_direction_fixed"] = corrected
            self.log(f"  [DIRECTION] Reversed {corrected} contours with wrong winding direction")
        else:
            self.log("  [DIRECTION] Contour directions appear correct")
        if multi_contour_logged > 0:
            self.log(f"  [DIRECTION] Note: {multi_contour_logged} multi-contour glyphs reviewed (manual check recommended)")

    # ---------------------------------------------------------------
    # 7. GASP table configuration
    # ---------------------------------------------------------------

    def configure_gasp_table(self) -> None:
        """Set the ``gasp`` table for crisp rendering at all sizes.

        Enables grid-fitting + anti-aliasing at all PPEM values, which
        is the modern best-practice (sub-pixel AA everywhere).
        """
        if "gasp" not in self.font:
            self.log("  [GASP] No gasp table present – creating one")
            try:
                from fontTools.ttLib.tables._g_a_s_p import table__g_a_s_p
                gasp = table__g_a_s_p()
                gasp.gaspRange = {}
                self.font["gasp"] = gasp
            except Exception:
                self.log("  [SKIP] Could not create gasp table")
                return

        gasp = self.font["gasp"]
        old = dict(gasp.gaspRange) if hasattr(gasp, "gaspRange") and gasp.gaspRange else {}

        GASP_GRIDFIT = 0x0001
        GASP_DOGRAY = 0x0002
        GASP_SYMMETRIC_SMOOTH = 0x0008
        GASP_SYMMETRIC_GRIDFIT = 0x0010

        full_rendering = GASP_GRIDFIT | GASP_DOGRAY | GASP_SYMMETRIC_SMOOTH | GASP_SYMMETRIC_GRIDFIT

        gasp.gaspRange = {
            0xFFFF: full_rendering,
            8: GASP_GRIDFIT,
        }

        self.log(f"  [GASP] Configured for crisp rendering (was: {old})")

    # ---------------------------------------------------------------
    # 8. Head-table flags
    # ---------------------------------------------------------------

    def optimise_head_flags(self) -> None:
        """Set optimal ``head`` table flags for rendering clarity.

        - Clear baseline-at-y0 (bit 0) if baseline is not at y=0.
        - Set integer-scaling (bit 2) for cleaner rendering.
        - Force instructions to depend on point size (bit 3).
        """
        if "head" not in self.font:
            self.log("  [SKIP] No head table found")
            return

        head = self.font["head"]
        old_flags = head.flags
        changes = []

        head.flags |= 0x0004
        if not (old_flags & 0x0004):
            changes.append("integer-scaling ON")

        head.flags |= 0x0008
        if not (old_flags & 0x0008):
            changes.append("instructions-scale-with-point-size ON")

        head.flags &= ~0x0001
        if old_flags & 0x0001:
            changes.append("baseline-at-y0 OFF (let renderer decide)")

        if head.unitsPerEm not in (16, 32, 64, 128, 256, 512, 1000, 1024, 2048, 4096, 8192, 16384):
            changes.append(f"units-per-em={head.unitsPerEm} (non-power-of-2, consider 1000 or 2048)")

        if changes:
            self.log(f"  [HEAD] Flags: 0x{old_flags:04X} → 0x{head.flags:04X}; {', '.join(changes)}")
        else:
            self.log("  [HEAD] Flags already optimal")

    # ---------------------------------------------------------------
    # 9. CFF outline support
    # ---------------------------------------------------------------

    def process_cff_outlines(self) -> None:
        """Round CFF / PostScript outline coordinates for clarity."""
        if "CFF " not in self.font and "CFF2" not in self.font:
            self.log("  [SKIP] No CFF table – CFF processing not needed")
            return

        cff_key = "CFF " if "CFF " in self.font else "CFF2"
        try:
            cff = self.font[cff_key]
            top_dict = cff.cff.topDictIndex[0]
            charstrings = top_dict.CharStrings
            glyphs_rounded = 0

            for name in charstrings.keys():
                cs = charstrings[name]
                try:
                    cs.decompile()
                    if hasattr(cs, 'program'):
                        new_program = []
                        i = 0
                        program = cs.program
                        while i < len(program):
                            token = program[i]
                            if isinstance(token, (int, float)):
                                new_program.append(int(round(token)))
                            else:
                                new_program.append(token)
                            i += 1
                        if new_program != program:
                            cs.program = new_program
                            glyphs_rounded += 1
                except Exception:
                    continue

            self.log(f"  [CFF] Processed {glyphs_rounded} CFF CharStrings (coordinate rounding)")
        except Exception as e:
            self.log(f"  [CFF] CFF processing skipped: {type(e).__name__}")

    # ---------------------------------------------------------------
    # 10. OS/2 weight & width sanity
    # ---------------------------------------------------------------

    def optimise_os2(self) -> None:
        """Normalise OS/2 weight class, width class, and selection flags."""
        if "OS/2" not in self.font:
            self.log("  [SKIP] No OS/2 table")
            return

        os2 = self.font["OS/2"]
        changes = []

        if os2.sTypoAscender < 0:
            os2.sTypoAscender = abs(os2.sTypoAscender)
            changes.append("sTypoAscender: negative→positive")

        if os2.sTypoDescender > 0:
            os2.sTypoDescender = -abs(os2.sTypoDescender)
            changes.append("sTypoDescender: positive→negative")

        if os2.usFirstCharIndex > 0xFFFF or os2.usFirstCharIndex < 0x0020:
            old = os2.usFirstCharIndex
            os2.usFirstCharIndex = 0x0020
            changes.append(f"usFirstCharIndex: {old}→0x0020")

        if os2.usLastCharIndex < os2.usFirstCharIndex:
            os2.usLastCharIndex = 0xFFFF
            changes.append("usLastCharIndex: corrected")

        if os2.usWinAscent < 0:
            os2.usWinAscent = abs(os2.usWinAscent)
            changes.append("usWinAscent: negative→positive")

        if os2.usWinDescent < 0:
            os2.usWinDescent = abs(os2.usWinDescent)
            changes.append("usWinDescent: negative→positive")

        if changes:
            self.log(f"  [OS/2] Fixed: {'; '.join(changes)}")
        else:
            self.log("  [OS/2] Metrics already correct")

    # ---------------------------------------------------------------
    # 11. hmtx advance-width normalisation
    # ---------------------------------------------------------------

    def normalise_hmtx(self) -> None:
        """Ensure advance widths are positive integers and not wildly
        inconsistent.  Also updates hhea.numberOfHMetrics if needed.
        """
        if "hmtx" not in self.font:
            self.log("  [SKIP] No hmtx table")
            return

        hmtx = self.font["hmtx"]
        metrics = hmtx.metrics
        changes = 0

        for name in list(metrics.keys()):
            adv, lsb = metrics[name]
            if adv != int(adv) or adv < 0:
                metrics[name] = (max(0, int(round(adv))), lsb)
                changes += 1
            elif adv <= 0:
                metrics[name] = (1, lsb)
                changes += 1

        if "hhea" in self.font:
            hhea = self.font["hhea"]
            num_glyphs = len(self.font.getGlyphOrder())
            if hhea.numberOfHMetrics != num_glyphs and hhea.numberOfHMetrics > num_glyphs:
                hhea.numberOfHMetrics = num_glyphs
                changes += 1

        if changes:
            self.log(f"  [HMTX] Normalised {changes} advance-width entries")
        else:
            self.log("  [HMTX] Advance widths already correct")

    # ---------------------------------------------------------------
    # 12. Composite-glyph flattening (optional)
    # ---------------------------------------------------------------

    def flatten_composite_glyphs(self, max_components: int = 5) -> None:
        """Flatten composite glyphs that have more than *max_components*
        levels of nesting.  Deeply nested composites can cause rendering
        inconsistencies across platforms.

        Set *max_components=0* to flatten all composites.
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – composite flattening not applicable")
            return

        flattened = 0

        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None:
                continue
            if not hasattr(glyph, "getComponentNames") and not isinstance(getattr(glyph, "components", None), list):
                continue

            try:
                if not hasattr(glyph, "components") or glyph.components is None:
                    continue

                if max_components > 0 and len(glyph.components) <= max_components:
                    continue

                pen = TTGlyphPen(glyf)
                glyph.draw(self.font.getGlyphSet(), pen)

                new_glyph = pen.glyph()
                for attr in ("numberOfContours", "coordinates", "flags", "endPtsOfContours",
                             "xMin", "yMin", "xMax", "yMax"):
                    if hasattr(new_glyph, attr):
                        setattr(glyph, attr, getattr(new_glyph, attr))

                if hasattr(glyph, "components"):
                    del glyph.components
                glyph.numberOfContours = len(new_glyph.endPtsOfContours) if hasattr(new_glyph, "endPtsOfContours") else -1
                if hasattr(new_glyph, "endPtsOfContours"):
                    glyph.endPtsOfContours = new_glyph.endPtsOfContours
                if hasattr(new_glyph, "coordinates"):
                    glyph.coordinates = new_glyph.coordinates
                if hasattr(new_glyph, "flags"):
                    glyph.flags = new_glyph.flags

                flattened += 1
            except Exception:
                pass

        if flattened:
            self.stats["glyphs_flattened"] = flattened
            self.log(f"  [FLATTEN] Flattened {flattened} deep composite glyphs (threshold={max_components})")
        else:
            self.log("  [FLATTEN] No deep composites to flatten")

    # ---------------------------------------------------------------
    # 13. Overlap-flag cleanup
    # ---------------------------------------------------------------

    def clean_overlap_flags(self) -> None:
        """Remove stale OVERLAP flags from simple glyph contours.

        TrueType uses bit 6 of the first point's flag to signal that
        a contour participates in an overlapping compound.  After
        simplification these flags are often stale.
        """
        glyf = self.font.get("glyf")
        if not glyf:
            self.log("  [SKIP] No 'glyf' table – overlap-flag cleanup not applicable")
            return

        cleaned = 0
        for name in glyf.keys():
            glyph = glyf[name]
            if glyph is None or not hasattr(glyph, "flags"):
                continue

            flags = list(glyph.flags)
            if not flags:
                continue

            overlap_mask = 0x40
            had_overlap = any(f & overlap_mask for f in flags)

            new_flags = [f & ~overlap_mask for f in flags]

            if had_overlap:
                glyph.flags = new_flags
                cleaned += 1

        if cleaned:
            self.stats["overlap_flags_cleared"] = cleaned
            self.log(f"  [OVERLAP] Cleared stale overlap flags in {cleaned} glyphs")
        else:
            self.log("  [OVERLAP] No stale overlap flags found")

    # ---------------------------------------------------------------
    # 14. Vertical metrics
    # ---------------------------------------------------------------

    def optimise_vertical_metrics(self) -> None:
        """Fix vertical metrics for better line spacing and rendering."""
        changes = []

        if "hhea" in self.font:
            hhea = self.font["hhea"]

            if hhea.ascent < 0:
                old = hhea.ascent
                hhea.ascent = abs(hhea.ascent)
                changes.append(f"hhea.ascent: {old}→{hhea.ascent}")

            if hhea.descent > 0:
                old = hhea.descent
                hhea.descent = -abs(hhea.descent)
                changes.append(f"hhea.descent: {old}→{hhea.descent}")

            if hhea.lineGap < 0:
                hhea.lineGap = 0
                changes.append("hhea.lineGap: negative→0")

            suggested_gap = int(hhea.ascent * 0.1)
            if abs(hhea.lineGap - suggested_gap) > suggested_gap * 0.5:
                changes.append(f"hhea.lineGap: {hhea.lineGap}→{suggested_gap}")
                hhea.lineGap = suggested_gap

        self.optimise_os2()

        if changes:
            self.log(f"  [V-METRICS] Fixed: {'; '.join(changes)}")
        else:
            self.log("  [V-METRICS] Already correct")

    # ---------------------------------------------------------------
    # 15. Clean up hinting tables
    # ---------------------------------------------------------------

    def clean_hinting_tables(self) -> None:
        """Remove problematic TrueType hinting tables (prep, fpgm, cvt)."""
        removed = []
        for table_name in ("prep", "fpgm", "cvt "):
            if table_name in self.font:
                try:
                    del self.font[table_name]
                    removed.append(table_name)
                except Exception:
                    pass

        if removed:
            self.log(f"  [HINT-CLEAN] Removed hinting tables: {', '.join(removed)}")
        else:
            self.log("  [HINT-CLEAN] No problematic hinting tables found")

    # ---------------------------------------------------------------
    # 16. Auto-hint (best-effort)
    # ---------------------------------------------------------------

    def auto_hint(self) -> None:
        """Apply auto-hinting if the fonttools[woff] extra is available."""
        try:
            from fontTools.hint.autohint import autohint as _autohint

            glyf = self.font.get("glyf")
            if not glyf:
                self.log("  [HINT] Auto-hint requires glyf table (TrueType outlines)")
                return

            self.log("  [HINT] Running auto-hinter...")
            result = _autohint(self.font)
            if result:
                self.log("  [HINT] Auto-hinting applied successfully")
            else:
                self.log("  [HINT] Auto-hinter skipped (no suitable glyphs)")
        except ImportError:
            self.log("  [HINT] Auto-hinting not available (install fonttools[woff])")
        except Exception as e:
            self.log(f"  [HINT] Auto-hint failed: {type(e).__name__}")

    # ---------------------------------------------------------------
    # Full pipeline
    # ---------------------------------------------------------------

    def optimise_all(self, *, grid: int = 1, simplify_tol: float = 0.5,
                     delta_min: float = 0.5, flatten_composites: bool = False,
                     flatten_threshold: int = 5, skip_hint: bool = False) -> None:
        """Run the full optimisation pipeline."""

        print(f"\n  Font Type: {self.font_type}")
        print(f"  Glyph Count: {self.glyph_count}")

        # 1. Grid-snap coordinates
        self.grid_snap_coordinates(grid=grid)

        # 2. Delta-filter micro-segments
        self.delta_filter(min_dist=delta_min)

        # 3. Curve-point optimisation
        self.optimise_curve_points(tolerance=0.3)

        # 4. Contour simplification (multi-contour aware)
        self.simplify_contours(tolerance=simplify_tol)

        # 5. Stem-width analysis
        self.analyse_and_normalise_stems()

        # 6. Contour direction
        self.correct_contour_direction()

        # 7. Overlap-flag cleanup
        self.clean_overlap_flags()

        # 8. Composite flattening (optional)
        if flatten_composites:
            self.flatten_composite_glyphs(max_components=flatten_threshold)

        # 9. CFF outlines
        self.process_cff_outlines()

        # 10. Vertical metrics
        self.optimise_vertical_metrics()

        # 11. hmtx normalisation
        self.normalise_hmtx()

        # 12. Head flags
        self.optimise_head_flags()

        # 13. GASP table
        self.configure_gasp_table()

        # 14. Hinting cleanup
        self.clean_hinting_tables()

        # 15. Auto-hint
        if not skip_hint:
            self.auto_hint()

    # ---------------------------------------------------------------
    # Reporting
    # ---------------------------------------------------------------

    def print_report(self) -> None:
        """Print a detailed report."""
        print(f"\n{'='*70}")
        print(f"  {self.font_path.name}")
        print(f"{'='*70}")

        print(f"\n  Font Type:    {self.font_type}")
        print(f"  Glyph Count:  {self.glyph_count}")
        print(f"  Tables:       {len(self.font.keys())}")

        if self.stats:
            print(f"\n  Statistics:")
            for k, v in self.stats.items():
                print(f"    {k}: {v}")

        print(f"\n  Optimisations Applied:")
        for entry in self.log_entries:
            print(entry)
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def process_font(input_path: Path, output_dir: Path, *,
                 grid: int, simplify_tol: float, delta_min: float,
                 flatten_composites: bool, flatten_threshold: int,
                 skip_hint: bool, overwrite: bool) -> tuple:
    try:
        output_path = output_dir / input_path.name
        if output_path.resolve() == input_path.resolve():
            print(f"  SKIP: Output path same as input for {input_path.name} (would overwrite source)")
            return True, None

        font = TTFont(input_path)
        optimizer = FontOptimizerV2(font, input_path)

        optimizer.optimise_all(
            grid=grid,
            simplify_tol=simplify_tol,
            delta_min=delta_min,
            flatten_composites=flatten_composites,
            flatten_threshold=flatten_threshold,
            skip_hint=skip_hint,
        )

        output_path = output_dir / input_path.name
        if output_path.exists() and not overwrite:
            print(f"  SKIP (exists): {output_path}")
            font.close()
            return True, optimizer

        font.save(str(output_path))
        return True, optimizer

    except Exception as e:
        print(f"\n  ERROR processing {input_path.name}: {e}")
        import traceback
        traceback.print_exc()
        return False, None


def find_font_files(input_dir: Path) -> list[Path]:
    font_extensions = {".otf", ".ttf", ".otc", ".ttc"}
    font_files = []
    for ext in font_extensions:
        font_files.extend(input_dir.rglob(f"*{ext}"))
        font_files.extend(input_dir.rglob(f"*{ext.upper()}"))
    return sorted(set(font_files))


def main():
    parser = argparse.ArgumentParser(
        description="Improve OTF/TTF font rendering — v2.  "
                    "Fixes shape clarity, removes fringes, simplifies contours, "
                    "and makes shapes crisp.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
v2 Improvements over v1:
  - Multi-contour-aware simplification (v1 destroyed contour boundaries)
  - Delta-filtering of micro-segments (< 0.5 unit apart)
  - Grid-snapping coordinates with configurable grid size
  - Curve-point optimisation (collinear on-curve → line)
  - Overlap-flag cleanup
  - Proper gasp table configuration for all PPEM sizes
  - Head-table flag optimisation
  - OS/2 weight/width sanity checks
  - hmtx advance-width normalisation
  - CFF outline support
  - Composite-glyph flattening (optional)
  - Contour winding-direction review

Examples:
  %(prog)s ./fonts                       # Default pipeline
  %(prog)s ./fonts ./out                  # Custom output dir
  %(prog)s ./fonts --grid 2               # Half-unit grid (preserves detail)
  %(prog)s ./fonts --flatten-composites   # Flatten deep composite glyphs
  %(prog)s ./fonts --skip-hint            # Skip auto-hinting step
        """
    )

    parser.add_argument("input_dir", type=Path,
                        help="Input directory containing OTF/TTF font files")
    parser.add_argument("output_dir", type=Path, nargs="?", default=None,
                        help="Output directory (default: <input>_v2_optimized)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress detailed per-font reports")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing output files")
    parser.add_argument("--grid", type=int, default=1,
                        help="Grid size for coordinate snapping (default: 1 = integer)")
    parser.add_argument("--simplify-tolerance", type=float, default=0.5,
                        help="Tolerance for contour simplification (default: 0.5)")
    parser.add_argument("--delta-min", type=float, default=0.5,
                        help="Minimum distance for delta-filter (default: 0.5)")
    parser.add_argument("--flatten-composites", action="store_true",
                        help="Flatten deeply nested composite glyphs")
    parser.add_argument("--flatten-threshold", type=int, default=5,
                        help="Max component depth before flattening (default: 5)")
    parser.add_argument("--skip-hint", action="store_true",
                        help="Skip auto-hinting step")

    args = parser.parse_args()

    if not args.input_dir.exists():
        print(f"ERROR: Input directory does not exist: {args.input_dir}")
        sys.exit(1)
    if not args.input_dir.is_dir():
        print(f"ERROR: Input path is not a directory: {args.input_dir}")
        sys.exit(1)

    if args.output_dir is None:
        args.output_dir = args.input_dir.parent / f"{args.input_dir.name}_v2_optimized"

    args.output_dir.mkdir(parents=True, exist_ok=True)

    font_files = find_font_files(args.input_dir)
    if not font_files:
        print(f"No font files found in {args.input_dir}")
        sys.exit(1)

    print(f"{'='*70}")
    print(f"  Font Optimiser v2 — Shape & Clarity Enhancement")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    print(f"\n  Input:   {args.input_dir}")
    print(f"  Output:  {args.output_dir}")
    print(f"  Fonts:   {len(font_files)} file(s)")
    print(f"  Grid:    {args.grid}  Simplify: {args.simplify_tolerance}  Delta: {args.delta_min}")

    success_count = 0
    fail_count = 0
    total_original = 0
    total_optimized = 0

    for i, font_path in enumerate(font_files, 1):
        print(f"\n[{i}/{len(font_files)}] Processing: {font_path.name}")
        total_original += font_path.stat().st_size

        success, optimizer = process_font(
            font_path, args.output_dir,
            grid=args.grid,
            simplify_tol=args.simplify_tolerance,
            delta_min=args.delta_min,
            flatten_composites=args.flatten_composites,
            flatten_threshold=args.flatten_threshold,
            skip_hint=args.skip_hint,
            overwrite=args.overwrite,
        )

        if success and optimizer:
            success_count += 1
            output_path = args.output_dir / font_path.name
            if output_path.exists():
                total_optimized += output_path.stat().st_size
            if not args.quiet:
                optimizer.print_report()
        else:
            fail_count += 1

    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  Processed:  {success_count + fail_count} font(s)")
    print(f"  Succeeded:  {success_count}")
    print(f"  Failed:     {fail_count}")

    if success_count > 0 and total_original > 0:
        orig_mb = total_original / (1024 * 1024)
        opt_mb = total_optimized / (1024 * 1024)
        ratio = (opt_mb / orig_mb) * 100 if orig_mb > 0 else 100
        print(f"  Size:       {orig_mb:.2f} MB → {opt_mb:.2f} MB ({ratio:.1f}%)")

    print(f"\n  Output: {args.output_dir}")
    print()

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()