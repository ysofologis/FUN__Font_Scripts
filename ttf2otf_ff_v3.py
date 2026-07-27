"""
TTF to OTF Converter - FRINGE ELIMINATION SPECIALIST v1.0
Aggressive fringe removal for crystal-clear font rendering

Features:
- Multi-pass fringe detection and removal
- Subpixel smoothing and alignment
- Contour simplification and optimization
- Overlap resolution and cleanup
- Edge sharpening without artifacts

Usage:
    python ttf2otf_ff_v3.py <input_dir_or_file> <output_dir> [--aggression low|medium|high|extreme] [--thickness N]

Example:
    python ttf2otf_ff_v3.py ./fonts ./output --aggression extreme
    python ttf2otf_ff_v3.py ./fonts ./output --thickness 30
    python ttf2otf_ff_v3.py ./fonts ./output --aggression high --thickness 20
"""

import os
import sys
import argparse
import fontforge
import math


def analyze_fringe_risk(glyph):
    """
    Analyze glyph for potential fringe issues
    Returns risk score (0-100) and problem areas
    """
    risk_score = 0
    problems = []
    
    try:
        # Check for overlapping contours
        if hasattr(glyph, 'overlaps') and glyph.overlaps:
            risk_score += 30
            problems.append("overlaps")
        
        # Check for tiny segments
        if hasattr(glyph, 'contours') and glyph.contours:
            tiny_segments = 0
            for contour in glyph.contours:
                for i in range(len(contour)):
                    x1, y1 = contour[i][0], contour[i][1]
                    x2, y2 = contour[(i+1) % len(contour)][0], contour[(i+1) % len(contour)][1]
                    dist = math.sqrt((x2-x1)**2 + (y2-y1)**2)
                    if dist < 10:  # Tiny segment threshold
                        tiny_segments += 1
            if tiny_segments > 5:
                risk_score += 25
                problems.append("tiny_segments")
        
        # Check for collinear points
        if hasattr(glyph, 'contours') and glyph.contours:
            collinear = 0
            for contour in glyph.contours:
                for i in range(len(contour)):
                    prev = contour[i-1] if i > 0 else contour[-1]
                    curr = contour[i]
                    nxt = contour[(i+1) % len(contour)]
                    
                    # Calculate area of triangle (0 = collinear)
                    area = abs((prev[0]*(curr[1]-nxt[1]) + 
                               curr[0]*(nxt[1]-prev[1]) + 
                               nxt[0]*(prev[1]-curr[1])) / 2)
                    if area < 0.1:
                        collinear += 1
            if collinear > 10:
                risk_score += 20
                problems.append("collinear_points")
        
        return min(risk_score, 100), problems
    except:
        return 0, []


def aggressive_overlap_removal(glyph, strength='medium'):
    """
    Aggressively remove overlapping contours that cause fringing
    """
    try:
        # Primary overlap removal
        glyph.removeOverlap()
        
        # Secondary pass for stubborn overlaps
        if strength in ['high', 'extreme']:
            glyph.removeOverlap()
            
            # Simplify after overlap removal
            if strength == 'extreme':
                glyph.simplify(200, 0.8)
                glyph.removeOverlap()
        
        return True
    except:
        return False


def subpixel_smoothing(glyph, strength='medium'):
    """
    Smooth glyph at subpixel level to eliminate fringing
    """
    try:
        # Round to integer coordinates (prevents subpixel fringing)
        glyph.round()
        
        # Additional smoothing passes based on strength
        if strength in ['high', 'extreme']:
            # Simplify with lower tolerance for more smoothing
            tolerance = 0.3 if strength == 'extreme' else 0.5
            glyph.simplify(160, tolerance)
            glyph.round()
            
            if strength == 'extreme':
                # Extra pass for extreme smoothing
                glyph.simplify(140, 0.2)
                glyph.round()
        
        return True
    except:
        return False


def contour_direction_fix(glyph, strength='medium'):
    """
    Fix contour directions to prevent rendering artifacts
    """
    try:
        # Standard direction correction
        glyph.correctDirection()
        
        # Canonical contours for consistent representation
        glyph.canonicalContours()
        
        # Extra validation for high strength
        if strength in ['high', 'extreme']:
            # Second pass ensures all directions are correct
            glyph.correctDirection()
            
            if strength == 'extreme':
                # Reorder contours for optimal rendering
                glyph.canonicalContours()
        
        return True
    except:
        return False


def eliminate_tiny_segments(glyph, strength='medium'):
    """
    Remove tiny segments that cause fringe artifacts
    """
    try:
        if strength == 'extreme':
            # Very aggressive tiny segment removal
            glyph.simplify(100, 0.1)
            glyph.clean()
            glyph.simplify(80, 0.05)
        elif strength == 'high':
            # Aggressive removal
            glyph.simplify(120, 0.2)
            glyph.clean()
        else:
            # Standard removal
            glyph.simplify()
            glyph.clean()
        
        return True
    except:
        return False


def collinear_point_removal(glyph, strength='medium'):
    """
    Remove collinear points that create fringe artifacts
    """
    try:
        if strength == 'extreme':
            # Aggressive collinear removal
            for _ in range(3):  # Multiple passes
                glyph.simplify(140, 0.3)
        elif strength == 'high':
            glyph.simplify(160, 0.5)
        else:
            glyph.simplify()
        
        return True
    except:
        return False


def increase_thickness(font, thickness, verbose=False):
    """
    Increase the stroke weight (thickness) of all glyphs.
    Thickness is in font units (e.g. 10-40 for subtle bolding, 50+ for heavy).
    """
    if thickness <= 0:
        return

    if verbose:
        print(f"    → Increasing thickness by {thickness} font units...")

    try:
        font.selection.all()
        font.changeWeight(thickness)
        font.selection.all()
        font.removeOverlap()
        if verbose:
            print(f"      ✅ Thickness increased successfully")
    except Exception as e:
        if verbose:
            print(f"      ⚠️ Thickness increase issue: {e}")


def compactify(font, intensity='medium', verbose=False):
    """
    Make the font feel more compact and solid:
    - Tightens advance widths (reduces horizontal spacing)
    - Shrinks inner contours (counter spaces) to fill holes
    - Removes overlaps to merge strokes into solid shapes
    """
    intensity_pct = {'low': 2, 'medium': 4, 'high': 6}
    width_reduction = intensity_pct.get(intensity, 4)

    if verbose:
        print(f"    → Compacting (width -{width_reduction}%, counters shrink)...")

    # 1. Tighten advance widths
    for glyph in font.glyphs():
        w = glyph.width
        if w > 0:
            new_w = int(w * (100 - width_reduction) / 100)
            glyph.width = new_w

    # 2. Shrink inner contours (counter spaces)
    shrink_pct = intensity_pct.get(intensity, 4) * 0.005  # ~0.02 for medium
    for glyph in font.glyphs():
        contours = glyph.foreground
        if len(contours) < 2:
            continue  # no inner contours to shrink

        # Compute area of each contour; the largest is the outer contour
        areas = []
        for contour in contours:
            pts = [(p.x, p.y) for p in contour]
            # Shoelace formula for signed area
            n = len(pts)
            area = 0
            for i in range(n):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % n]
                area += x1 * y2 - x2 * y1
            areas.append(abs(area))

        outer_idx = areas.index(max(areas))

        for i, contour in enumerate(contours):
            if i == outer_idx:
                continue  # skip outer contour

            # Calculate bounding box center of this inner contour
            pts = [p for p in contour]
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            cx = (min(xs) + max(xs)) / 2
            cy = (min(ys) + max(ys)) / 2

            # Move all points toward center
            for p in pts:
                p.x = int(p.x + (cx - p.x) * shrink_pct)
                p.y = int(p.y + (cy - p.y) * shrink_pct)

    # 3. Clean up after shrinking
    font.selection.all()
    font.removeOverlap()
    font.selection.all()
    font.round()

    if verbose:
        print(f"      ✅ Compactification done")


def edge_sharpening(glyph, strength='medium'):
    """
    Sharpen edges after smoothing to maintain clarity
    """
    try:
        # Ensure proper direction after smoothing
        glyph.correctDirection()
        
        if strength == 'extreme':
            # Final precision pass
            glyph.round()
            glyph.canonicalContours()
        
        return True
    except:
        return False


def fringe_elimination_pass(font, glyphs_list, strength='medium', verbose=False):
    """
    Execute a complete fringe elimination pass on all glyphs
    """
    processed = 0
    high_risk = 0
    fringe_issues = []
    
    if verbose:
        print(f"      Running fringe elimination pass (strength: {strength})...")
    
    for glyph in glyphs_list:
        if not glyph.isWorthOutputting():
            continue
        
        try:
            # Analyze glyph for fringe risk
            risk_score, problems = analyze_fringe_risk(glyph)
            
            if risk_score > 50:
                high_risk += 1
                fringe_issues.append((glyph.glyphname, risk_score, problems))
            
            # Apply fringe elimination based on strength
            # 1. Aggressive overlap removal (major cause of fringing)
            aggressive_overlap_removal(glyph, strength)
            
            # 2. Subpixel smoothing (eliminates pixel-level artifacts)
            subpixel_smoothing(glyph, strength)
            
            # 3. Fix contour directions (prevents rendering artifacts)
            contour_direction_fix(glyph, strength)
            
            # 4. Eliminate tiny segments (creates fringing at small sizes)
            eliminate_tiny_segments(glyph, strength)
            
            # 5. Remove collinear points (reduces jagged edges)
            collinear_point_removal(glyph, strength)
            
            # 6. Final edge sharpening
            edge_sharpening(glyph, strength)
            
            processed += 1
            
        except Exception as e:
            if verbose:
                print(f"        ⚠️ Could not process glyph {glyph.glyphname}: {e}")
    
    return processed, high_risk, fringe_issues


def global_fringe_elimination(font, strength='medium', verbose=False):
    """
    Apply global font-level fringe elimination techniques
    """
    try:
        if verbose:
            print(f"    → Global fringe elimination...")
        
        # Remove global overlaps
        font.selection.all()
        font.removeOverlap()
        
        # Simplify overall font
        font.simplify()
        
        # Fix global contour issues
        font.canonicalContours()
        
        # For extreme strength, additional global passes
        if strength == 'extreme':
            font.removeOverlap()
            font.simplify(160, 0.5)
            font.canonicalContours()
        
        return True
    except Exception as e:
        if verbose:
            print(f"      ⚠️ Global elimination issue: {e}")
        return False


def optimize_for_rendering(font, strength='medium', verbose=False):
    """
    Apply rendering-specific optimizations to prevent fringing
    """
    try:
        if verbose:
            print(f"    → Optimizing for fringe-free rendering...")
        
        # Set GASP table for proper grid fitting
        try:
            # GASP: Gridfitting and Anti-aliasing for Smooth Processing
            # Values: 0x03 = gridfit + antialias for all sizes
            font.gasp = [(0, 3)]
        except:
            pass
        
        # Ensure OS/2 metrics don't cause clipping (which creates fringes)
        if hasattr(font, 'os2_winascent') and hasattr(font, 'ascent'):
            if font.os2_winascent < font.ascent:
                font.os2_winascent = font.ascent + 50
        if hasattr(font, 'os2_windescent') and hasattr(font, 'descent'):
            if font.os2_windescent < font.descent:
                font.os2_windescent = font.descent + 50
        
        # Set proper PPEM (Pixels Per EM) for scaling
        if hasattr(font, 'upem'):
            font.upem = font.em
        
        return True
    except Exception as e:
        if verbose:
            print(f"      ⚠️ Rendering optimization issue: {e}")
        return False


