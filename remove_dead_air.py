#!/usr/bin/env python3
"""Main script for removing dead air from MKV video files."""

import argparse
import sys
from pathlib import Path

from silence_detector import SilenceDetector
from video_processor import VideoProcessor
from json_logger import JSONLogger
from transcriber import Transcriber


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
        description='Remove dead air (silence) from video files',
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
        '--output',
        type=str,
        default=None,
        help='Output video filename (default: {input}_processed.mkv)'
    )
    
    parser.add_argument(
        '--json-output',
        type=str,
        default=None,
        help='JSON log filename (default: {input}_modifications.json)'
    )
    
    parser.add_argument(
        '--encode-video',
        action='store_true',
        default=False,
        help='Enable video encoding (default: False, only generates JSON file)'
    )
    
    parser.add_argument(
        '--sentences',
        action='store_true',
        default=False,
        help='Enable sentence detection from audio transcription (default: False)'
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
    
    # Determine output paths
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.parent / f"{input_path.stem}_processed{input_path.suffix}"
    
    if args.json_output:
        json_path = Path(args.json_output)
    else:
        json_path = input_path.parent / f"{input_path.stem}_modifications.json"
    
    print(f"Processing: {input_path}")
    if args.encode_video:
        print(f"Output video: {output_path}")
    print(f"JSON log: {json_path}")
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
        
        # Initialize video processor only if encoding is enabled
        processor = None
        if args.encode_video:
            processor = VideoProcessor()
        
        # Initialize transcription if enabled
        transcriber = None
        if args.sentences:
            print("Initializing transcription...")
            transcriber = Transcriber()
        
        # Run sentence detection if enabled
        if args.sentences:
            print("\n" + "="*60)
            print("SENTENCE DETECTION")
            print("="*60)
            
            # Transcribe all audio tracks with word timestamps
            sentences = transcriber.transcribe_all_tracks(input_path)
            
            if sentences:
                # Add sentences to logger
                print(f"\nIdentified {len(sentences)} sentence(s):")
                for sentence in sentences:
                    logger.add_sentence(
                        sentence['start'],
                        sentence['end'],
                        sentence['words'],
                        sentence['audio_track']
                    )
                    print(f"  Track {sentence['audio_track']}: {sentence['start']:.2f}s - {sentence['end']:.2f}s: {sentence['words'][:60]}...")
            else:
                print("Warning: No sentences found in audio tracks.")
            
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
        
        # Only process video if encoding is enabled
        if args.encode_video:
            # Ensure output doesn't overwrite input
            if output_path.resolve() == input_path.resolve():
                print("Error: Output file cannot be the same as input file", file=sys.stderr)
                sys.exit(1)
            
            if not silence_segments:
                print("\nNo silence detected. Video will be copied without modification.")
                processor._copy_video(input_path, output_path)
                print(f"Done! Output saved to: {output_path}")
            else:
                # Process video
                print("\nRemoving segments from video...")
                processor.remove_segments(input_path, output_path, silence_segments)
                print(f"\nDone! Output saved to: {output_path}")
        else:
            print("\nVideo encoding skipped (use --encode-video to enable)")
            print(f"Review the JSON log at: {json_path}")
        
    except KeyboardInterrupt:
        print("\n\nProcessing interrupted by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

