# Font Rendering Optimizer

This directory contains two Python scripts for improving OTF/TTF font rendering quality:

| Script | Library | Best For |
|--------|---------|----------|
| `improve_font_rendering.py` | [fonttools](https://github.com/fonttools/fonttools) | Light touch-ups, CFF fonts, cross-platform |
| `improve_fontforge.py` | [FontForge](https://fontforge.org/) | Aggressive cleanup, TrueType, complex shapes |

---

## Quick Start

### fonttools version

```bash
# Install dependencies
pip install fonttools

# Run
python improve_font_rendering.py ./fonts                    # output → fonts_optimized/
python improve_font_rendering.py ./fonts ./cleaned/         # custom output
python improve_font_rendering.py ./fonts -q                 # quiet mode
```

### FontForge version

```bash
# Install FontForge (Arch/Manjaro)
sudo pacman -S fontforge

# Run
python improve_fontforge.py ./fonts                    # output → fonts_fontforge_optimized/
python improve_fontforge.py ./fonts ./cleaned/         # custom output
python improve_fontforge.py ./fonts -q                 # quiet mode
python improve_fontforge.py ./fonts --remove-hints     # strip all hinting
python improve_fontforge.py ./fonts --format woff2     # convert to WOFF2
```

---

## What They Do

### 1. Coordinate Rounding
Removes subpixel fractional coordinates that cause fringe artifacts during rendering.

### 2. Contour Simplification
Reduces redundant points while preserving shape fidelity. FontForge's version is more aggressive.

### 3. Extreme Point Removal
Removes micro-points that don't meaningfully affect the shape but add rendering noise.

### 4. Direction Correction
Ensures all contours have correct winding order (counter-clockwise for holes), preventing fill artifacts.

### 5. Outline Validation
Detects and fixes self-intersecting contours that cause rendering issues.

### 6. Vertical Metrics Fix
Corrects ascent/descent signs and line gap values for proper line spacing.

### 7. Hinting Optimization
- **fonttools**: Removes problematic `prep`, `fpgm`, `cvt` tables
- **FontForge**: Applies fresh auto-hinting or strips all hints (`--remove-hints`)

### 8. Overlap Removal
- **fonttools**: Warns about overlaps
- **FontForge**: Actively removes overlaps via `glyph.intersect()`

---

## FontForge-Specific Features

The FontForge version provides additional capabilities:

| Feature | Description |
|---------|-------------|
| `glyph.round()` | FontForge's optimized coordinate rounding |
| `glyph.simplify(tolerance)` | Aggressive contour simplification with configurable tolerance |
| `glyph.removeOverlap()` | True overlap removal |
| `glyph.correctDirection()` | Automatic winding order correction |
| `glyph.clusterCluster()` | Merges points within tolerance |
| `glyph.selfIntersects()` | Detects self-intersection problems |
| `font.autoHint()` | Regenerates fresh hinting from scratch |

---

## Choosing Between Scripts

| Scenario | Recommended |
|----------|-------------|
| CFF/PostScript fonts (OTF) | `improve_font_rendering.py` |
| TrueType fonts with broken hints | `improve_fontforge.py` |
| Complex glyphs with many points | `improve_fontforge.py` |
| Need WOFF/WOFF2 output | `improve_fontforge.py` |
| Minimal changes, preserve everything | `improve_font_rendering.py` |
| Batch processing, speed important | `improve_font_rendering.py` |

---

## Output

Both scripts:
- Preserve original filenames
- Maintain font format (OTF→OTF, TTF→TTF unless `--format` specified)
- Output to a dedicated directory (doesn't overwrite originals)
- Print detailed per-font reports

### Sample Output

```
======================================================================
  FontForge Optimizer - Shape Clarity Enhancement
  2026-06-03 19:30:00
======================================================================

  Input:   ./fonts
  Output:  ./fonts_fontforge_optimized
  Fonts:   3 file(s)

[1/3] Processing: MyFont-Bold.otf

======================================================================
  📄 MyFont-Bold.otf
======================================================================

  Glyph Count:  512
  Font Format:  OpenType
  Units/Em:     1000

  Optimizations Applied:
  [OK] Rounded coordinates in 512 glyphs
  [SIMPLIFIED] 48 glyphs, removed 234 points
  [CLEANED] Removed 12 extreme points
  [OK] Corrected contour direction in 512 glyphs
  [OK] No outline validation issues
  [FIXED] Vertical metrics: linegap→98
  [OK] Auto-hinting applied
  [OK] Saved as MyFont-Bold.otf

======================================================================
  SUMMARY
======================================================================
  Processed:  3 font(s)
  Succeeded:  3
  Failed:     0
  Size:       1.24 MB → 1.18 MB (95.2%)

  Output: ./fonts_fontforge_optimized
```

---

## OTF/TTF Font Optimizer (`otf_optimize-v11.py`)

A separate script for **font scaling, widening/narrowing, and auto-hinting** using `ttfautohint` (TrueType) and `psautohint` (CFF/OTF).

### Quick Start

```bash
# Install dependencies
pip install fonttools ttfautohint psautohint

# Scale up 7.5% and auto-hint
python otf_optimize-v11.py --scale 7.5 input.otf output.otf

# Widen by 20% (factor 1.2)
python otf_optimize-v11.py --widen 1.2 input.otf output.otf

# Narrow by 10% (factor 0.9)
python otf_optimize-v11.py --widen 0.9 input.otf output.otf

# Combine scaling + widening
python otf_optimize-v11.py --scale 5 --widen 1.15 input.otf output.otf

# Batch process directory
python otf_optimize-v11.py --widen 1.1 input_fonts/ output_fonts/

# CFF tuning with widening
python otf_optimize-v11.py --widen 1.1 --allow-changes --no-flex in.otf out.otf

# Just auto-hint (no scaling)
python otf_optimize-v11.py input.otf output.otf

# Check dependencies
python otf_optimize-v11.py --check-deps
```

### Parameters

| Parameter | Description |
|-----------|-------------|
| `--scale FLOAT` | **Uniform scale percentage** (positive = larger, negative = smaller). Default: 0 |
| `--widen FLOAT` | **Horizontal scale factor** (<1.0 = narrow, >1.0 = widen, 1.0 = no change). Default: 1.0 |
| `--strength INT` | `ttfautohint` hinting strength ceiling |
| `--allow-changes` | `psautohint --allow-changes` (reorder paths) |
| `--no-flex` | `psautohint --no-flex` (disable flex hints) |
| `--no-hint-sub` | `psautohint --no-hint-sub` |
| `--no-combining` | `ttfautohint --no-combining-chars` |
| `--fallback-stem INT` | `ttfautohint --fallback-stem-width` |
| `--detailed` | `ttfautohint --detailed-info` |
| `-v, --verbose` | Debug logging |
| `--check-deps` | Check `ttfautohint`/`psautohint` availability |

### Version History

| Version | Key Changes |
|---------|-------------|
| **v11** | **Added `--widen` parameter** for horizontal scaling (widen/narrow). TrueType: X-coordinate scaling. CFF: horizontal-only affine transform via `TransformPen`. Scales horizontal metrics (`hmtx`, `hhea`, `OS/2` horiz) only. Vertical metrics unchanged. |
| **v10** | **Precision improvements**: CFF scaling via `TransformPen` + `T2CharStringPen` (no manual coordinate math). Single-pass rounding. CFF hint value scaling (`BlueValues`, `StemSnapH/V`, `BlueShift`, `BlueFuzz`, `FontBBox`). `head` table bbox + `lowestRecPPEM` scaling. Removed redundant `preserve_curves` flag. Composite glyph offset handling fixed. |
| **v9** | Initial scaling support (`--scale`). TrueType + CFF auto-hinting. Directory batch processing. |

### How It Works

```
Input font (.otf/.ttf)
       │
       ├─► [--scale] Uniform scaling (v10)
       │       ├─ TrueType: scale all coords + composites
       │       └─ CFF: TransformPen → T2CharStringPen → new CharStrings
       │
       ├─► [--widen] Horizontal scaling (v11)
       │       ├─ TrueType: scale X coords only
       │       └─ CFF: TransformPen(factor, 0, 0, 1, 0, 0)
       │
       ├─► Analyze format: glyf (TTF) vs CFF/CFF2 (OTF)
       │
       ├─► [ttfautohint] TrueType fonts
       │       --default-script=latn --fallback-scaling --symbol
       │
       └─► [psautohint] CFF fonts
               --no-zones-stems (re-hint from scaled outlines)
```

### Dependencies

| Tool | Install |
|------|---------|
| `ttfautohint` | `pip install ttfautohint` (or system package) |
| `psautohint` | `pip install psautohint` (part of AFDKO) |
| `fonttools` | `pip install fonttools` |

### Notes

- **Order of operations**: `--scale` applied first, then `--widen` (commutative for final result)
- **CFF hint scaling** (v10): `psautohint --no-zones-stems` re-hints from scaled outlines; CFF `Private` dict hints pre-scaled for correct alignment zones
- **Composite glyphs**: Component offsets scaled; transform matrices (a,b,c,d) left unchanged
- **Output format**: Preserves input format (OTF→OTF, TTF→TTF)
- **Validation**: `--widen` factor must be > 0

---

## Troubleshooting

### "No module named 'fontforge'"
FontForge Python bindings not installed. On Arch/Manjaro:
```bash
sudo pacman -S fontforge
```

### "No module named 'fontTools'"
```bash
pip install fonttools
```

### Fonts still look fuzzy
- Try FontForge version for more aggressive simplification
- Use `--remove-hints` if hinting seems to be causing issues
- Check if your renderer (browser, OS) is applying its own antialiasing

### Self-intersection errors
FontForge version will auto-fix these. fonttools version will warn but not fix.
