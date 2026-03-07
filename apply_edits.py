#!/usr/bin/env python3
"""Launch video editing UI for reviewing and editing video based on JSON modifications file."""

import argparse
import sys
from pathlib import Path


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
        description='Launch video editing UI for reviewing and editing video',
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
    
    args = parser.parse_args()
    
    # Convert to Path objects
    input_path = Path(args.input_file)
    
    # Validate input file
    try:
        validate_input_file(input_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Create output directory in project folder (same as preprocess.py)
    project_dir = Path(__file__).parent.absolute()
    output_dir = project_dir / f"{input_path.stem}_preprocessed"
    output_dir.mkdir(exist_ok=True)
    
    # Determine JSON input path
    if args.json_input:
        json_path = Path(args.json_input)
        if not json_path.is_absolute():
            # Try output directory first, then relative to input
            if (output_dir / json_path).exists():
                json_path = output_dir / json_path
            else:
                json_path = input_path.parent / json_path
    else:
        # Default: look in output directory first, then input directory
        default_json = f"{input_path.stem}_modifications.json"
        if (output_dir / default_json).exists():
            json_path = output_dir / default_json
        else:
            json_path = input_path.parent / default_json
    
    # Launch GUI
    try:
        # Store original video path before any mixing
        original_video_path = input_path
        
        # Check if keyframe-optimized version exists in output directory (from preprocess.py)
        optimized_video_path = output_dir / f"{input_path.stem}_keyframe_optimized.mov"
        if optimized_video_path.exists():
            print(f"Using keyframe-optimized video: {optimized_video_path}")
            # Use optimized version for rendering (preserves all audio tracks)
            original_video_path = optimized_video_path
        
        # Check if mixed audio file exists in output directory (from preprocess.py)
        mixed_video_path = output_dir / f"{input_path.stem}_mixed_audio.mov"
        
        if mixed_video_path.exists():
            print(f"Using pre-mixed audio file: {mixed_video_path}")
            mixed_video_path_for_ui = mixed_video_path
        else:
            # Fallback: Check if video has multiple audio tracks and mix them if needed
            import ffmpeg
            try:
                probe = ffmpeg.probe(str(input_path))
                audio_streams = [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']
                
                if len(audio_streams) > 1:
                    print(f"Detected {len(audio_streams)} audio tracks. Mixing audio tracks...")
                    print("This may take a moment for large videos...")
                    
                    mixed_video_path = output_dir / f"{input_path.stem}_mixed_audio.mov"
                    
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
                        audio_bitrate='192k',  # CBR AAC for Kdenlive compatibility
                        **{'y': None}
                    )
                    
                    ffmpeg.run(output, quiet=False, overwrite_output=True)  # Show progress
                    
                    print(f"Audio mixing complete. Mixed video saved to: {mixed_video_path}")
                    mixed_video_path_for_ui = mixed_video_path
                else:
                    # Only one track, no mixing needed
                    mixed_video_path_for_ui = original_video_path
            except Exception as e:
                print(f"Warning: Could not mix audio tracks: {e}. Using original file.", file=sys.stderr)
                mixed_video_path_for_ui = original_video_path
        
        from PySide6.QtWidgets import QApplication
        from video_review_ui import VideoReviewWindow
        
        app = QApplication(sys.argv)
        window = VideoReviewWindow(mixed_video_path_for_ui, json_path, original_video_path)
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


if __name__ == '__main__':
    main()

