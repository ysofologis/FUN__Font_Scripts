#!/usr/bin/env python3
"""
ttf2otf_foundry_v1.py — TTF → OTF (CFF) converter built on foundrytools.

Why this script:
    The existing ttf2otf_ff_v*.py scripts lean on FontForge (which is excellent for
    fringe-elimination / morphological ops but adds a heavy native dependency and
    tends to over-process). The ttf2otf_afdko_v6.py script shells out to AFDKO
    ``tx``, which produces the highest fidelity results but requires ``tx`` on PATH.
    This script takes a third route: use foundrytools' pure-Python TTF→OTF pipeline,
    which is fast, dependency-light (just ``pip install foundrytools``), and gives
    clean controllable quality through the ``tolerance`` and ``correct_contours``
    knobs.

Pipeline:
    1. Open with ``foundrytools.Font`` (validates font + exposes is_ps/is_tt/is_variable).
    2. If variable → instantiate to a named instance via ``foundrytools.app.var2static``.
       Default instance is used unless ``--instance NAME`` matches a subfamilyNameID.
    3. ``Font.to_otf(tolerance=…, correct_contours=…)`` — qu2cu + skia-pathops.
    4. Optional post-pass: ``Font.correct_contours()`` for an extra skia cleanup.
    5. Optional hint-tune via ``foundrytools.app.ttf_autohint`` (TTF) / ``otf_autohint``
       (OTF) — only if ``tx`` is on PATH (AFDKO check) and ``--hint-tune`` is set.
    6. ``Font.save(out_path)`` — writes OTF/CFF.

Usage:
    # Single font:
    python ttf2otf_foundry_v1.py font.ttf out.otf

    # Whole directory (recursive):
    python ttf2otf_foundry_v1.py ./ttf_input/ ./otf_output/

    # Higher fidelity (smaller tolerance + extra correct_contours pass):
    python ttf2otf_foundry_v1.py font.ttf out.otf --tolerance 0.5 --extra-correct

    # VF: pick a named instance ("Regular", "Bold", etc.) before conversion:
    python ttf2otf_foundry_v1.py Variable.ttf out.otf --instance Regular

    # Add CFF stem-hint tuning after conversion (requires afdko on PATH):
    python ttf2otf_foundry_v1.py font.ttf out.otf --hint-tune auto

Notes on quality:
    * ``tolerance`` controls qu2cu conversion deviation in font units. Lower = more
      accurate but larger file (more cubic segments per quadratic curve).
        - 0.1  → near-exact reproduction (largest files)
        - 0.5  → high fidelity, reasonable size (recommended)
        - 1.0  → foundrytools default; balanced
        - 2.0+ → aggressive simplification; visible drift on tight curves
    * ``correct_contours=True`` runs skia-pathops union+simplify on every glyph,
      merging overlapping stems and culling tiny sub-paths. Strongly recommended.
    * ``--extra-correct`` adds a second skia-pathops pass AFTER to_otf. Cheap (~1s)
      and catches anything the conversion introduced.

Requirements:
    pip install foundrytools
    # Optional, only for --hint-tune:
    pip install afdko     # needs 'tx' on PATH

Author: Modelgrok — created 2026-09-22 alongside otf_optimize-v12.
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
warnings.filterwarnings("ignore", message=".*pkg_resources.*deprecated.*", category=UserWarning)

try:
    from foundrytools import Font
    from foundrytools.constants import T_NAME
    from foundrytools.core.font import FontError
    from foundrytools.app.var2static import run as var2static_run, Var2StaticError
except ImportError:
    sys.exit(
        "❌ foundrytools not installed.\n"
        "   Install with:  pip install foundrytools\n"
    )

# Used in _scale_font() — add a no-op cmap shim import guard.
try:
    from fontTools.pens.transformPen import TransformPen  # noqa: F401
except ImportError:
    TransformPen = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _human(n_bytes: int) -> str:
    if n_bytes < 1024:
        return f"{n_bytes}B"
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.1f}KB"
    return f"{n_bytes / 1024 / 1024:.2f}MB"


def _has_afdko_tx() -> bool:
    """AFDKO's `tx` binary is required for CFF hint-tune and some stem recalc tools."""
    return shutil.which("tx") is not None


