#!/usr/bin/env python3
"""autovidedit command-line interface.

    autovidedit VIDEO                preprocess (if needed), then open review UI
    autovidedit preprocess VIDEO     analyze video and write the edit plan
    autovidedit review VIDEO         open the browser review UI
    autovidedit render VIDEO         headless render from the saved edit plan
"""

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm"}


def output_dir_for(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_preprocessed"


def plan_path_for(input_path: Path) -> Path:
    return output_dir_for(input_path) / f"{input_path.stem}_modifications.json"


def preview_path_for(input_path: Path) -> Path:
    return output_dir_for(input_path) / f"{input_path.stem}_preview.mp4"


def keyframe_path_for(input_path: Path) -> Path:
    return output_dir_for(input_path) / f"{input_path.stem}_keyframe_optimized.mov"


def render_source_for(input_path: Path) -> Path:
    """Prefer the keyframe-optimized file (precise stream-copy cuts) if present."""
    optimized = keyframe_path_for(input_path)
    return optimized if optimized.exists() else input_path


def validate_input(input_path: Path):
    if not input_path.is_file():
        sys.exit(f"Error: input file not found: {input_path}")
    if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
        print(
            f"Warning: '{input_path.suffix}' may not be a video format", file=sys.stderr
        )


def cmd_preprocess(args) -> Path:
    from .core import ffprobe
    from .core.edit_plan import EditPlan
    from .core.mixing import make_preview_proxy
    from .core.render import VideoRenderer
    from .core.silence import SilenceDetector
    from .core.transcribe import Transcriber

    input_path = Path(args.input_file).resolve()
    validate_input(input_path)
    ffprobe.check_ffmpeg_installed()

    out_dir = output_dir_for(input_path)
    out_dir.mkdir(exist_ok=True)
    plan_path = plan_path_for(input_path)

    if plan_path.exists() and not args.force:
        sys.exit(
            f"Error: {plan_path} already exists. Use --force to re-analyze "
            "(this discards existing review decisions)."
        )

    duration = ffprobe.get_duration(input_path)
    plan = EditPlan()

    if not args.no_sentences:
        print("=" * 60 + "\nSENTENCE DETECTION\n" + "=" * 60)
        transcriber = Transcriber(model_size=args.whisper_model)
        sentences = transcriber.transcribe_all_tracks(input_path)
        for s in sentences:
            plan.add_sentence(s["start"], s["end"], s["words"], s["audio_track"])
        print(f"Identified {len(sentences)} sentence(s)")

    print("=" * 60 + "\nSILENCE DETECTION\n" + "=" * 60)
    detector = SilenceDetector(
        silence_threshold=args.silence_threshold,
        min_silence_duration=args.min_silence_duration,
        padding=args.padding,
    )
    silences = detector.detect(input_path)
    for start, end in silences:
        plan.add_silence(start, end)
    total = sum(end - start for start, end in silences)
    print(f"Found {len(silences)} silence segment(s), {total:.1f}s of dead air")

    plan.ensure_gaps(duration)
    plan.save(plan_path)
    print(f"\nEdit plan saved to: {plan_path}")

    print("\nGenerating browser preview...")
    make_preview_proxy(input_path, preview_path_for(input_path))

    if args.optimize_keyframes:
        print("\nOptimizing keyframes (GOP=5) for precise stream-copy cuts...")
        VideoRenderer().optimize_keyframes(
            input_path, keyframe_path_for(input_path), progress=print
        )

    print("\nPreprocessing complete. Review with:")
    print(f"  autovidedit review {input_path.name}")
    return plan_path


def cmd_review(args):
    import uvicorn

    from .core.mixing import make_preview_proxy
    from .server.app import create_app

    input_path = Path(args.input_file).resolve()
    validate_input(input_path)

    plan_path = plan_path_for(input_path)
    if not plan_path.exists():
        sys.exit(
            f"Error: no edit plan found at {plan_path}.\n"
            f"Run first: autovidedit preprocess {input_path.name}"
        )

    preview_path = preview_path_for(input_path)
    if not preview_path.exists():
        print("No preview file found; generating one (this may take a moment)...")
        make_preview_proxy(input_path, preview_path)

    app = create_app(
        video_name=input_path.name,
        preview_path=preview_path,
        render_source=render_source_for(input_path),
        plan_path=plan_path,
        default_output=output_dir_for(input_path) / f"{input_path.stem}_processed.mov",
    )

    url = f"http://127.0.0.1:{args.port}"
    print(f"Review UI: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def cmd_render(args):
    from .core import ffprobe
    from .core.edit_plan import EditPlan
    from .core.render import render_edit_plan

    input_path = Path(args.input_file).resolve()
    validate_input(input_path)

    plan_path = plan_path_for(input_path)
    if not plan_path.exists():
        sys.exit(f"Error: no edit plan found at {plan_path}")

    output_path = (
        Path(args.output)
        if args.output
        else output_dir_for(input_path) / f"{input_path.stem}_processed.mov"
    )
    if output_path.resolve() == input_path.resolve():
        sys.exit("Error: output file cannot be the same as the input file")

    source = render_source_for(input_path)
    plan = EditPlan.load(plan_path)
    duration = ffprobe.get_duration(source)
    segments = plan.segments_to_remove(duration)
    total = sum(end - start for start, end in segments)
    print(f"Removing {len(segments)} segment(s), {total:.1f}s total, from {source.name}")

    render_edit_plan(
        source, output_path, segments,
        progress=lambda pct, msg: print(f"  [{pct:3d}%] {msg}"),
        re_encode_video=args.re_encode,
    )
    print(f"Done! Output saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        prog="autovidedit",
        description="Remove dead air from videos with a human review step",
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="{preprocess,review,render}"
    )

    def add_common(p):
        p.add_argument("input_file", help="Input video file")

    p_pre = subparsers.add_parser("preprocess", help="Analyze video and write edit plan")
    add_common(p_pre)
    p_pre.add_argument("--silence-threshold", type=float, default=-40.0,
                       help="Audio level threshold in dB (default: -40)")
    p_pre.add_argument("--min-silence-duration", type=float, default=0.5,
                       help="Minimum silence duration to remove in seconds (default: 0.5)")
    p_pre.add_argument("--padding", type=float, default=0.1,
                       help="Buffer kept around speech in seconds (default: 0.1)")
    p_pre.add_argument("--no-sentences", action="store_true",
                       help="Skip transcription; silence-only edit plan")
    p_pre.add_argument("--whisper-model", default="medium",
                       help="faster-whisper model size (default: medium)")
    p_pre.add_argument("--optimize-keyframes", action="store_true",
                       help="Create GOP=5 version for precise stream-copy cuts")
    p_pre.add_argument("--force", action="store_true",
                       help="Overwrite an existing edit plan")

    p_rev = subparsers.add_parser("review", help="Open the browser review UI")
    add_common(p_rev)
    p_rev.add_argument("--port", type=int, default=8765)
    p_rev.add_argument("--no-browser", action="store_true",
                       help="Don't open the browser automatically")

    p_ren = subparsers.add_parser("render", help="Headless render from the edit plan")
    add_common(p_ren)
    p_ren.add_argument("--output", help="Output video path (default: *_processed.mov)")
    p_ren.add_argument("--re-encode", action="store_true",
                       help="Re-encode video for frame-precise cuts (slower)")

    # Default command: VIDEO -> preprocess if needed, then review
    p_auto = subparsers.add_parser("auto")
    add_common(p_auto)

    argv = sys.argv[1:]
    known_commands = {"preprocess", "review", "render", "auto", "-h", "--help"}
    if argv and argv[0] not in known_commands:
        argv = ["auto"] + argv
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "preprocess":
        cmd_preprocess(args)
    elif args.command == "review":
        cmd_review(args)
    elif args.command == "render":
        cmd_render(args)
    elif args.command == "auto":
        input_path = Path(args.input_file).resolve()
        validate_input(input_path)
        if not plan_path_for(input_path).exists():
            pre_args = argparse.Namespace(
                input_file=args.input_file, silence_threshold=-40.0,
                min_silence_duration=0.5, padding=0.1, no_sentences=False,
                whisper_model="medium", optimize_keyframes=False, force=False,
            )
            cmd_preprocess(pre_args)
        rev_args = argparse.Namespace(
            input_file=args.input_file, port=8765, no_browser=False
        )
        cmd_review(rev_args)


if __name__ == "__main__":
    main()
