"""Silence detection module for audio analysis."""

import tempfile
from pathlib import Path
from typing import List, Tuple
import ffmpeg
from pydub import AudioSegment
from pydub.silence import detect_silence


class SilenceDetector:
    """Detects silence segments in video files with multi-track support."""
    
    def __init__(
        self,
        silence_threshold: float = -40.0,
        min_silence_duration: float = 0.5,
        padding: float = 0.1
    ):
        """Initialize silence detector.
        
        Args:
            silence_threshold: Audio level threshold in dB (default: -40)
            min_silence_duration: Minimum silence duration to detect in seconds (default: 0.5)
            padding: Padding around silence segments in seconds (default: 0.1)
        """
        self.silence_threshold = silence_threshold
        self.min_silence_duration = min_silence_duration
        self.padding = padding
    
    def get_audio_track_count(self, video_path: Path) -> int:
        """Get the number of audio tracks in the video.
        
        Args:
            video_path: Path to input video file
            
        Returns:
            Number of audio tracks
            
        Raises:
            RuntimeError: If video has no audio tracks or probe fails
        """
        try:
            probe = ffmpeg.probe(str(video_path))
            audio_streams = [
                stream for stream in probe['streams']
                if stream.get('codec_type') == 'audio'
            ]
            
            if not audio_streams:
                raise RuntimeError(
                    f"Video file '{video_path}' does not contain any audio tracks. "
                    "This tool requires audio to detect silence."
                )
            
            return len(audio_streams)
            
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            raise RuntimeError(f"Failed to probe video file: {error_msg}")
        except Exception as e:
            raise RuntimeError(f"Error analyzing video file: {e}")
    
    def extract_audio_track(self, video_path: Path, track_index: int, chunk_start: float = None, chunk_duration: float = None) -> AudioSegment:
        """Extract a specific audio track from video file at lower quality for analysis.
        
        Extracts audio at 16kHz mono (much smaller file size) for analysis purposes only.
        Can extract a specific time chunk to avoid loading entire track into memory.
        
        Args:
            video_path: Path to input video file
            track_index: Index of audio track to extract (0-based)
            chunk_start: Optional start time in seconds for chunk extraction
            chunk_duration: Optional duration in seconds for chunk extraction
            
        Returns:
            AudioSegment object containing the audio (at 16kHz mono)
            
        Raises:
            RuntimeError: If audio extraction fails
        """
        # Create temporary file for audio extraction
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_file:
            tmp_audio_path = tmp_file.name
        
        try:
            # Use ffmpeg-python to extract specific audio track
            # Apply time constraints to input if extracting a chunk
            if chunk_start is not None:
                # Extract a specific time chunk - apply ss and t to input
                stream = ffmpeg.input(str(video_path), ss=chunk_start, t=chunk_duration if chunk_duration else None)
            else:
                stream = ffmpeg.input(str(video_path))
            
            audio_stream = stream['a:' + str(track_index)]  # Select specific audio track
            
            # Extract at lower quality for analysis: 16kHz mono (much smaller file size)
            # This reduces memory usage by ~3x compared to 44.1kHz
            output = ffmpeg.output(
                audio_stream,
                tmp_audio_path,
                acodec='pcm_s16le',  # PCM 16-bit little-endian
                ar=16000,  # 16kHz sample rate (lower than original for analysis only)
                ac=1,  # Mono (mix down to mono for analysis)
                **{'y': None}  # Overwrite output file
            )
            
            # Run ffmpeg
            ffmpeg.run(output, quiet=True, overwrite_output=True)
            
            # Check if temporary file was created and has content
            if not Path(tmp_audio_path).exists() or Path(tmp_audio_path).stat().st_size == 0:
                raise RuntimeError(f"Audio extraction for track {track_index} produced an empty file")
            
            # Load audio using pydub
            try:
                audio = AudioSegment.from_wav(tmp_audio_path)
                if len(audio) == 0:
                    raise RuntimeError(f"Extracted audio track {track_index} is empty")
                return audio
            except Exception as e:
                raise RuntimeError(f"Failed to load extracted audio track {track_index}: {e}")
            
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            if 'No audio stream' in error_msg or 'Stream map' in error_msg:
                raise RuntimeError(
                    f"Video file '{video_path}' does not have audio track {track_index}. "
                    "This tool requires audio to detect silence."
                )
            else:
                raise RuntimeError(
                    f"Failed to extract audio track {track_index} from video: {error_msg}"
                )
        finally:
            # Clean up temporary file immediately
            Path(tmp_audio_path).unlink(missing_ok=True)
    
    def detect_silence_in_track(
        self,
        video_path: Path,
        track_index: int,
        chunk_size_seconds: float = 300.0  # 5 minutes default
    ) -> List[Tuple[float, float]]:
        """Detect silence segments in an audio track using chunked processing.
        
        Processes audio in time-based chunks to avoid loading entire track into memory.
        Each chunk is extracted, analyzed, and then discarded before processing the next.
        
        Args:
            video_path: Path to input video file
            track_index: Index of audio track to analyze
            chunk_size_seconds: Size of each chunk in seconds (default: 300 = 5 minutes)
            
        Returns:
            List of (start_time, end_time) tuples in seconds
        """
        # Get total duration first
        try:
            probe = ffmpeg.probe(str(video_path))
            duration_str = probe['format'].get('duration') or probe['streams'][0].get('duration')
            if not duration_str or duration_str == 'N/A':
                # Estimate from file size or use a large value
                total_duration = 3600.0  # Default to 1 hour if can't determine
            else:
                total_duration = float(duration_str)
        except Exception:
            total_duration = 3600.0  # Default fallback
        
        # Convert thresholds to milliseconds for pydub
        min_silence_len = int(self.min_silence_duration * 1000)
        silence_thresh = int(self.silence_threshold)
        
        all_silence_segments = []
        chunk_start = 0.0
        
        # Process audio in chunks
        while chunk_start < total_duration:
            chunk_duration = min(chunk_size_seconds, total_duration - chunk_start)
            
            # Extract and analyze this chunk
            try:
                chunk_audio = self.extract_audio_track(
                    video_path,
                    track_index,
                    chunk_start=chunk_start,
                    chunk_duration=chunk_duration
                )
                
                # Detect silence in this chunk
                chunk_silence_ranges = detect_silence(
                    chunk_audio,
                    min_silence_len=min_silence_len,
                    silence_thresh=silence_thresh
                )
                
                # Convert to absolute time (add chunk_start offset) and apply padding
                for start_ms, end_ms in chunk_silence_ranges:
                    # Convert from milliseconds to seconds and add chunk offset
                    # Padding: extend silence region to avoid cutting too close to speech
                    start_time = chunk_start + (start_ms / 1000.0) + self.padding
                    end_time = chunk_start + (end_ms / 1000.0) - self.padding
                    
                    # Ensure times are non-negative
                    start_time = max(0.0, start_time)
                    end_time = max(start_time, end_time)
                    
                    all_silence_segments.append((start_time, end_time))
                
                # Explicitly delete chunk audio to free memory immediately
                del chunk_audio
                
            except Exception as e:
                # If chunk extraction fails, log and continue with next chunk
                print(f"    Warning: Failed to process chunk starting at {chunk_start:.1f}s: {e}")
            
            # Move to next chunk
            chunk_start += chunk_size_seconds
        
        return all_silence_segments
    
    def find_common_silence(
        self,
        all_track_silences: List[List[Tuple[float, float]]]
    ) -> List[Tuple[float, float]]:
        """Find silence periods where ALL tracks are silent (intersection).
        
        Args:
            all_track_silences: List of silence segment lists, one per audio track
            
        Returns:
            List of (start_time, end_time) tuples where all tracks are silent
        """
        if not all_track_silences:
            return []
        
        if len(all_track_silences) == 1:
            # Only one track, return its silence segments
            return all_track_silences[0]
        
        # Start with first track's silence segments
        track1_silence = all_track_silences[0].copy()

        if not track1_silence:
            return track1_silence
        
        common_silence = track1_silence
        
        # Intersect with each subsequent track
        for track_silences in all_track_silences[1:]:
            # If this track has no silence, there's no common silence
            if not track_silences:
                return []
            
            # Find intersection of current common_silence and this track's silences
            new_common = []
            
            for cs_start, cs_end in common_silence:
                # Check if this common silence period overlaps with any silence in current track
                for ts_start, ts_end in track_silences:
                    # Find overlap
                    overlap_start = max(cs_start, ts_start)
                    overlap_end = min(cs_end, ts_end)
                    
                    if overlap_start < overlap_end:
                        # There's an overlap - add it to new_common
                        new_common.append((overlap_start, overlap_end))
            
            # If no overlaps found, there's no common silence
            if not new_common:
                return []
            
            # Merge any overlapping segments in new_common before continuing
            common_silence = self.merge_overlapping_segments(new_common)
        
        return common_silence
    
    def detect_silence_segments(self, video_path: Path) -> List[Tuple[float, float]]:
        """Detect silence segments in video file (all tracks must be silent).
        
        Args:
            video_path: Path to input video file
            
        Returns:
            List of (start_time, end_time) tuples in seconds where ALL tracks are silent
        """
        # Get number of audio tracks
        num_tracks = self.get_audio_track_count(video_path)
        print(f"Found {num_tracks} audio track(s)")
        
        # Extract and analyze each audio track
        all_track_silences = []
        
        for track_idx in range(num_tracks):
            print(f"  Analyzing audio track {track_idx + 1}/{num_tracks} (processing in chunks)...")
            track_silences = self.detect_silence_in_track(video_path, track_idx)
            all_track_silences.append(track_silences)
            print(f"    Found {len(track_silences)} silence segment(s) in track {track_idx + 1}")
        
        # Find common silence (where ALL tracks are silent)
        print("Finding periods where all tracks are silent...")
        common_silence = self.find_common_silence(all_track_silences)
        
        return common_silence
    
    def merge_overlapping_segments(
        self,
        segments: List[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        """Merge overlapping or adjacent silence segments.
        
        Args:
            segments: List of (start_time, end_time) tuples
            
        Returns:
            Merged list of segments
        """
        if not segments:
            return []
        
        # Sort by start time
        sorted_segments = sorted(segments, key=lambda x: x[0])
        merged = [sorted_segments[0]]
        
        for current_start, current_end in sorted_segments[1:]:
            last_start, last_end = merged[-1]
            
            # If segments overlap or are adjacent, merge them
            if current_start <= last_end:
                merged[-1] = (last_start, max(last_end, current_end))
            else:
                merged.append((current_start, current_end))
        
        return merged
