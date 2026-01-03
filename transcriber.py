"""Transcription module for converting video audio to text using Whisper."""

from pathlib import Path
from typing import List, Dict, Any, Tuple
import tempfile
import ffmpeg
from silence_detector import SilenceDetector


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
    
    def _extract_audio(self, video_path: Path, track_index: int = 0) -> Path:
        """Extract audio track from video to temporary WAV file.
        
        Args:
            video_path: Path to input video file
            track_index: Index of audio track to extract (0-based)
            
        Returns:
            Path to temporary audio file
        """
        # Create temporary file for audio
        tmp_audio_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp_audio_path = Path(tmp_audio_file.name)
        tmp_audio_file.close()
        
        try:
            # Extract specific audio track to WAV format
            stream = ffmpeg.input(str(video_path))
            audio_stream = stream[f'a:{track_index}']  # Specific audio track
            
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
    
    def _detect_sentences_from_words(
        self,
        words: List[Dict[str, Any]],
        pause_threshold: float = 0.5
    ) -> List[Dict[str, Any]]:
        """Group words into sentences based on pauses between words.
        
        Args:
            words: List of word dictionaries with 'start', 'end', 'word' keys
            pause_threshold: Minimum gap in seconds to consider a sentence boundary (default: 0.5)
            
        Returns:
            List of sentence dictionaries with 'start', 'end', 'words' (text), 'word_list' keys
        """
        if not words:
            return []
        
        sentences = []
        current_sentence_words = []
        current_start = None
        
        for i, word_info in enumerate(words):
            word_start = float(word_info.get('start', 0))
            word_end = float(word_info.get('end', 0))
            word_text = word_info.get('word', '').strip()
            
            if not word_text:
                continue
            
            # Initialize first word
            if current_start is None:
                current_start = word_start
                current_sentence_words = [word_info]
            else:
                # Check gap between previous word and current word
                prev_word_end = float(current_sentence_words[-1].get('end', 0))
                gap = word_start - prev_word_end
                
                if gap > pause_threshold:
                    # Gap detected - end current sentence and start new one
                    sentence_end = prev_word_end
                    sentence_text = ' '.join([w.get('word', '').strip() for w in current_sentence_words])
                    
                    sentences.append({
                        'start': current_start,
                        'end': sentence_end,
                        'words': sentence_text,
                        'word_list': current_sentence_words
                    })
                    
                    # Start new sentence
                    current_start = word_start
                    current_sentence_words = [word_info]
                else:
                    # Continue current sentence
                    current_sentence_words.append(word_info)
        
        # Add final sentence
        if current_sentence_words:
            final_word_end = float(current_sentence_words[-1].get('end', 0))
            sentence_text = ' '.join([w.get('word', '').strip() for w in current_sentence_words])
            
            sentences.append({
                'start': current_start,
                'end': final_word_end,
                'words': sentence_text,
                'word_list': current_sentence_words
            })
        
        return sentences
    
    def transcribe(self, video_path: Path) -> List[Dict[str, Any]]:
        """Transcribe audio from video file (legacy method for single track).
        
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
    
    def transcribe_all_tracks(
        self,
        video_path: Path,
        pause_threshold: float = 0.5
    ) -> List[Dict[str, Any]]:
        """Transcribe all audio tracks from video file with word-level timestamps.
        
        Args:
            video_path: Path to input video file
            pause_threshold: Minimum gap in seconds to consider a sentence boundary (default: 0.5)
            
        Returns:
            List of sentence dictionaries, each containing:
            - 'start': Start time in seconds
            - 'end': End time in seconds
            - 'words': Sentence text
            - 'audio_track': Audio track number (1-indexed)
        """
        self._load_model()
        
        # Get number of audio tracks
        detector = SilenceDetector()
        num_tracks = detector.get_audio_track_count(video_path)
        
        all_sentences = []
        
        # Process each audio track
        for track_idx in range(num_tracks):
            track_num = track_idx + 1  # 1-indexed for display
            print(f"\nProcessing audio track {track_num}/{num_tracks}...")
            
            tmp_audio_path = None
            try:
                # Extract audio track to temporary file
                print(f"  Extracting audio track {track_num}...")
                tmp_audio_path = self._extract_audio(video_path, track_index=track_idx)
                
                # Transcribe with Whisper (with word timestamps)
                print(f"  Transcribing audio track {track_num} with Whisper (this may take a while)...")
                result = self._model.transcribe(
                    str(tmp_audio_path),
                    verbose=False,
                    word_timestamps=True  # Enable word-level timestamps
                )
                
                # Extract all words from all segments
                all_words = []
                for segment in result.get('segments', []):
                    words = segment.get('words', [])
                    if words:
                        all_words.extend(words)
                
                if all_words:
                    # Group words into sentences based on pauses
                    sentences = self._detect_sentences_from_words(all_words, pause_threshold)
                    
                    # Add track number to each sentence
                    for sentence in sentences:
                        sentence['audio_track'] = track_num
                        all_sentences.append(sentence)
                    
                    print(f"  Found {len(sentences)} sentence(s) in track {track_num}")
                else:
                    print(f"  No words found in track {track_num}")
                
            finally:
                # Clean up temporary audio file
                if tmp_audio_path and tmp_audio_path.exists():
                    tmp_audio_path.unlink()
        
        # Sort all sentences by start time
        all_sentences.sort(key=lambda x: x['start'])
        
        print(f"\nTranscription complete: {len(all_sentences)} total sentence(s) across {num_tracks} track(s)")
        return all_sentences

