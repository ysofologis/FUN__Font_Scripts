#!/usr/bin/env python3
"""
Font Glyph Replacer - Mix & Match Glyphs Between Fonts

Replace specific glyphs in a destination font with glyphs from a source font.
Supports both CFF/OTF and TrueType/TTF formats.

Usage:
  python font_glyph_replacer.py dest.otf source.otf --map "k=κ, g=ɡ" -o output.otf
  python font_glyph_replacer.py dest.otf source.otf --map-file mapping.txt -o output.otf
  python font_glyph_replacer.py dest.otf source.otf --range "U+0061-U+007A" -o output.otf  # replace a-z
"""

import sys
import argparse
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttFont import TTLibError
import copy


def parse_codepoint(s: str) -> int:
    """Parse Unicode codepoint from string (U+XXXX, 0xXXXX, or decimal)."""
    s = s.strip().lower()
    if s.startswith('u+'):
        return int(s[2:], 16)
    elif s.startswith('0x'):
        return int(s[2:], 16)
    else:
        return int(s)


def parse_mapping(mapping_str: str) -> dict:
    """Parse mapping string like 'k=κ, g=ɡ, U+0041=U+0391' into {dest_cp: src_cp}."""
    mapping = {}
    for pair in mapping_str.split(','):
        pair = pair.strip()
        if not pair:
            continue
        if '=' not in pair:
            raise ValueError(f"Invalid mapping pair: '{pair}' (expected 'dest=src')")
        dest_str, src_str = pair.split('=', 1)
        dest_cp = parse_codepoint(dest_str.strip())
        src_cp = parse_codepoint(src_str.strip())
        mapping[dest_cp] = src_cp
    return mapping