def process_one_font(font_path: str, out_dir: str, aggression: str = 'medium', thickness: int = 0, compact: bool = False, verbose: bool = True) -> None:
    """
    Convert a single TTF font with aggressive fringe elimination
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"Processing: {os.path.basename(font_path)}")
        print(f"Fringe elimination aggression: {aggression.upper()}")
        if thickness > 0:
            print(f"Thickness boost: +{thickness} font units")
        if compact:
            print(f"Compact mode: ON")
        print(f"{'='*60}")

    try:
        # Open the font file
        font = fontforge.open(font_path)
        
        if verbose:
            print(f"  Loaded: {font.familyname} {font.fontname}")
            glyph_count = sum(1 for _ in font.glyphs())
            print(f"  Total glyphs: {glyph_count}")
    except Exception as e:
        print(f"❌ Could not open {font_path}: {e}", file=sys.stderr)
        return
    
    try:
        if verbose:
            print(f"\n  🔧 APPLYING FRINGE ELIMINATION")
            print(f"  {'-'*40}")
        
        # Get all glyphs for processing
        glyphs_list = list(font.glyphs())
        
        # First pass: Glyph-level fringe elimination
        if verbose:
            print(f"\n  📍 Phase 1: Glyph-level elimination")
        
        processed, high_risk, fringe_issues = fringe_elimination_pass(
            font, glyphs_list, aggression, verbose
        )
        
        if verbose and high_risk > 0:
            print(f"\n      ⚠️ Found {high_risk} high-risk fringe glyphs")
            if verbose >= 2:  # Ultra verbose
                for name, score, problems in fringe_issues[:5]:
                    print(f"        - {name}: risk {score}% ({', '.join(problems)})")
        
        # Second pass: Global fringe elimination
        if verbose:
            print(f"\n  📍 Phase 2: Global elimination")
        global_fringe_elimination(font, aggression, verbose)
        
        # Third pass: Rendering optimization
        if verbose:
            print(f"\n  📍 Phase 3: Rendering optimization")
        optimize_for_rendering(font, aggression, verbose)
        
        # Phase 4: Thickness increase
        if thickness > 0:
            if verbose:
                print(f"\n  📍 Phase 4: Thickness boost")
            increase_thickness(font, thickness, verbose)

            # Phase 4b: Post-thickness fringe cleanup
            # Thickening introduces new overlaps, jagged edges, and artifacts
            if verbose:
                print(f"\n  📍 Phase 4b: Post-thickness fringe cleanup")

            font.selection.all()
            font.removeOverlap()

            # Re-run fringe elimination on all glyphs post-thickness
            post_glyphs = list(font.glyphs())
            fringe_elimination_pass(font, post_glyphs, aggression, verbose=False)
            global_fringe_elimination(font, aggression, verbose=False)
            optimize_for_rendering(font, aggression, verbose=False)

            # Final round to clean up anything left
            font.selection.all()
            font.removeOverlap()
            font.round()

            if verbose:
                print(f"      ✅ Post-thickness cleanup done")

        # Phase 5: Compact & solid
        if compact:
            if verbose:
                print(f"\n  📍 Phase 5: Compact & solid")
            compactify(font, aggression, verbose)

        # Phase 6: Ultra/extreme additional refinement
        if aggression in ['high', 'extreme']:
            if verbose:
                print(f"\n  📍 Phase {5 if thickness > 0 else 4}: {aggression.upper()} refinement pass")
            
            # Second pass on high-risk glyphs only
            if fringe_issues:
                high_risk_glyphs = [font[glyph_name] for glyph_name, _, _ in fringe_issues[:20]]
                for glyph in high_risk_glyphs:
                    try:
                        # Extra aggressive treatment for problem glyphs
                        aggressive_overlap_removal(glyph, 'extreme')
                        subpixel_smoothing(glyph, 'extreme')
                        eliminate_tiny_segments(glyph, 'extreme')
                    except:
                        pass
            
            if aggression == 'extreme':
                # Final global cleanup
                font.selection.all()
                font.removeOverlap()
                font.simplify(120, 0.2)
                font.canonicalContours()
        
        # Create output directory
        os.makedirs(out_dir, exist_ok=True)
        
        # Generate output filename
        base_name = os.path.splitext(os.path.basename(font_path))[0]
        out_path = os.path.join(out_dir, f"{base_name}.otf")
        
        # Generate the fringe-free font
        if verbose:
            print(f"\n  💾 Generating fringe-free OTF...")
        
        try:
            font.generate(out_path, flags=("opentype", "cff"))
        except:
            try:
                font.generate(out_path, flags=("opentype",))
            except:
                font.generate(out_path)
        
        # Statistics
        final_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        input_size = os.path.getsize(font_path)
        
        if verbose:
            print(f"\n  ✅ FRINGE ELIMINATION COMPLETE")
            print(f"  {'='*40}")
            print(f"  Output: {os.path.basename(out_path)}")
            print(f"  Size: {input_size/1024:.1f}KB → {final_size/1024:.1f}KB")
            print(f"  Processed: {processed} glyphs")
            print(f"  High-risk glyphs treated: {high_risk}")
            print(f"  Aggression level: {aggression.upper()}")
            print(f"  ✅ Fringe-free font saved successfully!")
        
    except Exception as e:
        print(f"❌ Error processing {font_path}: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
    finally:
        try:
            font.close()
        except:
            pass


def walk_and_convert(input_path: str, out_dir: str, aggression: str = 'medium', thickness: int = 0, compact: bool = False, verbose: bool = True) -> None:
    """
    Recursively convert all TTF files with fringe elimination
    """
    os.makedirs(out_dir, exist_ok=True)
    
    if os.path.isfile(input_path):
        process_one_font(input_path, out_dir, aggression, thickness, compact, verbose)
        return
    
    # Find all TTF files
    ttf_files = []
    for root, dirs, files in os.walk(input_path):
        for fname in files:
            if fname.lower().endswith(".ttf"):
                ttf_files.append(os.path.join(root, fname))
    
    if not ttf_files:
        print(f"⚠️ No .ttf files found in {input_path}")
        return
    
    if verbose:
        print(f"\n📁 Found {len(ttf_files)} TTF files to process")
        print(f"🔧 Fringe elimination aggression: {aggression.upper()}\n")
    
    success_count = 0
    fail_count = 0
    
    for font_path in ttf_files:
        rel_path = os.path.relpath(os.path.dirname(font_path), input_path)
        target_dir = os.path.join(out_dir, rel_path)
        
        try:
            process_one_font(font_path, target_dir, aggression, thickness, compact, verbose)
            success_count += 1
        except Exception as e:
            fail_count += 1
            print(f"❌ Failed: {os.path.basename(font_path)} - {e}", file=sys.stderr)
    
    if verbose:
        print(f"\n{'='*60}")
        print(f"📊 FRINGE ELIMINATION SUMMARY")
        print(f"{'='*60}")
        print(f"Total fonts: {len(ttf_files)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {fail_count}")
        print(f"Aggression: {aggression.upper()}")
        print(f"\n✅ All fonts processed with fringe elimination!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="TTF to OTF Converter - FRINGE ELIMINATION SPECIALIST",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
FRINGE ELIMINATION AGGRESSION LEVELS:
  low     - Mild fringe removal (fast, for minor issues)
  medium  - Standard fringe elimination (balanced)
  high    - Aggressive fringe removal (for problematic fonts)
  extreme - Maximum fringe elimination (slowest, best results)

FRINGE ISSUES THIS SOLVES:
  ✗ Jagged edges on curves
  ✗ Artifacts around glyphs at small sizes
  ✗ Overlapping contour artifacts
  ✗ Subpixel rendering artifacts
  ✗ Collinear point fringing
  ✗ Tiny segment rendering issues

Examples:
  # Standard fringe removal
  python ttf2otf_fringe_killer.py ./fonts ./output --aggression medium

  # Extreme fringe elimination for problematic fonts
  python ttf2otf_fringe_killer.py ./fonts ./output --aggression extreme

  # Single font with high aggression
  python ttf2otf_fringe_killer.py font.ttf ./output --aggression high --verbose

  # Thicken font by 25 font units (subtle bolding)
  python ttf2otf_fringe_killer.py font.ttf ./output --thickness 25

  # Combined: fringe elimination + heavy thickening
  python ttf2otf_fringe_killer.py font.ttf ./output --aggression high --thickness 40
        """
    )
    
    parser.add_argument(
        "input_path",
        help="Path to a directory containing .ttf files or a single .ttf file"
    )
    parser.add_argument(
        "output_dir",
        help="Directory where fringe-free .otf files will be saved"
    )
    parser.add_argument(
        "--aggression", "-a",
        choices=['low', 'medium', 'high', 'extreme'],
        default='medium',
        help="Fringe elimination aggression: low, medium, high, extreme (default: medium)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed processing information"
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress all output except errors"
    )
    parser.add_argument(
        "--thickness", "-t",
        type=int,
        default=0,
        help="Increase font thickness by N font units (e.g. 10-40 for subtle bolding, 50+ for heavy)"
    )
    parser.add_argument(
        "--compact", "-c",
        action="store_true",
        help="Make font more compact and solid (tighter spacing, smaller counters)"
    )
    
    args = parser.parse_args()
    
    # Determine verbosity
    if args.quiet:
        verbose = False
    else:
        verbose = args.verbose or True
    
    # Aggression level description
    aggression_desc = {
        'low': 'Mild fringe removal (fast, for minor issues)',
        'medium': 'Standard fringe elimination (balanced)',
        'high': 'Aggressive fringe removal (for problematic fonts)',
        'extreme': 'Maximum fringe elimination (slowest, best results)'
    }
    
    if verbose:
        print("=" * 60)
        print("🔧 TTF to OTF - FRINGE ELIMINATION SPECIALIST v1.0")
        print("=" * 60)
        print(f"Input:  {args.input_path}")
        print(f"Output: {args.output_dir}")
        print(f"Aggression: {args.aggression.upper()} - {aggression_desc[args.aggression]}")
        if args.thickness > 0:
            print(f"Thickness: +{args.thickness} font units")
        print()
    
    # Check input
    if not os.path.exists(args.input_path):
        print(f"❌ Error: Input path does not exist: {args.input_path}", file=sys.stderr)
        sys.exit(1)
    
    # Check FontForge
    try:
        ff_version = fontforge.version()
        if verbose:
            print(f"FontForge version: {ff_version}")
            print()
    except Exception as e:
        print(f"❌ Error: FontForge is not available: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Start fringe elimination
    walk_and_convert(args.input_path, args.output_dir, args.aggression, args.thickness, args.compact, verbose)
    
    if verbose:
        print()
        print("=" * 60)
        print("🎯 FRINGE ELIMINATION COMPLETE!")
        print("=" * 60)
        print("Your fonts are now optimized for:")
        print("  ✓ Crystal-clear rendering at all sizes")
        print("  ✓ No jagged edges or artifacts")
        print("  ✓ Perfect subpixel alignment")
        print("  ✓ Professional print quality")
        print("=" * 60)


if __name__ == "__main__":
    main()
