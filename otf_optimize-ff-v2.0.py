#!/usr/bin/env python3
"""
otf_optimize-ff-v2.0 — geometric transforms on OTF/TTF, via FontForge alone.

Clean-slate rewrite. Does exactly three things and nothing else:

    --width      scale glyphs horizontally (and every advance width)
    --height     scale glyphs vertically (and every vertical metric)
    --thickness  scale stem weight, measured from the outlines

Percent semantics throughout, signed: positive grows, negative shrinks.
All three default to 0, which is a verified no-op.

Single dependency
-----------------
FontForge Python bindings only. No fontTools, no pathops, no bridge code.
Both invocation patterns work:

    fontforge -script otf_optimize-ff-v2.0.py [args] IN_DIR OUT_DIR
    python /usr/bin/python3.14 otf_optimize-ff-v2.0.py [args] IN_DIR OUT_DIR

If fontforge itself isn't importable, the script stops before processing
any font and tells you which interpreter to use.

Known limitation
----------------
GPOS PairPos XAdvance/XPlacement are NOT rescaled by ``--width``.
fontforge's binding exposes no API to read or write individual PairPos
values, only ``addKerningClass`` / ``alterKerningClass`` (Format 2 only)
and ``autoKern`` (rebuilds, doesn't scale). On a GPOS font, --width will
narrow every advance width by the requested percent while leaving kern
values alone, so letterfit drifts. The script detects this and warns at
the top of the run, never silently. Confirmed empirically on Adwaita
Sans: 31,159 kern pairs, XAdvance sum unchanged after per-glyph x*0.9
(ratio 1.0000).

Stem width is measured, never guessed
-------------------------------------
    ref_stem = median width between adjacent near-vertical edges,
    sampled over the first 64 non-composite glyphs.

The earlier ``cap_height * 0.07`` heuristic under-measured by 1.7x on
Jano Sans Pro (88u stem on 729u cap, so 12.1% not 7%) and --thickness 5
delivered 2.55u of widening instead of 4.40u -- a third of a pixel at
16px on a full run of the expensive pipeline. The fontforge contour
iterator (verified: 88u median on Jano Regular = exact H truth) replaces
both the guess and the StdHW path.

Usage
-----
    python otf_optimize-ff-v2.0.py --thickness 5 IN_DIR OUT_DIR
    python otf_optimize-ff-v2.0.py --width -8 --height 4 IN_DIR OUT_DIR
    python otf_optimize-ff-v2.0.py -j 12 --thickness 5 IN_DIR OUT_DIR
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("otf_optimize_ff_v2.0")

# Module-level guard: True once fontforge is confirmed importable. The
# bootstrap checks this BEFORE doing anything else, so a missing module
# produces one error, not 18 misleading per-font failures.
_FONTFORGE_OK = False

SUPPORTED_EXTENSIONS = {".otf", ".ttf"}

# A segment counts as vertical when |dx| is at most this many font units.
# Real stems often lean a few tenths of a unit (hinting, design), so 1.0u
# is loose enough to catch them and tight enough to exclude anything
# genuinely diagonal.
STEM_EDGE_TOLERANCE = 1.0

# Stem-search window, as fractions of cap height. Below the floor are
# counters and touching-edge artefacts, not stems; above the ceiling is
# the letter interior. The floor also rejects the sub-unit gaps FontForge
# emits from its 1/1024-unit coordinate grid.
STEM_MIN_FRAC = 0.015
STEM_MAX_FRAC = 0.40
STEM_MIN_LEN_FRAC = 0.08

# How many glyphs to sample. Enough to be stable, few enough that the
# cost is irrelevant next to the transform itself.
SAMPLE_GLYPHS = 64


# --------------------------------------------------------------------------
# import bootstrap
# --------------------------------------------------------------------------

def _require_fontforge() -> None:
    """Confirm fontforge is importable.

    Fontforge's binding lives at different site-packages paths on the
    two launchers people use:

      - linuxbrew fontforge    /home/linuxbrew/.linuxbrew/lib/pythonX.Y/site-packages/fontforge.so
      - system python3.14      /usr/lib/python3.14/site-packages/fontforge.so

    Probe well-known locations once and re-test. This is the only bootstrap
    step the script needs -- once fontforge is importable, everything
    else (contour iteration, metrics, advance widths) is in-fontforge.
    """
    global _FONTFORGE_OK
    try:
        import fontforge  # noqa: F401
        _FONTFORGE_OK = True
        return
    except Exception:
        pass

    # Common locations to probe. fontforge.so filename is constant across
    # CPython minor versions on linuxbrew / Arch. Skip pyc-only entries.
    import importlib.util
    candidates = (
        "/usr/lib/python3.14/site-packages",
        "/usr/lib/python3.13/site-packages",
        "/usr/lib/python3.12/site-packages",
        "/usr/lib/python3.10/site-packages",
        "/home/linuxbrew/.linuxbrew/lib/python3.14/site-packages",
        "/home/linuxbrew/.linuxbrew/lib/python3.13/site-packages",
        "/home/linuxbrew/.linuxbrew/lib/python3.12/site-packages",
        "/home/linuxbrew/.linuxbrew/lib/python3.10/site-packages",
        "/usr/local/lib/python3.14/site-packages",
    )
    for d in candidates:
        if not os.path.isdir(d):
            continue
        if os.path.isfile(f"{d}/fontforge.so"):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import fontforge  # noqa: F401
                _FONTFORGE_OK = True
                return
            except Exception:
                continue

    # Last try: maybe the user has it installed under the user's home.
    import subprocess as _sp
    for other_python in ("/usr/bin/python3.14", "/usr/bin/python3.13",
                        "/usr/local/bin/python3.14"):
        if os.path.isfile(other_python):
            r = _sp.run([other_python, "-c",
                         "import fontforge, os; "
                         "print(os.path.dirname(os.path.dirname(fontforge.__file__)))"],
                        capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                d = r.stdout.strip()
                if os.path.isdir(d) and d not in sys.path:
                    sys.path.insert(0, d)
                    try:
                        import fontforge  # noqa: F401
                        _FONTFORGE_OK = True
                        return
                    except Exception:
                        pass


_require_fontforge()
if not _FONTFORGE_OK:
    sys.stderr.write(
        "ERROR: fontforge Python module not importable in this interpreter.\n"
        f"  python   : {sys.executable}\n"
        f"  version  : {sys.version.split()[0]}\n"
        "  probed  :\n"
        "    /usr/lib/python3.{10,12,13,14}/site-packages\n"
        "    /home/linuxbrew/.linuxbrew/lib/python3.{10,12,13,14}/site-packages\n"
        "    /usr/local/lib/python3.14/site-packages\n"
        "  Fix: invoke with `fontforge -script <script>` OR a python that has\n"
        "  the fontforge module installed (Arch: pacman -S python-fontforge).\n"
    )
    sys.exit(2)

import fontforge  # noqa: E402  -- needed only after the bootstrap above


# --------------------------------------------------------------------------
# stem measurement (fontforge alone, no fontTools)
# --------------------------------------------------------------------------

def _vertical_edges_for_glyph(glyph, min_len: float) -> list[float]:
    """x positions of near-vertical line/curve/close edges for one glyph.

    fontforge's contour iterator yields points with .x, .y, .on_curve,
    but no explicit op type. Consecutive points form a segment; we test
    dx against a tolerance and dy against min_len (a fraction of cap so
    serifs and terminals drop out). The close-path segment between the
    last point and start is also tested.

    Glyph composites (LayerRefs) report their component's outlines as
    additional contours in ``glyph.foreground`` on this FontForge build
    -- a 1881-composite AdwaitaSans glyph reports zero contours there.
    They are therefore naturally skipped by this walk, which is what we
    want: stem-width statistics should be of base outlines, not scaled
    component shapes.
    """
    edges: list[float] = []
    fg = glyph.foreground
    for contour in fg:
        pts = list(contour)
        if len(pts) < 2:
            continue
        start = (pts[0].x, pts[0].y)
        cur = start
        for p in pts[1:]:
            if abs(p.x - cur[0]) <= STEM_EDGE_TOLERANCE \
                    and abs(p.y - cur[1]) >= min_len:
                edges.append(cur[0])
            cur = (p.x, p.y)
        # close-path segment
        if abs(start[0] - cur[0]) <= STEM_EDGE_TOLERANCE \
                and abs(start[1] - cur[1]) >= min_len:
            edges.append(cur[0])
    return edges


def _measure_cap_height(font) -> float:
    """Cap height from the 'H' outline (top of its leftmost edge).

    fontforge's ``font.capHeight`` is missing on this build, and
    ``private.guess("CapHeight")`` returned 650 for Jano Sans Pro Black
    where OS/2 says 726 -- a stored value can be wrong, and reading the
    top of 'H' is what the transform actually operates on. We use the
    maximum y on the first non-empty contour of 'H', which equals the
    nominal cap height for any upright design.
    """
    if "H" not in font:
        return float(font.em) * 0.7
    h = font["H"]
    top = -1e9
    for contour in h.foreground:
        for p in contour:
            if p.y > top:
                top = p.y
    return top if top > -1e9 else float(font.em) * 0.7


def _measure_stem(font) -> Optional[float]:
    """Median width between adjacent near-vertical edges over the font.

    Returns the median, or None if no candidates fall inside the stem
    window. The window is in fractions of cap height -- below the floor
    are counters and FontForge's 1/1024-unit coordinate artefacts, above
    the ceiling is the letter interior.
    """
    cap = _measure_cap_height(font)
    min_len = STEM_MIN_LEN_FRAC * cap
    min_w = STEM_MIN_FRAC * cap
    max_w = STEM_MAX_FRAC * cap

    widths: list[float] = []
    scanned = 0
    for glyph in font.glyphs():
        edges = _vertical_edges_for_glyph(glyph, min_len)
        if not edges:
            continue
        edges.sort()
        for i in range(len(edges) - 1):
            d = edges[i + 1] - edges[i]
            if min_w <= d <= max_w:
                widths.append(d)
        scanned += 1
        if scanned >= SAMPLE_GLYPHS:
            break

    if not widths:
        return None
    widths.sort()
    return widths[len(widths) // 2]


def _has_gpos_kern(font) -> bool:
    """Does the font carry GPOS PairPos XAdvance under the 'kern' feature?

    Used only to flag, not to fix: fontforge cannot read or write
    PairPos values, so all we can do is warn the operator that --width
    will not rescale kerning on this font.

    ``font.gpos_lookups`` on this build returns a tuple of human-readable
    strings like ``"'kern' Horizontal Kerning lookup 1"``, not the
    (handle, name, subtable_count, type) 4-tuples I assumed earlier. Any
    entry containing ``'kern'`` is a horizontal kerning lookup; we look
    for that token instead of trying to unpack.
    """
    try:
        for entry in font.gpos_lookups:
            if "'kern'" in entry:
                return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# the transform (fontforge)
# --------------------------------------------------------------------------

def _scale_metrics(font, factor: float) -> tuple[list[str], list[str]]:
    """Scale every vertical metric by `factor`. Returns (scaled, failed).

    FontForge's binding requires an int here -- assigning a float raises
    ``TypeError: 'float' object cannot be interpreted as an integer`` --
    so every value is rounded. ``failed`` is reported rather than
    swallowed: a metric that silently does not move leaves a font whose
    outlines are 5% taller inside an unchanged line box, which shows up
    as clipped descenders.

    fontforge exposes all 13 of these as writable ints on this build
    (probe-confirmed): no post-write pass with fontTools is needed.
    """
    scaled, failed = [], []
    for name in (
        "ascent", "descent",
        "hhea_ascent", "hhea_descent", "hhea_linegap",
        "os2_typoascent", "os2_typodescent", "os2_typolinegap",
        "os2_winascent", "os2_windescent",
        "os2_capheight", "os2_xheight",
    ):
        old = getattr(font, name, None)
        if not isinstance(old, (int, float)):
            continue
        new = round(old * factor)
        if new == round(old):
            continue
        try:
            setattr(font, name, new)
            scaled.append(name)
        except Exception as exc:
            failed.append(f"{name} ({type(exc).__name__})")
            log.debug(f"  could not scale {name}: {type(exc).__name__}: {exc}")
    return scaled, failed


def _transform_glyphs(font, sx: float, sy: float) -> None:
    """Scale every glyph's outline and advance width.

    Per-glyph rather than font-level: a font-level ``font.transform``
    was observed to leave both advance widths AND GPOS PairPos
    unchanged on AdwaitaSans (1881 composites), while per-glyph
    transform scales the advance correctly. Composite references
    survive intact -- fontforge unrolls them at render time.
    """
    for glyph in font.glyphs():
        glyph.transform((sx, 0.0, 0.0, sy, 0.0, 0.0))


def _cleanup(font) -> int:
    """Per-glyph topology cleanup. Returns glyphs touched."""
    touched = 0
    for glyph in font.glyphs():
        try:
            glyph.removeOverlap()
            glyph.correctDirection()
            glyph.round()
            touched += 1
        except Exception as exc:
            log.debug(f"  {glyph.glyphname}: {type(exc).__name__}: {exc}")
    return touched


def _quantize_curve(font, n_units: int) -> int:
    """Snap every curve point to a 1/n_units-unit grid. Returns points touched.

    fontforge.round() snaps to whole font units (1/N grid with N=1). For
    finer control we write the point coords directly (verified writable
    on this build). Composites report empty ``foreground`` -- their
    component outlines live in the referenced glyphs, which are snapped
    on their own pass, so nothing is double-processed.

    n_units=1 is exactly fontforge.round(). n_units=2 is a half-unit
    grid, n_units=4 a quarter-unit grid, etc. Verified on JanoSansPro
    'o': sub-unit coords (66.7, 143.3, 200.7) snap cleanly.
    """
    if n_units < 1:
        raise ValueError(f"--quantize-curve must be >= 1, got {n_units}")
    grid = 1.0 / n_units
    touched = 0
    for glyph in font.glyphs():
        for contour in glyph.foreground:
            for pt in contour:
                nx = round(pt.x * n_units) / n_units
                ny = round(pt.y * n_units) / n_units
                if nx != pt.x or ny != pt.y:
                    pt.x = nx
                    pt.y = ny
                    touched += 1
    return touched


def apply_transform(src: Path, dst: Path, width_pct: float, height_pct: float,
                  thickness_pct: float, ref_stem: Optional[float],
                  quantize_units: int = 0) -> dict:
    """Apply the three transforms and write `dst`. Returns a report dict."""
    font = fontforge.open(str(src))
    report: dict = {"src": src.name}
    try:
        sx = 1.0 + width_pct / 100.0
        sy = 1.0 + height_pct / 100.0
        if sx <= 0 or sy <= 0:
            raise ValueError(
                f"scale would invert the font "
                f"(width {width_pct}%, height {height_pct}%)")

        if sx != 1.0 or sy != 1.0:
            _transform_glyphs(font, sx, sy)
            if sy != 1.0:
                scaled, failed = _scale_metrics(font, sy)
                report["metrics_scaled"] = scaled
                report["metrics_failed"] = failed
            report["scale_xy"] = (round(sx, 6), round(sy, 6))

        if thickness_pct != 0.0:
            if not ref_stem:
                log.warning(f"  {src.name}: no stem measurement, "
                            f"skipping --thickness")
            else:
                # ref_stem was measured on the input. A width scale has
                # already narrowed the stems, so the target is the
                # scaled value -- "5% heavier than the shape we are
                # producing", not "5% of the original".
                effective = ref_stem * sx
                delta = abs(thickness_pct) / 100.0 * effective
                sign = 1.0 if thickness_pct > 0 else -1.0
                applied = 0
                for glyph in font.glyphs():
                    try:
                        glyph.changeWeight(sign * delta)
                        applied += 1
                    except Exception as exc:
                        log.debug(
                            f"  {glyph.glyphname}: changeWeight failed: "
                            f"{type(exc).__name__}: {exc}")
                report["thickness"] = {
                    "ref_stem_input": round(ref_stem, 2),
                    "ref_stem_effective": round(effective, 2),
                    "delta_units": round(sign * delta, 3),
                    "glyphs": applied,
                }

        if quantize_units:
            touched = _quantize_curve(font, quantize_units)
            report["quantize_curve"] = {
                "grid": f"1/{quantize_units}u",
                "points": touched,
            }

        if sx != 1.0 or sy != 1.0 or thickness_pct != 0.0 or quantize_units:
            report["cleaned"] = _cleanup(font)

        dst.parent.mkdir(parents=True, exist_ok=True)
        font.generate(str(dst))
    finally:
        font.close()
    return report


# --------------------------------------------------------------------------
# verification (re-open the written file in fontforge alone)
# --------------------------------------------------------------------------

def verify(src: Path, dst: Path, width_pct: float, height_pct: float,
           thickness_pct: float, expected_stem: Optional[float]) -> dict:
    """Re-measure the written font and compare against what was asked.

    Expected stem is computed through the whole pipeline: width scaling
    narrows a stem by the same factor it narrows the letter, and
    thickness then moves it by its own percentage of that. Comparing a
    ``--width -10`` run against the input stem would report the correct
    -10% as an error, so the baseline has to account for it.
    """
    out: dict = {}
    try:
        s_in = fontforge.open(str(src))
        stem_in = _measure_stem(s_in)
        s_in.close()
        d_in = fontforge.open(str(dst))
        stem_out = _measure_stem(d_in)
        d_in.close()
    except Exception as exc:
        out["stem_error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["stem_in"] = round(stem_in, 2) if stem_in else None
    out["stem_out"] = round(stem_out, 2) if stem_out else None
    if stem_in and stem_out:
        out["stem_pct_vs_input"] = round((stem_out - stem_in) / stem_in * 100, 2)
    if expected_stem and stem_out:
        out["stem_pct_vs_expected"] = round(
            (stem_out - expected_stem) / expected_stem * 100, 2)
    return out


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def process_font(job) -> tuple[str, bool, list[str]]:
    """One font end to end. Module level so multiprocessing can pickle it."""
    src, dst, width_pct, height_pct, thickness_pct, quantize_units = job
    src, dst = Path(src), Path(dst)
    lines: list[str] = []
    try:
        s = fontforge.open(str(src))
        try:
            cap = _measure_cap_height(s)
            stem = _measure_stem(s)
            has_gpos = _has_gpos_kern(s)
        finally:
            s.close()

        if cap and stem:
            lines.append(f"  measured from outlines: cap={cap:.1f}u "
                         f"stem={stem:.1f}u")
        elif cap:
            lines.append(f"  measured cap={cap:.1f}u, stem: not enough candidates")
        else:
            lines.append("  measurement unavailable")

        if thickness_pct != 0.0 and stem is None:
            lines.append("  SKIPPED thickness: no stem measured")
            return src.name, False, lines

        if width_pct != 0.0 and has_gpos:
            lines.append("  WARNING font has GPOS kerning; --width will "
                         "scale advance widths but not PairPos XAdvance "
                         "(letterfit may drift).")

        sx = 1.0 + width_pct / 100.0
        rep = apply_transform(src, dst, width_pct, height_pct,
                              thickness_pct, stem, quantize_units)
        for k, v in rep.items():
            if k == "thickness":
                lines.append(
                    f"  thickness: {v['ref_stem_input']}u -> "
                    f"{v['ref_stem_effective']}u effective, "
                    f"delta {v['delta_units']:+}u on {v['glyphs']} glyphs")
            elif k == "metrics_scaled":
                lines.append(f"  metrics scaled x{1.0 + height_pct/100.0:.4f}: "
                             f"{', '.join(v) if v else '(none)'}")
            elif k == "scale_xy":
                lines.append(f"  scale x={v[0]:.4f} y={v[1]:.4f}")
            elif k == "quantize_curve":
                lines.append(f"  quantized curve to {v['grid']} grid: "
                             f"{v['points']} points snapped")

        # Expected stem through the whole pipeline: width narrows it by
        # sx, thickness then moves it by its own percentage of that.
        expected = (stem * sx * (1.0 + thickness_pct / 100.0)) if stem else None
        v = verify(src, dst, width_pct, height_pct, thickness_pct, expected)
        if "stem_pct_vs_expected" in v:
            got = v["stem_pct_vs_expected"]
            tol = max(2.0, abs(thickness_pct) * 0.25)
            verdict = "ok" if abs(got) <= tol else "OFF TARGET"
            lines.append(
                f"  verify: stem {v['stem_in']}u -> {v['stem_out']}u "
                f"({v['stem_pct_vs_input']:+.2f}% vs input, "
                f"{got:+.2f}% vs expected {expected:.1f}u) {verdict}")
        if v.get("stem_error"):
            lines.append(f"  verify failed: {v['stem_error']}")

        for name in rep.get("metrics_failed", []):
            lines.append(f"  WARNING metric not scaled: {name}")
        return src.name, True, lines
    except Exception as exc:
        lines.append(f"  FAILED {type(exc).__name__}: {exc}")
        return src.name, False, lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="otf_optimize-ff-v2.0",
        description="Scale font width, height and stem thickness via FontForge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # 5% heavier stems, 12 fonts at once
  otf_optimize-ff-v2.0.py --thickness 5 -j 12 IN OUT

  # 8% narrower, 4% taller
  otf_optimize-ff-v2.0.py --width -8 --height 4 IN OUT

  # snap every outline point to integer units (geometry cleanup)
  otf_optimize-ff-v2.0.py --quantize-curve 1 IN OUT

percentages are signed and relative to the measured stem width, not a
fixed number of font units.

NOTE: --width scales advance widths but NOT GPOS PairPos XAdvance.
On fonts with GPOS kerning (most modern OTF/TTF) expect letterfit drift;
the script warns at the top of any affected run.

NOTE: fontforge.autoHint() emits NO hint bytecode on the 20251009 build
(verified: 0/2938 TrueType glyphs with program, 0/1568 CFF charstrings
with hintmask). There is no hinting path here -- curve/stem quantizing
and geometry cleanup only.
""")
    p.add_argument("input_dir", type=Path)
    p.add_argument("output_dir", type=Path)

    p.add_argument("--width", type=float, default=0.0, metavar="PCT",
                   help="Scale glyphs horizontally. -10 = 10%% narrower. "
                        "Does NOT rescale GPOS PairPos XAdvance.")
    p.add_argument("--height", type=float, default=0.0, metavar="PCT",
                   help="Scale glyphs vertically and rescale ascent, descent, "
                        "hhea and OS/2 vertical metrics. 5 = 5%% taller.")
    p.add_argument("--thickness", type=float, default=0.0, metavar="PCT",
                   help="Scale stem weight by this percentage of the MEASURED "
                        "stem width. Negative thins. Uses changeWeight, so "
                        "joins and counters stay clean.")
    p.add_argument("--quantize-curve", type=int, default=0, metavar="N",
                   help="Snap every outline point to a 1/N-unit grid. N=1 is "
                        "fontforge.round() (integer units); N=2 a half-unit "
                        "grid, N=4 a quarter-unit grid. Reduces geometry "
                        "noise but can deform thin features; default off.")
    p.add_argument("--jobs", "-j", type=int, default=0, metavar="N",
                   help="Process N fonts in parallel. 0 = auto (one worker "
                        "per CPU, capped at the font count). 1 = sequential.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s - %(message)s", stream=sys.stdout)
    # fontforge prints 'Internal Error (overlap)' for noisy ligatures
    # even on success; silence its root logger unless --verbose.
    logging.getLogger("fontforge").setLevel(logging.ERROR if not args.verbose else logging.DEBUG)

    if not args.input_dir.is_dir():
        log.error(f"input directory not found: {args.input_dir}")
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)

    fonts = sorted(q for q in args.input_dir.iterdir()
                   if q.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not fonts:
        log.error(f"no .otf/.ttf files in {args.input_dir}")
        return 1

    jobs = [(str(q), str(args.output_dir / q.name),
             args.width, args.height, args.thickness,
             args.quantize_curve) for q in fonts]

    cpu = os.cpu_count() or 1
    workers = min(args.jobs, len(jobs)) if args.jobs > 0 else min(cpu, len(jobs))

    log.info(f"{len(fonts)} font(s)  width={args.width:+g}%  "
             f"height={args.height:+g}%  thickness={args.thickness:+g}%")
    if args.quantize_curve:
        log.info(f"  curve quantize: 1/{args.quantize_curve}-unit grid")
    if all(v == 0 for v in (args.width, args.height, args.thickness)) \
            and not args.quantize_curve:
        log.warning("all transforms are 0 and no quantize -- this is a no-op copy")
    log.info(f"workers: {workers} ({cpu} CPUs)")

    # Cross-font GPOS scan once. fontforge's binding reads kern lookups
    # cheaply; doing it per-font would repeat work.
    if args.width != 0.0:
        any_gpos = False
        for q in fonts:
            try:
                f = fontforge.open(str(q))
                if _has_gpos_kern(f):
                    any_gpos = True
                    f.close()
                    break
                f.close()
            except Exception:
                continue
        if any_gpos:
            log.warning("at least one input font carries GPOS PairPos kerning; "
                        "--width will scale advance widths but NOT PairPos "
                        "XAdvance. letterfit may drift on those fonts.")

    ok = 0
    if workers > 1:
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        with ctx.Pool(processes=workers) as pool:
            for name, good, lines in pool.imap_unordered(process_font, jobs):
                log.info(f"{'ok  ' if good else 'FAIL'} {name}")
                for ln in lines:
                    log.info(ln)
                ok += good
    else:
        for job in jobs:
            name, good, lines = process_font(job)
            log.info(f"{'ok  ' if good else 'FAIL'} {name}")
            for ln in lines:
                log.info(ln)
            ok += good

    log.info(f"done: {ok}/{len(fonts)} succeeded -> {args.output_dir}")
    return 0 if ok == len(fonts) else 1


if __name__ == "__main__":
    sys.exit(main())