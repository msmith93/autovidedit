#!/usr/bin/env python3
"""Analysis script for detecting silence and categorizing video content."""

import argparse
import sys
from pathlib import Path

from silence_detector import SilenceDetector
from json_logger import JSONLogger
from transcriber import Transcriber
from categorizer import Categorizer
from video_processor import VideoProcessor


def validate_input_file(input_path: Path):
    """Validate that input file exists and is a valid video file.
    
    Args:
        input_path: Path to input file
        
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a video file
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    if not input_path.is_file():
        raise ValueError(f"Input path is not a file: {input_path}")
    
    # Check file extension
    if input_path.suffix.lower() not in ['.mkv', '.mp4', '.avi', '.mov', '.webm']:
        print(f"Warning: File extension '{input_path.suffix}' may not be a video format", file=sys.stderr)


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Analyze video files to detect silence and optionally categorize content',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        'input_file',
        type=str,
        help='Input video file (MKV format recommended)'
    )
    
    parser.add_argument(
        '--silence-threshold',
        type=float,
        default=-40.0,
        help='Audio level threshold in dB (default: -40)'
    )
    
    parser.add_argument(
        '--min-silence-duration',
        type=float,
        default=0.5,
        help='Minimum silence duration to remove in seconds (default: 0.5)'
    )
    
    parser.add_argument(
        '--padding',
        type=float,
        default=0.1,
        help='Padding around silence segments in seconds (default: 0.1)'
    )
    
    parser.add_argument(
        '--json-output',
        type=str,
        default=None,
        help='JSON log filename (default: {input}_modifications.json)'
    )
    
    parser.add_argument(
        '--categorize',
        action='store_true',
        default=False,
        help='Enable AI categorization to identify topic sections (default: False)'
    )
    
    args = parser.parse_args()
    
    # Convert to Path objects
    input_path = Path(args.input_file)
    
    # Validate input file
    try:
        validate_input_file(input_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Determine JSON output path
    if args.json_output:
        json_path = Path(args.json_output)
    else:
        json_path = input_path.parent / f"{input_path.stem}_modifications.json"
    
    print(f"Analyzing: {input_path}")
    print(f"JSON output: {json_path}")
    print()
    
    try:
        # Initialize components
        print("Initializing components...")
        detector = SilenceDetector(
            silence_threshold=args.silence_threshold,
            min_silence_duration=args.min_silence_duration,
            padding=args.padding
        )
        logger = JSONLogger()
        
        # Initialize transcription and categorization if enabled
        transcriber = None
        categorizer = None
        if args.categorize:
            print("Initializing transcription and categorization...")
            transcriber = Transcriber()
            try:
                categorizer = Categorizer()
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        
        # Run categorization if enabled
        if args.categorize:
            print("\n" + "="*60)
            print("CATEGORIZATION")
            print("="*60)
            
            # Get video duration for categorization
            # We need to create a VideoProcessor just to get duration (it's a private method)
            temp_processor = VideoProcessor()
            video_duration = temp_processor._get_video_duration(input_path)
            
            # Transcribe video
            transcript_segments = transcriber.transcribe(input_path)
            
            if transcript_segments:
                # Categorize transcript
                print("\nAnalyzing transcript to identify topic sections...")
                categories = categorizer.categorize(transcript_segments, video_duration)
                
                # Add categories to logger
                print(f"\nIdentified {len(categories)} topic section(s):")
                for start, end, description in categories:
                    logger.add_categorization(start, end, description)
                    print(f"  {start:.2f}s - {end:.2f}s: {description}")
            else:
                print("Warning: No transcript segments found. Skipping categorization.")
            
            print()
        
        # Detect silence segments
        print("="*60)
        print("SILENCE DETECTION")
        print("="*60)
        print("Extracting audio and detecting silence...")
        silence_segments = detector.detect_silence_segments(input_path)
        
        # Merge overlapping segments
        silence_segments = detector.merge_overlapping_segments(silence_segments)
        
        print(f"Found {len(silence_segments)} silence segment(s) to remove")
        
        # Log all segments to JSON
        total_removed = 0.0
        for start, end in silence_segments:
            logger.add_removal(start, end, reason="dead air")
            total_removed += (end - start)
            print(f"  Removing: {start:.2f}s - {end:.2f}s ({end - start:.2f}s)")
        
        print(f"\nTotal dead air to remove: {total_removed:.2f} seconds")
        
        # Save JSON log (always generated)
        logger.save(json_path)
        print(f"\nJSON log saved to: {json_path}")
        print("\nAnalysis complete! Use apply_edits.py to apply these modifications to the video.")
        
    except KeyboardInterrupt:
        print("\n\nAnalysis interrupted by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

