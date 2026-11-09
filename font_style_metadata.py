#!/usr/bin/env python3
"""Shared OS/2 style-metadata helpers for the ttf2otf converters.

WHY THIS EXISTS
    A family whose faces all declare OS/2.usWeightClass = 400 (Absolut Pro:
    all 24 faces) gives fontconfig 24 equally-valid Regular candidates, so
    `fc-match "Absolut Pro"` ties and the winner is arbitrary -- it came
    back with AbsolutPro-Black. The OS/2 fsSelection table made it worse:
    the source sets REGULAR on every face, and the converters copied that
    bit onto faces they had just marked BOLD, producing the
    self-contradictory fsSelection = 0x0060 (REGULAR|BOLD).

    The rules live here so the FontForge converter (ttf2otf_ff_v5.py) and
    the fontTools converter (ttf2otf_ft_v6.py) cannot drift apart. They
    used to disagree about vertical metrics too, and that divergence is
    exactly what made the original bug hard to see.

WHAT THIS MODULE OWNS
    * the canonical CSS-style weight value for a style token
    * which face of a family may claim the REGULAR bit
    * how to resolve fsSelection without leaking REGULAR onto a bold face
    * how to keep head.macStyle in agreement with fsSelection

IT DOES NOT OWN
    the name table, the glyph geometry, or anything else about the font.
    The two converters stay standalone in every other respect: this module
    is imported by name, and a missing copy degrades to "no metadata
    repair" rather than a crash.

Usage:
    from font_style_metadata import (resolve_weight_class,
                                     resolve_fs_selection,
                                     style_bits, is_regular_weight)

Requires: nothing beyond the standard library.
"""
from __future__ import annotations

import re

__all__ = [
    "is_italic",
    "is_bold",
    "is_bold_word",
    "is_style_word",
    "style_bits",
    "is_regular_weight",
    "weight_class_for",
    "resolve_weight_class",
    "resolve_fs_selection",
    "STYLE_WORDS",
]

# ---------------------------------------------------------------------------
# bold / italic detection
# ---------------------------------------------------------------------------

def is_italic(*texts):
    """True when any of the given strings names an italic or oblique face."""
    low = " ".join(t for t in texts if t).lower()
    return "italic" in low or "oblique" in low


def is_bold_word(w):
    """True for words that actually mean bold.

    Substring matching is wrong here: 'Extra Bold' and 'Semi Bold' both
    contain 'bold', but collapsing them onto plain 'Bold' makes three
    distinct weights claim one name ID 17, which is the collision Golca
    reported. Exact tokens are matched instead, with the common compounds
    spelled out.
    """
    k = re.sub(r"[^a-z]", "", w.lower())
    return k in ("bold", "black", "heavy", "extrabold", "extrablack",
                 "ultrabold", "ultrablack", "semibold", "demibold",
                 "heavyitalic", "blackitalic", "bolditalic")


def is_bold(*texts):
    """True when any of the given strings names a bold face."""
    return any(is_bold_word(w) for t in texts if t
               for w in re.split(r"[^A-Za-z]+", t) if w)


# Style words that carry no bold/italic bit but still name a distinct face.
# Without these a "Thin" or "Extra Light" face recovers nothing and falls
# back to REGULAR, which is what made Golca's light weights collide with
# its regular.
STYLE_WORDS = frozenset((
    "regular", "italic", "oblique", "bold", "black", "heavy", "extrabold",
    "extrablack", "ultrabold", "ultrablack", "semibold", "demibold",
    "book", "medium", "roman", "normal", "light", "extralight", "ultralight",
    "thin", "hairline", "extrathin", "ultrathin", "condensed", "expanded",
    "semilight", "demilight", "spaced", "display", "text", "mono",
    # Intensity qualifiers. Without these 'Extra Bold' lost the 'Extra' and
    # collapsed onto plain 'Bold', so Extra Bold and Semi Bold and Bold all
    # claimed one name ID 17 -- the collision that started this.
    "extra", "ultra", "semi", "demi", "super", "hyper",
))


def is_style_word(w):
    """True when the word names a distinct face of a family."""
    if not w:
        return False
    key = re.sub(r"[^a-z]", "", w.lower())
    return bool(key) and (key in STYLE_WORDS
                          or "bold" in key or "black" in key
                          or "light" in key or "italic" in key
                          or "oblique" in key)


def style_bits(family, style, ps):
    """Derive (bold, italic) from the face's own names."""
    return is_bold(style, ps), is_italic(style, ps, family)


# ---------------------------------------------------------------------------
# weight class
# ---------------------------------------------------------------------------

# Canonical CSS-style usWeightClass values for a style token. These are the
# CSS font-weight keywords, which is what fontconfig expects when it scores
# a face against a requested weight.
WEIGHT_TOKENS = {
    "hairline": 100, "thin": 100, "extrathin": 100, "ultrathin": 100,
    "extralight": 200, "ultralight": 200,
    "light": 300, "semilight": 300, "demilight": 300,
    "book": 400, "regular": 400, "normal": 400, "roman": 400, "text": 400,
    "medium": 500,
    "semibold": 600, "demibold": 600,
    "bold": 700,
    "extrabold": 800, "ultrabold": 800,
    "extrablack": 900, "ultrablack": 950,
    "fat": 800,
    "black": 900, "heavy": 900,
}