def _log(msg: str, *, verbose: bool = True) -> None:
    if verbose:
        print(msg)


INSTANCE_ALIASES = {"regular": 0, "normal": 0}  # case-insensitive → instance index


def _resolve_instance(f: Font, requested: str | None) -> tuple[int, str]:
    """
    Pick a named instance index from the variable font.

    Resolution order:
        1. ``requested=None`` → try to find "Regular" / "Normal" by name;
           fall back to instance index 0 if no such instance exists.
        2. ``requested=<name>`` → exact-match against subfamilyNameID and
           postscriptNameID (case-insensitive). If no exact match, fall back
           to substring containment; if still nothing, raise with a helpful
           error listing available instances.

    Why exact-match first: substring matching has nasty surprises —
    ``--instance Bold`` would otherwise pick "SemiBold", "ExtraBold", "BoldItalic",
    etc. The substring fallback only kicks in when the user clearly asked for
    something we don't have verbatim (e.g. ``--instance BoldItalic`` matches
    "Bold Italic" with a space).

    Returns (index, subfamily_name).
    """
    if "fvar" not in f.ttfont:
        return -1, ""
    instances = f.t_fvar.table.instances
    if not instances:
        return -1, ""

    name_table = f.ttfont[T_NAME]

    def _name(nid: int) -> str:
        try:
            return name_table.getDebugName(nid) or ""
        except Exception:
            return ""

    def _all_names(inst) -> list[str]:
        """All candidate strings we compare against (lowercased, stripped)."""
        out: list[str] = []
        for nid in (inst.subfamilyNameID, inst.postscriptNameID):
            n = _name(nid).strip().lower()
            if n:
                out.append(n)
        return out

    if requested is None:
        # Prefer "Regular" / "Normal" by exact-match; else first instance.
        for i, inst in enumerate(instances):
            for n in _all_names(inst):
                if n in INSTANCE_ALIASES:
                    return i, _name(inst.subfamilyNameID)
        idx = 0
        return idx, _name(instances[idx].subfamilyNameID)

    req = requested.strip().lower()

    # Pass 1: exact match.
    for i, inst in enumerate(instances):
        if req in _all_names(inst):
            return i, _name(inst.subfamilyNameID)

    # Pass 2: substring fallback (helps with "BoldItalic" vs "Bold Italic").
    for i, inst in enumerate(instances):
        for n in _all_names(inst):
            if req in n:
                return i, _name(inst.subfamilyNameID)

    # No match — list what's available for a helpful error message.
    available = [_name(inst.subfamilyNameID) for inst in instances]
    raise Var2StaticError(
        f"No named instance matching '{requested}'. "
        f"Available instances: {', '.join(available) if available else '(none)'}."
    )


def _static_font(f: Font, instance_request: str | None, verbose: bool) -> Font:
    """
    Return a static instance of ``f`` if it's variable, else return ``f`` unchanged.

    The returned font is a *new* foundrytools.Font object whose ``ttfont`` lives
    in memory; the caller is responsible for closing ``f`` and the returned font.
    """
    if not f.is_variable:
        return f

    if verbose:
        print("    → Variable font detected; instantiating to static instance...")

    idx, sub_name = _resolve_instance(f, instance_request)
    if idx < 0:
        raise Var2StaticError("Variable font has no named instances to instantiate.")

    inst = f.t_fvar.table.instances[idx]
    static, stem = var2static_run(f, inst, update_font_names=True)

    if verbose:
        print(f"      ✓ Instantiated instance [{idx}]: '{sub_name}' (stem='{stem}')")
    return static


