# thicken.py & otf_optimize-v13/v14 — Recipe Cookbook

Working CLI invocations for common scenarios. All commands assume the standard
install path (`pip install fonttools pathops foundrytools`; for `otf_optimize-v14`
also `pip install ttfautohint` for TrueType autohinting).

The **two fundamental modes** are:

- **Curve mode (v13 default, `--flatten 0`)** — curves preserved as Beziers.
  Best for body text and subtle thicken. May leave small halos at inside-of-curve
  points when combined with `--stem-quantize`.
- **Polygon mode (v14, `--flatten 8+`)** — curves linearized before stroke.
  Eliminates halos, exact stem quantization. File size grows 2-5×.

Use `--flatten` to switch modes. All other knobs compose with both.

---

## 1. Body text, subtle thicken (most common)

```bash
python thicken.py input.ttf output.ttf --thickness 2.5
```

Same as v13. Curves preserved. ~5-15% file size growth. No halo risk.

## 2. Display / heavy thicken, fringe-free

```bash
python thicken.py input.ttf output.ttf --thickness 8 \
    --flatten 8 --grid 1.0 --simplify-epsilon 1.0 --stem-quantize 10
```

Curves linearized to 8-segment polygons before stroke. Stroked path is an
offset polygon → epsilon-merge operates on real vertices → halos vanish.
Stem widths land on multiples of 10u (pixel-perfect on 1000-UPM at 16px).
File size 2-5× larger.

## 3. Pixel-perfect stems (CJK-friendly)

```bash
python thicken.py input.ttf output.ttf --thickness 5 \
    --flatten 12 --grid 1.0 --stem-quantize 20
```

Heavy flatten (12 segments/curve) for very tight curves on CJK ideographs.
Stem quantize 20u → chunky/poster look.

## 4. Maximum hinting determinism

```bash
python thicken.py input.ttf output.ttf --thickness 2.5 \
    --grid 2.0 --simplify-epsilon 2.0 --stem-quantize 10 \
    --flatten 4
```

Aggressive quantization in BOTH geometry (grid=2u, epsilon=2u) AND stem
width (10u). Mild flatten (4 segments/curve). Result: every coordinate on
the 2u grid, every stem a multiple of 10u. Autohinters produce nearly
identical output across size ramps.

## 5. Bold conversion (display + heavy)

```bash
python thicken.py input.ttf output.ttf --thickness 15 \
    --flatten 8 --grid 1.0 --stem-quantize 20 \
    --correct
```

Heavy thicken. Use `--correct` first if the source font has bad topology
(foundrytools contour correction runs once, before thicken).

## 6. Full optimize pipeline (the proven command)

```bash
python otf_optimize-v14.py \
    --scale 5 --thickness 5 \
    --thicken-flatten 8 --thicken-grid 1.0 \
    --thicken-epsilon 1.0 --thicken-stem-quantize 10 \
    --correct --zones --check-outlines --hint-tune auto \
    in/ out/
```

- `--scale 5` → uniform scale up 5%
- `--thickness 5` → +5% stem thickness (in polygon mode)
- `--thicken-flatten 8` → linearize curves first (fringe-free)
- `--thicken-grid 1.0` → snap coords to 1u grid
- `--thicken-epsilon 1.0` → aggressive epsilon-merge
- `--thicken-stem-quantize 10` → stems land on 10u multiples
- `--correct` → contour correction pass
- `--zones` → recompute CFF BlueValues/StemSnap
- `--check-outlines` → AFDKO topology audit
- `--hint-tune auto` → re-hint with auto-picked strength from UPM

## 7. Display Black / Heavy weight (Latin, /20u grid)

```bash
python otf_optimize-v14.py \
    --scale 1.0 --thickness 10.0 \
    --thicken-flatten 8 --thicken-grid 20.0 \
    --thicken-stem-quantize 20 --thicken-epsilon 1.0 \
    --spacing 5 \
    ./ff-nort/ ./output/
```

- `--scale 1.0` → no scaling (preserve UPM and metrics)
- `--thickness 10.0` → +10u stem widen (≈Bold/Black territory at 1000-UPM)
- `--thicken-flatten 8` → polygon-mode: stem_quantize becomes EXACT on polygon
  edges (no inside-of-curve halos at heavy stroke widths)
- `--thicken-grid 20.0` → snap coords to /20u grid
- `--thicken-stem-quantize 20` → perpendicular snap to /20u (matches grid → single lattice)
- `--thicken-epsilon 1.0` → aggressive union-merge of overlapping contours
- `--spacing 5` → tolerance for contour pairing post-union (keeps paired contours together)

