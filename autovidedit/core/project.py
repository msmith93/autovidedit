"""Every path derived from an input video lives here.

Outputs go to `<video dir>/<stem>_preprocessed/`, next to the input.
"""

from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm"}


@dataclass(frozen=True)
class Project:
    video: Path

    @classmethod
    def from_path(cls, path) -> "Project":
        return cls(Path(path).expanduser().resolve())

    @property
    def stem(self) -> str:
        return self.video.stem

    @property
    def out_dir(self) -> Path:
        return self.video.parent / f"{self.stem}_preprocessed"

    @property
    def plan_path(self) -> Path:
        return self.out_dir / f"{self.stem}_modifications.json"

    @property
    def preview_path(self) -> Path:
        return self.out_dir / f"{self.stem}_preview.mp4"

    @property
    def waveform_path(self) -> Path:
        return self.out_dir / f"{self.stem}_preview.waveform.json"

    @property
    def frames_dir(self) -> Path:
        return self.out_dir / "frames"

    @property
    def decisions_path(self) -> Path:
        return self.out_dir / f"{self.stem}_decisions.json"

    @property
    def output_path(self) -> Path:
        return self.out_dir / f"{self.stem}_edited.mp4"

    def ensure_out_dir(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir
