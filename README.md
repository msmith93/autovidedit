# Auto Video Edit - Dead Air Removal

A Python tool that automatically detects and removes dead air (silence) from video files, with a review UI to approve edits before applying them.

## Requirements

- Python 3.7+
- FFmpeg (must be installed separately and available in PATH)

### Installing FFmpeg

**Ubuntu/Debian:**
```bash
sudo apt-get install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Step 1: Preprocess

Analyzes the video, detects silence, transcribes audio, and generates a JSON edit plan:

```bash
python preprocess.py input.mkv --padding=0.25 --sentences --optimize-keyframes
```

**Options:**
- `--silence-threshold`: Audio level threshold in dB (default: -40)
- `--min-silence-duration`: Minimum silence duration to remove in seconds (default: 0.5)
- `--padding`: Padding around silence segments in seconds (default: 0.1)
- `--sentences`: Group cuts by sentence boundaries using audio transcription
- `--optimize-keyframes`: Pre-process video with GOP=5 for precise lossless cuts
- `--json-output`: JSON log filename (default: `{input}_preprocessed/{input}_modifications.json`)

### Step 2: Review and Apply

Opens a UI to review the proposed edits, then renders the final video:

```bash
python apply_edits.py input.mkv
```

**Options:**
- `--json-input`: JSON modifications file (default: auto-detected from `{input}_preprocessed/`)

## Output

Both scripts write to a `{input}_preprocessed/` directory containing:
- `{input}_modifications.json` — edit plan (created by preprocess, read by apply_edits)
- Final rendered video

## Future Features

- Filler word removal
- Automatic zoom detection for coding walkthroughs
