#!/usr/bin/env python3
"""
OTF/TTF Font Optimization Script

This script optimizes fonts by:
- Autohinting TrueType-based fonts with ttfautohint
- Autohinting CFF-based OTF fonts with psautohint
- Properly identifying CFF vs TrueType outlines
- Maintaining OTF output format

Requirements:
- ttfautohint (pip install ttfautohint)
- psautohint (pip install psautohint)
- fonttools (pip install fonttools)
"""

import os
import sys
import argparse
import subprocess
import logging
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttFont import TTLibError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def check_dependencies() -> bool:
    """Check if required tools are installed"""
    tools = [
        ('ttfautohint', 'pip install ttfautohint'),
        ('psautohint', 'pip install psautohint')
    ]
    
    all_found = True
    
    for tool, install_cmd in tools:
        try:
            result = subprocess.run([tool, '--version'], 
                                  capture_output=True, text=True)
            if result.returncode != 0:
                raise FileNotFoundError(f"{tool} not found")
            logger.debug(f"Found {tool}: {result.stdout.strip()}")
        except FileNotFoundError:
            logger.error(f"Missing dependency: {tool}")
            logger.info(f"Please install: {install_cmd}")
            all_found = False
        except Exception as e:
            logger.error(f"Error checking {tool} installation: {e}")
            all_found = False
    
    return all_found

def analyze_font_type(font_path: str) -> dict:
    """
    Analyze font to determine its outline format
    
    Returns:
        Dictionary with keys:
        - is_truetype: True if font has TrueType outlines
        - is_cff: True if font has CFF outlines
        - is_otf: True if file is OTF format
        - is_ttf: True if file is TTF format
    """
    try:
        font = TTFont(font_path)
        
        # Determine file extension
        file_ext = os.path.splitext(font_path)[1].lower()
        
        # Check for CFF table (CFF-based fonts)
        has_cff = 'CFF ' in font or 'CFF2' in font
        
        # Check for glyf table (TrueType outlines)
        has_glyf = 'glyf' in font
        
        font.close()
        
        return {
            'is_truetype': has_glyf and not has_cff,
            'is_cff': has_cff,
            'is_otf': file_ext == '.otf',
            'is_ttf': file_ext == '.ttf'
        }
        
    except TTLibError as e:
        logger.error(f"Invalid font file {font_path}: {e}")
        return {
            'is_truetype': False,
            'is_cff': False,
            'is_otf': False,
            'is_ttf': False
        }
    except Exception as e:
        logger.error(f"Error analyzing font {font_path}: {e}")
        return {
            'is_truetype': False,
            'is_cff': False,
            'is_otf': False,
            'is_ttf': False
        }

def optimize_truetype_font(input_path: str, output_path: str, options: dict) -> bool:
    """Optimize TrueType-based font with ttfautohint"""
    try:
        # Build ttfautohint command
        cmd = ['ttfautohint']
        
        # Add options based on user preferences
        if options.get('hinting_strength'):
            cmd.extend(['--hinting-limit', str(options['hinting_strength'])])
        
        if options.get('no_combining_chars'):
            cmd.append('--no-combining-chars')
            
        if options.get('detailed_info'):
            cmd.append('--detailed-info')
            
        if options.get('fallback_stem_width'):
            cmd.extend(['--fallback-stem-width', str(options['fallback_stem_width'])])
        
        # Default options for better rendering
        cmd.extend([
            '--default-script=latn',   # Default script
            '--fallback-script=none',  # No fallback script
            '--symbol',                # Process symbol area
            '--fallback-scaling',      # Use fallback scaling
        ])
        
        # Input and output files
        cmd.extend([input_path, output_path])
        
        logger.debug(f"Running ttfautohint: {' '.join(cmd)}")
        
        # Run ttfautohint
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode != 0:
            logger.error(f"ttfautohint failed for {input_path}: {result.stderr}")
            return False
            
        return True
        
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while optimizing {input_path}")
        return False
    except Exception as e:
        logger.error(f"Error optimizing {input_path}: {str(e)}")
        return False


def optimize_cff_font(input_path: str, output_path: str, options: dict) -> bool:
    """Optimize CFF-based font with psautohint"""
    try:
        # Build psautohint command
        cmd = ['psautohint']
        
        # Add output file specification
        cmd.extend(['-o', output_path])
        
        # Add optimization options
        if options.get('allow_changes'):
            cmd.append('--allow-changes')
            
        if options.get('no_flex'):
            cmd.append('--no-flex')
            
        if options.get('no_hint_sub'):
            cmd.append('--no-hint-sub')
        
        # Add option to allow fonts without zones/stems
        cmd.append('--no-zones-stems')
        
        # Add verbose flag if requested
        if options.get('verbose'):
            cmd.append('-v')
        
        # Input font
        cmd.append(input_path)
        
        logger.debug(f"Running psautohint: {' '.join(cmd)}")
        
        # Run psautohint
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode != 0:
            logger.error(f"psautohint failed for {input_path}: {result.stderr}")
            return False
            
        return True
        
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while optimizing {input_path}")
        return False
    except Exception as e:
        logger.error(f"Error optimizing {input_path}: {str(e)}")
        return False