def _scale_font(f: Font, factor: float, verbose: bool) -> None:
    """
    Uniformly scale every glyph coordinate, advance width, sidebearing, and
    OpenType metric by ``factor``. Also updates ``unitsPerEm`` (UPM) so the
    font keeps its identity — a 1000-UPM font scaled by 0.9 emerges as a
    900-UPM font with all geometry and metrics scaled proportionally.

    IMPORTANT — apply AFTER VF instantiation and BEFORE ``to_otf``. The
    ``to_otf`` step is geometry-read-only on CFF, so scaling pre-conversion
    keeps the CFF natively scaled.

    What scales (proportional, ``x_new = x_old * factor``):
        * All glyph outlines (every contour coordinate; recomputes xMin/yMin/xMax/yMax).
        * Advance widths (``hmtx.advanceWidth``) and left sidebearings
          (recomputed from new xMin).
        * Vertical metrics if present (``vmtx`` advance + tsb).
        * ``OS/2`` typo + win metrics (``sTypoAscender/Descender/LineGap``,
          ``usWinAscent/Descent``, ``sxHeight/capHeight/xAvgCharWidth``).
        * ``hhea.ascent/Descent/LineGap``.
        * ``head.unitsPerEm`` (``= round(old_upm * factor)``).

    What is intentionally NOT scaled:
        * cmap subtables (glyph IDs unchanged).
        * ``post.italicAngle`` (typographic angle, scale-invariant).
        * ``name`` table strings.
        * ``OS/2.usDefaultChar/usBreakChar`` (glyph indices).

    Mutates ``f.ttfont`` in place. Returns None.
    """
    if factor == 1.0:
        if verbose:
            print("    \u24d8  --scale 1.000 \u2192 no-op (skipping scale pass).")
        return
    if factor <= 0:
        raise ValueError(f"--scale factor must be > 0 (got {factor})")

    tt = f.ttfont
    upm_old = tt["head"].unitsPerEm
    upm_new = max(16, round(upm_old * factor))
    if upm_new == upm_old:
        if verbose:
            print(f"    \u24d8  scale={factor} rounds UPM={upm_old} unchanged \u2192 no-op.")
        return

    if verbose:
        print(
            f"    \u2192 Scaling geometry \u00d7{factor:.4f}  "
            f"(UPM {upm_old} \u2192 {upm_new}, all metrics proportional)..."
        )

    # \u2500\u2500 1. Scale every glyph outline via fontTools' documented path. ─────
    # ``getCoordinatesAndControls`` → multiply → ``setCoordinates``:
    # this auto-recomputes each glyph's xMin/yMin/xMax/yMax and is the
    # supported path for in-place glyph geometry scaling.
    glyph_order = tt.getGlyphOrder()

    h_metrics = tt["hmtx"].metrics
    v_metrics = getattr(tt.get("vmtx"), "metrics", None)

    for gname in glyph_order:
        # Use ``_getCoordinatesAndControls`` (modern fontTools API).
        # Signature: _getCoordinatesAndControls(glyphName, hMetrics, vMetrics=None)
        # where hMetrics is the .metrics DICT (not the ttFont!). Returns
        # (coords, controls) — coords includes 4 trailing phantom points.
        result = tt["glyf"]._getCoordinatesAndControls(gname, h_metrics, v_metrics)
        if result is None:
            continue  # glyph not present (shouldn't happen for valid glyphOrder)
        # Newer fontTools (>=4.40) returns 3-tuple (coords, controls, components);
        # older fontTools returns 2-tuple (coords, controls). Tolerate both.
        if len(result) == 3:
            coords, controls, _components = result
        else:
            coords, controls = result

        if len(coords) == 0:
            continue  # empty / spacing glyph

        scaled = [
            (
                int(round(p[0] * factor)),
                int(round(p[1] * factor)),
            )
            for p in coords
        ]
        # Two ``_setCoordinates`` signatures exist across fontTools versions:
        #   OLD: _setCoordinates(glyphName, coord, hMetrics, vMetrics=None)
        #   NEW: _setCoordinates(glyphName, ttFont, coord, hMetrics, vMetrics, controls)
        # Both propagate phantom-point math into hmtx/vmtx automatically.
        # Pick the right one via try/except — fall back to the other.
        try:
            # Try NEW signature first (more flexible).
            tt["glyf"]._setCoordinates(
                gname, tt, scaled, h_metrics, v_metrics, controls
            )
        except TypeError:
            # Fall back to OLD signature (no ``controls`` arg).
            tt["glyf"]._setCoordinates(gname, scaled, h_metrics, v_metrics)

    # Refresh head bounding box (drawn from per-glyph bounds after scale).
    if "glyf" in tt:
        head = tt["head"]
        gx0 = gy0 = float("inf")
        gx1 = gy1 = float("-inf")
        for gname in glyph_order:
            g = tt["glyf"][gname]
            n = getattr(g, "numberOfContours", 0) or 0
            if n == 0:
                continue
            if getattr(g, "xMin", 0) is None:
                continue
            gx0 = min(gx0, g.xMin)
            gy0 = min(gy0, g.yMin)
            gx1 = max(gx1, g.xMax)
            gy1 = max(gy1, g.yMax)
        if gx0 != float("inf"):
            head.xMin, head.yMin, head.xMax, head.yMax = gx0, gy0, gx1, gy1

    # \u2500\u2500 2. hmtx: scale advance widths; recompute lsb from new xMin. \u2500\u2500
    # NB: hmtx is auto-updated by ``_setCoordinates`` via phantom-point math,
    # so we DO NOT run a second manual scaling pass here (would double-scale by factor^2).

    # \u2500\u2500 3. vmtx: scale vertical advance + tsb. \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    # NB: vmtx is auto-updated by ``_setCoordinates`` when v_metrics is passed.

    # \u2500\u2500 4. hhea metrics. \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    hhea = tt["hhea"]
    for attr in ("ascent", "descent", "lineGap"):
        old = getattr(hhea, attr, 0) or 0
        setattr(hhea, attr, int(round(old * factor)))

    # \u2500\u2500 5. OS/2 metrics. \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    if "OS/2" in tt:
        os2 = tt["OS/2"]
        for attr in (
            "sTypoAscender",
            "sTypoDescender",
            "sTypoLineGap",
            "usWinAscent",
            "usWinDescent",
            "sxHeight",
            "sCapHeight",
            "xAvgCharWidth",
        ):
            old = getattr(os2, attr, None)
            if old is None:
                continue
            try:
                v = int(old)
            except (TypeError, ValueError):
                continue
            setattr(os2, attr, int(round(v * factor)))

    # \u2500\u2500 6. head.unitsPerEm \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    tt["head"].unitsPerEm = upm_new

    # \u2500\u2500 7. post.italicAngle: left alone (typographic angle, not geometry).

    if verbose:
        print(f"      scale pass complete (UPM now {upm_new}).")


