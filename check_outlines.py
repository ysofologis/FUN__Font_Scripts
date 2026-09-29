"""OTF/CFF topology check + auto-fix via AFDKO checkoutlinesufo.

Wraps ``foundrytools.app.otf_check_outlines.run`` which under the hood
calls AFDKO's ``checkoutlinesufo`` tool. That tool:

  - converts the input OTF/CFF to UFO,
  - walks every glyph and runs a battery of topology checks
    (overlapping contours, coincident points, colinear lines, flat curves,
    tiny sub-paths, wrong-direction paths, etc.),
  - with ``--error-correction-mode`` (always enabled by foundrytools),
    repairs problems in-place rather than just reporting them,
  - converts the fixed UFO back to OTF/CFF.

For v12's ``--check-outlines`` flag, we want this on the **final
topology** so any bugs the v12 transforms (scale/thicken/spacing) may
have introduced get caught and cleaned before save.

Why it runs BEFORE ``--hint-tune``:
  - ``checkoutlinesufo`` may rewrite charstrings (fixing overlaps
    produces new paths).
  - The autohinter then sees the cleaned outlines AND builds hints
    for the final paths (otherwise we'd hint bad topology and then
    clean it, leaving hints pointing at the wrong stem widths).

Why it runs AFTER ``--correct`` / scale / thicken / spacing:
  - ``--correct`` does Skia pathops overlap removal (fast, no temp file).
  - ``checkoutlinesufo`` is slower (converts OTF->UFO->OTF, ~6s for
    SourceCodePro's 1568 glyphs) but more thorough: catches things
    Skia misses (coincident points, colinear lines, flat curves,
    tiny paths, wrong winding).

Public API:
    check_outlines(font) -> bool
    HAS_FOUNDRYTOOLS, HAS_TX   -- capability flags.

Mirrors the shape of correct.py / zones.py / thicken.py: small focused
module, optional deps, graceful skip + warning.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from fontTools.ttLib import TTFont

log = logging.getLogger(__name__)

# foundrytools is OPTIONAL. Required by otf_check_outlines.
try:
    from foundrytools import Font as _FtFont
    from foundrytools.app.otf_check_outlines import run as _check_outlines_run
    HAS_FOUNDRYTOOLS = True
except ImportError:
    _FtFont = None  # type: ignore[assignment]
    _check_outlines_run = None  # type: ignore[assignment]
    HAS_FOUNDRYTOOLS = False

# checkoutlinesufo (and foundrytools' wrapper) require the AFDKO `tx`
# binary on PATH for OTF/CFF -> UFO conversion. tx is shipped with
# AFDKO but lives in ~/.local/bin which is often NOT on PATH in
# default shells. Detect via `which` so we can give a precise error.
HAS_TX = shutil.which("tx") is not None


def check_outlines(font: TTFont) -> bool:
    """Run checkoutlinesufo topology audit + auto-fix on the font.

    Returns True if processing ran successfully (regardless of whether
    any glyphs were actually modified), False if skipped.

    Skips (returns False + logs) when:
      - foundrytools is not installed
      - ``tx`` binary (AFDKO) is not on PATH
      - font is not CFF/PostScript (TTF/glyf has no CFF topology to audit;
        the underlying tool only handles CFF/UFO/CFF2, not glyf outlines)

    Implementation note: foundrytools' Font(f) does a BytesIO round-trip
    (creates a NEW TTFont, not a wrapper around f). So we:

      1. Save the raw TTFont to a temp file
      2. Open it via foundrytools (file-path mode avoids the round-trip)
      3. Run otf_check_outlines on the foundrytools Font (it auto-saves
         to its own internal temp file, runs checkoutlinesufo, reloads)
      4. Reload the modified file as a fresh TTFont
      5. Copy the CFF table back into our original font

    This mirrors the --hint-tune pattern (HINT_TAGS round-trip) but
    copying CFF is safe here because the wrapper doesn't trigger the
    ``getGlyphOrder()`` invariant violation that the hint-tables copy
    does -- we're swapping a single table for a single table.
    """
    if not HAS_FOUNDRYTOOLS:
        log.warning(
            "foundrytools not installed; --check-outlines skipped. "
            "pip install foundrytools to enable."
        )
        return False
    if not HAS_TX:
        log.warning(
            "AFDKO `tx` binary not on PATH; --check-outlines skipped. "
            "Add ~/.local/bin to PATH or install afdko to enable."
        )
        return False
    if "CFF " not in font:
        log.info("--check-outlines skipped: not a CFF/PostScript font "
                 "(TTF uses TrueType instructions, not CFF topology)")
        return False

    # --- Save raw TTFont to temp file ------------------------------------
    tmp_path: Path | None = None
    pre_size: int = 0
    post_size: int = 0
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".otf", delete=False, dir=tempfile.gettempdir()
        ) as tmp:
            tmp_path = Path(tmp.name)
        font.save(str(tmp_path))
        pre_size = os.path.getsize(tmp_path)

        # --- Open via foundrytools (file-path mode, no BytesIO round-trip)
        ft_font = _FtFont(tmp_path)
        try:
            _check_outlines_run(ft_font, drop_hinting_data=False)
        except Exception as e:
            log.warning(f"checkoutlinesufo failed: {e}; --check-outlines skipped")
            return False
        finally:
            # ft_font.close() releases the underlying TTFont handle.
            try:
                ft_font.close()
            except Exception:
                pass

        post_size = os.path.getsize(tmp_path)
        delta = post_size - pre_size

        # --- Reload the modified file as a fresh TTFont and copy CFF back
        # We re-read instead of using ft_font.ttfont because the wrapper's
        # BytesIO round-trip creates a separate TTFont instance; if we
        # stole ft_font.ttfont the in-flight state of the original `font`
        # would diverge. Reload + table swap is the cleanest cross-check.
        new_font = TTFont(str(tmp_path))
        font.tables["CFF "] = new_font["CFF "]
        log.info(
            f"--check-outlines complete (file: "
            f"{pre_size:,} -> {post_size:,} bytes, delta {delta:+,})"
        )
        return True

    except Exception as e:
        log.warning(f"--check-outlines failed: {e}")
        return False

    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
