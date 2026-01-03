#!/usr/bin/env python3
"""Script to apply video edits based on JSON modifications file."""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple

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


def load_json_modifications(json_path: Path) -> List[dict]:
    """Load modifications from JSON file.
    
    Args:
        json_path: Path to JSON modifications file
        
    Returns:
        List of modification dictionaries
        
    Raises:
        FileNotFoundError: If JSON file doesn't exist
        ValueError: If JSON file is invalid
    """
    if not json_path.exists():
        raise FileNotFoundError(f"JSON modifications file not found: {json_path}")
    
    try:
        with open(json_path, 'r') as f:
            modifications = json.load(f)
        
        if not isinstance(modifications, list):
            raise ValueError(f"JSON file must contain a list of modifications, got {type(modifications)}")
        
        return modifications
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON file: {e}")


def extract_removed_segments(modifications: List[dict]) -> List[Tuple[float, float]]:
    """Extract REMOVED segments from modifications list.
    
    Args:
        modifications: List of modification dictionaries
        
    Returns:
        List of (start_time, end_time) tuples for REMOVED entries
    """
    removed_segments = []
    
    for mod in modifications:
        if mod.get('modification') == 'REMOVED':
            start_time = float(mod.get('start_time', 0.0))
            end_time = float(mod.get('end_time', 0.0))
            removed_segments.append((start_time, end_time))
    
    return removed_segments


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Apply video edits based on JSON modifications file',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        'input_file',
        type=str,
        help='Input video file (MKV format recommended)'
    )
    
    parser.add_argument(
        '--json-input',
        type=str,
        default=None,
        help='JSON modifications file (default: {input}_modifications.json)'
    )
    
    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='Output video filename (default: {input}_processed.mkv)'
    )
    
    parser.add_argument(
        '--chunk-size',
        type=float,
        default=5.0,
        help='Size of chunks in seconds for extracting long segments (default: 5.0)'
    )
    
    parser.add_argument(
        '--ui',
        action='store_true',
        default=False,
        help='Launch GUI for video review and editing (default: False)'
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
    
    # Determine JSON input path
    if args.json_input:
        json_path = Path(args.json_input)
    else:
        json_path = input_path.parent / f"{input_path.stem}_modifications.json"
    
    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.parent / f"{input_path.stem}_processed{input_path.suffix}"
    
    # Ensure output doesn't overwrite input
    if output_path.resolve() == input_path.resolve():
        print("Error: Output file cannot be the same as input file", file=sys.stderr)
        sys.exit(1)
    
    # If UI flag is set, launch GUI instead of CLI processing
    if args.ui:
        try:
            # Check if video has multiple audio tracks and mix them if needed
            import ffmpeg
            try:
                probe = ffmpeg.probe(str(input_path))
                audio_streams = [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']
                
                if len(audio_streams) > 1:
                    print(f"Detected {len(audio_streams)} audio tracks. Mixing audio tracks...")
                    print("This may take a moment for large videos...")
                    
                    import tempfile
                    temp_file = tempfile.NamedTemporaryFile(suffix='.mkv', delete=False)
                    temp_path = Path(temp_file.name)
                    temp_file.close()
                    
                    # Mix all audio tracks using FFmpeg
                    stream = ffmpeg.input(str(input_path))
                    audio_inputs = [stream[f'a:{i}'] for i in range(len(audio_streams))]
                    mixed_audio = ffmpeg.filter(audio_inputs, 'amix', inputs=len(audio_inputs))
                    
                    output = ffmpeg.output(
                        stream.video,
                        mixed_audio,
                        str(temp_path),
                        vcodec='copy',
                        acodec='aac',
                        **{'y': None}
                    )
                    
                    ffmpeg.run(output, quiet=False, overwrite_output=True)  # Show progress
                    
                    print(f"Audio mixing complete. Using temporary file: {temp_path}")
                    # Use the mixed file for the UI
                    input_path = temp_path
            except Exception as e:
                print(f"Warning: Could not mix audio tracks: {e}. Using original file.", file=sys.stderr)
            
            from PySide6.QtWidgets import QApplication
            from video_review_ui import VideoReviewWindow
            
            app = QApplication(sys.argv)
            window = VideoReviewWindow(input_path, json_path)
            window.show()
            # Load video after window is shown so VLC can embed properly
            window._load_video()
            sys.exit(app.exec())
        except ImportError as e:
            print(f"Error: Required packages not installed. Install with: pip install -r requirements.txt", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error launching UI: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            sys.exit(1)
    
    print(f"Input video: {input_path}")
    print(f"JSON modifications: {json_path}")
    print(f"Output video: {output_path}")
    print()
    
    try:
        # Load JSON modifications
        print("Loading JSON modifications file...")
        modifications = load_json_modifications(json_path)
        print(f"Loaded {len(modifications)} modification(s)")
        
        # Extract REMOVED segments
        removed_segments = extract_removed_segments(modifications)
        
        if not removed_segments:
            print("\nWarning: No REMOVED segments found in JSON file.")
            print("Video will be copied without modification.")
            processor = VideoProcessor()
            processor._copy_video(input_path, output_path)
            print(f"\nDone! Output saved to: {output_path}")
            return
        
        print(f"Found {len(removed_segments)} segment(s) to remove:")
        total_removed = 0.0
        for start, end in removed_segments:
            duration = end - start
            total_removed += duration
            print(f"  {start:.2f}s - {end:.2f}s ({duration:.2f}s)")
        print(f"\nTotal duration to remove: {total_removed:.2f} seconds")
        
        # Process video
        print("\nApplying edits to video...")
        processor = VideoProcessor()
        processor.remove_segments(input_path, output_path, removed_segments, chunk_size=args.chunk_size)
        
        print(f"\nDone! Output saved to: {output_path}")
        
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

