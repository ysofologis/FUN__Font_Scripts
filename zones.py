"""OTF/CFF blue-zone + stem-snap recomputation via foundrytools + AFDKO.

Wraps:
  - foundrytools.app.otf_recalc_zones.run   -> BlueValues, OtherBlues
  - foundrytools.app.otf_recalc_stems.run   -> StdHW, StdVW, StemSnapH, StemSnapV

These metadata values drive hint alignment at small sizes. Recomputing
from actual outlines gives font-specific values rather than generic
defaults baked into the source. The two most impactful wins:

1. StemSnapH/V: instead of the v12 --hint-tune fallback ``[UPM/2]``
   (one snap value for the entire stem grid), we get the actual
   dominant stem widths in font units. Hinting snaps to those widths
   for crisper rendering at 8-14pt.

2. BlueValues: the alignment zones for baseline, x-height, cap-height,
   and descender. Foundrytools classifies the union of all glyph
   bounds in each category (A-Z, a-z, ascender, descender), picks the
   two most-common values per zone, and writes a properly ordered,
   non-overlapping list. Bad values = blobs at small sizes.

Public API:
    recalc_zones(font) -> bool
    HAS_FOUNDRYTOOLS, HAS_AFDKO   -- capability flags.

Mirrors the shape of thicken.py and correct.py: small focused module,
optional deps, graceful skip + warning.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fontTools.ttLib import TTFont

log = logging.getLogger(__name__)

# foundrytools is OPTIONAL. Both features below require it.
try:
    from foundrytools import Font as _FtFont
    from foundrytools.app.otf_recalc_stems import run as _recalc_stems_run
    from foundrytools.app.otf_recalc_zones import run as _recalc_zones_run
    HAS_FOUNDRYTOOLS = True
except ImportError:
    _FtFont = None  # type: ignore[assignment]
    _recalc_stems_run = None  # type: ignore[assignment]
    _recalc_zones_run = None  # type: ignore[assignment]
    HAS_FOUNDRYTOOLS = False

# AFDKO is required by otf_recalc_stems (it parses the file via AFDKO's
# own fontWrapper, not via fontTools). Detected separately so we can give
# a precise error message rather than a generic ImportError deep inside.
try:
    import afdko  # noqa: F401
    HAS_AFDKO = True
except ImportError:
    HAS_AFDKO = False


def _apply_zone_and_stem_values(font: TTFont, other_blues: list[int],
                                blue_values: list[int],
                                std_hw: int | None, std_vw: int | None,
                                stem_snap_h: list[int] | None,
                                stem_snap_v: list[int] | None) -> None:
    """Write the recomputed metadata directly into the raw TTFont's CFF table.

    We use the raw fontTools CFF access pattern because we already have a
    raw ``TTFont`` from the main pipeline. foundrytools' ``Font`` wrapper
    does a BytesIO round-trip (``Font(f)`` creates a NEW TTFont, not a
    wrapper around ``f``), so we'd lose any in-flight mutations otherwise.

    Critical CFF gotcha (same one that bit --hint-tune): ``TopDict`` and
    ``PrivateDict`` inherit from ``BaseDict`` which caches accessed values
    as instance attrs. Mutating ``td.rawDict[...]`` after any read of
    ``td.someField`` is silently dropped on save. We must also call
    ``setattr(td, ...)`` to invalidate the cache.
    """
    top_dict = font["CFF "].cff.topDictIndex[0]
    private_dict = top_dict.Private

    if other_blues is not None:
        private_dict.rawDict["OtherBlues"] = list(other_blues)
        setattr(private_dict, "OtherBlues", list(other_blues))
    if blue_values is not None:
        private_dict.rawDict["BlueValues"] = list(blue_values)
        setattr(private_dict, "BlueValues", list(blue_values))
    if std_hw is not None:
        private_dict.rawDict["StdHW"] = std_hw
        setattr(private_dict, "StdHW", std_hw)
    if std_vw is not None:
        private_dict.rawDict["StdVW"] = std_vw
        setattr(private_dict, "StdVW", std_vw)
    if stem_snap_h is not None:
        private_dict.rawDict["StemSnapH"] = list(stem_snap_h)
        setattr(private_dict, "StemSnapH", list(stem_snap_h))
    if stem_snap_v is not None:
        private_dict.rawDict["StemSnapV"] = list(stem_snap_v)
        setattr(private_dict, "StemSnapV", list(stem_snap_v))


def recalc_zones(font: TTFont) -> bool:
    """Recompute OTF/CFF blue zones + stem snaps. Returns True on success.

    Skips (returns False + logs) when:
      - foundrytools or afdko are not installed
      - font is not CFF/PostScript (glyf-only TrueType has no blue zones)

    Stems are computed in a temporary file because AFDKO's
    ``otf_recalc_stems.run`` requires a disk path (it opens the file
    itself via AFDKO's fontWrapper). The temp file is cleaned up in
    a try/finally so it's gone even if computation throws.
    """
    if not HAS_FOUNDRYTOOLS:
        log.warning(
            "foundrytools not installed; --zones skipped. "
            "pip install foundrytools to enable."
        )
        return False
    if not HAS_AFDKO:
        log.warning(
            "afdko not installed; --zones skipped (stems require AFDKO). "
            "pip install afdko to enable."
        )
        return False
    if "CFF " not in font:
        log.info("--zones skipped: not a CFF/PostScript font "
                 "(TTF uses TrueType instructions, not blue zones)")
        return False

    # === Blue zones ===
    # Use the foundrytools Font wrapper for this — it accepts a raw
    # TTFont (via its BytesIO init) and returns (other_blues, blue_values).
    # We don't keep the wrapped Font; we just steal the computed values.
    ft_font = _FtFont(font)
    try:
        other_blues, blue_values = _recalc_zones_run(ft_font)
    except Exception as e:
        log.warning(f"otf_recalc_zones failed: {e}; --zones skipped")
        return False

    # === Stem snaps ===
    # otf_recalc_stems.run() wants a file path. Save to a temp file,
    # run stems, delete the temp file.
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".otf", delete=False, dir=tempfile.gettempdir()
        ) as tmp:
            tmp_path = Path(tmp.name)
        font.save(str(tmp_path))
        std_hw, std_vw, stem_snap_h, stem_snap_v = _recalc_stems_run(tmp_path)
    except Exception as e:
        log.warning(f"otf_recalc_stems failed: {e}; --zones skipped")
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return False
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    # === Apply ===
    _apply_zone_and_stem_values(
        font, other_blues, blue_values,
        std_hw, std_vw, stem_snap_h, stem_snap_v,
    )
    log.info(
        f"Recomputed OTF metadata: BlueValues={blue_values}, "
        f"OtherBlues={other_blues}, StdHW={std_hw}, StdVW={std_vw}, "
        f"StemSnapH={stem_snap_h}, StemSnapV={stem_snap_v}"
    )
    return True
