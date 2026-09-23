#!/usr/bin/env python3
"""autovidedit command-line interface.

    autovidedit VIDEO                     preprocess (if needed), then open review UI
    autovidedit preprocess VIDEO          analyze video and write the edit plan
    autovidedit review VIDEO              open the browser review UI
    autovidedit render VIDEO              render the edit plan to MP4
    autovidedit transcript VIDEO          print the plan as an id-tagged transcript
    autovidedit frames VIDEO              extract still frames for visual review
    autovidedit plan show|set|apply|options VIDEO ...
    autovidedit suggest VIDEO             ask Claude to propose cuts (needs [ai] extra)
"""

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

from .core.project import VIDEO_EXTENSIONS, Project

DEFAULT_PORT = 8791
COMMANDS = {"preprocess", "review", "render", "transcript", "frames", "plan",
            "suggest", "auto"}


def die(message: str):
    sys.exit(f"Error: {message}")


def project_from(path: str) -> Project:
    project = Project.from_path(path)
    if not project.video.is_file():
        die(f"input file not found: {project.video}")
    if project.video.suffix.lower() not in VIDEO_EXTENSIONS:
        print(f"Warning: '{project.video.suffix}' may not be a video format", file=sys.stderr)
    return project


def load_plan_or_die(project: Project):
    from .core.pipeline import load_plan

    try:
        return load_plan(project)
    except FileNotFoundError as e:
        die(str(e))


# ---------- commands ----------

def analyze_options(args):
    from .core.pipeline import AnalyzeOptions

    return AnalyzeOptions(
        silence_threshold=args.silence_threshold,
        min_silence_duration=args.min_silence_duration,
        padding=args.padding,
        transcribe=not args.no_sentences,
        whisper_model=args.whisper_model,
        max_pause=args.max_pause,
    )


def cmd_preprocess(args):
    from .core import ffprobe
    from .core.pipeline import preprocess

    project = project_from(args.input_file)
    ffprobe.check_ffmpeg_installed()
    try:
        preprocess(project, analyze_options(args), force=args.force)
    except FileExistsError as e:
        die(str(e))
    print(f"\nPreprocessing complete. Review with:\n  autovidedit review {project.video.name}")