def load_mapping_file(path: str) -> dict:
    """Load mappings from file (one 'dest=src' per line, # comments)."""
    mapping = {}
    with open(path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                dest_str, src_str = line.split('=', 1)
                dest_cp = parse_codepoint(dest_str.strip())
                src_cp = parse_codepoint(src_str.strip())
                mapping[dest_cp] = src_cp
            except Exception as e:
                raise ValueError(f"Line {line_num}: {e}")
    return mapping


def parse_range(range_str: str) -> list:
    """Parse range like 'U+0061-U+007A' into list of codepoints."""
    if '-' not in range_str:
        return [parse_codepoint(range_str)]
    start_str, end_str = range_str.split('-', 1)
    start = parse_codepoint(start_str.strip())
    end = parse_codepoint(end_str.strip())
    if start > end:
        raise ValueError(f"Invalid range: start > end")
    return list(range(start, end + 1))


def get_cmap_reverse(font: TTFont) -> dict:
    """Return {codepoint: glyph_name} for all cmap entries."""
    cmap = font.getBestCmap()
    return {cp: name for cp, name in cmap.items()}


def get_glyph_name(font: TTFont, codepoint: int) -> str:
    """Get glyph name for a codepoint, or None if not in font."""
    cmap = font.getBestCmap()
    return cmap.get(codepoint)


def copy_glyph_cff(dest_font: TTFont, src_font: TTFont, dest_name: str, src_name: str) -> bool:
    """Copy CFF CharString from src to dest."""
    cff_key_dest = 'CFF2' if 'CFF2' in dest_font else 'CFF '
    cff_key_src = 'CFF2' if 'CFF2' in src_font else 'CFF '
    
    if cff_key_dest not in dest_font or cff_key_src not in src_font:
        return False
    
    dest_top = dest_font[cff_key_dest].cff.topDictIndex[0]
    src_top = src_font[cff_key_src].cff.topDictIndex[0]
    
    if src_name not in src_top.CharStrings:
        return False
    
    dest_top.CharStrings[dest_name] = src_top.CharStrings[src_name]
    # Ensure private dict reference for hinting
    if hasattr(src_top, 'Private'):
        dest_top.CharStrings[dest_name].private = src_top.Private
    return True


def copy_glyph_truetype(dest_font: TTFont, src_font: TTFont, dest_name: str, src_name: str) -> bool:
    """Copy TrueType glyf data from src to dest."""
    if 'glyf' not in dest_font or 'glyf' not in src_font:
        return False
    
    if src_name not in src_font['glyf'].glyphs:
        return False
    
    dest_font['glyf'].glyphs[dest_name] = copy.deepcopy(src_font['glyf'].glyphs[src_name])
    return True


def copy_metrics(dest_font: TTFont, src_font: TTFont, dest_name: str, src_name: str) -> bool:
    """Copy hmtx/vmtx metrics from src to dest."""
    copied = False
    
    # Horizontal metrics
    if 'hmtx' in dest_font and 'hmtx' in src_font:
        if src_name in src_font['hmtx'].metrics:
            dest_font['hmtx'].metrics[dest_name] = src_font['hmtx'].metrics[src_name]
            copied = True
    
    # Vertical metrics
    if 'vmtx' in dest_font and 'vmtx' in src_font:
        if src_name in src_font['vmtx'].metrics:
            dest_font['vmtx'].metrics[dest_name] = src_font['vmtx'].metrics[src_name]
            copied = True
    
    return copied


def copy_kerning(dest_font: TTFont, src_font: TTFont, dest_name: str, src_name: str, verbose: bool = False) -> int:
    """
    Copy kerning pairs involving src_name to dest_name.
    Handles both GPOS (OTF) and kern (TTF) tables.
    Returns number of pairs copied.
    """
    count = 0
    
    # --- GPOS table (OpenType Layout) ---
    if 'GPOS' in dest_font and 'GPOS' in src_font:
        src_gpos = src_font['GPOS'].table
        dest_gpos = dest_font['GPOS'].table
        
        if hasattr(src_gpos, 'LookupList') and hasattr(dest_gpos, 'LookupList'):
            # This is complex - GPOS lookups are indexed by glyph names via Coverage tables
            # For simplicity, we skip deep GPOS copying (requires full layout engine)
            if verbose:
                print("  ⚠ GPOS kerning copy not implemented (complex), skipping")
    
    # --- kern table (legacy TrueType/OpenType) ---
    if 'kern' in dest_font and 'kern' in src_font:
        src_kern = src_font['kern']
        dest_kern = dest_font['kern']
        
        for table_idx, src_subtable in enumerate(src_kern.kernTables):
            if not hasattr(src_subtable, 'kernTable'):
                continue
            
            # Ensure dest has same number of subtables
            while len(dest_kern.kernTables) <= table_idx:
                from fontTools.ttLib.tables._k_e_r_n import KernTable, KernSubtable
                new_subtable = KernSubtable()
                new_subtable.kernTable = {}
                dest_kern.kernTables.append(new_subtable)
            
            dest_subtable = dest_kern.kernTables[table_idx]
            if not hasattr(dest_subtable, 'kernTable'):
                dest_subtable.kernTable = {}
            
            # Copy pairs where src_name is left or right glyph
            for (left, right), value in src_subtable.kernTable.items():
                if left == src_name:
                    dest_subtable.kernTable[(dest_name, right)] = value
                    count += 1
                if right == src_name:
                    dest_subtable.kernTable[(left, dest_name)] = value
                    count += 1
    
    return count


def replace_glyphs(dest_font: TTFont, src_font: TTFont, mapping: dict, 
                   copy_kern: bool = True, verbose: bool = False) -> dict:
    """
    Replace glyphs in dest_font with glyphs from src_font per mapping.
    
    mapping: {dest_codepoint: src_codepoint}
    
    Returns stats dict.
    """
    stats = {
        'total': len(mapping),
        'success': 0,
        'failed': 0,
        'missing_src': 0,
        'missing_dest': 0,
        'kern_pairs': 0,
    }
    
    dest_cmap = get_cmap_reverse(dest_font)
    src_cmap = get_cmap_reverse(src_font)
    
    for dest_cp, src_cp in mapping.items():
        dest_name = dest_cmap.get(dest_cp)
        src_name = src_cmap.get(src_cp)
        
        if verbose:
            print(f"  U+{dest_cp:04X} ('{dest_name}') ← U+{src_cp:04X} ('{src_name}')", end=' ')
        
        if dest_name is None:
            if verbose:
                print("✗ dest glyph not in font")
            stats['missing_dest'] += 1
            stats['failed'] += 1
            continue
        
        if src_name is None:
            if verbose:
                print("✗ src glyph not in font")
            stats['missing_src'] += 1
            stats['failed'] += 1
            continue
        
        # Copy outline
        outline_ok = False
        if 'CFF ' in dest_font or 'CFF2' in dest_font:
            outline_ok = copy_glyph_cff(dest_font, src_font, dest_name, src_name)
        elif 'glyf' in dest_font:
            outline_ok = copy_glyph_truetype(dest_font, src_font, dest_name, src_name)
        
        if not outline_ok:
            if verbose:
                print("✗ outline copy failed")
            stats['failed'] += 1
            continue
        
        # Copy metrics
        copy_metrics(dest_font, src_font, dest_name, src_name)
        
        # Copy kerning
        if copy_kern:
            kern_count = copy_kerning(dest_font, src_font, dest_name, src_name, verbose)
            stats['kern_pairs'] += kern_count
        
        if verbose:
            print("✓")
        stats['success'] += 1
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Replace glyphs in a destination font with glyphs from a source font.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Replace Latin k with Greek kappa, Latin g with single-storey g
  python font_glyph_replacer.py dest.otf source.otf --map "k=κ, g=ɡ" -o output.otf

  # Using codepoints explicitly
  python font_glyph_replacer.py dest.otf source.otf --map "U+006B=U+03BA, U+0067=U+0261" -o output.otf

  # Load mappings from file
  python font_glyph_replacer.py dest.otf source.otf --map-file mappings.txt -o output.otf

  # Replace entire range (a-z from source)
  python font_glyph_replacer.py dest.otf source.otf --range "U+0061-U+007A" -o output.otf

  # Combine multiple options
  python font_glyph_replacer.py dest.otf source.otf --map "k=κ" --range "U+0041-U+005A" -o output.otf

Mapping file format (mappings.txt):
  # Comments start with #
  k = κ           # Latin k → Greek kappa
  U+0067 = U+0261 # Latin g → single-storey g
  U+0041 = U+0391 # Latin A → Greek Alpha
"""
    )
    
    parser.add_argument('dest_font', help='Destination font file (to be modified)')
    parser.add_argument('src_font', help='Source font file (glyphs copied from)')
    parser.add_argument('-o', '--output', required=True, help='Output font file')
    parser.add_argument('--map', help='Comma-separated mappings: "dest=src, dest=src" (codepoints or chars)')
    parser.add_argument('--map-file', help='File with mappings (one "dest=src" per line)')
    parser.add_argument('--range', help='Unicode range to replace entirely: "U+XXXX-U+YYYY"')
    parser.add_argument('--no-kern', action='store_true', help='Skip copying kerning pairs')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    # Build mapping
    mapping = {}
    
    if args.map:
        try:
            mapping.update(parse_mapping(args.map))
        except ValueError as e:
            print(f"Error parsing --map: {e}")
            sys.exit(1)
    
    if args.map_file:
        try:
            mapping.update(load_mapping_file(args.map_file))
        except ValueError as e:
            print(f"Error loading --map-file: {e}")
            sys.exit(1)
    
    if args.range:
        try:
            codepoints = parse_range(args.range)
            # Map each codepoint to itself (replace with same codepoint from source)
            for cp in codepoints:
                mapping[cp] = cp
        except ValueError as e:
            print(f"Error parsing --range: {e}")
            sys.exit(1)
    
    if not mapping:
        print("Error: No mappings specified. Use --map, --map-file, or --range")
        sys.exit(1)
    
    # Load fonts
    try:
        if args.verbose:
            print(f"Loading destination: {args.dest_font}")
        dest_font = TTFont(args.dest_font)
    except TTLibError as e:
        print(f"Error loading destination font: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading destination font: {e}")
        sys.exit(1)
    
    try:
        if args.verbose:
            print(f"Loading source: {args.src_font}")
        src_font = TTFont(args.src_font)
    except TTLibError as e:
        print(f"Error loading source font: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading source font: {e}")
        sys.exit(1)
    
    # Check format compatibility
    dest_is_cff = 'CFF ' in dest_font or 'CFF2' in dest_font
    dest_is_ttf = 'glyf' in dest_font
    src_is_cff = 'CFF ' in src_font or 'CFF2' in src_font
    src_is_ttf = 'glyf' in src_font
    
    if args.verbose:
        print(f"Destination: {'CFF' if dest_is_cff else 'TrueType' if dest_is_ttf else 'Unknown'}")
        print(f"Source:      {'CFF' if src_is_cff else 'TrueType' if src_is_ttf else 'Unknown'}")
    
    if dest_is_cff and not src_is_cff:
        print("Warning: Destination is CFF but source is TrueType. Outline copy may fail.")
    elif dest_is_ttf and not src_is_ttf:
        print("Warning: Destination is TrueType but source is CFF. Outline copy may fail.")
    
    # Perform replacements
    if args.verbose:
        print(f"\nProcessing {len(mapping)} glyph replacement(s)...")
    
    stats = replace_glyphs(
        dest_font, src_font, mapping,
        copy_kern=not args.no_kern,
        verbose=args.verbose
    )
    
    # Save
    try:
        dest_font.save(args.output)
        if args.verbose:
            print(f"\nSaved to: {args.output}")
    except Exception as e:
        print(f"Error saving font: {e}")
        sys.exit(1)
    
    # Summary
    print(f"\nDone: {stats['success']}/{stats['total']} succeeded")
    if stats['failed']:
        print(f"  Failed: {stats['failed']} (src missing: {stats['missing_src']}, dest missing: {stats['missing_dest']})")
    if stats['kern_pairs']:
        print(f"  Kerning pairs copied: {stats['kern_pairs']}")
    
    if stats['failed'] > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()