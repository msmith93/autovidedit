"""Library entry points for the whole workflow.

    analyze(project, options)   -> EditPlan   (transcribe + detect silence)
    preprocess(project, ...)    -> EditPlan   (analyze, save plan, build preview)
    render(project, plan, ...)  -> Path       (cut and encode)

The CLI and the review server are thin wrappers around these functions, so an
agent or a script can drive any stage directly from Python.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import ffprobe
from .edit_plan import EditPlan, PlanOptions
from .project import Project

LogFn = Callable[[str], None]


@dataclass
class AnalyzeOptions:
    silence_threshold: float = -40.0
    min_silence_duration: float = 0.5
    padding: float = 0.1
    transcribe: bool = True
    whisper_model: str = "medium"
    max_pause: float = 0.5


def analyze(project: Project, options: AnalyzeOptions = AnalyzeOptions(),
            log: LogFn = print) -> EditPlan:
    """Build a fresh edit plan for the project's video. Does not save it."""
    from .silence import SilenceDetector

    duration = ffprobe.get_duration(project.video)
    plan = EditPlan(
        video=project.video.name,
        duration=round(duration, 3),
        options=PlanOptions(max_pause=options.max_pause),
    )

    if options.transcribe:
        from .transcribe import Transcriber

        log("== Transcription ==")
        sentences = Transcriber(model_size=options.whisper_model).transcribe_all_tracks(
            project.video, log=log
        )
        for s in sentences:
            plan.add_sentence(s["start"], s["end"], s["text"], s["audio_track"], s["words"])
        log(f"Identified {len(sentences)} sentence(s)")

    log("== Silence detection ==")
    detector = SilenceDetector(
        silence_threshold=options.silence_threshold,
        min_silence_duration=options.min_silence_duration,
        padding=options.padding,
    )
    silences = detector.detect(project.video, log=log)
    for start, end in silences:
        plan.add_silence(start, end)
    log(f"Found {len(silences)} silence span(s), "
        f"{sum(e - s for s, e in silences):.1f}s where every track is quiet")

    plan.ensure_gaps(duration)
    flagged = plan.flag_hallucinations()
    if flagged:
        log(f"Marked {len(flagged)} likely-hallucinated sentence(s) for removal")
    return plan


def preprocess(project: Project, options: AnalyzeOptions = AnalyzeOptions(),
               force: bool = False, log: LogFn = print) -> EditPlan:
    """Analyze, save the plan, and build the browser preview proxy."""
    project.ensure_out_dir()
    if project.plan_path.exists() and not force:
        raise FileExistsError(
            f"{project.plan_path} already exists. Use --force to re-analyze "
            "(this discards existing review decisions)."
        )
    plan = analyze(project, options, log)
    plan.save(project.plan_path)
    log(f"Edit plan saved to: {project.plan_path}")
    ensure_preview(project, log)
    return plan


def ensure_preview(project: Project, log: LogFn = print) -> Path:
    from .mixing import make_preview_proxy

    if not project.preview_path.exists():
        log("Generating browser preview...")
        project.ensure_out_dir()
        make_preview_proxy(project.video, project.preview_path)
        project.waveform_path.unlink(missing_ok=True)
    return project.preview_path


def load_plan(project: Project) -> EditPlan:
    """Load the project's plan, filling in fields old plans lack."""
    if not project.plan_path.exists():
        raise FileNotFoundError(
            f"No edit plan at {project.plan_path}. "
            f"Run first: autovidedit preprocess {project.video.name}"
        )
    plan = EditPlan.load(project.plan_path)
    if not plan.video:
        plan.video = project.video.name
    if not plan.duration:
        plan.duration = round(ffprobe.get_duration(project.video), 3)
    plan.ensure_gaps(plan.duration)
    return plan


def render(project: Project, plan: EditPlan, output: Optional[Path] = None,
           progress: Optional[Callable[[int, str], None]] = None,
           cancel_event: "threading.Event | None" = None) -> Path:
    """Render the plan's cuts to `output` (default: project.output_path)."""
    from .render import render_video

    output = Path(output) if output else project.output_path
    if output.resolve() == project.video.resolve():
        raise ValueError("Output file cannot be the same as the input file")
    segments = plan.segments_to_remove(ffprobe.get_duration(project.video))
    return render_video(project.video, output, segments,
                        progress=progress, cancel_event=cancel_event)