def cmd_review(args):
    import uvicorn

    from .core.pipeline import ensure_preview
    from .server.app import create_app

    project = project_from(args.input_file)
    load_plan_or_die(project)
    ensure_preview(project)
    app = create_app(
        video_path=project.video,
        preview_path=project.preview_path,
        plan_path=project.plan_path,
        default_output=project.output_path,
    )
    url = f"http://127.0.0.1:{args.port}"
    print(f"Review UI: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def cmd_render(args):
    from .core import ffprobe
    from .core.pipeline import render
    from .core.segments import total_length

    project = project_from(args.input_file)
    plan = load_plan_or_die(project)
    segments = plan.segments_to_remove(ffprobe.get_duration(project.video))
    print(f"Removing {len(segments)} span(s), {total_length(segments):.1f}s total")

    last = {"pct": -10}

    def progress(pct, msg):
        if pct >= last["pct"] + 10 or pct == 100:
            print(f"  [{pct:3d}%] {msg}")
            last["pct"] = pct

    try:
        output = render(project, plan, Path(args.output) if args.output else None, progress)
    except ValueError as e:
        die(str(e))
    print(f"Done! Output saved to: {output}")


def cmd_transcript(args):
    from .core.transcript import format_transcript, transcript_json

    plan = load_plan_or_die(project_from(args.input_file))
    if args.json:
        print(json.dumps(transcript_json(plan, args.start, args.end, args.words), indent=1))
    else:
        print(format_transcript(plan, args.start, args.end))


def cmd_frames(args):
    from .core.frames import extract_frames, frame_at

    project = project_from(args.input_file)
    if args.at:
        for t in args.at:
            print(frame_at(project, t, args.width))
        return
    index = extract_frames(project, args.every, args.width, args.start or 0.0, args.end)
    print(f"{len(index)} frame(s) in {project.frames_dir} (index: frames.json)")


def cmd_plan(args):
    from .core.edit_plan import DecisionSet, removal_stats
    from .core.transcript import format_entry

    project = project_from(args.input_file)
    plan = load_plan_or_die(project)

    if args.plan_command == "show":
        removals = plan.segments_to_remove()
        stats = removal_stats(removals)
        summary = {
            "video": plan.video,
            "duration": plan.duration,
            "options": plan.options.model_dump(),
            "entries": {k: len(plan._of_kind(k)) for k in ("sentence", "gap", "silence", "cut")},
            "removed_entries": sum(1 for e in plan.entries if e.removed and e.kind != "silence"),
            "sources": {s: sum(1 for e in plan.entries if e.source == s)
                        for s in ("analyzer", "ai", "human")},
            "removals": stats,
            "output_duration": round(plan.duration - stats["seconds"], 3),
        }
        if args.json:
            summary["removal_spans"] = removals
        print(json.dumps(summary, indent=2))
        return

    if args.plan_command == "set":
        decision = "keep" if args.keep else "remove"
        missing = [i for i in args.ids if plan.get(i) is None]
        if missing:
            die(f"unknown entry id(s): {missing}")
        for entry_id in args.ids:
            plan.set_decision(entry_id, decision, source=args.source, rationale=args.rationale)
            print(format_entry(plan.get(entry_id)))
        plan.save(project.plan_path)
        return

    if args.plan_command == "apply":
        raw = sys.stdin.read() if args.decisions == "-" else Path(args.decisions).read_text()
        decision_set = DecisionSet.model_validate_json(raw)
        try:
            result = plan.apply_decisions(decision_set, source=args.source, force=args.force)
        except ValueError as e:
            die(str(e))
        plan.save(project.plan_path)
        print(json.dumps(result.model_dump(), indent=2))
        return

    if args.plan_command == "options":
        if args.max_pause is not None:
            plan.options.max_pause = args.max_pause
            plan.save(project.plan_path)
        print(json.dumps(plan.options.model_dump(), indent=2))


def cmd_suggest(args):
    try:
        from .core.suggest import SuggestOptions, suggest
    except ImportError as e:
        die(f"{e}. Install the AI extra: pip install -e .[ai]")

    project = project_from(args.input_file)
    plan = load_plan_or_die(project)
    options = SuggestOptions(
        model=args.model, window_minutes=args.window_minutes,
        frame_every=args.frame_every, effort=args.effort,
    )
    decision_set, report = suggest(project, plan, options)
    project.decisions_path.write_text(decision_set.model_dump_json(indent=2))
    print(report)
    print(f"Decisions written to {project.decisions_path}")
    if args.dry_run:
        print(f"Dry run: plan unchanged. Apply with:\n"
              f"  autovidedit plan apply {project.video.name} {project.decisions_path}")
        return
    result = plan.apply_decisions(decision_set, source="ai")
    plan.save(project.plan_path)
    print(json.dumps(result.model_dump(), indent=2))


def cmd_auto(args):
    from .core.pipeline import AnalyzeOptions, preprocess

    project = project_from(args.input_file)
    if not project.plan_path.exists():
        preprocess(project, AnalyzeOptions())
    args.port, args.no_browser = DEFAULT_PORT, False
    cmd_review(args)


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autovidedit",
        description="Cut dead air and unwanted takes from videos, with a review step",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def video_cmd(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("input_file", help="Input video file")
        return p

    p = video_cmd("preprocess", "Analyze video and write the edit plan")
    p.add_argument("--silence-threshold", type=float, default=-40.0,
                   help="Audio level counted as silence, dB (default: -40)")
    p.add_argument("--min-silence-duration", type=float, default=0.5,
                   help="Shortest silence detected, seconds (default: 0.5)")
    p.add_argument("--padding", type=float, default=0.1,
                   help="Trim this much off each end of detected silences (default: 0.1)")
    p.add_argument("--max-pause", type=float, default=0.5,
                   help="Longer pauses are shortened to this many seconds (default: 0.5)")
    p.add_argument("--no-sentences", action="store_true",
                   help="Skip transcription; silence-only edit plan")
    p.add_argument("--whisper-model", default="medium",
                   help="faster-whisper model size (default: medium)")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing edit plan")

    p = video_cmd("review", "Open the browser review UI")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true",
                   help="Don't open the browser automatically")

    p = video_cmd("render", "Render the edit plan to MP4")
    p.add_argument("--output", help="Output path (default: <stem>_edited.mp4)")

    p = video_cmd("transcript", "Print the plan as an id-tagged transcript")
    p.add_argument("--json", action="store_true", help="JSON entries instead of text")
    p.add_argument("--words", action="store_true", help="Include word timings (JSON only)")
    p.add_argument("--start", type=float, help="Only entries after this time (s)")
    p.add_argument("--end", type=float, help="Only entries before this time (s)")

    p = video_cmd("frames", "Extract still frames for visual review")
    p.add_argument("--every", type=float, default=20.0, help="Seconds between frames")
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--at", type=float, nargs="+", help="Extract only these timestamps")

    p_plan = sub.add_parser("plan", help="Inspect or edit the plan")
    plan_sub = p_plan.add_subparsers(dest="plan_command", metavar="ACTION", required=True)

    p = plan_sub.add_parser("show", help="Summarize the plan (JSON)")
    p.add_argument("input_file")
    p.add_argument("--json", action="store_true", help="Include every removal span")

    p = plan_sub.add_parser("set", help="Set keep/remove on entries by id")
    p.add_argument("input_file")
    p.add_argument("ids", nargs="+")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--keep", action="store_true")
    group.add_argument("--remove", action="store_true")
    p.add_argument("--rationale")
    p.add_argument("--source", choices=["human", "ai"], default="human")

    p = plan_sub.add_parser("apply", help="Merge a decisions JSON file ('-' for stdin)")
    p.add_argument("input_file")
    p.add_argument("decisions")
    p.add_argument("--source", choices=["ai", "human"], default="ai")
    p.add_argument("--force", action="store_true", help="Also override human decisions")

    p = plan_sub.add_parser("options", help="Show or change plan options")
    p.add_argument("input_file")
    p.add_argument("--max-pause", type=float)

    p = video_cmd("suggest", "Ask Claude to propose cuts (transcript + frames)")
    p.add_argument("--dry-run", action="store_true",
                   help="Write decisions JSON only; don't change the plan")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--window-minutes", type=float, default=10.0)
    p.add_argument("--frame-every", type=float, default=20.0)

    video_cmd("auto", "Preprocess if needed, then open the review UI")
    return parser


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["auto"] + argv
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "preprocess": cmd_preprocess, "review": cmd_review, "render": cmd_render,
        "transcript": cmd_transcript, "frames": cmd_frames, "plan": cmd_plan,
        "suggest": cmd_suggest, "auto": cmd_auto,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
