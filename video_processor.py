"""Video processing module for cutting silence segments."""

from pathlib import Path
from typing import List, Tuple
import ffmpeg


class VideoProcessor:
    """Handles video processing operations using FFmpeg via ffmpeg-python."""
    
    def __init__(self):
        """Initialize video processor."""
        self._check_ffmpeg()
        self._nvenc_available = self._check_nvenc()
    
    def _check_nvenc(self) -> bool:
        """Check if NVIDIA NVENC hardware encoding is available.
        
        Returns:
            True if NVENC is available, False otherwise
        """
        try:
            import subprocess
            # Check if h264_nvenc encoder is available
            result = subprocess.run(
                ['ffmpeg', '-hide_banner', '-encoders'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                # Check if h264_nvenc is in the list of encoders
                return 'h264_nvenc' in result.stdout
            return False
        except Exception:
            return False
    
    def _check_gpu_memory(self, video_path: Path, num_segments: int = 1) -> None:
        """Check if there's sufficient GPU memory for encoding.
        
        Estimates memory requirements based on video resolution, number of segments,
        and exits with error if insufficient memory is available.
        
        Args:
            video_path: Path to input video file
            num_segments: Number of segments being processed (affects memory usage)
            
        Raises:
            RuntimeError: If insufficient GPU memory is available
        """
        try:
            import subprocess
            import re
            
            # Get video resolution
            probe = ffmpeg.probe(str(video_path))
            video_stream = next((s for s in probe['streams'] if s.get('codec_type') == 'video'), None)
            if not video_stream:
                # Can't determine requirements, skip check
                return
            
            width = int(video_stream.get('width', 1920))
            height = int(video_stream.get('height', 1080))
            resolution = width * height
            
            # More conservative memory estimation for NVENC
            # Base encoding: ~1MB per 1M pixels
            # Filter operations: additional overhead for filter_complex
            # Segment overhead: each segment in filter_complex adds memory
            base_memory_mb = (resolution / 1_000_000) * 1.0  # Base encoding memory
            filter_overhead_mb = 512  # Base filter_complex overhead
            segment_overhead_mb = num_segments * 10  # ~10MB per segment in filter
            estimated_memory_mb = base_memory_mb + filter_overhead_mb + segment_overhead_mb
            estimated_memory_mb = max(estimated_memory_mb, 1536)  # Minimum 1.5GB for safety
            
            # Check available GPU memory using nvidia-smi
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                # nvidia-smi failed, can't check memory - fail to be safe
                raise RuntimeError(
                    "Could not check GPU memory availability. "
                    "Please ensure nvidia-smi is available and the GPU is accessible. "
                    "Cannot proceed without GPU memory verification."
                )
            
            # Parse available memory (in MB)
            available_memory_mb = None
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        available_memory_mb = float(line.strip())
                        break
                    except ValueError:
                        continue
            
            if available_memory_mb is None:
                raise RuntimeError(
                    "Could not parse GPU memory information. "
                    "Cannot proceed without GPU memory verification."
                )
            
            # Check if sufficient memory is available (require at least 20% headroom)
            required_with_headroom = estimated_memory_mb * 1.2
            if available_memory_mb < required_with_headroom:
                raise RuntimeError(
                    f"Insufficient GPU memory for encoding. "
                    f"Required: ~{required_with_headroom:.0f}MB (estimated {estimated_memory_mb:.0f}MB + 20% headroom), "
                    f"Available: {available_memory_mb:.0f}MB. "
                    f"Please free up GPU memory, close other GPU-accelerated applications, "
                    f"or reduce the number of segments per batch."
                )
            
            print(f"GPU memory check passed: {available_memory_mb:.0f}MB available, "
                  f"~{estimated_memory_mb:.0f}MB estimated requirement")
            
        except RuntimeError:
            # Re-raise our custom errors
            raise
        except Exception as e:
            # For other errors, fail to be safe (user requested no fallback)
            raise RuntimeError(
                f"GPU memory check failed: {e}. "
                "Cannot proceed without GPU memory verification."
            )
    
    def _check_ffmpeg(self):
        """Check if FFmpeg is available by attempting to probe."""
        try:
            # Try to run a simple ffmpeg command to check availability
            ffmpeg.probe('dummy', v='error')
        except (ffmpeg.Error, FileNotFoundError):
            # The error is expected for a dummy file, but if ffmpeg isn't found,
            # we'll get FileNotFoundError
            try:
                # Try to get ffmpeg version
                import subprocess
                subprocess.run(
                    ['ffmpeg', '-version'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                raise RuntimeError(
                    "FFmpeg is not installed or not available in PATH. "
                    "Please install FFmpeg to use this tool."
                )
    
    def get_audio_track_count(self, video_path: Path) -> int:
        """Get the number of audio tracks in the video.
        
        Args:
            video_path: Path to video file
            
        Returns:
            Number of audio tracks
        """
        try:
            probe = ffmpeg.probe(str(video_path))
            audio_streams = [
                stream for stream in probe['streams']
                if stream.get('codec_type') == 'audio'
            ]
            return len(audio_streams)
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            raise RuntimeError(f"Failed to probe video file: {error_msg}")
    
    def _get_video_duration(self, video_path: Path) -> float:
        """Get video duration in seconds.
        
        Tries multiple methods to get duration:
        1. Format duration from probe
        2. Stream duration from video stream
        3. Calculate from last packet timestamp
        
        Args:
            video_path: Path to video file
            
        Returns:
            Duration in seconds
            
        Raises:
            RuntimeError: If duration cannot be retrieved
        """
        try:
            probe = ffmpeg.probe(str(video_path))
            
            # Method 1: Try format duration
            duration_str = probe['format'].get('duration')
            if duration_str and duration_str != 'N/A':
                try:
                    duration = float(duration_str)
                    if duration > 0:
                        return duration
                except (ValueError, TypeError):
                    pass
            
            # Method 2: Try video stream duration
            for stream in probe.get('streams', []):
                if stream.get('codec_type') == 'video':
                    duration_str = stream.get('duration')
                    if duration_str and duration_str != 'N/A':
                        try:
                            duration = float(duration_str)
                            if duration > 0:
                                return duration
                        except (ValueError, TypeError):
                            pass
            
            # Method 3: Try to calculate from last packet timestamp
            # Use ffprobe to get the last packet's pts_time
            import subprocess
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'packet=pts_time',
                '-of', 'csv=p=0',
                str(video_path)
            ]
            
            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    # Get the last timestamp
                    timestamps = [float(t) for t in result.stdout.strip().split('\n') if t]
                    if timestamps:
                        # Add a small buffer (0.1s) to account for the last frame
                        duration = max(timestamps) + 0.1
                        if duration > 0:
                            return duration
            except (subprocess.TimeoutExpired, ValueError, IndexError):
                pass
            
            # Method 4: Try to get duration from audio stream
            # Probe audio streams for duration
            try:
                for stream in probe.get('streams', []):
                    if stream.get('codec_type') == 'audio':
                        duration_str = stream.get('duration')
                        if duration_str and duration_str != 'N/A':
                            try:
                                duration = float(duration_str)
                                if duration > 0:
                                    return duration
                            except (ValueError, TypeError):
                                continue
            except Exception:
                pass
            
            # If all methods fail, raise an error
            raise RuntimeError(
                f"Could not determine duration of video: {video_path}. "
                "The file may be incomplete or corrupted."
            )
            
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            raise RuntimeError(
                f"Failed to get video duration: {error_msg}"
            )
        except (ValueError, KeyError) as e:
            raise RuntimeError(
                f"Could not parse video duration: {e}"
            )
    
    def _merge_overlapping_segments(
        self,
        segments: List[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        """Merge overlapping or adjacent segments.
        
        Args:
            segments: List of (start_time, end_time) tuples
            
        Returns:
            List of merged (start_time, end_time) tuples with no overlaps
        """
        if not segments:
            return []
        
        # Sort segments by start time
        sorted_segments = sorted(segments, key=lambda x: x[0])
        merged = [sorted_segments[0]]
        
        for current_start, current_end in sorted_segments[1:]:
            last_start, last_end = merged[-1]
            
            # If current segment overlaps or is adjacent to the last merged segment
            if current_start <= last_end:
                # Merge by extending the end time
                merged[-1] = (last_start, max(last_end, current_end))
            else:
                # No overlap, add as new segment
                merged.append((current_start, current_end))
        
        return merged
    
    def _extract_segment(
        self,
        input_path: Path,
        start_time: float,
        end_time: float,
        output_path: Path
    ):
        """Extract a single segment from video to a temporary file.
        
        Uses stream copy (-c copy) for fast, low-memory extraction.
        Preserves all video and audio tracks.
        
        Args:
            input_path: Path to input video file
            start_time: Start time of segment (seconds)
            end_time: End time of segment (seconds)
            output_path: Path to output segment file
        """
        import subprocess
        
        duration = end_time - start_time
        
        # Use FFmpeg with -ss after -i for more accurate seeking
        # When -ss is before -i, FFmpeg seeks to nearest keyframe (fast but imprecise)
        # When -ss is after -i, FFmpeg does frame-accurate seeking (slower but precise)
        # We use frame-accurate seeking to prevent audio overlap at boundaries
        cmd = [
            'ffmpeg',
            '-i', str(input_path),
            '-ss', str(start_time),  # Seek to start (after -i = frame-accurate seeking)
            '-t', str(duration),  # Duration to extract
            '-map', '0:v',  # Map all video streams
            '-map', '0:a',  # Map all audio streams
            '-c', 'copy',  # Stream copy (no re-encoding)
            '-avoid_negative_ts', 'make_zero',  # Handle timestamp issues at boundaries
            '-y', str(output_path)
        ]
        
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else "Unknown error"
            raise RuntimeError(f"FFmpeg failed to extract segment ({start_time:.2f}s - {end_time:.2f}s): {error_msg}")
    
    def _extract_segment_chunked(
        self,
        input_path: Path,
        start_time: float,
        end_time: float,
        temp_dir: Path,
        chunk_size: float = 5.0
    ) -> Path:
        """Extract a segment longer than chunk_size in multiple chunks.
        
        Splits the segment into chunks, extracts each chunk, then concatenates
        them into a single segment file.
        
        Args:
            input_path: Path to input video file
            start_time: Start time of segment (seconds)
            end_time: End time of segment (seconds)
            temp_dir: Temporary directory for chunk files
            chunk_size: Size of each chunk in seconds (default: 5.0)
            
        Returns:
            Path to the final concatenated segment file
        """
        import tempfile
        
        duration = end_time - start_time
        chunk_files = []
        
        try:
            # Extract chunks
            current_time = start_time
            chunk_idx = 0
            
            while current_time < end_time:
                chunk_end = min(current_time + chunk_size, end_time)
                chunk_duration = chunk_end - current_time
                
                chunk_file = temp_dir / f"chunk_{chunk_idx:04d}.mkv"
                self._extract_segment(input_path, current_time, chunk_end, chunk_file)
                chunk_files.append(chunk_file)
                
                current_time = chunk_end
                chunk_idx += 1
            
            # Concatenate chunks into final segment file
            segment_file = temp_dir / f"segment_merged_{start_time:.2f}_{end_time:.2f}.mkv"
            
            # Create concat file for chunks
            concat_file = temp_dir / f"concat_chunks_{chunk_idx}.txt"
            with open(concat_file, 'w') as f:
                for chunk_file in chunk_files:
                    f.write(f"file '{chunk_file.absolute()}'\n")
            
            # Concatenate chunks
            import subprocess
            cmd = [
                'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-c', 'copy',  # Stream copy
                '-y', str(segment_file)
            ]
            
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else "Unknown error"
                raise RuntimeError(f"FFmpeg failed to concatenate chunks: {error_msg}")
            
            # Clean up chunk files and concat file
            for chunk_file in chunk_files:
                chunk_file.unlink(missing_ok=True)
            concat_file.unlink(missing_ok=True)
            
            return segment_file
            
        except Exception as e:
            # Clean up on error
            for chunk_file in chunk_files:
                chunk_file.unlink(missing_ok=True)
            raise
    
    
    def remove_segments(
        self,
        input_path: Path,
        output_path: Path,
        segments_to_remove: List[Tuple[float, float]],
        chunk_size: float = 5.0
    ):
        """Remove specified segments from video, preserving all audio tracks.
        
        Args:
            input_path: Path to input video file
            output_path: Path to output video file
            segments_to_remove: List of (start_time, end_time) tuples to remove
            chunk_size: Size of chunks in seconds for segments longer than chunk_size (default: 5.0)
        """
        if not segments_to_remove:
            # No segments to remove, just copy the file
            self._copy_video(input_path, output_path)
            return
        
        # Get video duration and audio track count
        duration = self._get_video_duration(input_path)
        num_audio_tracks = self.get_audio_track_count(input_path)
        
        print(f"Processing video with {num_audio_tracks} audio track(s)")
        
        # Build list of segments to keep (everything except removed segments)
        segments_to_keep = []
        current_time = 0.0
        
        for remove_start, remove_end in sorted(segments_to_remove, key=lambda x: x[0]):
            # If there's a gap before this removal, add it as a segment to keep
            if remove_start > current_time:
                segments_to_keep.append((current_time, remove_start))
            
            current_time = max(current_time, remove_end)
        
        # Add final segment if there's content after last removal
        if current_time < duration:
            segments_to_keep.append((current_time, duration))
        
        if not segments_to_keep:
            raise ValueError("All video content would be removed. Aborting.")
        
        # Merge overlapping segments to avoid redundant extraction
        segments_to_keep = self._merge_overlapping_segments(segments_to_keep)
        print(f"Merged segments: {segments_to_keep}")
        # import sys
        # sys.exit(0)

        # Use chunked extraction approach: extract each segment individually,
        # then concatenate all segments. This avoids large filter_complex commands
        # that cause memory issues.
        import tempfile
        import shutil
        
        temp_dir = Path(tempfile.mkdtemp(prefix='autovidedit_segments_'))
        print(f"Temporary directory: {temp_dir}")
        segment_files = []
        
        try:
            print(f"Extracting {len(segments_to_keep)} segment(s) to temporary files...")
            
            for seg_idx, (seg_start, seg_end) in enumerate(segments_to_keep):
                seg_duration = seg_end - seg_start

                curr_segment = seg_idx + 1
                total_segments = len(segments_to_keep)
                progress = max((curr_segment / total_segments) * 100 - 10, 1)
                self.progress.emit(progress, f"Extracting segment {curr_segment} of {total_segments}")
                print(f"  Segment {curr_segment}/{total_segments}: {seg_start:.2f}s - {seg_end:.2f}s ({seg_duration:.2f}s)")
                
                segment_file = temp_dir / f"segment_{seg_idx:04d}.mkv"
                
                if seg_duration <= chunk_size:
                    # Extract directly (short segment)
                    self._extract_segment(input_path, seg_start, seg_end, segment_file)
                else:
                    # Extract in chunks (long segment)
                    segment_file = self._extract_segment_chunked(
                        input_path, seg_start, seg_end, temp_dir, chunk_size
                    )
                
                segment_files.append(segment_file)
            
            # Concatenate all segments
            print(f"\nConcatenating {len(segment_files)} segment(s)...")
            self._concatenate_videos(segment_files, output_path, num_audio_tracks)
            
        finally:
            # Clean up temporary directory and all files
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            pass
    
    def _concatenate_videos(
        self,
        video_files: List[Path],
        output_path: Path,
        num_audio_tracks: int
    ):
        """Concatenate multiple video files into one.
        
        Args:
            video_files: List of video file paths to concatenate
            output_path: Path to output video file
            num_audio_tracks: Number of audio tracks
        """
        import tempfile
        
        # Create concat file for FFmpeg
        concat_file = Path(tempfile.mktemp(suffix='.txt'))
        
        try:
            with open(concat_file, 'w') as f:
                for video_file in video_files:
                    f.write(f"file '{video_file.absolute()}'\n")
            
            # Use concat demuxer to join videos (fast, no re-encoding)
            # The concat demuxer preserves all streams by default, but we explicitly
            # map them to be safe (especially important for multiple audio tracks)
            import subprocess
            cmd = [
                'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-map', '0',  # Map all streams from first input (concat demuxer creates single input)
                '-c', 'copy',  # Copy streams without re-encoding
                '-y', str(output_path)
            ]
            
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else "Unknown error"
                raise RuntimeError(f"FFmpeg failed to concatenate videos: {error_msg}")
                
        finally:
            concat_file.unlink(missing_ok=True)
    
    
    def _copy_video(self, input_path: Path, output_path: Path):
        """Copy video file without modification, preserving all tracks.
        
        Args:
            input_path: Source video path
            output_path: Destination video path
            
        Raises:
            RuntimeError: If video copy fails
        """
        try:
            input_stream = ffmpeg.input(str(input_path))
            output = ffmpeg.output(
                input_stream,
                str(output_path),
                codec='copy',  # Copy all streams without re-encoding
                **{'y': None}   # Overwrite output
            )
            ffmpeg.run(output, quiet=True, overwrite_output=True)
        except ffmpeg.Error as e:
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            raise RuntimeError(f"Failed to copy video: {error_msg}")