# Two-word intensities: the prefix is part of the weight name, so it cannot
# be resolved by looking up the second word alone ("Extra Bold" is 800, not
# 700; "Ultra Light" is 200, not 300). The key is the joined pair.
WEIGHT_COMPOUNDS = {
    "extrabold": 800, "ultrabold": 800,
    "semibold": 600, "demibold": 600,
    "extralight": 200, "ultralight": 200,
    "semilight": 300, "demilight": 300,
    "extrathin": 100, "ultrathin": 100,
    "extrablack": 900, "ultrablack": 950,
    "superblack": 950, "hyperblack": 950,
    "extrabook": 400, "ultrabook": 400,
    "extrabolditalic": 800, "ultrabolditalic": 800,
    "semibolditalic": 600, "demibolditalic": 600,
}


def weight_class_for(style):
    """Derive OS/2.usWeightClass from a face's style name.

    Returns None when the style carries no weight word at all, so the
    caller can keep the source value.
    """
    if not style:
        return None
    tokens = [t.lower() for t in re.split(r"[^A-Za-z]+", style) if t]

    # The prefix is part of the weight name, so the pair is resolved first:
    # "Extra Bold" is 800, not 700, and "Ultra Light" is 200, not 300.
    for i in range(len(tokens) - 1):
        val = WEIGHT_COMPOUNDS.get(tokens[i] + tokens[i + 1])
        if val is not None:
            return val

    for tok in tokens:
        val = WEIGHT_TOKENS.get(tok)
        if val is not None:
            return val
    return None


def resolve_weight_class(src_weight, style):
    """Pick the OS/2.usWeightClass to write.

    A source weight of 400 is treated as a placeholder rather than a fact:
    every face of a family that never updated its OS/2 table declares 400,
    which makes them all Regular candidates in fontconfig. When the style
    name names a definite non-400 weight, that name wins.

    A source weight that is anything else is taken at face value. Some
    families use a non-CSS value on purpose (PFBeauSansPro-Thin declares
    200 for its "Thin" cut), and second-guessing that would be a regression.
    """
    derived = weight_class_for(style)
    if derived is None:
        return src_weight
    if src_weight in (None, 400) and derived != 400:
        return derived
    return src_weight


# ---------------------------------------------------------------------------
# fsSelection
# ---------------------------------------------------------------------------

# OS/2 fsSelection bits.
FS_ITALIC = 0x0001
FS_BOLD = 0x0020
FS_REGULAR = 0x0040
FS_USE_TYPO_METRICS = 0x0100
# The style bits plus REGULAR. USE_TYPO_METRICS (bit 8) is unrelated to
# bold/italic and must survive: dropping it makes a font fall back on
# hhea/win metrics and shifts every line of text.
FS_STYLE_MASK = FS_ITALIC | FS_BOLD | FS_REGULAR


def is_regular_weight(style):
    """True when a style name denotes the Regular weight of its family.

    'Regular', 'Book' and 'Roman' are the names a family gives its
    weight-400 cut; 'Thin', 'Light', 'Medium', 'Bold' and 'Black' are not.
    """
    if not style:
        return True
    tokens = [t.lower() for t in re.split(r"[^A-Za-z]+", style) if t]
    if not tokens:
        return True
    # A style that names any weight other than the regular one is not it.
    for tok in tokens:
        if tok in ("italic", "oblique"):
            continue
        if tok in WEIGHT_TOKENS:
            return WEIGHT_TOKENS[tok] == 400
    return True


def resolve_fs_selection(src_selection, style, resolved_style=None):
    """Resolve OS/2.fsSelection from the source value and the style name.

    src_selection is the source font's own fsSelection (0 if unknown).
    style is the legacy subfamily, resolved_style the typographic one
    (name ID 17) when the source has it -- resolved_style wins.

    The source's own bits are trusted when it sets BOLD or ITALIC; those
    are facts. REGULAR is never carried over: it is mutually exclusive
    with BOLD and ITALIC, and a source that sets REGULAR on every face
    (Absolut Pro does -- all 24 faces ship fsSelection=0x0040) otherwise
    leaks REGULAR onto faces that were just marked BOLD, giving the
    self-contradictory 0x0060 (REGULAR|BOLD).

    REGULAR is granted only to the face that is neither bold nor italic
    AND is not a lighter or heavier cut, so a family has exactly one
    Regular and 'Thin'/'Light'/'Medium' stop competing for the slot.
    """
    bold, italic = style_bits("", resolved_style or style, "")
    src = src_selection or 0
    if src & (FS_ITALIC | FS_BOLD):
        bold = bool(src & FS_BOLD)
        italic = bool(src & FS_ITALIC)

    val = src & ~FS_STYLE_MASK
    if italic:
        val |= FS_ITALIC
    if bold:
        val |= FS_BOLD
    if not (val & (FS_ITALIC | FS_BOLD)) and is_regular_weight(
            resolved_style or style):
        val |= FS_REGULAR
    return val