def optimize_font(input_path: str, output_path: str, options: dict) -> bool:
    """
    Optimize a single font file
    
    Args:
        input_path: Path to input font file
        output_path: Path to output optimized font file
        options: Dictionary of optimization options
    
    Returns:
        True if successful, False otherwise
    """
    try:
        font_name = os.path.basename(input_path)
        logger.info(f"Analyzing {font_name}...")
        
        # Determine font type
        font_info = analyze_font_type(input_path)
        
        if not font_info['is_truetype'] and not font_info['is_cff']:
            logger.error(f"Unsupported font format for {font_name}")
            return False
        
        if font_info['is_truetype']:
            logger.info(f"Optimizing {font_name} with TrueType outlines using ttfautohint...")
            
            # Optimize TrueType fonts
            if optimize_truetype_font(input_path, output_path, options):
                # Log optimization results
                input_size = os.path.getsize(input_path)
                output_size = os.path.getsize(output_path)
                
                if input_size > 0:
                    reduction = ((input_size - output_size) / input_size) * 100
                    logger.info(f"✓ Optimized {font_name} "
                               f"({input_size:,} → {output_size:,} bytes, "
                               f"{reduction:.1f}% size change)")
                else:
                    logger.info(f"✓ Optimized {font_name}")
                return True
            else:
                return False
                
        elif font_info['is_cff']:
            logger.info(f"Optimizing {font_name} with CFF outlines using psautohint...")
            
            # Optimize CFF fonts
            if optimize_cff_font(input_path, output_path, options):
                # Log optimization results
                input_size = os.path.getsize(input_path)
                output_size = os.path.getsize(output_path)
                
                if input_size > 0:
                    reduction = ((input_size - output_size) / input_size) * 100
                    logger.info(f"✓ Optimized {font_name} "
                               f"({input_size:,} → {output_size:,} bytes, "
                               f"{reduction:.1f}% size change)")
                else:
                    logger.info(f"✓ Optimized {font_name}")
                return True
            else:
                return False
        
    except Exception as e:
        logger.error(f"Error processing {input_path}: {str(e)}")
        return False

def process_directory(input_dir: str, output_dir: str, options: dict) -> int:
    """
    Process all font files in a directory
    
    Args:
        input_dir: Input directory path
        output_dir: Output directory path
        options: Optimization options
    
    Returns:
        Number of successfully processed files
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    
    # Validate input directory
    if not input_path.exists():
        logger.error(f"Input directory does not exist: {input_dir}")
        return 0
    
    if not input_path.is_dir():
        logger.error(f"Input path is not a directory: {input_dir}")
        return 0
    
    # Create output directory if it doesn't exist
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Find all font files (OTF and TTF)
    font_extensions = ['*.otf', '*.ttf']
    font_files = []
    for ext in font_extensions:
        font_files.extend(input_path.glob(ext))
    
    if not font_files:
        logger.warning(f"No font files found in {input_dir}")
        return 0
    
    logger.info(f"Found {len(font_files)} font files to process")
    
    success_count = 0
    
    # Process each file
    for font_file in font_files:
        output_file = output_path / font_file.name
        
        if optimize_font(str(font_file), str(output_file), options):
            success_count += 1
    
    return success_count

def main():
    parser = argparse.ArgumentParser(
        description="Optimize font hinting and rendering for both TrueType and CFF-based fonts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input_fonts/ output_fonts/
  %(prog)s --strength 200 input_fonts/ output_fonts/
  %(prog)s --no-combining --detailed input_fonts/ output_fonts/
  %(prog)s --allow-changes --no-flex input_fonts/ output_fonts/
        """
    )
    
    parser.add_argument(
        "input_dir",
        help="Input directory containing font files (.otf, .ttf)"
    )
    
    parser.add_argument(
        "output_dir",
        help="Output directory for optimized font files"
    )
    
    # ttfautohint options (for TrueType fonts)
    parser.add_argument(
        "--strength",
        type=int,
        default=100,
        dest="hinting_strength",
        help="Hinting strength limit for TrueType fonts (default: 100)"
    )
    
    parser.add_argument(
        "--no-combining",
        action="store_true",
        dest="no_combining_chars",
        help="Don't set fallbacks for combining characters (TrueType fonts)"
    )
    
    parser.add_argument(
        "--detailed",
        action="store_true",
        dest="detailed_info",
        help="Add detailed TTF instructions information (TrueType fonts)"
    )
    
    parser.add_argument(
        "--stem-width",
        type=int,
        dest="fallback_stem_width",
        help="Fallback stem width value (TrueType fonts)"
    )
    
    # psautohint options (for CFF fonts)
    parser.add_argument(
        "--allow-changes",
        action="store_true",
        dest="allow_changes",
        help="Allow changes to glyph outlines (reorder paths) for CFF fonts"
    )
    
    parser.add_argument(
        "--no-flex",
        action="store_true",
        dest="no_flex",
        help="Suppress generation of flex commands for CFF fonts"
    )
    
    parser.add_argument(
        "--no-hint-sub",
        action="store_true",
        dest="no_hint_sub",
        help="Suppress hint substitution for CFF fonts"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    # Set logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Prepare optimization options
    options = {
        'hinting_strength': args.hinting_strength,
        'no_combining_chars': args.no_combining_chars,
        'detailed_info': args.detailed_info,
        'fallback_stem_width': args.fallback_stem_width,
        'allow_changes': args.allow_changes,
        'no_flex': args.no_flex,
        'no_hint_sub': args.no_hint_sub,
        'verbose': args.verbose
    }
    
    logger.info("Starting font optimization...")
    logger.info(f"Input directory: {args.input_dir}")
    logger.info(f"Output directory: {args.output_dir}")
    
    # Process fonts
    success_count = process_directory(
        args.input_dir,
        args.output_dir,
        options
    )
    
    logger.info(f"Optimization complete! Successfully processed {success_count} fonts.")

if __name__ == "__main__":
    main()