**Why this combo works:** grid and stem-quantize share the same /20u lattice,
so stem edges and surrounding contours snap to the same grid → no fractional
half-pixels where stems meet diagonals. `--flatten 8` makes stem_quantize exact
on linearized curves instead of approximate on control polygon → pixel-perfect
stems at `--thickness 10`. Use for Latin display cuts where you want a single
"heavy" weight without shipping a full weight axis.

Verified on **ff-nort** (`/home/developer/Downloads/Fonts/ff-nort/`) — produces
a heavy Black cut from the regular master with no inside-of-curve halos on 'o',
'e', 'p', 'a', 'g'.

**Variants:**
- For even heavier (poster-grade), try `--thickness 15.0`
- For sub-pixel polish on 'S', '&', '%', bump `--thicken-flatten 12` (often byte-identical on Latin)
- For thin/light masters, expect larger relative visual change — stems are thinner so +10u shifts proportionally more

---

## 8. Fix a vendor font's broken OS/2 usWeightClass

```bash
python fix_sadi_sans_weights.py INPUT_DIR OUTPUT_DIR
```

Not a geometry tool — a metadata repair, kept here because it fits the same
"input dir → output dir" shape as the rest of the repo, and because the audit
step is reusable on any family that ships broken weight tables.

**The bug it fixes:** every Sadi Sans `.otf` declared `usWeightClass = 400`
(Regular) regardless of its actual style. The `style` name said "Bold" while
the OS/2 table said "Regular", so every file scored as an *exact* match for a
Regular request and the tie broke arbitrarily. Symptom on Linux:

```bash
fc-match "Sadi Sans"    # -> SadiSans-Bold.otf  (wrong)
```

Fontconfig scores candidates by distance from the requested weight. Because
all 20 files claimed 400, the request `weight=80` (Regular) tied at distance 0
across the whole family, and the Bold file won on tie-break. With correct
tables, Bold sits at distance 120 from Regular and Regular wins outright.

**Audit first** — always confirm the declared weights before rewriting:

```bash
python -c "
from fontTools.ttLib import TTFont
from pathlib import Path
for p in sorted(Path('INPUT_DIR').glob('*.otf')):
    f = TTFont(str(p))
    print(f'{p.name:34s} style={f[\"name\"].getDebugName(2):16s} usWeightClass={f[\"OS/2\"].usWeightClass}')"
```

**Verify after:**

```bash
fc-scan OUTPUT_DIR/SadiSans-Bold.otf | grep weight          # expect 700
fc-match -f '%{weight} %{style[0]}' "Sadi Sans"             # expect 80 Regular
```

**Reusing the audit on another family.** The three steps generalize even though
this script doesn't:
1. Dump `style` name + `usWeightClass` for every file, compare against the
   filename's weight token.
2. Map style name → canonical class (`Thin` 100 … `Heavy` 900) via the
   `EXPECTED` dict at the top.
3. Rewrite only the mismatches; copy correct files through so the output dir
   is always a complete installable family.

**Known remaining issue:** `OS/2.fsSelection` is `0x0040`
(`USE_TYPO_METRICS` only) in every Sadi Sans file — bit 0 (ITALIC) is unset on
italic cuts, bit 5 (BOLD) is unset on bold cuts. fontconfig infers slant from
the `style` name so `fc-match` is correct, but consumers reading
`fsSelection` directly (some PDF rasterizers, certain CSS engines) see no
italic flag. Fix by OR-ing the bit in when patching.

---

## 9. Raise UPM before any geometry pass (1000 → 2048)

```bash
python scale_upem_batch.py INPUT_DIR OUTPUT_DIR --upm 2048
```

**Do this FIRST, before `otf_optimize-v14.py` / `thicken.py`.** The grid and
quantize knobs in those tools are in *absolute font units*, so raising UPM
afterwards silently lands you on a coarser lattice than you asked for. See the
scaling table below.

**What it actually does:** a pure coordinate rescale. Every outline, advance
width, cap/x-height and vertical metric is multiplied by the target ratio; the
em-square itself is redefined. Rendered output is **visually identical** — the
only thing you buy is finer quantization resolution for the passes that follow
(a `--thicken-grid` of `5.0` means half the real-world distance at 2048 UPM).
FontTools' `scaleUpem` visitor updates the CFF `FontMatrix` correctly, so this
works on CFF/OTF and not just `glyf`/TTF.

