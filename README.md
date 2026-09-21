| `--width FLOAT` | **[v8.5 NEW] Adjust horizontal width only.** Positive = expand (e.g. 5 = 5% wider), negative = condense (e.g. -8 = 8% narrower). Affects outlines + advance widths + horizontal metrics. **Aliases: `--expand N`, `--condense N`**. Independent of `--scale`. Safety cap [0.10, 4.00]. | 0 |

| `--expand FLOAT` | **[v8.5 NEW]** Alias for `--width` with positive value. `e.g. --expand 5` = `--width 5`. Mutually exclusive with `--width`. | — |

| `--condense FLOAT` | **[v8.5 NEW]** Alias for `--width` with negative value. `e.g. --condense 8` = `--width -8`. Mutually exclusive with `--width`. | — |

| `--blue-quantise INT` | **[v8.5 NEW]** Round CFF `BlueValues`/`OtherBlues`/`FamilyBlues` to a clean N-unit grid. Eliminates fractional values that can cause rasteriser mis-snap. `0` = disabled (default), `1` = each unit (highest precision), `2/4/8` = cleaner grid. | 0 |

| `--no-thickness` | **[v8.5 NEW]** Disable `--thickness` (escape hatch for scripts that pass `--thickness 0` from elsewhere). | off |

# Font Rendering Optimizer
| `--width FLOAT` | **[v8.5 NEW] Adjust horizontal width only.** Positive = expand (e.g. 5 = 5% wider), negative = condense (e.g. -8 = 8% narrower). Affects outlines + advance widths + horizontal metrics. **Aliases: `--expand N`, `--condense N`**. Independent of `--scale`. Safety cap [0.10, 4.00]. | 0 |

| `--expand FLOAT` | **[v8.5 NEW]** Alias for `--width` with positive value. `e.g. --expand 5` = `--width 5`. Mutually exclusive with `--width`. | — |

| `--condense FLOAT` | **[v8.5 NEW]** Alias for `--width` with negative value. `e.g. --condense 8` = `--width -8`. Mutually exclusive with `--width`. | — |

| `--blue-quantise INT` | **[v8.5 NEW]** Round CFF `BlueValues`/`OtherBlues`/`FamilyBlues` to a clean N-unit grid. Eliminates fractional values that can cause rasteriser mis-snap. `0` = disabled (default), `1` = each unit (highest precision), `2/4/8` = cleaner grid. | 0 |

| `--no-thickness` | **[v8.5 NEW]** Disable `--thickness` (escape hatch for scripts that pass `--thickness 0` from elsewhere). | off |



This directory contains Python scripts for improving OTF/TTF font rendering quality.



| Script | Focus |

|--------|-------|

| `otf_optimize_ff-v2.1.py` | **Latest (FontForge-based):** Single-tool dependency (FontForge), fontTools for post-processing. Has proper --thickness via FontForge changeWeight. |

