#!/usr/bin/env python3
"""Preprocessing script for video analysis and audio mixing."""

import argparse
import sys
import os
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
        description='Preprocess video files: mix audio tracks, detect silence, and transcribe sentences',
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
    
    parser.add_argument(
        '--optimize-keyframes',
        action='store_true',
        default=False,
        help='Create keyframe-optimized version with GOP=5 for precise cuts with -c copy (default: False)'
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
    
    # Create output directory in project folder
    project_dir = Path(__file__).parent.absolute()
    output_dir = project_dir / f"{input_path.stem}_preprocessed"
    output_dir.mkdir(exist_ok=True)
    
    print(f"Output directory: {output_dir}")
    
    # Determine output paths (in output directory)
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = output_dir / output_path
    else:
        output_path = output_dir / f"{input_path.stem}_processed{input_path.suffix}"
    
    if args.json_output:
        json_path = Path(args.json_output)
        if not json_path.is_absolute():
            json_path = output_dir / json_path
    else:
        json_path = output_dir / f"{input_path.stem}_modifications.json"
    
    # Check for multiple audio tracks and mix them
    mixed_video_path = input_path
    import ffmpeg
    try:
        probe = ffmpeg.probe(str(input_path))
        audio_streams = [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']
        
        if len(audio_streams) > 1:
            print(f"\n{'='*60}")
            print("AUDIO MIXING")
            print(f"{'='*60}")
            print(f"Detected {len(audio_streams)} audio tracks. Mixing audio tracks...")
            print("This may take a moment for large videos...")
            
            mixed_video_path = output_dir / f"{input_path.stem}_mixed_audio{input_path.suffix}"
            
            # Mix all audio tracks using FFmpeg
            stream = ffmpeg.input(str(input_path))
            audio_inputs = [stream[f'a:{i}'] for i in range(len(audio_streams))]
            mixed_audio = ffmpeg.filter(audio_inputs, 'amix', inputs=len(audio_inputs))
            
            output = ffmpeg.output(
                stream.video,
                mixed_audio,
                str(mixed_video_path),
                vcodec='copy',
                acodec='aac',
                **{'y': None}
            )
            
            ffmpeg.run(output, quiet=False, overwrite_output=True)  # Show progress
            
            print(f"Audio mixing complete. Mixed video saved to: {mixed_video_path}")
            print()
        else:
            print(f"Video has {len(audio_streams)} audio track(s). No mixing needed.")
            print()
    except Exception as e:
        print(f"Warning: Could not check/mix audio tracks: {e}. Using original file.", file=sys.stderr)
        print()
    
    # Keyframe optimization (if enabled)
    optimized_video_path = None
    if args.optimize_keyframes:
        print(f"\n{'='*60}")
        print("KEYFRAME OPTIMIZATION")
        print(f"{'='*60}")
        print("Creating keyframe-optimized version with GOP=5...")
        print("This allows precise cuts with -c copy during rendering.")
        print("This may take a moment for large videos...")
        
        try:
            processor_optimize = VideoProcessor()
            optimized_video_path = output_dir / f"{input_path.stem}_keyframe_optimized{input_path.suffix}"
            
            def progress_callback(message: str):
                print(f"  {message}")
            
            processor_optimize.optimize_keyframes(input_path, optimized_video_path, progress=progress_callback)
            print(f"Keyframe optimization complete. Optimized video saved to: {optimized_video_path}")
            print()
        except Exception as e:
            print(f"Warning: Keyframe optimization failed: {e}. Continuing without optimization.", file=sys.stderr)
            print("You can still use re-encoding at render time for precise cuts.", file=sys.stderr)
            print()
    
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
            
            # Transcribe all audio tracks with word timestamps (use original video, not mixed)
            # Transcription needs to process each track individually to identify which track each sentence comes from
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
        
        # Detect silence segments (use mixed video if available)
        print("="*60)
        print("SILENCE DETECTION")
        print("="*60)
        print("Extracting audio and detecting silence...")
        silence_segments = detector.detect_silence_segments(mixed_video_path)
        
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
                processor._copy_video(mixed_video_path, output_path)
                print(f"Done! Output saved to: {output_path}")
            else:
                # Process video (use mixed video if available)
                print("\nRemoving segments from video...")
                processor.remove_segments(mixed_video_path, output_path, silence_segments)
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

