#!/usr/bin/env python3
"""
Improve OTF/TTF font rendering using FontForge.

Removes fringes, cleans up hinting, simplifies contours, and makes shapes clear.

FontForge provides more aggressive contour manipulation than fonttools,
making it better suited for aggressive shape cleanup.
"""

import sys
import os
import argparse
from pathlib import Path
from datetime import datetime

import fontforge


class FontForgeOptimizer:
    """Font optimization using FontForge's advanced contour operations."""

    def __init__(self, font_path: Path):
        self.font_path = font_path
        self.font = None
        self.log_entries = []
        self.glyphs_processed = 0
        self.points_removed = 0
        self.contours_simplified = 0

    def log(self, message: str):
        self.log_entries.append(message)

    def open_font(self) -> bool:
        """Open the font file."""
        try:
            self.font = fontforge.open(str(self.font_path))
            return True
        except Exception as e:
            self.log(f"  [ERROR] Failed to open: {e}")
            return False

    def round_coordinates(self) -> None:
        """Round all coordinates to integers to remove subpixel fringes."""
        try:
            glyphs_rounded = 0
            for glyph in self.font.glyphs():
                if glyph.isWorthOutputting():
                    # Round all points to integers
                    glyph.round()
                    glyphs_rounded += 1
            
            self.glyphs_processed = glyphs_rounded
            if glyphs_rounded > 0:
                self.log(f"  [OK] Rounded coordinates in {glyphs_rounded} glyphs")
            else:
                self.log("  [WARN] No outputtable glyphs found")
        except Exception as e:
            self.log(f"  [ERROR] Coordinate rounding failed: {e}")

    def simplify_contours(self, tolerance: float = 1.0) -> None:
        """
        Simplify glyph contours using FontForge's built-in simplifier.
        
        This removes redundant points and smooths jagged edges.
        """
        try:
            simplified = 0
            for glyph in self.font.glyphs():
                if not glyph.isWorthOutputting():
                    continue
                
                # Get initial point count
                initial_points = self._count_points(glyph)
                
                # Simplify the glyph
                glyph.simplify(tolerance, ("setwidthto", "logerror", "forcelines"))
                
                # Get final point count
                final_points = self._count_points(glyph)
                
                if final_points < initial_points:
                    simplified += 1
                    self.points_removed += (initial_points - final_points)
            
            self.contours_simplified = simplified
            if simplified > 0:
                self.log(f"  [SIMPLIFIED] {simplified} glyphs, removed {self.points_removed} points")
            else:
                self.log("  [OK] Contours already minimal")
        except Exception as e:
            self.log(f"  [ERROR] Contour simplification failed: {e}")

    def _count_points(self, glyph) -> int:
        """Count the number of points in a glyph's contours."""
        count = 0
        for contour in glyph.layers[1]:  # layer 1 is the outline
            if hasattr(contour, "__iter__"):
                count += len(contour)
        return count

    def remove_overlaps(self) -> None:
        """
        Remove overlapping contours using FontForge's union operation.
        
        Overlapping paths can cause rendering artifacts.
        """
        try:
            removed = 0
            for glyph in self.font.glyphs():
                if not glyph.isWorthOutputting():
                    continue
                
                # Remove overlap by using the intersect command
                glyph.intersect()
                removed += 1
            
            self.log(f"  [OK] Overlap removal applied to {removed} glyphs")
        except Exception as e:
            self.log(f"  [WARN] Overlap removal skipped: {e}")

    def correct_direction(self) -> None:
        """
        Ensure all contours have correct winding direction.
        
        Incorrect direction causes fill artifacts.
        """
        try:
            corrected = 0
            for glyph in self.font.glyphs():
                if not glyph.isWorthOutputting():
                    continue
                
                # Force correct direction (counter-clockwise for holes)
                glyph.correctDirection()
                corrected += 1
            
            self.log(f"  [OK] Corrected contour direction in {corrected} glyphs")
        except Exception as e:
            self.log(f"  [WARN] Direction correction skipped: {e}")

    def auto_hint(self) -> None:
        """
        Apply automatic hinting to improve screen rendering.
        """
        try:
            self.font.autoHint()
            self.log("  [OK] Auto-hinting applied")
        except Exception as e:
            self.log(f"  [WARN] Auto-hinting failed: {e}")

    def remove_hinting(self) -> None:
        """
        Remove all TrueType hinting instructions.
        
        Useful when hinting is broken and causing fringes.
        """
        try:
            self.font.dehint()
            self.log("  [OK] Hinting instructions removed")
        except Exception as e:
            self.log(f"  [WARN] Hinting removal failed: {e}")

    def set_vertical_metrics(self) -> None:
        """Set proper vertical metrics for better line spacing."""
        try:
            changes = []
            
            # Get current values
            ascent = self.font.ascent
            descent = self.font.descent
            
            # Ensure positive values
            if ascent < 0:
                self.font.ascent = abs(ascent)
                changes.append(f"ascent: {ascent}→{abs(ascent)}")
            
            if descent > 0:
                self.font.descent = -abs(descent)
                changes.append(f"descent: {descent}→{-abs(descent)}")
            
            # Set proper line gap (10% of ascent)
            suggested_gap = int(self.font.ascent * 0.1)
            if abs(self.font.descent) < suggested_gap:
                self.font.linegap = suggested_gap
                changes.append(f"linegap→{suggested_gap}")
            
            if changes:
                self.log(f"  [FIXED] Vertical metrics: {', '.join(changes)}")
            else:
                self.log("  [OK] Vertical metrics already correct")
        except Exception as e:
            self.log(f"  [WARN] Metrics fix failed: {e}")

    def remove_extreme_points(self) -> None:
        """
        Remove extreme points (points that don't significantly affect shape).
        
        This helps reduce rendering artifacts.
        """
        try:
            removed = 0
            for glyph in self.font.glyphs():
                if not glyph.isWorthOutputting():
                    continue
                
                before = self._count_points(glyph)
                glyph.removeOverlap()
                
                # Use cluster optimization to merge close points
                glyph.clusterCluster(1.0)  # 1 unit tolerance
                
                after = self._count_points(glyph)
                if after < before:
                    removed += (before - after)
            
            if removed > 0:
                self.log(f"  [CLEANED] Removed {removed} extreme/overlapping points")
            else:
                self.log("  [OK] No extreme points found")
        except Exception as e:
            self.log(f"  [WARN] Extreme point removal failed: {e}")

    def validate_outlines(self) -> None:
        """Validate and fix common outline problems."""
        try:
            issues_found = 0
            
            for glyph in self.font.glyphs():
                if not glyph.isWorthOutputting():
                    continue
                
                # Check for self-intersection
                if glyph.selfIntersects():
                    glyph.removeOverlap()
                    issues_found += 1
                
                # Validate contour count
                contour_count = len(glyph.layers[1])
                
                # Warn about glyphs with many contours (might be problematic)
                if contour_count > 50:
                    self.log(f"  [WARN] {glyph.glyphname}: {contour_count} contours (may be complex)")
            
            if issues_found > 0:
                self.log(f"  [FIXED] {issues_found} self-intersecting glyphs")
            else:
                self.log("  [OK] No outline validation issues")
        except Exception as e:
            self.log(f"  [WARN] Outline validation skipped: {e}")

    def optimize_all(self, remove_hints: bool = False) -> None:
        """
        Apply all optimizations.
        
        Args:
            remove_hints: If True, remove all hinting. If False, try auto-hinting.
        """
        # Step 1: Round coordinates
        self.round_coordinates()
        
        # Step 2: Simplify contours
        self.simplify_contours(tolerance=1.0)
        
        # Step 3: Remove extreme points
        self.remove_extreme_points()
        
        # Step 4: Correct contour direction
        self.correct_direction()
        
        # Step 5: Validate outlines
        self.validate_outlines()
        
        # Step 6: Fix vertical metrics
        self.set_vertical_metrics()
        
        # Step 7: Hinting
        if remove_hints:
            self.remove_hinting()
        else:
            # Try auto-hinting, fall back to removing hints if it fails
            try:
                self.auto_hint()
            except Exception:
                self.remove_hinting()

    def save_font(self, output_path: Path, format: str = None) -> bool:
        """
        Save the optimized font.
        
        Args:
            output_path: Path to save the font
            format: Output format (None = auto-detect from extension)
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            # Set output format based on extension if not specified
            if format is None:
                ext = output_path.suffix.lower()
                if ext == ".otf":
                    format = "opentype"
                elif ext == ".ttf":
                    format = "truetype"
                elif ext == ".woff":
                    format = "woff"
                elif ext == ".woff2":
                    format = "woff2"
                else:
                    format = "opentype"  # Default to OTF
            
            # Set generation flags for optimal rendering
            self.font.generate(
                str(output_path),
                flags=(
                    "opentype",      # Generate OpenType tables
                    "round",          # Round coordinates
                    "noflex",         # Disable Flex hints for clarity
                    "hint",           # Include hints
                    "normal",         # Standard output
                ),
                format=format if format else "opentype"
            )
            
            self.log(f"  [OK] Saved as {output_path.name}")
            return True
            
        except Exception as e:
            self.log(f"  [ERROR] Save failed: {e}")
            return False

    def print_report(self) -> None:
        """Print detailed report for this font."""
        print(f"\n{'='*70}")
        print(f"  📄 {self.font_path.name}")
        print(f"{'='*70}")
        
        # Font info
        glyph_count = len([g for g in self.font.glyphs() if g.isWorthOutputting()])
        print(f"\n  Glyph Count:  {glyph_count}")
        print(f"  Font Format:  {self.font.fonttype}")
        print(f"  Units/Em:     {self.font.unitsPerEm}")
        
        # Optimization log
        print(f"\n  Optimizations Applied:")
        for entry in self.log_entries:
            print(entry)
        
        # Summary stats
        stats = []
        if self.glyphs_processed > 0:
            stats.append(f"{self.glyphs_processed} glyphs processed")
        if self.contours_simplified > 0:
            stats.append(f"{self.contours_simplified} simplified")
        if self.points_removed > 0:
            stats.append(f"{self.points_removed} points removed")
        
        if stats:
            print(f"\n  Stats: {', '.join(stats)}")
        
        print()

    def close(self):
        """Close the font to free resources."""
        if self.font:
            self.font.close()


def find_font_files(input_dir: Path) -> list:
    """Find all OTF and TTF font files in the input directory."""
    font_extensions = {".otf", ".ttf"}
    font_files = []
    
    for ext in font_extensions:
        font_files.extend(input_dir.rglob(f"*{ext}"))
        font_files.extend(input_dir.rglob(f"*{ext.upper()}"))
    
    return sorted(set(font_files))


def main():
    parser = argparse.ArgumentParser(
        description="Improve OTF/TTF font rendering using FontForge. "
                    "Removes fringes, simplifies contours, and makes shapes clear.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Techniques Applied:
  1. Coordinate rounding   - Removes subpixel fringes
  2. Contour simplification - FontForge's aggressive point reduction
  3. Extreme point removal  - Removes redundant micro points
  4. Direction correction   - Fixes winding order
  5. Outline validation     - Fixes self-intersections
  6. Vertical metrics fix   - Proper line spacing
  7. Auto-hinting          - Fresh hinting instructions (or removal)

Examples:
  %(prog)s ./fonts                    # Process fonts, output to fonts_optimized/
  %(prog)s ./fonts ./my_output        # Custom output directory
  %(prog)s ./fonts -q                 # Quiet mode (summary only)
  %(prog)s ./fonts --remove-hints     # Remove all hinting instead of auto-hinting
        """
    )
    
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Input directory containing OTF/TTF font files"
    )
    
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=None,
        help="Output directory for processed fonts (default: <input_dir>_optimized)"
    )
    
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress detailed per-font reports"
    )
    
    parser.add_argument(
        "--remove-hints",
        action="store_true",
        help="Remove all hinting instructions instead of auto-hinting"
    )
    
    parser.add_argument(
        "--format",
        choices=["opentype", "truetype", "woff", "woff2"],
        default=None,
        help="Output format (default: auto-detect from input extension)"
    )
    
    args = parser.parse_args()
    
    # Validate input directory
    if not args.input_dir.exists():
        print(f"ERROR: Input directory does not exist: {args.input_dir}")
        sys.exit(1)
    
    if not args.input_dir.is_dir():
        print(f"ERROR: Input path is not a directory: {args.input_dir}")
        sys.exit(1)
    
    # Set default output directory if not specified
    if args.output_dir is None:
        args.output_dir = args.input_dir.parent / f"{args.input_dir.name}_fontforge_optimized"
    
    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find font files
    font_files = find_font_files(args.input_dir)
    
    if not font_files:
        print(f"No font files found in {args.input_dir}")
        sys.exit(1)
    
    # Header
    print(f"{'='*70}")
    print(f"  FontForge Optimizer - Shape Clarity Enhancement")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    print(f"\n  Input:   {args.input_dir}")
    print(f"  Output:  {args.output_dir}")
    print(f"  Fonts:   {len(font_files)} file(s)")
    print(f"  Hints:   {'Remove all' if args.remove_hints else 'Auto-hint'}")
    
    # Process each font
    success_count = 0
    fail_count = 0
    total_original_size = 0
    total_optimized_size = 0
    
    for i, font_path in enumerate(font_files, 1):
        print(f"\n[{i}/{len(font_files)}] Processing: {font_path.name}")
        total_original_size += font_path.stat().st_size
        
        # Create optimizer
        optimizer = FontForgeOptimizer(font_path)
        
        # Open font
        if not optimizer.open_font():
            fail_count += 1
            continue
        
        # Apply optimizations
        optimizer.optimize_all(remove_hints=args.remove_hints)
        
        # Determine output path
        output_path = args.output_dir / font_path.name
        
        # Save font
        if optimizer.save_font(output_path, format=args.format):
            success_count += 1
            if output_path.exists():
                total_optimized_size += output_path.stat().st_size
        else:
            fail_count += 1
        
        # Print report
        if not args.quiet:
            optimizer.print_report()
        
        # Close font
        optimizer.close()
    
    # Summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  Processed:  {success_count + fail_count} font(s)")
    print(f"  Succeeded:  {success_count}")
    print(f"  Failed:     {fail_count}")
    
    if success_count > 0:
        orig_mb = total_original_size / (1024 * 1024)
        opt_mb = total_optimized_size / (1024 * 1024)
        ratio = (opt_mb / orig_mb) * 100 if orig_mb > 0 else 100
        print(f"  Size:       {orig_mb:.2f} MB → {opt_mb:.2f} MB ({ratio:.1f}%)")
    
    print(f"\n  Output: {args.output_dir}")
    print()
    
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
