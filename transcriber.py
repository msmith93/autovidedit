"""Transcription module for converting video audio to text using Whisper."""

from pathlib import Path
from typing import List, Dict, Any
import tempfile
import ffmpeg


class Transcriber:
    """Handles audio transcription using Whisper."""
    
    def __init__(self, model_size: str = "medium", device: str = None):
        """Initialize transcriber with Whisper model.
        
        Args:
            model_size: Whisper model size ("tiny", "base", "small", "medium", "large-v2")
            device: Device to use ("cuda", "cpu", or None for auto-detect)
        """
        self.model_size = model_size
        self.device = device
        self._model = None
    
    def _load_model(self):
        """Lazy load Whisper model."""
        if self._model is None:
            try:
                import whisper
                
                # Auto-detect device if not specified
                if self.device is None:
                    import torch
                    self.device = "cuda" if torch.cuda.is_available() else "cpu"
                
                print(f"Loading Whisper model '{self.model_size}' on {self.device}...")
                self._model = whisper.load_model(self.model_size, device=self.device)
                print(f"Whisper model loaded successfully")
            except ImportError:
                raise RuntimeError(
                    "Whisper library not installed. Install with: pip install openai-whisper"
                )
            except Exception as e:
                raise RuntimeError(f"Failed to load Whisper model: {e}")
    
    def _extract_audio(self, video_path: Path) -> Path:
        """Extract audio track from video to temporary WAV file.
        
        Args:
            video_path: Path to input video file
            
        Returns:
            Path to temporary audio file
        """
        # Create temporary file for audio
        tmp_audio_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp_audio_path = Path(tmp_audio_file.name)
        tmp_audio_file.close()
        
        try:
            # Extract first audio track to WAV format
            stream = ffmpeg.input(str(video_path))
            audio_stream = stream['a:0']  # First audio track
            
            output = ffmpeg.output(
                audio_stream,
                str(tmp_audio_path),
                acodec='pcm_s16le',  # PCM 16-bit
                ar=16000,  # 16kHz sample rate (Whisper's native rate)
                ac=1,  # Mono
                **{'y': None}
            )
            
            ffmpeg.run(output, quiet=True, overwrite_output=True)
            return tmp_audio_path
        except ffmpeg.Error as e:
            # Clean up on error
            if tmp_audio_path.exists():
                tmp_audio_path.unlink()
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            raise RuntimeError(f"Failed to extract audio from video: {error_msg}")
    
    def transcribe(self, video_path: Path) -> List[Dict[str, Any]]:
        """Transcribe audio from video file.
        
        Args:
            video_path: Path to input video file
            
        Returns:
            List of transcript segments, each containing:
            - 'start': Start time in seconds
            - 'end': End time in seconds
            - 'text': Transcript text
        """
        self._load_model()
        
        # Extract audio to temporary file
        tmp_audio_path = None
        try:
            print("Extracting audio from video...")
            tmp_audio_path = self._extract_audio(video_path)
            
            # Transcribe with Whisper
            print(f"Transcribing audio with Whisper (this may take a while)...")
            result = self._model.transcribe(
                str(tmp_audio_path),
                verbose=False,  # Suppress progress output (we'll print our own)
                word_timestamps=False  # We only need segment-level timestamps
            )
            
            # Convert Whisper segments to our format
            segments = []
            for segment in result.get('segments', []):
                segments.append({
                    'start': float(segment['start']),
                    'end': float(segment['end']),
                    'text': segment['text'].strip()
                })
            
            print(f"Transcription complete: {len(segments)} segments, {result.get('language', 'unknown')} language")
            return segments
            
        finally:
            # Clean up temporary audio file
            if tmp_audio_path and tmp_audio_path.exists():
                tmp_audio_path.unlink()

