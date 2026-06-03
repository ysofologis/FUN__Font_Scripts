import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

def setup_logging(verbose=False):
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")

class AdobeAFDKOProcessor:
    """
    Processor that uses Adobe AFDKO tools for professional OTF processing.
    Provides industry-standard font optimization using makeotf and related tools.
    """
    
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.afdko_available = self._check_afdko_availability()
        
    def _check_afdko_availability(self) -> bool:
        """Check if essential AFDKO tools are available in system PATH."""
        required_tools = ['makeotf', 'tx']
        missing_tools = []
        
        for tool in required_tools:
            try:
                result = subprocess.run([tool, '-h'], 
                                      capture_output=True, 
                                      text=True,
                                      timeout=10)
                if result.returncode != 0:
                    missing_tools.append(tool)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                if self.verbose:
                    logging.debug(f"Tool check failed for {tool}: {e}")
                missing_tools.append(tool)
        
        if missing_tools:
            logging.error(f"Missing AFDKO tools: {', '.join(missing_tools)}. Please install Adobe AFDKO.")
            return False
            
        logging.info("All required AFDKO tools found")
        return True
    
    def process_font_with_makeotf(
        self, input_path: Path, output_path: Path
    ) -> Tuple[bool, str]:
        """
        Process font using makeotf with professional settings.
        
        Args:
            input_path (Path): Path to input font file
            output_path (Path): Path to output optimized OTF file
            
        Returns:
            tuple: (success: bool, message: str)
        """
        if not self.afdko_available:
            return False, "AFDKO tools not available"
            
        start_time = time.time()
        
        # Build makeotf command with professional options
        cmd = [
            'makeotf',
            '-f', str(input_path),           # Input font file
            '-o', str(output_path),          # Output OTF file
            '-r',                            # Release mode (no stubbing)
            '-gs',                           # Suppress glyph warnings
            '-shw',                          # Don't override OS/2 xHeight, capHeight
            '-omitMacNames'                  # Omit Macintosh name table entries
        ]
        
        # Add feature file if it exists in the same directory
        feature_file = input_path.parent / 'features.fea'
        if feature_file.exists():
            cmd.extend(['-ff', str(feature_file)])
            if self.verbose:
                logging.info(f"Using feature file: {feature_file}")
        
        if self.verbose:
            logging.info(f"Running command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, 
                                  capture_output=True, 
                                  text=True, 
                                  check=False,  # Don't raise exception on non-zero exit
                                  timeout=120) # 2 minute timeout
            
            elapsed = time.time() - start_time
            
            if self.verbose:
                logging.info(f"makeotf return code: {result.returncode}")
                if result.stdout:
                    logging.info(f"makeotf stdout:\n{result.stdout}")
                if result.stderr:
                    logging.info(f"makeotf stderr:\n{result.stderr}")
            
            # Check if output file was created
            if output_path.exists() and output_path.stat().st_size > 0:
                return True, f"Processed successfully in {elapsed:.1f}s"
            elif result.returncode != 0:
                error_detail = result.stderr.strip() if result.stderr else "No error output"
                return False, f"Failed after {elapsed:.1f}s (exit code {result.returncode}): {error_detail}"
            else:
                return False, f"Failed after {elapsed:.1f}s: No output file generated"
                
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - start_time
            return False, f"Timeout after {elapsed:.1f}s: Command took too long to complete"
        except Exception as e:
            elapsed = time.time() - start_time
            return False, f"Exception after {elapsed:.1f}s: {str(e)} ({type(e).__name__})"
    
    def convert_with_tx(
        self, input_path: Path, output_path: Path
    ) -> Tuple[bool, str]:
        """
        Convert font using tx tool for clean PostScript conversion.
        
        Args:
            input_path (Path): Path to input font file
            output_path (Path): Path to output converted font file
            
        Returns:
            tuple: (success: bool, message: str)
        """
        if not self.afdko_available:
            return False, "AFDKO tools not available"
            
        start_time = time.time()
        
        # Use tx for clean conversion to CFF
        cmd = [
            'tx',
            '-cff',      # Convert to CFF
            '+b',        # Preserve glyph order
            str(input_path),
            str(output_path)
        ]
        
        if self.verbose:
            logging.info(f"Running tx command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, 
                                  capture_output=True, 
                                  text=True, 
                                  check=False,  # Don't raise exception on non-zero exit
                                  timeout=60)   # 1 minute timeout
            
            elapsed = time.time() - start_time
            
            if self.verbose:
                logging.info(f"tx return code: {result.returncode}")
                if result.stdout:
                    logging.info(f"tx stdout:\n{result.stdout}")
                if result.stderr:
                    logging.info(f"tx stderr:\n{result.stderr}")
            
            # Check if output file was created
            if output_path.exists() and output_path.stat().st_size > 0:
                return True, f"Converted successfully in {elapsed:.1f}s"
            elif result.returncode != 0:
                error_detail = result.stderr.strip() if result.stderr else "No error output"
                return False, f"Failed after {elapsed:.1f}s (exit code {result.returncode}): {error_detail}"
            else:
                return False, f"Failed after {elapsed:.1f}s: No output file generated"
                
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - start_time
            return False, f"Timeout after {elapsed:.1f}s: Command took too long to complete"
        except Exception as e:
            elapsed = time.time() - start_time
            return False, f"Exception after {elapsed:.1f}s: {str(e)} ({type(e).__name__})"
    
    def process_directory(self, input_dir: Path, output_dir: Path) -> None:
        """
        Process all font files in a directory using AFDKO tools.
        
        Args:
            input_dir (Path): Input directory containing font files
            output_dir (Path): Output directory for processed files
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Find all supported font files
        font_extensions = ['*.otf', '*.ttf', '*.pfa', '*.pfb', '*.ufo']
        font_files = []
        for ext in font_extensions:
            font_files.extend(input_dir.glob(ext))
            font_files.extend(input_dir.glob(ext.upper()))
        
        if not font_files:
            logging.info(f"No font files found in {input_dir}")
            return
            
        logging.info(f"Found {len(font_files)} font files to process")
        
        successful = 0
        failed = 0
        
        for font_file in font_files:
            logging.info(f"Processing {font_file.name}...")
            output_file = output_dir / f"{font_file.stem}_afdko.otf"
            
            # Process directly with makeotf for OTF/TTF files
            if font_file.suffix.lower() in ['.otf', '.ttf']:
                success, message = self.process_font_with_makeotf(font_file, output_file)
            else:
                # For source formats, convert first then process
                logging.info(f"Converting {font_file.name} to CFF first...")
                temp_cff = output_file.with_suffix('.tmp.cff')
                convert_success, convert_message = self.convert_with_tx(font_file, temp_cff)
                
                if convert_success:
                    logging.info(f"Conversion successful, now processing with makeotf...")
                    success, message = self.process_font_with_makeotf(temp_cff, output_file)
                    # Clean up temp file
                    if temp_cff.exists():
                        temp_cff.unlink()
                else:
                    success, message = False, f"Conversion failed: {convert_message}"
            
            if success:
                logging.info(f"✓ {font_file.name}: {message}")
                successful += 1
            else:
                logging.error(f"✗ {font_file.name}: {message}")
                failed += 1
                
        logging.info(f"Processing complete: {successful} successful, {failed} failed")

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Process fonts using Adobe AFDKO for professional OTF generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input_fonts/ output_fonts/
  %(prog)s input_fonts/ output_fonts/ -v
  %(prog)s . ./output/ --verbose
        """
    )
    parser.add_argument("input_dir", help="Input directory containing font files")
    parser.add_argument("output_dir", help="Output directory for processed OTF files")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    input_path = Path(args.input_dir)
    if not input_path.exists():
        logging.error(f"Input directory does not exist: {input_path}")
        sys.exit(1)
    if not input_path.is_dir():
        logging.error(f"Input path is not a directory: {input_path}")
        sys.exit(1)
    
    logging.info("Initializing Adobe AFDKO Processor...")
    processor = AdobeAFDKOProcessor(verbose=args.verbose)
    
    if not processor.afdko_available:
        logging.error("AFDKO tools are not available. Please install Adobe AFDKO:")
        logging.error("  pip install afdko")
        sys.exit(1)
    
    processor.process_directory(input_path, Path(args.output_dir))

if __name__ == "__main__":
    main()
