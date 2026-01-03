# Auto Video Edit - Dead Air Removal

A Python tool that automatically removes dead air (silence) from MKV video files, outputting a cleaned video and a JSON log of all modifications.

## Features

- Detects and removes silence/dead air segments from video files
- Preserves video/audio sync and quality
- Generates detailed JSON log of all removed segments
- Configurable silence detection thresholds

## Requirements

- Python 3.7+
- FFmpeg (must be installed separately and available in PATH)

### Installing FFmpeg

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

**Windows:**
Download from [FFmpeg website](https://ffmpeg.org/download.html) and add to PATH.

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

## Usage

Basic usage:
```bash
python remove_dead_air.py input.mkv
```

With custom thresholds:
```bash
python remove_dead_air.py input.mkv --silence-threshold -35 --min-silence-duration 1.0
```

### Command-line Options

- `--silence-threshold`: Audio level threshold in dB (default: -40)
- `--min-silence-duration`: Minimum silence duration to remove in seconds (default: 0.5)
- `--padding`: Padding around silence segments in seconds (default: 0.1)
- `--output`: Output filename (default: `{input}_processed.mkv`)
- `--json-output`: JSON log filename (default: `{input}_modifications.json`)

## Output

The script generates two files:

1. **Processed video**: `{input}_processed.mkv` - The video with dead air removed
2. **JSON log**: `{input}_modifications.json` - Detailed log of all removed segments

### JSON Output Format

```json
[
  {
    "start_time": 12.5,
    "end_time": 13.8,
    "modification": "REMOVED",
    "reason": "dead air",
    "duration": 1.3
  }
]
```

## Future Features

- Filler word removal
- Automatic zoom detection for coding walkthroughs
- Additional video enhancement features