| `otf_optimize-v9.py` | **Latest (foundrytools-based):** Uses `foundrytools` library (https://foundrytools.readthedocs.io) for canonical APIs. 892 lines (vs 2314 for v8.5). Adds StdHW/StdVW/StemSnap* recalculation from real stem widths, contour correction via skia-pathops, and `set_production_names`. |

| `otf_optimize-v8.5.py` | **Previous (fontTools + psautohint):** Best CFF hint quality (uses external psautohint). Adds --width/--expand/--condense and --blue-quantise over v8.3. |

| `otf_optimize-v8.3.py` | Previous version with inset-rescale --thickness. FontForge v2.1 recommended for thickening. |

| `otf_optimize-v8.1.py` | Previous version (kept in sync for reference) |

| `improve_font_rendering.py` | Light touch-ups, CFF fonts, cross-platform |

| `improve_fontforge.py` | Aggressive cleanup, TrueType, complex shapes |



---



## OTF/TTF Font Optimizer (`otf_optimize-v8.5.py`)



The latest fontTools-based script for **font scaling, widening/narrowing, thickening, auto-hinting, hint tuning, and solid/concrete rendering** using `ttfautohint` (TrueType) and `psautohint` (CFF/OTF). v8.5 adds safe, additive improvements over v8.3 without breaking v8.3's `--thickness` behavior.



### Quick Start



```bash

# Install dependencies

pip install fonttools ttfautohint psautohint



# Proven command (recommended for Samsung & similar Korean fonts)

python otf_optimize-v8.5.py --solid --hint-tune --rebuild-hints --thickness 2.5 --scale 5.0 input_fonts/ output_fonts/



# Basic optimisation

python otf_optimize-v8.5.py input_fonts/ output_fonts/



# Just clear shaping (lighter touch)

python otf_optimize-v8.5.py --clear-shaping input_fonts/ output_fonts/



# Make a semi-condensed variant (NEW in v8.5)

python otf_optimize-v8.5.py --condense 8 ./input/ ./output/



# Maximum boldness (use with caution - see weight-offset warning below)

python otf_optimize-v8.5.py --solid --thickness 15 --weight-offset 80 input_fonts/ output_fonts/

```



### Your Proven Command



```bash

python /storage/drive-S/Work/SideProjects/fonts/otf_optimize-v8.5.py \

  --solid \

  --hint-tune \

  --rebuild-hints \

  --thickness 2.5 \

  --scale 5.0 \

  ./input.otf2/ \

  ./output/

```



This command:

- **Scales up** the font by 5% (`--scale 5.0`)
- **Thickens** vertical stems by 2.5% (`--thickness 2.5`)
- **Enables solid mode** (`--solid`) for concrete, bolder rendering
- **Tunes CFF hinting** (`--hint-tune`) — synthesises BlueValues/OtherBlues when

  the font has none, sets LanguageGroup=1, ExpansionFactor, BlueShift/BlueFuzz.
- **Rebuilds hints** (`--rebuild-hints`) — fixes hint drift after scaling/thickening.

  **This is the single highest-impact fix for fringe elimination.**
- **Auto-hints** with psautohint (CFF) or ttfautohint (TrueType)
- **Applies clear-shaping** post-processing (GASP table, head flags, etc.)



### What's New in v8.5 (over v8.3)



v8.5 is a SAFE additive update — it does NOT change v8.3's `--thickness` behavior. New features only activate when their flags are used.



| Feature | What it does | Impact |

|---------|-------------|--------|

| **`--width / --expand / --condense`** | **[v8.5 NEW]** Horizontal-only width adjustment. Positive = expand (`--expand 5` = 5% wider), negative = condense (`--condense 8` = 8% narrower). Affects outlines + advance widths + horizontal metrics. **Independent of `--scale`**: --scale is uniform, --width is horizontal-only. Use cases: semi-condensed variants, expanded display variants. | High |

| **`--blue-quantise N`** | **[v8.5 NEW]** Round CFF `BlueValues`/`OtherBlues`/`FamilyBlues` to a clean N-unit grid. Eliminates fractional values that can cause rasteriser mis-snap. `0` = disabled (default), `1` = each unit (highest precision), `2/4/8` = cleaner grid. | Medium |

| **`--no-thickness`** | **[v8.5 NEW]** Escape hatch to disable `--thickness` even if a config file or wrapper passes `--thickness 0` or similar. Useful for scripts. | Low |

| **Improved `--hint-tune`** | **[v8.5 IMPROVED]** `_tune_cff_hinting()` now synthesises **proper 4-zone BlueValues** (descender + baseline + x-height + cap-height) instead of just 2 zones. Zone width is UPM-aware (`UPM/100`, ~10 for UPM 1000). Better descender zone handling. | High |

| **Improved `--rebuild-hints`** | **[v8.5 IMPROVED]** Same 4-zone synthesis, ensuring rebuilt BlueValues match the current scaled outline positions. | High |



### What's New in v8.3 (preserved in v8.5)



v8.3 fixes a fundamental misunderstanding of `--thickness`:



| Feature | What it does | Impact |

|---------|-------------|--------|

| **`--thickness` REWORKED** | v8.3 thins stems via inset-rescale. v8.2 used non-uniform X scale (factor_x > factor_y) which made the entire font wider AND taller while also thickening stems. The v8.3 approach uses per-contour inset-rescale: each contour's left/bottom edge is preserved, inner counters shrink by `2*dx` horizontally, making stems appear thicker while keeping advance widths unchanged. (NOTE: v8.4 attempted radial dilation to fix the outer-shrink side-effect but it caused uneven curve thickening. v8.4 was removed; FontForge-based v2.1 is recommended for proper thickening.) | **Improvement (with caveat)** |

| **`--hint-tune`** | CFF Private dict tuning: sets `LanguageGroup=1`, `ExpansionFactor`, `BlueShift/BlueFuzz`, and **synthesises `BlueValues`/`OtherBlues` from OS/2 metrics when the font has none**. Most Samsung-style fonts ship without alignment zones, so this is a massive improvement. | **Highest** |

| **`--rebuild-hints`** | Rebuilds CFF `BlueValues`/`OtherBlues` from current OS/2 `sCapHeight`/`sxHeight` **even when they already exist**. Critical when scaling/thickening, since original BlueValues no longer match scaled outlines (causing hint drift). Applied BEFORE psautohint so hints are correctly aligned. | **High** |

| **`--shape-cleanup`** | TrueType outline cleanup: removes collinear points (3+ on a line) and near-duplicate points (within 0.5 units). Cleaner curves, smoother rendering, smaller files. | Medium |

| **`--gasp-detail {aggressive,balanced,minimal}`** | 5-range granular GASP table with optimal AA mode per range (vs v8.1's 4 ranges). | Medium |

| **Stem width normalisation** | Rounds `StdHW`/`StdVW`/`StemSnapH`/`StemSnapV` to clean integers. | Low |

| **Subpixel coordinate snapping** | Forces all glyph coordinates to integers, eliminating subpixel rendering fringes. | Medium |

| **psautohint long-name auto-exclusion** | Auto-detects glyphs with names > 64 chars (Apple/Google emoji fonts) and excludes them from psautohint via `-x`. Prevents "Bad input data. Glyph name is greater than 64 chars" errors. | Critical for emoji fonts |

| **hmtx auto-fill for long-named glyphs** | Ensures hmtx has entries for all glyphs so `fontTools.getGlyphSet()` doesn't fail on emoji fonts with very long glyph names. | Critical for emoji fonts |

| **Head bit 3 cleared** | Explicitly clears `head.flags` bit 3 (Force PPEM integer) — Chrome/Skia rejects fonts with this flag. The previous `|=` didn't clear existing bits, so fonts with the flag set would still fail. | Chrome compat |

| **`--scale` dual mode** | Values in `[0, 1)` are direct multipliers (e.g. `0.8` = 80% size); values `>=1` are percentage changes (e.g. `5` = +5%). Intuitive "shrink to X%" support. Safety cap: [0.10, 4.00]. | UX |



### All Parameters



| Parameter | Description | Default |

|-----------|-------------|---------|

| `--solid` | **Master switch for solid, concrete rendering.** Head macStyle bold bit (on weight ≥600), aggressive GASP table (grid-fit + AA + symmetric), expanded hinting range (4-128ppem). Includes all `--clear-shaping` features. **Does NOT bump usWeightClass** anymore (see `--weight-offset`). | off |

| `--clear-shaping` | Clearer shaping: enhanced hinting ranges, ClearType compatibility, GASP table optimisation, head table flag tuning, overlap removal. | off |

| `--hint-tune` | CFF hint tuning: `LanguageGroup`, `ExpansionFactor`, `BlueShift/BlueFuzz`, synthesise `BlueValues`/`OtherBlues` if missing. | off |

| `--rebuild-hints` | **[v8.3]** Rebuilds `BlueValues`/`OtherBlues` from OS/2 metrics even when they already exist. **Use after `--scale` or `--thickness` to fix hint drift.** | off |

| `--shape-cleanup` | TrueType outline cleanup: collinear + near-duplicate point removal. | off |

| `--pixel-snap INT` | Pixel-snap phase grid (TrueType only). 0 = disable, default 4 = 1/4 pixel at 16ppem. Higher = more aggressive. | 4 |

| `--pixel-gasp` | Use pixel-aligned 7-range GASP table (SYMMETRIC_SMOOTHING at 16+ppem). Overrides `--gasp-detail`. | off |

| `--dropout-control` | Add dropout control via PREP table (SCANMODE mode 2) for TrueType. Eliminates half-pixel dropouts. | off |

| `--gasp-detail {aggressive,balanced,minimal}` | Granular GASP control (5 ranges, optimal AA per range). | aggressive |

| `--no-stem-round` | Skip stem width rounding (`StdHW`/`StdVW`/`StemSnap`). Used with `--hint-tune`. | off |

| `--scale FLOAT` | **[dual mode]** Scale font size: `\|value\| < 1.0` → direct multiplier (e.g. `0.5` = ×0.50 / 50% size); `\|value\| >= 1.0` → percentage change (e.g. `5` = +5%). Safety cap [0.10, 4.00]. | 0 |

| `--thickness FLOAT` | **[inset-rescale] Thicken stems by dilating each contour from the glyph center. Outer contour expands outward (glyph grows slightly), inner counter shrinks inward (counter smaller). Stems appear thicker on BOTH sides. Advance widths preserved. `2.5` = subtle bolder, `10` = clearly thicker. Recommended: 1-10. Works alongside `--scale`. | 0 |

| `--weight-offset INT` | **[v8.3 CHANGED]** Add this N to OS/2 `usWeightClass`. **Default 0 (no automatic bump)**. WARNING: bumping weight class can affect font matching (Fontconfig maps 400=Regular, 700=Bold). Only use when you understand the impact. Pass `--weight-offset 50` explicitly if needed. | 0 |

| `--x-height-hint INT` | Percentage to increase x-height for better small-size legibility (e.g., 3 = 3% larger lowercase). Recommended: 2-5. | 0 |

| `--hinting-range-min INT` | Minimum ppem for hinting generation. Lower = better tiny-size rendering. Default: 8 (standard), 6 (with `--clear-shaping`), 4 (with `--solid`). | auto |

| `--hinting-range-max INT` | Maximum ppem for hinting generation. Higher = better large-size rendering. Default: 72 (standard), 96 (with `--clear-shaping`), 128 (with `--solid`). | auto |

| `--gasp-mode {detailed,simple}` | GASP table mode: `detailed` = multi-range optimisation (default), `simple` = uniform grid-fitting + grayscale. | detailed |

| `--no-overlap-remove` | Skip removal of overlapping paths. | off |

| `--strength INT` | ttfautohint hinting strength limit. | 100 |

| `--no-combining` | Don't set fallbacks for combining characters (TrueType). | off |

| `--detailed` | Add detailed TTF instructions information (TrueType). | off |

| `--stem-width INT` | Fallback stem width value (TrueType). | — |

| `--allow-changes` | Allow changes to glyph outlines (CFF). | off |

| `--no-flex` | Suppress generation of flex commands (CFF). | off |

| `--no-hint-sub` | Suppress hint substitution (CFF). | off |

| `-v, --verbose` | Enable verbose logging. | off |



### Examples



```bash

# Proven command (recommended for Samsung-style fonts)

python otf_optimize-v8.5.py --solid --hint-tune --thickness 2.5 --scale 5.0 ./input/ ./output/



# Clear shaping only (no boldness, lighter touch)

python otf_optimize-v8.5.py --clear-shaping ./input/ ./output/



# Heavy bold with full v8.2 pipeline

python otf_optimize-v8.5.py --solid --hint-tune --shape-cleanup --thickness 15 --weight-offset 80 ./input/ ./output/



# Just hint tuning (for fonts with no BlueValues, like Samsung)

python otf_optimize-v8.5.py --hint-tune ./input/ ./output/



# Custom GASP granularity

python otf_optimize-v8.5.py --solid --hint-tune --gasp-detail minimal ./input/ ./output/



# Scale up 10% with hint tuning

python otf_optimize-v8.5.py --clear-shaping --hint-tune --scale 10 ./input/ ./output/



# ---- v8.5 NEW: Width / condense / expand ----

# Make a semi-condensed variant of a font (8% narrower)

python otf_optimize-v8.5.py --condense 8 ./input/ ./output/



# Make an expanded display variant (5% wider)

python otf_optimize-v8.5.py --expand 5 ./input/ ./output/



# Use --width directly with negative value (condense 12%)

python otf_optimize-v8.5.py --width -12 ./input/ ./output/



# Combine width adjust with scale + thickness (independent ops)

python otf_optimize-v8.5.py --scale 5.0 --width -3 --thickness 2.5 ./input/ ./output/



# ---- v8.5 NEW: BlueValues quantisation ----

# Round BlueValues to a clean 8-unit grid (eliminates fractional mis-snaps)

python otf_optimize-v8.5.py --hint-tune --blue-quantise 8 ./input/ ./output/



# Full CFF hint optimisation with quantised zones

python otf_optimize-v8.5.py --solid --hint-tune --rebuild-hints --blue-quantise 4 ./input/ ./output/



# ---- v8.5 NEW: Disable thickness from a config script ----

python otf_optimize-v8.5.py --no-thickness --solid ./input/ ./output/

```



### How It Works



```

Input font (.otf/.ttf)

       │

       ├─► [--scale] Uniform scaling

       │     [--thickness] Non-uniform X scaling (bold effect)

       │       ├─ TrueType: scale all coords + composites

       │       └─ CFF: TransformPen → T2CharStringPen → new CharStrings

       │

       ├─► [--width / --expand / --condense] (v8.5 NEW) Horizontal-only scaling
       │     Affects outlines + advance widths + horizontal metrics
       │     (vertical metrics unchanged)
       │

       ├─► Analyse format: glyf (TrueType) vs CFF/CFF2 (OTF)

       │

       ├─► [ttfautohint] TrueType fonts

       │       --hinting-range-min/max --win --opentype-features

       │       --increase-x-height --fallback-script=com

       │

       ├─► [psautohint] CFF fonts

       │       -a -c -d (all glyphs, allow changes, decimal)

       │       -x long-named-glyphs (auto-excluded if >64 chars)

       │       --no-zones-stems --no-flex

       │

       └─► Post-processing

             ├─ head table flags (Chrome-safe — no bit 3)

             ├─ GASP table (multi-range grid-fitting + AA + symmetric)

             ├─ OS/2 weight class bump (--solid)

             ├─ head macStyle bold bit (--solid, weight ≥600)

             ├─ Overlap removal (if pathops available)

             ├─ [v8.2] CFF hint tuning (LanguageGroup, BlueValues, etc.)

             ├─ [v8.2] Subpixel coordinate snapping

             └─ [v8.2] Stem width normalisation

```



### Chrome Compatibility Notes



The script has been tested in Chrome via CDP (Chrome DevTools Protocol). Key findings:



| Feature | Chrome Status |

|---------|--------------|

| `--scale` | ✅ Fully supported |

| `--thickness` | ✅ Fully supported |

| `--solid` (weight class, bold bit, GASP) | ✅ Fully supported |

| `--clear-shaping` | ✅ Fully supported |

| `--hint-tune` (CFF Private dict) | ✅ Fully supported |

| CFF `ForceBold` | ❌ Chrome rejects the font |

| head flag bit 3 (Force PPEM integer) | ❌ Chrome rejects the font |

| GASP monochrome (no AA at small sizes) | ❌ Causes fringes on diagonals |

| GASP + head flags in same save | ❌ fontTools checksum bug (fixed: 2 saves) |



All Chrome-breaking issues have been fixed:

1. `ForceBold` is **not used** (weight class bump + bold bit instead)

2. Head flag bit 3 is **omitted** (Chrome uses fractional ppem)

3. GASP + head flags are saved in **two separate steps**

4. GASP enables AA at **all** sizes (no monochrome mode)



### Version History



| Version | Key Changes |

|---------|-------------|

| **v8.5** | **Latest (fontTools + psautohint).** Added `--width/--expand/--condense` for horizontal expand/condense (independent of --scale), `--blue-quantise N` to round CFF BlueValues/OtherBlues to a clean N-unit grid, `--no-thickness` escape hatch. Improved `_tune_cff_hinting` to synthesise proper 4-zone BlueValues (descender, baseline, x-height, cap-height) with UPM-aware zone width. |
| **v8.5 BUGFIX** | **CRITICAL: `--rebuild-hints` no longer overwrites correct zones with bad ones.** The previous logic always synthesised new zones from OS/2 metrics, replacing the font's original well-tuned BlueValues with synthesised 20-unit-wide zones plus a fabricated descender zone. This caused bad alignment-zone matching in GTK renderers (Pango/Cairo), where stems snapped to incorrect positions, producing the "thickness abnormal" / "stems not solid" appearance. The fix: when `--rebuild-hints` runs and the font already has BlueValues, **scale them uniformly** by the same factor used to scale the font (preserving zone widths and structure). When the font has no BlueValues, synthesise with proper ~11-unit-wide overshoot zones (instead of the previous too-wide 20 units). |
| **v9** | **NEW GENERATION: foundrytools-based.** Complete rewrite using `foundrytools` library. 892 lines (vs 2314 for v8.5, ~62% reduction). Uses canonical foundrytools APIs: `Font.scale_upm`, `Font.correct_contours`, `Font.set_production_names`, `app.otf_recalc_zones`, `app.otf_recalc_stems`, `app.otf_autohint`, `app.ttf_autohint`. New features: real stem-width recalculation (StdHW/StdVW/StemSnap*), contour overlap removal via skia-pathops, `set_production_names`. Preserves all v8.5 features. |

| **v8.3** | `--thickness` reworked: per-contour inset-rescale (outer contour left/bottom preserved, inner counter shrunk → stems thicker, advance widths unchanged). Also shrank the outer contour, making glyphs look smaller. A subsequent v8.4 attempt with radial dilation caused uneven curve thickening and was removed; FontForge-based v2.1 is recommended for proper thickening via FontForge's changeWeight. |

| **v8.3** | **Hint drift fix.** `--rebuild-hints` rebuilds CFF BlueValues/OtherBlues from OS/2 metrics **before** psautohint runs (was applied AFTER, which was useless). Fixes hidden bug: `head.flags` bit 3 (Force PPEM integer) survived via `\|=` because the previous code added bits without clearing the existing one. Now explicitly cleared. **`--weight-offset` now defaults to 0** (was silently +50 with `--solid`), which preserves `fc-match` correctness. |

| **v8.2** | `--hint-tune` for CFF Private dict (synthesises BlueValues, sets LanguageGroup, ExpansionFactor, BlueShift/BlueFuzz). `--shape-cleanup` for TrueType outline collinear/dedup removal. `--gasp-detail` for granular GASP control. Stem width normalisation. Subpixel coordinate snapping. Long-glyph-name auto-exclusion for emoji fonts (Apple, Google). All v8.1 features preserved. |

| **v8.1** | `--solid` mode (weight class, bold bit, aggressive GASP). `--thickness` (non-uniform X scaling for bold effect). `--clear-shaping` mode. `--weight-offset`, `--x-height-hint`, `--hinting-range-min/max`, `--gasp-mode`. Chrome compatibility fixes. |

| **v8** | Initial scaling support (`--scale`). TrueType + CFF auto-hinting. Directory batch processing. |

| **ff-v2.1** | FontForge-based equivalent of v8.2 (FontForge autoHint for hinting, no external psautohint dependency). |

| **ff-v2.1** | FontForge-based equivalent of v8.x. v2.0 → v2.1 added: `--width/--expand/--condense` for horizontal scaling (A), fringe-elimination phases (B): `_phase_stem_normalise`, `_phase_flatten_curves`, `_phase_collinear_remove`, etc., `--gasp-detail {minimal,standard,aggressive}` (C), `--shape-cleanup` (D), better CFF BlueValues synthesis from OS/2 metrics (E), `--rebuild-hints` flag (H). |

| **ttf2otf_ff_v5** | Fringe elimination specialist (FontForge). v4 → v5 added: `--width` for horizontal expand/condense, `--quantise-curve` for Bezier flattening, `--blue-quantise` for BlueValues grid rounding, `_phase_flatten_curves`, `_phase_stem_align`, `_phase_bezier_integrity`, `_phase_extra_global`, `_phase_quantise_blue_values`. More aggressive anti-fringe passes per glyph. |



### Dependencies



| Tool | Install |

|------|---------|

| `ttfautohint` | `pip install ttfautohint` (or system package) |

| `psautohint` | `pip install psautohint` (part of AFDKO) |

| `fonttools` | `pip install fonttools` |



Optional:

| `pathops` | `pip install pathops` (enables overlap removal) |



---



## OTF/TTF Font Optimizer (`otf_optimize-v8.1.py`) — Previous Version



The v8.1 script is preserved for reference but **v8.2 is recommended** for new work. v8.2 adds `--hint-tune`, `--shape-cleanup`, and finer GASP control.



If you must use v8.1, the proven working command is:

```bash

python /storage/drive-S/Work/SideProjects/fonts/otf_optimize-v8.1.py \

  --solid --thickness 2.5 --scale 5.0 ./input.otf2/ ./output/

```



v8.1 features (all included in v8.2):

- `--scale`, `--thickness`, `--solid`, `--clear-shaping`, `--weight-offset`

- `--x-height-hint`, `--hinting-range-min/max`, `--gasp-mode`

- GASP table optimisation, head flags, overlap removal

- Chrome compatibility fixes



---



## Other Scripts in This Directory



#### `improve_font_rendering.py` (fonttools-based)

Light touch-ups: coordinate rounding, contour simplification, direction correction, outline validation, vertical metrics fix.



#### `improve_fontforge.py` (FontForge-based)

Aggressive cleanup: overlap removal, auto-hinting, contour simplification, WOFF/WOFF2 output.



#### `otf_optimize-v11.py` (legacy)

Earlier version with `--widen` parameter for horizontal scaling. Superseded by v8.1's `--thickness` and v8.2's `--hint-tune`.



---



## Troubleshooting



### "No module named 'fontforge'"

FontForge Python bindings not installed. On Arch/Manjaro:

```bash

sudo pacman -S fontforge

```



### "No module named 'fonttools'"

```bash

pip install fonttools

```



### Fonts not visible in Chrome

Make sure you're using v8.1 or later. Earlier versions had Chrome-incompatible flags (ForceBold, head bit 3). The current scripts (v8.1+) explicitly clear head bit 3.



### Fonts render with extra space ("w a t e r" appearance)

This has multiple possible causes:

1. **System Fontconfig issue**: Clear font cache and rebuild → `sudo fc-cache -fv` (the script also does this when used correctly).

2. **Stale CFF BlueValues**: Use `--rebuild-hints` to regenerate alignment zones from OS/2 metrics. **This is the most common cause after `--scale`/`--thickness`.**

3. **Outdated font cache in Chrome**: Restart Chrome.



### Fonts show "SemiCondensed" or wrong style in `fc-match`

This is caused by `--weight-offset` modifying `usWeightClass`. As of v8.3, `--weight-offset` defaults to 0 and is opt-in only via `--weight-offset N`. Re-run your fonts to get the fix.



### Fringes on diagonal strokes

Use `--solid` mode (it enables anti-aliasing at all sizes in the GASP table). Or add `--gasp-detail aggressive` for v8.2's 5-range GASP with symmetric smoothing. **Most importantly**, use `--rebuild-hints` after `--scale` to fix hint drift.



### Fonts look too thin

Add `--thickness 5` to `--thickness 15` to thicken vertical stems. Combine with `--solid` for the boldest result.



### Fonts hint poorly (especially Samsung-style fonts)

Add `--hint-tune`. Many foundry fonts ship without `BlueValues`, which means psautohint has nothing to align to. `--hint-tune` synthesises alignment zones from OS/2 `sxHeight` and `sCapHeight`. **Combined with `--rebuild-hints`** to also fix hint drift after scaling.



### Stems look thick/abnormal in GTK but OK in KDE (v8.5 BUGFIX)

**Symptom:** Font renders correctly in KDE/Qt apps but stems appear uneven, abnormally thick, or "not solid" in GTK apps (Pango/Cairo-based renderers like GNOME apps, Firefox GTK, GIMP, etc.).

**Cause:** v8.5's previous `--rebuild-hints` logic overwrote the font's original well-tuned `BlueValues` with synthesised ones that had zone widths of 20 units (way too wide for UPM 1000) and a fabricated descender zone at the wrong position. CFF renderers that strictly honour `BlueValues` (GTK/Pango through FreeType) snap stems to these wrong zones, while KDE/Qt's hint engine is more forgiving.

**Fix:** The bug has been fixed in v8.5. `--rebuild-hints` now:

- When font already has BlueValues: **scales them uniformly** by the same factor used to scale the font (preserves structure and zone widths).
- When font has no BlueValues: synthesises with proper ~11-unit-wide overshoot zones (instead of too-wide 20 units).
- Never fabricates a descender zone unless the original font had one or `sTypoDescender` exists.

If you have an old v8.5 output affected by this bug, re-run with the same command to get the fixed output.



---



### All Parameters



| Parameter | Description | Default |

|-----------|-------------|---------|

| `--solid` | **Master switch for solid, concrete rendering.** Activates: OS/2 weight class bump (+50), head macStyle bold bit (on weight ≥600), aggressive GASP table (grid-fit + AA + symmetric), expanded hinting range (4-128ppem). Includes all `--clear-shaping` features. | off |

| `--clear-shaping` | Clearer shaping: enhanced hinting ranges, ClearType compatibility, GASP table optimisation, head table flag tuning, overlap removal. | off |

| `--scale FLOAT` | **Uniform scale percentage.** Positive = larger, negative = smaller. Applied as pre-processing before hinting. | 0 |

| `--thickness FLOAT` | **[inset-rescale] Thicken stems by dilating each contour from the glyph center. Outer contour expands outward (glyph grows slightly), inner counter shrinks inward (counter smaller). Stems appear thicker on BOTH sides. Advance widths preserved. `2.5` = subtle bolder, `10` = clearly thicker. Recommended: 1-10. Works alongside `--scale`. | 0 |

| `--weight-offset INT` | Amount to add to OS/2 usWeightClass for bolder appearance. Default with `--solid`: 50. | 0 |

| `--x-height-hint INT` | Percentage to increase x-height for better small-size legibility (e.g., 3 = 3% larger lowercase). Recommended: 2-5. | 0 |

| `--hinting-range-min INT` | Minimum ppem for hinting generation. Lower = better tiny-size rendering. Default: 8 (standard), 6 (with `--clear-shaping`), 4 (with `--solid`). | auto |

| `--hinting-range-max INT` | Maximum ppem for hinting generation. Higher = better large-size rendering. Default: 72 (standard), 96 (with `--clear-shaping`), 128 (with `--solid`). | auto |

| `--gasp-mode {detailed,simple}` | GASP table mode: `detailed` = multi-range optimisation (default), `simple` = uniform grid-fitting + grayscale. | detailed |

| `--no-overlap-remove` | Skip removal of overlapping paths. | off |

| `--strength INT` | ttfautohint hinting strength limit. | 100 |

| `--no-combining` | Don't set fallbacks for combining characters (TrueType). | off |

| `--detailed` | Add detailed TTF instructions information (TrueType). | off |

| `--stem-width INT` | Fallback stem width value (TrueType). | — |

| `--allow-changes` | Allow changes to glyph outlines (CFF). | off |

| `--no-flex` | Suppress generation of flex commands (CFF). | off |

| `--no-hint-sub` | Suppress hint substitution (CFF). | off |

| `-v, --verbose` | Enable verbose logging. | off |



---



### Examples



```bash

# Solid, concrete rendering with thickness

python otf_optimize-v8.1.py --solid --thickness 2.5 --scale 5.0 input/ output/



# Just clear shaping

python otf_optimize-v8.1.py --clear-shaping input/ output/



# Heavy bold

python otf_optimize-v8.1.py --solid --thickness 15 --weight-offset 80 input/ output/



# Scale up 10% and optimise

python otf_optimize-v8.1.py --clear-shaping --scale 10 input/ output/



# Only hinting, no scaling

python otf_optimize-v8.1.py input/ output/

```



---



### How It Works



```

Input font (.otf/.ttf)

       │

       ├─► [--scale] Uniform scaling

       │     [--thickness] Non-uniform X scaling (bold effect)

       │       ├─ TrueType: scale all coords + composites

       │       └─ CFF: TransformPen → T2CharStringPen → new CharStrings

       │

       ├─► Analyse format: glyf (TrueType) vs CFF/CFF2 (OTF)

       │

       ├─► [ttfautohint] TrueType fonts

       │       --hinting-range-min/max --win --opentype-features

       │       --increase-x-height --fallback-script=com

       │

       ├─► [psautohint] CFF fonts

       │       -a -c -d (all glyphs, allow changes, decimal)

       │       --no-zones-stems --no-flex

       │

       └─► Post-processing (clear-shaping / solid mode)

             ├─ GASP table (multi-range grid-fitting + AA)

             ├─ head table flags (no bit 3 — Chrome compat)

             ├─ OS/2 weight class bump (--solid)

             ├─ head macStyle bold bit (--solid, weight ≥600)

             └─ Overlap removal (if pathops available)

```



---



### Chrome Compatibility Notes



The script has been tested in Chrome via CDP (Chrome DevTools Protocol). Key findings:



| Feature | Chrome Status |

|---------|--------------|

| `--scale` | ✅ Fully supported |

| `--thickness` | ✅ Fully supported |

| `--solid` (weight class, bold bit, GASP) | ✅ Fully supported |

| `--clear-shaping` | ✅ Fully supported |

| CFF `ForceBold` | ❌ Chrome rejects the font |

| head flag bit 3 (Force PPEM integer) | ❌ Chrome rejects the font |

| GASP monochrome (no AA at small sizes) | ❌ Causes fringes on diagonals |

| GASP + head flags in same save | ❌ fontTools checksum bug (fixed: 2 saves) |



All three Chrome-breaking issues have been fixed in v8.1:

1. `ForceBold` is **not used** (weight class bump + bold bit instead)

2. Head flag bit 3 is **omitted** (Chrome uses fractional ppem)

3. GASP + head flags are saved in **two separate steps**



---



### Version History



| Version | Key Changes |

|---------|-------------|

| **v8.1** | **Current version.** `--solid` mode (weight class, bold bit, aggressive GASP). `--thickness` (non-uniform X scaling for bold effect). `--clear-shaping` mode (GASP, head flags, overlap removal). `--weight-offset`, `--x-height-hint`, `--hinting-range-min/max`, `--gasp-mode`. Chrome compatibility fixes. |

| **v8** | Initial scaling support (`--scale`). TrueType + CFF auto-hinting. Directory batch processing. |



---



### Dependencies



| Tool | Install |

|------|---------|

| `ttfautohint` | `pip install ttfautohint` (or system package) |

| `psautohint` | `pip install psautohint` (part of AFDKO) |

| `fonttools` | `pip install fonttools` |



Optional:

| `pathops` | `pip install pathops` (enables overlap removal) |



---



### Other Scripts in This Directory



#### `improve_font_rendering.py` (fonttools-based)

Light touch-ups: coordinate rounding, contour simplification, direction correction, outline validation, vertical metrics fix.



#### `improve_fontforge.py` (FontForge-based)

Aggressive cleanup: overlap removal, auto-hinting, contour simplification, WOFF/WOFF2 output.



#### `otf_optimize-v11.py` (legacy)

Earlier version with `--widen` parameter for horizontal scaling. Superseded by v8.1's `--thickness`.



---



## Troubleshooting



### "No module named 'fontforge'"

FontForge Python bindings not installed. On Arch/Manjaro:

```bash

sudo pacman -S fontforge

```



### "No module named 'fonttools'"

```bash

pip install fonttools

```



### Fonts not visible in Chrome

Make sure you're using v8.1 or later. Earlier versions had Chrome-incompatible flags (ForceBold, head bit 3).



### Fringes on diagonal strokes

Use `--solid` mode (it enables anti-aliasing at all sizes in the GASP table). Or add `--gasp-mode detailed` for symmetric smoothing at larger sizes.



### Fonts look too thin

Add `--thickness 5` to `--thickness 15` to thicken vertical stems. Combine with `--solid` for the boldest result.