# Font Rendering Optimizer

This directory contains Python scripts for improving OTF/TTF font rendering quality.

| Script | Focus |
|--------|-------|
| `otf_optimize-v8.1.py` | **Font scaling, thickness, hinting, and solid/concrete shaping** |
| `improve_font_rendering.py` | Light touch-ups, CFF fonts, cross-platform |
| `improve_fontforge.py` | Aggressive cleanup, TrueType, complex shapes |

---

## OTF/TTF Font Optimizer (`otf_optimize-v8.1.py`)

The primary script for **font scaling, thickening, auto-hinting, and solid/concrete rendering** using `ttfautohint` (TrueType) and `psautohint` (CFF/OTF).

### Quick Start

```bash
# Install dependencies
pip install fonttools ttfautohint psautohint

# Basic optimisation
python otf_optimize-v8.1.py input_fonts/ output_fonts/

# Solid, concrete rendering (recommended)
python otf_optimize-v8.1.py --solid --thickness 2.5 --scale 5.0 input_fonts/ output_fonts/

# Just clear shaping (lighter touch)
python otf_optimize-v8.1.py --clear-shaping input_fonts/ output_fonts/

# Maximum boldness
python otf_optimize-v8.1.py --solid --thickness 15 --weight-offset 80 input_fonts/ output_fonts/
```

### Your Command (Proven Working)

```bash
python /storage/drive-S/Work/SideProjects/fonts/otf_optimize-v8.1.py \
  --solid \
  --thickness 2.5 \
  --scale 5.0 \
  ./input.otf2/ \
  ./output/
```

This command:
- **Scales up** the font by 5% (`--scale 5.0`)
- **Thickens** vertical stems by 2.5% (`--thickness 2.5`)
- **Enables solid mode** (`--solid`) for concrete, bolder rendering
- **Auto-hints** with psautohint (CFF) or ttfautohint (TrueType)
- **Applies clear-shaping** post-processing (GASP table, head flags, etc.)

---

### All Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--solid` | **Master switch for solid, concrete rendering.** Activates: OS/2 weight class bump (+50), head macStyle bold bit (on weight ≥600), aggressive GASP table (grid-fit + AA + symmetric), expanded hinting range (4-128ppem). Includes all `--clear-shaping` features. | off |
| `--clear-shaping` | Clearer shaping: enhanced hinting ranges, ClearType compatibility, GASP table optimisation, head table flag tuning, overlap removal. | off |
| `--scale FLOAT` | **Uniform scale percentage.** Positive = larger, negative = smaller. Applied as pre-processing before hinting. | 0 |
| `--thickness FLOAT` | **Extra thickness for vertical stems (%).** Thickens the font by applying more horizontal scaling. `10` = 10% thicker vertical stems. Recommended: 2-15. Applied before hinting. | 0 |
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