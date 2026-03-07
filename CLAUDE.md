# autovidedit

Automatically removes dead air/silence from video files with a human review step.

## Workflow

```bash
# Step 1: Analyze and generate edit suggestions (JSON)
python preprocess.py <video.mkv> --padding=0.25 --sentences --optimize-keyframes

# Step 2: Review edits in UI and apply them
python apply_edits.py <video.mkv>
```

## Setup

```bash
source venv/bin/activate
pip install -r requirements.txt
```

FFmpeg must be installed system-wide (`sudo apt install ffmpeg`). NVENC GPU acceleration is used automatically if available.