**Why 2048, not 1000:** UPM is the number of integer coordinate units per
em-square — it exists so outlines can be stored as integers with fine
precision instead of fractional pixels. 1000 UPM is the old TrueType-era
default; 2048 is the modern convention (Microsoft ClearType, and what Inter,
Source Sans and most current families ship). It is *not* a display size —
raising UPM makes text larger on screen.

**Scaling the other knobs** (only the unit-valued ones change):

| Knob | 1000 UPM | 2048 UPM | Why |
|---|---|---|---|
| `--thicken-grid` | `5.0` | `10.0` | absolute font units |
| `--thicken-stem-quantize` | `5` | `10` | absolute font units |
| `--thicken-flatten` | `8` | `8` | segments per cubic, unitless |
| `--thicken-epsilon` | `1.0` | `1.0` | font units, but merge-tolerance scale |
| `--thickness` | `5.0` | `5.0` | percentage — UPM-independent |
| `--scale` | any | any | percentage — UPM-independent |
| `--hint-tune auto` | `0.70` | `0.85` | auto-picks from UPM; stronger at 2048 |

Grid and quantize must stay on the **same lattice** after doubling, or stems
and coordinates snap to different grids and you reintroduce the fractional
half-pixel problem recipe #7 exists to avoid.

**Verify:**

```bash
python -c "
from fontTools.ttLib import TTFont
from pathlib import Path
for p in sorted(Path('OUTPUT_DIR').glob('*.otf'))[:4]:
    f = TTFont(str(p))
    print(f'{p.name:32s} UPM={f[\"head\"].unitsPerEm:5d} cap={f[\"OS/2\"].sCapHeight:5d}')"
# expect UPM=2048 and cap ≈ 2.048x the input (Regular: 681 -> 1395)
```