def _maybe_hint_tune(f: Font, mode: str, verbose: bool) -> bool:
    """
    Run AFDKO's otfautohint on a freshly-converted CFF font.

    ``mode`` follows the same convention as otf_optimize-v12's --hint-tune:
        off   → no hinting
        auto  → UPM-aware strength (1000→0.70, 2048→0.85, else 0.75)
        float → explicit strength in [0, 1]

    Returns True if hints were applied, False otherwise.
    """
    if mode == "off":
        return False
    if not f.is_ps:
        if verbose:
            print("      ⓘ  --hint-tune skipped: font is not CFF/PostScript.")
        return False
    if not _has_afdko_tx():
        print(
            "      ⚠ --hint-tune skipped: AFDKO `tx` binary not on PATH.\n"
            "        Install afdko (pip install afdko) and ensure ~/.local/bin is on PATH.",
            file=sys.stderr,
        )
        return False

    if mode == "auto":
        upm = f.ttfont["head"].unitsPerEm
        strength = 0.70 if upm == 1000 else (0.85 if upm == 2048 else 0.75)
    else:
        strength = float(mode)
        if not (0.0 <= strength <= 1.0):
            raise ValueError(f"--hint-tune strength must be in [0, 1], got {mode}")

    # Build kwargs the same way otf_optimize-v12 does:
    #   <0.3 → standard pass
    #   <0.7 → hintAll=False (default)
    #   >=0.7 → hintAll=True, allowChanges=True, StemSnap scaled to UPM
    if strength < 0.3:
        kwargs: dict = {}
    elif strength < 0.7:
        kwargs = {"hintAll": False}
    else:
        upm = f.ttfont["head"].unitsPerEm
        kwargs = {
            "hintAll": True,
            "allowChanges": True,
            "hintSetRange": True,
        }
        # otfautohint StemSnap kwargs aren't always exposed; only pass when relevant.

    from foundrytools.app.otf_autohint import run as otf_autohint_run

    if verbose:
        print(f"      → Running AFDKO otfautohint (strength={strength:.2f})...")

    try:
        # otf_autohint_run expects a foundrytools.Font, not a raw TTFont.
        otf_autohint_run(f, **kwargs)
        return True
    except Exception as e:
        print(f"      ⚠ otfautohint failed: {e}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Core: convert one font
# ─────────────────────────────────────────────────────────────────────────────


def convert_one(
    src_path: Path,
    out_path: Path,
    *,
    tolerance: float = 0.5,
    correct_contours: bool = True,
    extra_correct: bool = False,
    hint_tune: str = "off",
    instance_request: str | None = None,
    scale: float = 1.0,
    verbose: bool = True,
) -> bool:
    """
    Convert a single TTF (or VF) to OTF. Returns True on success, False on failure.

    On failure, an error message is printed to stderr and the partial output file
    (if any) is left untouched. The caller decides whether to clean it up.
    """
    src_size = src_path.stat().st_size
    _log(f"\n  {src_path.name}  ({_human(src_size)})", verbose=verbose)
    _log(f"    → {out_path}", verbose=verbose)

    t_start = time.time()
    f = Font(str(src_path))

    try:
        # ── 1. Variable → static (if needed) ────────────────────────────────
        if f.is_variable:
            if instance_request is None:
                if verbose:
                    n_inst = len(f.t_fvar.table.instances)
                    print(f"    ⓘ  No --instance given; picking 'Regular' (or first of {n_inst} instances).")
            elif verbose:
                print(f"    ⓘ  --instance {instance_request!r} requested.")
        f = _static_font(f, instance_request, verbose=verbose)
        t_static = time.time()

        # ── 1b. Optional uniform geometry/metric scale ─────────────────────
        if scale != 1.0:
            _scale_font(f, scale, verbose=verbose)

        # ── 2. Reject if input is already OTF (nothing to convert) ──────────
        if f.is_ps:
            raise FontError(
                f"{src_path.name} is already a PostScript font; nothing to convert."
            )

        glyph_count = len(f.ttfont.getGlyphOrder())
        _log(f"    → {glyph_count} glyphs, UPM={f.ttfont['head'].unitsPerEm}", verbose=verbose)

        # ── 3. TTF → OTF (CFF) ──────────────────────────────────────────────
        _log(
            f"    → to_otf(tolerance={tolerance}, correct_contours={correct_contours})...",
            verbose=verbose,
        )
        t0 = time.time()
        f.to_otf(tolerance=tolerance, correct_contours=correct_contours)
        t_conv = time.time()
        _log(f"      ✓ conversion done in {t_conv - t0:.2f}s", verbose=verbose)

        # ── 4. Optional second correct_contours pass ────────────────────────
        if extra_correct:
            _log("    → extra correct_contours pass (skia-pathops cleanup)...", verbose=verbose)
            t1 = time.time()
            touched = f.correct_contours(
                remove_hinting=True,
                ignore_errors=True,
                remove_unused_subroutines=True,
                min_area=25,
            )
            t_extra = time.time()
            _log(
                f"      ✓ {len(touched)} glyph(s) touched in {t_extra - t1:.2f}s",
                verbose=verbose,
            )

        # ── 5. Optional CFF hint-tune ───────────────────────────────────────
        hinted = _maybe_hint_tune(f, hint_tune, verbose)

        # ── 6. Save ─────────────────────────────────────────────────────────
        out_path.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(out_path), reorder_tables=True)
        out_size = out_path.stat().st_size

        elapsed = time.time() - t_start
        ratio = (out_size / src_size) if src_size else 0.0
        _log(
            f"    ✅ {_human(src_size)} → {_human(out_size)} "
            f"({ratio:.2f}×) in {elapsed:.2f}s"
            f"{'  [hinted]' if hinted else ''}",
            verbose=verbose,
        )
        return True

    except FontError as e:
        print(f"    ❌ {e}", file=sys.stderr)
        return False
    except Var2StaticError as e:
        print(f"    ❌ Instance selection failed: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"    ❌ Unexpected error: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
        return False
    finally:
        try:
            f.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# CLI + batch driver
# ─────────────────────────────────────────────────────────────────────────────


def _collect_ttf_paths(input_path: Path, recursive: bool) -> list[Path]:
    """Yield TTF paths (case-insensitive) under input_path (file or dir)."""
    if input_path.is_file():
        return [input_path]

    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    if recursive:
        return sorted(p for p in input_path.rglob("*") if p.suffix.lower() == ".ttf")

    return sorted(p for p in input_path.glob("*") if p.suffix.lower() == ".ttf")


def _derive_out_path(src: Path, input_root: Path, output_dir: Path) -> Path:
    """Mirror the input directory tree under output_dir, replacing .ttf → .otf."""
    rel = src.relative_to(input_root) if input_root else src.name
    return (output_dir / rel).with_suffix(".otf")


def main() -> None:
    p = argparse.ArgumentParser(
        prog="ttf2otf_foundry_v1.py",
        description=(
            "TTF → OTF (CFF) converter using foundrytools. "
            "Fast, dependency-light, with a quality-vs-size knob."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quality knobs (in order of impact):
  --tolerance N          qu2cu deviation in font units (default 0.5)
                         smaller = higher fidelity, larger file
                         0.1  near-exact (largest files)
                         0.5  high fidelity (recommended)
                         1.0  foundrytools default (balanced)
                         2.0+ aggressive simplification (visible drift)
  --no-correct-contours  skip skia-pathops cleanup (NOT recommended; saves ~5%)
  --extra-correct        second skia-pathops pass AFTER to_otf (catches drift)

Variable fonts:
  --instance NAME        pick named instance (matches subfamilyNameID or
                         postscriptNameID, case-insensitive substring).
                         Default: first instance (usually "Regular").

Hinting (optional, requires AFDKO `tx` on PATH):
  --hint-tune off        no hinting (default)
  --hint-tune auto       UPM-aware strength (1000→0.70, 2048→0.85)
  --hint-tune N          explicit strength in [0, 1]

Uniform scale (optional — applied BEFORE TTF→OTF conversion):
  --scale F              multiply all geometry and metrics by F (default 1.0).
                          e.g. --scale 0.9 → 90% size, UPM 1000→900.
                          Outlines, advance widths, sidebearings, hhea/OS/2
                          metrics all scale proportionally. cmap/name/post
                          left intact.

Examples:
  # Single file, default quality:
  python ttf2otf_foundry_v1.py font.ttf out.otf

  # Whole directory tree, high fidelity + extra cleanup + hint:
  python ttf2otf_foundry_v1.py ./ttf/ ./otf/ --recursive --tolerance 0.5 \\
                               --extra-correct --hint-tune auto

  # VF → OTF picking the "Bold" instance:
  python ttf2otf_foundry_v1.py Variable.ttf out_bold.otf --instance Bold

  # Shrink output to 90% (smaller glyphs, smaller advance widths):
  python ttf2otf_foundry_v1.py font.ttf out.otf --scale 0.9
""",
    )

    p.add_argument("input", type=Path, help="Input TTF file or directory of TTFs.")
    p.add_argument(
        "output",
        type=Path,
        help="Output OTF file (if input is a file) or output directory (if input is a dir).",
    )

    g = p.add_argument_group("Quality")
    g.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        metavar="N",
        help="qu2cu deviation tolerance in font units (default: 0.5).",
    )
    g.add_argument(
        "--no-correct-contours",
        dest="correct_contours",
        action="store_false",
        help="Skip skia-pathops overlap removal (NOT recommended).",
    )
    g.add_argument(
        "--extra-correct",
        action="store_true",
        help="Run a second skia-pathops pass after to_otf for extra cleanup.",
    )
    g.add_argument(
        "--hint-tune",
        default="off",
        metavar="MODE",
        help="CFF hint tuning: 'off' (default), 'auto', or float in [0, 1].",
    )

    p.add_argument(
        "--instance",
        default=None,
        metavar="NAME",
        help="For VF inputs, pick a named instance by subfamily/postscript name.",
    )
    g2 = p.add_argument_group("Geometry")
    g2.add_argument(
        "--scale",
        type=float,
        default=1.0,
        metavar="F",
        help=(
            "Uniformly scale all glyph geometry, advance widths, sidebearings, "
            "and OT metrics by F. Updates unitsPerEm. Default: 1.0 (no change). "
            "Example: --scale 0.9 makes 1000-UPM font → 900-UPM at 90%% size."
        ),
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="When input is a directory, recurse into subdirectories.",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress per-file progress output."
    )

    args = p.parse_args()
    verbose = not args.quiet

    # ── Banner ────────────────────────────────────────────────────────────
    if verbose:
        print("=" * 64)
        print("  ttf2otf_foundry_v1.py — TTF → OTF via foundrytools")
        print("=" * 64)
        print(f"  Input:        {args.input}")
        print(f"  Output:       {args.output}")
        print(f"  Tolerance:    {args.tolerance} font units")
        print(f"  Scale:        \u00d7{args.scale}")
        print(f"  Correct:      {'yes' if args.correct_contours else 'NO (skia cleanup disabled)'}")
        print(f"  Extra pass:   {'yes' if args.extra_correct else 'no'}")
        print(f"  Hint-tune:    {args.hint_tune}")
        print(f"  AFDKO `tx`:   {'available' if _has_afdko_tx() else 'NOT on PATH (hint-tune will skip)'}")
        if args.input.is_dir() or args.input.is_file():
            pass  # input validated below
        print()

    # ── Resolve inputs ────────────────────────────────────────────────────
    try:
        ttf_paths = _collect_ttf_paths(args.input, args.recursive)
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if not ttf_paths:
        print(f"⚠ No .ttf files found under {args.input}", file=sys.stderr)
        sys.exit(1)

    # ── Single-file mode: output is the literal output path ───────────────
    if len(ttf_paths) == 1 and args.input.is_file():
        src = ttf_paths[0]
        out = args.output
        if out.suffix.lower() != ".otf":
            print(f"⚠ Output extension is {out.suffix!r}; expected .otf", file=sys.stderr)
        ok = convert_one(
            src,
            out,
            tolerance=args.tolerance,
            correct_contours=args.correct_contours,
            extra_correct=args.extra_correct,
            hint_tune=args.hint_tune,
            instance_request=args.instance,
            scale=args.scale,
            verbose=verbose,
        )
        sys.exit(0 if ok else 1)

    # ── Batch mode: mirror directory tree ─────────────────────────────────
    args.output.mkdir(parents=True, exist_ok=True)
    input_root = args.input if args.input.is_dir() else args.input.parent
    successes = 0
    failures: list[tuple[Path, str]] = []

    if verbose:
        print(f"📁 Converting {len(ttf_paths)} TTF file(s)...\n")

    for src in ttf_paths:
        out = _derive_out_path(src, input_root, args.output)
        ok = convert_one(
            src,
            out,
            tolerance=args.tolerance,
            correct_contours=args.correct_contours,
            extra_correct=args.extra_correct,
            hint_tune=args.hint_tune,
            instance_request=args.instance,
            scale=args.scale,
            verbose=verbose,
        )
        if ok:
            successes += 1
        else:
            failures.append((src, "see error above"))

    # ── Summary ───────────────────────────────────────────────────────────
    if verbose:
        print()
        print("=" * 64)
        print(f"  ✅ {successes}/{len(ttf_paths)} converted")
        if failures:
            print(f"  ❌ {len(failures)} failed:")
            for path, reason in failures:
                print(f"      - {path.name}: {reason}")
        print("=" * 64)

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