**Expected side effect:** output files are ~11% *smaller* (e.g. 152,400 →
135,272 bytes). Not loss — rescaling lets CFF re-encode charstrings with
tighter integer precision. Glyph counts are unchanged (the script fails loudly
if they aren't). The `CFF mtx` column reads ~`5e-09` rather than `1/2048`:
fontTools collapses `FontMatrix` to near-zero and bakes the scale into the
charstrings plus `nominalWidthX`/`defaultWidthX` instead. Equivalent result,
different encoding.

**Anti-pattern specific to this step:**

| Don't | Why |
|---|---|
| Thicken first, raise UPM after | Grid/quantize values silently become 2x too coarse |
| Raise UPM after hinting | Hinting was computed for the old UPM; re-hint after |
| Use it to make text look bigger | It doesn't change rendered size — that's `--scale` |
| Run it on variable fonts expecting the axis to follow | See below |

**Variable fonts need separate handling.** `scale_upem` rescale the default
instance geometry, but a variable font's `wght` axis range and `avar` segment
mapping are *not* plain multipliers — the axis coordinates in `fvar` need
explicit rescaling too, or the variable font's `wght` axis silently reports
the old range against the new geometry. The script keeps variable fonts at
UPM 400 as a conventional default; handle them deliberately.

---

## Tuning rules of thumb

| Scenario | Adjust |
|---|---|
| Halos at inside-of-curve points | Increase `--flatten` (4 → 8 → 16) |
| Stems look slightly off-grid at 16px | Add `--stem-quantize 10` (or 20 for chunky) |
| Hint drift across size ramps | `--grid 1.0` + `--stem-quantize 10` |
| Output file too big | Reduce `--flatten` (8 → 4) or accept it |
| Visible polygonal facets | Increase `--flatten` (8 → 16) |
| Body text showing subtle fringe | Switch from curve mode: add `--flatten 8` |
| Display text too smooth | Reduce `--flatten` or remove it |
| Output has overlapping contours | Add `--simplify-epsilon 1.0` |
| Stems too tight after quantize | Lower `--thickness` or `--stem-quantize` |

## Anti-patterns

| Don't | Why |
|---|---|
| `--flatten 0` + `--stem-quantize 10` (heavy) | Quantize operates on curves, halos reappear at inside-of-curve points |
| `--flatten 16` + `--grid 0.25` | Polygons with sub-unit precision is wasteful — flatten to grid |
| `--thickness 20` + `--flatten 0` (heavy) | Bowl drift at high thicken + curves. Use `--flatten 8` |
| `--stem-quantize 10` on CJK/Arabic/Devanagari | The heuristic silently no-ops on most glyphs (no axial stems). Skip the knob or rely on post-process `ttfautohint --quantize-stem-widths` |
| Geometry pass before `scale_upem_batch.py` | Grid/quantize are absolute units; doubling UPM afterwards halves their real-world resolution |
| Expecting `scale_upem` to change rendered size | Pure rescale — use `--scale` for that |
| Pointing `scale_upem_batch.py` at already-converted fonts | Safe (passes through) but pointless; it will not re-scale |

## Picking `--flatten` value

| Value | Use case |
|---|---|
| `0` (default) | Body text, subtle thicken, halos unlikely |
| `4` | Display text, anti-halo insurance |
| `8` | **Most common — recommended starting point** |
| `16` | Sub-pixel quality, sub-pixel at all sizes, large file size |
| `32+` | Only if 8-16 still shows facets on tight curves |

The `_FlatteningPen` is adaptive (De Casteljau midpoint split with a 0.25u
flatness threshold), so curves that are already flat don't get subdivided
beyond their flatness limit. In practice `--flatten 16` produces the same
output as `--flatten 8` for typical Latin glyphs.

## When to NOT use `--flatten`

- Body-text fonts where the visual character must be preserved at small sizes
- Fonts that will be re-hinted by `ttfautohint --quantize-stem-widths` post-process
  (the post-process already handles stem quantization via hints, not geometry)
- Heavy scripts (Arabic, Devanagari) where polygon facets are more visible

For these, stick with curve mode (default, no `--flatten`) and rely on
ttfautohint / AFDKO otfautohint for the final pixel-perfect alignment.

---

## FontForge-only optimizer — `otf_optimize-ff-v2.0.py`

A clean-slate **single-dependency** rewrite: FontForge only, **no fontTools**, no
pathops, no bridge code. This is the path when you want the transform engine
with the smallest possible dependency surface.

### Launchers (both work, identical results)

```bash
fontforge -script otf_optimize-ff-v2.0.py [flags] IN_DIR OUT_DIR
python /usr/bin/python3.14 otf_optimize-ff-v2.0.py [flags] IN_DIR OUT_DIR
```

If FontForge isn't importable, the script stops before touching any font and
names the interpreter to use.

### Flags

| Flag | Effect | Notes |
|------|--------|-------|
| `--width PCT` | Horizontal scale (advance widths + outlines) | Signed; `-10` narrows 10%. Scales advance widths but **NOT** GPOS PairPos XAdvance — see caveat. |
| `--height PCT` | Vertical scale + all vertical metrics | Scales 12 metrics incl. `os2_capheight`/`os2_xheight`. |
| `--thickness PCT` | Stem weight, % of **measured** stem | Uses `changeWeight`, joins/counters stay clean. |
| `--spacing PCT` | Letter-spacing: % change to advance widths | Grows the advance edge only; outlines never move (FF's lsb setter shifts the outline, so this uses `width` alone). Negative tightens. Zero-width glyphs skipped. |
| `--quantize-curve N` | Snap every point to a 1/N-unit grid | N=1 integers, N=4 quarter-unit (recommended), N≥1024 is no-op (FF coordinate floor). |
| `--hint MODE` | Autohint output as the LAST step | `auto` picks by format; `cff`→otfautohint, `tt`→ttfautohint, `none`=off. FontForge's own autoHint is a no-op on 20251009, so an external binary does it. |
| `-j N` | Parallel workers | 0 = auto (per CPU, capped at font count). |

### ttf2otf_ff_v6.0 — `--scale` and `--spacing`

`ttf2otf_ff_v6.py` is the v5 script renamed in place, with two new flags:

| Flag | Effect | Notes |
|------|--------|-------|
| `--scale PCT` | Uniform X-and-Y scale (geometry **and** advance widths) | Distinct from `--width`, which is X-only. `font.transform((s,0,0,s,0,0))` then `glyph.width *= s`. Refuses factors <= 0 (would invert the font). |
| `--spacing PCT` | Letter-spacing: % change to advance widths | Same semantics as `otf_optimize-ff-v2.0.py --spacing`: outlines never move, zero-width glyphs skipped. |

Pipeline order (v5 → v6):

```
5  phase_compact    (--compact)
5b phase_width      (--width)
5c phase_scale      (--scale)         ← new
5d phase_spacing    (--spacing)       ← new
6  phase_high_risk_repass
```

Both new flags default to 0 (no-op) so v5 invocations keep behaving identically. Verified on RoadUA-Black (639 glyphs):

| run | log evidence | output |
|-----|--------------|--------|
| `--scale 5` | `Phase 5c: Uniform scale ×1.0500 (+5%)` | 639 glyphs, 93,216 B |
| `--spacing 10` | `Phase 5d: Letter-spacing +10%` — `638 touched, 1 skipped (zero-width preserved)` | 639 glyphs, 84,716 B |
| `--scale 5 --spacing 10` | both phases run in order | 639 glyphs, 93,232 B |

The `1 skipped (zero-width preserved)` is a combining mark whose width stayed at 0 — exactly the behaviour documented in the help text, and exactly the bug class this guard was added for.

### Verified limits & behaviour

- **Stem is measured from outlines**, never guessed. Jano Sans Pro Regular: 88u
  stem true; old `cap*0.07` heuristic under-measured by 1.7×.
- **The three transforms verified within ±0.5%** of target on Jano Reg/Bold
  under both launchers, including combined runs.
- **`--quantize-curve` sweet spot is N=4.** Measured on Jano Regular (82,927
  points): N=4 fixes the same ~61k points as N=1 but with 4× smaller
  displacement (max 0.123u vs 0.5u). Finer than N=4 adds nothing; ≥ N=1024 is
  a no-op because FontForge stores coords at 1/1024-unit precision.

### Known limitations (by design, and honest)

1. **`--width` does NOT rescale GPOS PairPos XAdvance/XPlacement.** FontForge's
   binding exposes no API to read/write individual PairPos values
   (`addKerningClass`/`alterKerningClass` are Format-2-only; `autoKern` rebuilds
   instead of scaling). The script detects GPOS kerning and **warns loudly**
   per font and cross-font — it never does it silently. Verified on
   AdwaitaSans: 31,159 kern pairs, XAdvance sum unchanged after `x*0.9`.
   → If letterfit correctness matters, use the fontTools-bridge variant.
2. **No fontforge hinting — but `--hint` fixes it via external tools.**
   `font.autoHint()` emits **zero** hint bytecode on FontForge **20251009**
   (linuxbrew): 0/2938 TrueType glyphs with program, 0/1568 CFF charstrings
   with hintmask. There is no hinting path in FontForge alone. `--hint` runs
   `otfautohint` (CFF/AFDKO) or `ttfautohint` (TrueType) as the last step.
   Verified: Jano CFF charstring bytes 206KB→266KB, hint ops present;
   Adwaita 1965/2938 glyphs gained program bytecode + fpgm/prep.
   Launcher-independence: afdko lives in user site-packages, which a
   `fontforge -script`-spawned child loses; the script injects the user site
   path into the child PYTHONPATH so hinting works under both launchers.
3. **No CFF hint-dict tuning.** `fontforge.private` exposes only `.guess(k)`;
   no access to BlueValues/StemSnapH/StdHW. Do that with fontTools/AFDKO.
4. **`font.transform()` (font-level) is a trap** — leaves both hmtx and GPOS
   untouched on this build. Always per-glyph transform (the script does; that
   path scales hmtx correctly, but still not GPOS).

### GPOS gotcha for future tooling

`font.gpos_lookups` returns a tuple of **human-readable strings** (e.g.
`"'kern' Horizontal Kerning lookup 1"`), NOT `(handle,name,count,type)` 4-tuples.
Detect GPOS kern via `"'kern'" in entry`.

---

*Last updated: 2026-09-29 — Added the FontForge-only optimizer
(`otf_optimize-ff-v2.0.py`) section: single-dependency path with
`--quantize-curve`, `--hint`, and `--spacing`, verified limits (GPOS PairPos
not rescale, FontForge autoHint is a no-op on 20251009 so `--hint` uses
otfautohint/ttfautohint, N=4 curve sweet spot). Distinguish from the
fontTools-bridge underscore variant (`otf_optimize-ff-v1.0/1.1`, formerly `otf_optimize_ff-v2.0/2.1`).

*Previously: 2026-09-27 — Recipe #9 added: raise UPM 1000 → 2048 via
`scale_upem_batch.py`, with the ordering rule that matters (bump UPM *before*
any geometry pass, and double the unit-valued grid/quantize knobs to match).
Recipe #8: fix a vendor font's broken OS/2 usWeightClass (Sadi Sans shipped
every file as weight 400, which made `fc-match "Sadi Sans"` return Bold).
Audit-then-rewrite pattern generalizes to any family with bad weight tables;
the shipped script stays concrete.

*Previously: 2026-09-25 — v14 adds `--flatten` / `--thicken-flatten` for
polygon-mode fringe-free output. v13 quantization knobs preserved.
Recipe #7 added: Display Black / Heavy weight (Latin, /20u grid) — verified on ff-nort.*