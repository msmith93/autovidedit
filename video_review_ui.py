#!/usr/bin/env python3
"""PySide6 UI for video review and sentence editing."""

import json
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QPushButton, QSlider,
    QLabel, QComboBox, QProgressDialog, QMessageBox, QFileDialog,
    QMenuBar, QMenu, QToolBar, QStatusBar, QDialog, QDialogButtonBox, QCheckBox
)
from PySide6.QtCore import Qt, QUrl, Signal, QThread, QTimer, QSize
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QBrush, QColor
import vlc
import platform

from video_processor import VideoProcessor


class RenderWorker(QThread):
    """Worker thread for video rendering to keep UI responsive."""
    
    progress = Signal(int, str)  # progress percentage, status message
    finished = Signal(bool, str)  # success, message
    error = Signal(str)  # error message
    
    def __init__(self, original_video_path: Path, output_path: Path, 
                 segments_to_remove: List[Tuple[float, float]], 
                 remove_space_between_sentences: bool = False,
                 modifications: List[Dict[str, Any]] = None,
                 re_encode_video: bool = False,
                 chunk_size: float = 5.0):
        super().__init__()
        self.input_path = original_video_path  # Use original, not mixed
        self.output_path = output_path
        self.segments_to_remove = segments_to_remove
        self.remove_space_between_sentences = remove_space_between_sentences
        self.modifications = modifications
        self.re_encode_video = re_encode_video
        self.chunk_size = chunk_size
        self._cancelled = False
    
    def cancel(self):
        """Cancel the render operation."""
        self._cancelled = True
    
    def run(self):
        """Run the render operation."""
        try:
            processor = VideoProcessor()
            
            # Calculate segments to keep for progress estimation
            duration = processor._get_video_duration(self.input_path)
            segments_to_keep = []
            current_time = 0.0
            
            for remove_start, remove_end in sorted(self.segments_to_remove, key=lambda x: x[0]):
                if remove_start > current_time:
                    segments_to_keep.append((current_time, remove_start))
                current_time = max(current_time, remove_end)
            
            if current_time < duration:
                segments_to_keep.append((current_time, duration))
            
            total_segments = len(segments_to_keep)
            
            self.progress.emit(0, "Starting video processing...")
            
            if self._cancelled:
                self.finished.emit(False, "Render cancelled")
                return
            
            # Process to temporary MKV file first (fast, no audio re-encoding)
            temp_output = self.output_path.parent / f"{self.output_path.stem}_temp.mkv"
            
            # Process video (we can't easily intercept progress from remove_segments,
            # so we'll estimate based on time)
            import time
            start_time = time.time()
            
            processor.remove_segments(
                self.input_path,
                temp_output,
                self.segments_to_remove,
                self.progress,
                remove_space_between_sentences=self.remove_space_between_sentences,
                modifications=self.modifications,
                re_encode_video=self.re_encode_video,
                chunk_size=self.chunk_size
            )
            
            if self._cancelled:
                temp_output.unlink(missing_ok=True)  # Clean up temp file
                self.finished.emit(False, "Render cancelled")
                return
            
            # Convert to MOV with CBR AAC and constant frame rate if output path is .mov
            if self.output_path.suffix.lower() == '.mov':
                self.progress.emit(95, "Converting to constant frame rate and CBR AAC audio")
                processor._convert_to_mov(temp_output, self.output_path)
                temp_output.unlink()  # Remove temporary MKV file
            else:
                # Output is MKV, just rename the temp file
                temp_output.rename(self.output_path)
            
            self.progress.emit(100, "Rendering complete!")
            self.finished.emit(True, f"Video saved to: {self.output_path}")
            
        except Exception as e:
            self.error.emit(str(e))


class SentenceItem(QListWidgetItem):
    """Custom list item for sentence display."""
    
    def __init__(self, sentence_data: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.sentence_data = sentence_data
        self.start_time = float(sentence_data.get('start_time', 0.0))
        self.end_time = float(sentence_data.get('end_time', 0.0))
        self.is_removed = sentence_data.get('modification') == 'REMOVED'
        self.words = sentence_data.get('content', {}).get('words', '')
        self.audio_track = sentence_data.get('content', {}).get('audio_track', 1)
        
        # Format timestamp
        timestamp = self._format_time(self.start_time)
        track_text = f"Track {self.audio_track}"
        
        # Set display text
        display_text = f"[{timestamp}] {track_text}\n{self.words}"
        self.setText(display_text)
        
        # Visual styling for removed sentences
        if self.is_removed:
            self.setForeground(Qt.GlobalColor.red)
            font = self.font()
            font.setStrikeOut(True)
            self.setFont(font)
    
    def _format_time(self, seconds: float) -> str:
        """Format seconds as MM:SS or HH:MM:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"
    
    def mark_for_removal(self):
        """Mark this sentence for removal."""
        self.is_removed = True
        self.sentence_data['modification'] = 'REMOVED'
        self.setForeground(Qt.GlobalColor.red)
        font = self.font()
        font.setStrikeOut(True)
        self.setFont(font)
    
    def unmark_for_removal(self):
        """Unmark this sentence for removal."""
        self.is_removed = False
        self.sentence_data['modification'] = 'NONE'
        self.setForeground(Qt.GlobalColor.black)
        font = self.font()
        font.setStrikeOut(False)
        self.setFont(font)
    
    def set_overlap_warning(self, has_warning: bool):
        """Set yellow highlight to warn about overlap impact."""
        if has_warning and not self.is_removed:
            self.setBackground(QBrush(QColor(255, 255, 0, 100)))  # Semi-transparent yellow
        else:
            self.setBackground(QBrush())  # Clear background


class InBetweenItem(QListWidgetItem):
    """Custom list item for in-between (gap) time spans."""
    
    def __init__(self, start_time: float, end_time: float, parent=None):
        super().__init__(parent)
        self.start_time = start_time
        self.end_time = end_time
        self.is_enabled = False  # Disabled by default (will be removed)
        
        # Format timestamp
        timestamp = self._format_time(start_time)
        duration = end_time - start_time
        
        # Set display text
        display_text = f"[{timestamp}] GAP ({duration:.1f}s)"
        self.setText(display_text)
        
        # Visual styling for disabled gaps (grayed out)
        self._update_styling()
    
    def _format_time(self, seconds: float) -> str:
        """Format seconds as MM:SS or HH:MM:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"
    
    def _update_styling(self):
        """Update visual styling based on enabled state."""
        if self.is_enabled:
            # Enabled = keep in video (normal styling)
            self.setForeground(Qt.GlobalColor.darkGreen)
            font = self.font()
            font.setItalic(True)
            font.setStrikeOut(False)
            self.setFont(font)
        else:
            # Disabled = remove from video (grayed out with strikeout)
            self.setForeground(Qt.GlobalColor.gray)
            font = self.font()
            font.setItalic(True)
            font.setStrikeOut(True)
            self.setFont(font)
    
    def enable(self):
        """Enable this gap (keep in final video)."""
        self.is_enabled = True
        self._update_styling()
    
    def disable(self):
        """Disable this gap (remove from final video)."""
        self.is_enabled = False
        self._update_styling()


def calculate_in_between_spans(
    sentences: List[Dict[str, Any]], 
    video_duration: Optional[float] = None,
    padding: float = 0.2
) -> List[Tuple[float, float]]:
    """Calculate time spans where no audio track has an active sentence.
    
    Merges all sentences from both tracks into a unified timeline,
    then identifies gaps between the merged segments.
    
    Note: Gaps are calculated based on ALL sentences regardless of removal status.
    A gap represents a time period where no sentence exists on any track.
    Padding (default 0.2s) is applied around each sentence to eliminate very small gaps.
    
    Args:
        sentences: List of sentence modification dictionaries
        video_duration: Optional video duration to include final gap
        padding: Padding in seconds to add before and after each sentence (default: 0.2)
        
    Returns:
        List of (start_time, end_time) tuples for gaps between sentences
    """
    if not sentences:
        return []
    
    # Extract sentence time ranges (ALL sentences, regardless of removal status)
    # Gaps represent periods where no sentence exists, not periods where no enabled sentence exists
    # Apply padding around each sentence to eliminate very small gaps
    sentence_ranges = []
    for sent in sentences:
        if sent.get('reason') == 'Sentence':
            start = float(sent.get('start_time', 0.0))
            end = float(sent.get('end_time', 0.0))
            if end > start:
                # Apply padding: extend sentence range by padding seconds on each side
                padded_start = max(0.0, start - padding)
                padded_end = end + padding
                sentence_ranges.append((padded_start, padded_end))
    
    if not sentence_ranges:
        return []
    
    # Sort by start time
    sentence_ranges.sort(key=lambda x: x[0])
    
    # Merge overlapping sentences across all tracks
    merged = [sentence_ranges[0]]
    for current_start, current_end in sentence_ranges[1:]:
        last_start, last_end = merged[-1]
        
        # If current sentence overlaps or is adjacent to the last merged sentence
        if current_start <= last_end:
            # Merge by extending the end time
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            # No overlap, add as new segment
            merged.append((current_start, current_end))
    
    # Calculate gaps between merged sentences (which are already padded)
    gaps = []
    for i in range(len(merged) - 1):
        gap_start = merged[i][1]
        gap_end = merged[i + 1][0]
        if gap_end > gap_start:
            gaps.append((gap_start, gap_end))
    
    # Optionally add gap at the end if video_duration is provided
    if video_duration and merged:
        last_end = merged[-1][1]
        if video_duration > last_end:
            gaps.append((last_end, video_duration))
    
    # Optionally add gap at the beginning if first sentence doesn't start at 0
    if merged and merged[0][0] > 0:
        gaps.insert(0, (0.0, merged[0][0]))
    
    return gaps


def find_overlapping_sentences(
    sentences: List['SentenceItem']
) -> Dict[int, List[int]]:
    """Find sentences that overlap with sentences from different audio tracks.
    
    Args:
        sentences: List of SentenceItem objects
        
    Returns:
        Dictionary mapping sentence index to list of overlapping sentence indices
        (from different audio tracks)
    """
    overlaps: Dict[int, List[int]] = {}
    
    for i, sent_a in enumerate(sentences):
        overlaps[i] = []
        for j, sent_b in enumerate(sentences):
            if i == j:
                continue
            # Check if different tracks and time ranges overlap
            if sent_a.audio_track != sent_b.audio_track:
                # Check for overlap: A.start < B.end AND B.start < A.end
                if sent_a.start_time < sent_b.end_time and sent_b.start_time < sent_a.end_time:
                    overlaps[i].append(j)
    
    return overlaps


class VideoPlayerWidget(QWidget):
    """Widget for video playback using VLC."""
    
    position_changed = Signal(float)  # current position in seconds
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Create VLC instance with options for multiple audio tracks
        vlc_args = [
            '--intf', 'dummy',  # No interface
            '--no-video-title-show',  # Don't show title
            '--quiet',  # Suppress VLC output
            '--vout', 'xcb_window',  # Force X11 embedded window output for Qt embedding
        ]
        
        self.vlc_instance = vlc.Instance(vlc_args)
        self.media_player = self.vlc_instance.media_player_new()
        
        # Set up position polling timer (VLC doesn't have continuous position signals)
        self.position_timer = QTimer()
        self.position_timer.timeout.connect(self._poll_position)
        self.position_timer.setInterval(100)  # Poll every 100ms
        
        # Set up event manager for VLC events
        self.event_manager = self.media_player.event_manager()
        self.event_manager.event_attach(vlc.EventType.MediaPlayerTimeChanged, self._on_time_changed)
        self.event_manager.event_attach(vlc.EventType.MediaPlayerLengthChanged, self._on_length_changed)
        self.event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_state_changed)
        self.event_manager.event_attach(vlc.EventType.MediaPlayerPaused, self._on_state_changed)
        self.event_manager.event_attach(vlc.EventType.MediaPlayerStopped, self._on_state_changed)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create widget to hold VLC video output
        # WA_NativeWindow ensures a real X11 window ID for VLC embedding
        self.video_widget = QWidget()
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_widget.setStyleSheet("background-color: black;")
        layout.addWidget(self.video_widget)
        
        self.setLayout(layout)
        
        # Embed VLC player in the widget (platform-specific)
        self._embed_vlc_player()
    
    def _embed_vlc_player(self):
        """Embed VLC player in the Qt widget (platform-specific)."""
        # Note: Embedding must happen after widget is shown and has a valid window ID
        # This will be called from load_video after the widget is visible
        pass
    
    def _do_embed_vlc_player(self):
        """Actually embed VLC player (call this after widget is shown)."""
        try:
            if platform.system() == "Linux":
                # Linux: use X11 window ID
                self.media_player.set_xwindow(int(self.video_widget.winId()))
            elif platform.system() == "Windows":
                # Windows: use HWND
                self.media_player.set_hwnd(int(self.video_widget.winId()))
            elif platform.system() == "Darwin":  # macOS
                # macOS: use NSView
                self.media_player.set_nsobject(int(self.video_widget.winId()))
            else:
                # Fallback: try X11 (works on most Unix-like systems)
                try:
                    self.media_player.set_xwindow(int(self.video_widget.winId()))
                except Exception:
                    pass
        except Exception as e:
            print(f"Warning: Could not embed VLC player: {e}")
    
    def load_video(self, video_path: Path):
        """Load a video file with all audio tracks."""
        # Embed VLC player in widget (must be done after widget is shown)
        self._do_embed_vlc_player()
        
        # Create VLC media from file path
        media = self.vlc_instance.media_new(str(video_path.absolute()))
        self.media_player.set_media(media)
        
        # Note: If video has multiple audio tracks, they should be pre-mixed
        # before the UI opens (handled in apply_edits.py)
        # VLC will play the mixed audio track
        
        # Start position polling
        self.position_timer.start()
    
    def play(self):
        """Start playback."""
        self.media_player.play()
    
    def pause(self):
        """Pause playback."""
        self.media_player.pause()
    
    def stop(self):
        """Stop playback."""
        self.media_player.stop()
    
    def set_position_seconds(self, seconds: float):
        """Set playback position in seconds."""
        self.media_player.set_time(int(seconds * 1000))
    
    def get_position_seconds(self) -> float:
        """Get current playback position in seconds."""
        time_ms = self.media_player.get_time()
        if time_ms < 0:
            return 0.0
        return time_ms / 1000.0
    
    def get_duration_seconds(self) -> float:
        """Get video duration in seconds."""
        length_ms = self.media_player.get_length()
        if length_ms < 0:
            return 0.0
        return length_ms / 1000.0
    
    def is_playing(self) -> bool:
        """Check if video is currently playing."""
        state = self.media_player.get_state()
        return state == vlc.State.Playing
    
    def set_playback_rate(self, rate: float):
        """Set playback speed (0.5x to 8.0x)."""
        self.media_player.set_rate(rate)
    
    def _poll_position(self):
        """Poll VLC for current position and emit signal."""
        if self.is_playing() or self.media_player.get_state() == vlc.State.Paused:
            position = self.get_position_seconds()
            if position >= 0:
                self.position_changed.emit(position)
    
    def _on_time_changed(self, event):
        """Handle VLC time changed event."""
        time_ms = event.u.new_time
        if time_ms >= 0:
            self.position_changed.emit(time_ms / 1000.0)
    
    def _on_length_changed(self, event):
        """Handle VLC length changed event."""
        # Duration is now available
        pass
    
    def _on_state_changed(self, event):
        """Handle VLC state changed event."""
        # State changed, update if needed
        pass
    
    def cleanup(self):
        """Clean up VLC resources."""
        self.position_timer.stop()
        if self.media_player:
            self.media_player.stop()
            self.media_player.release()
        if self.vlc_instance:
            self.vlc_instance.release()


class SentenceListWidget(QListWidget):
    """Widget for displaying and managing sentences and in-between gaps."""
    
    sentence_clicked = Signal(float)  # start_time in seconds
    sentence_modified = Signal()  # emitted when a sentence/gap is marked/unmarked
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sentences: List[SentenceItem] = []
        self.gaps: List[InBetweenItem] = []
        self.all_items: List[QListWidgetItem] = []  # Combined list for display
        self.current_sentence_index = -1
        self._has_unsaved_changes = False
        self._overlap_map: Dict[int, List[int]] = {}  # Maps sentence index to overlapping indices
        
        # Enable multi-selection (Ctrl+Click for individual, Shift+Click for range)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        
        self.itemClicked.connect(self._on_item_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
    
    def load_sentences(self, modifications: List[Dict[str, Any]], video_duration: Optional[float] = None):
        """Load sentences and calculate in-between gaps from modifications list."""
        self.clear()
        self.sentences = []
        self.gaps = []
        self.all_items = []
        self._has_unsaved_changes = False
        
        # Filter for sentences only
        sentence_mods = [
            mod for mod in modifications
            if mod.get('modification') in ('NONE', 'REMOVED') and mod.get('reason') == 'Sentence'
        ]
        
        # Sort by start_time
        sentence_mods.sort(key=lambda x: float(x.get('start_time', 0.0)))
        
        # Create sentence items
        for mod in sentence_mods:
            item = SentenceItem(mod)
            self.sentences.append(item)
        
        # Calculate overlap map for sentences
        self._overlap_map = find_overlapping_sentences(self.sentences)
        
        # Calculate in-between gaps (only from non-removed sentences for gap calculation)
        gap_ranges = calculate_in_between_spans(sentence_mods, video_duration)
        for gap_start, gap_end in gap_ranges:
            gap_item = InBetweenItem(gap_start, gap_end)
            self.gaps.append(gap_item)
        
        # Combine and sort all items chronologically by start time
        combined: List[Tuple[float, QListWidgetItem]] = []
        for sent in self.sentences:
            combined.append((sent.start_time, sent))
        for gap in self.gaps:
            combined.append((gap.start_time, gap))
        
        combined.sort(key=lambda x: x[0])
        
        # Add items to widget in sorted order
        for _, item in combined:
            self.all_items.append(item)
            self.addItem(item)
    
    def _on_item_clicked(self, item: QListWidgetItem):
        """Handle item click - seek to timestamp (unless modifier keys are pressed for multi-select)."""
        # Get current keyboard modifiers
        modifiers = QApplication.keyboardModifiers()
        
        # Only seek to timestamp if no modifier keys are pressed (single click without Ctrl/Shift)
        # This allows multi-select to work without interfering with video seeking
        if modifiers == Qt.KeyboardModifier.NoModifier:
            if isinstance(item, SentenceItem):
                self.sentence_clicked.emit(item.start_time)
            elif isinstance(item, InBetweenItem):
                self.sentence_clicked.emit(item.start_time)
            self._center_item(item)
    
    def _center_item(self, item: QListWidgetItem):
        """Center the clicked item in the view."""
        index = self.row(item)
        self.scrollToItem(item, QListWidget.ScrollHint.PositionAtCenter)
        self.setCurrentItem(item)
    
    def _show_context_menu(self, position):
        """Show context menu for sentence/gap actions."""
        selected_items = self.selectedItems()
        
        # Separate sentences and gaps
        selected_sentences = [item for item in selected_items if isinstance(item, SentenceItem)]
        selected_gaps = [item for item in selected_items if isinstance(item, InBetweenItem)]
        
        menu = QMenu(self)
        
        # Handle sentence selection
        if len(selected_sentences) > 1:
            # Multiple sentence selection - show both actions
            mark_action = menu.addAction(f"Mark {len(selected_sentences)} sentences for removal")
            unmark_action = menu.addAction(f"Unmark {len(selected_sentences)} sentences for removal")
            mark_action.triggered.connect(self._mark_selected_sentences)
            unmark_action.triggered.connect(self._unmark_selected_sentences)
        elif len(selected_sentences) == 1 and not selected_gaps:
            # Single sentence selection - show appropriate action based on state
            item = selected_sentences[0]
            if item.is_removed:
                unmark_action = menu.addAction("Unmark sentence for removal")
                unmark_action.triggered.connect(lambda: self._unmark_sentence(item))
            else:
                mark_action = menu.addAction("Mark sentence for removal")
                mark_action.triggered.connect(lambda: self._mark_sentence(item))
        
        # Handle gap selection
        if len(selected_gaps) > 1:
            # Multiple gap selection
            if selected_sentences:
                menu.addSeparator()
            enable_action = menu.addAction(f"Enable {len(selected_gaps)} gaps (keep in video)")
            disable_action = menu.addAction(f"Disable {len(selected_gaps)} gaps (remove from video)")
            enable_action.triggered.connect(self._enable_selected_gaps)
            disable_action.triggered.connect(self._disable_selected_gaps)
        elif len(selected_gaps) == 1 and not selected_sentences:
            # Single gap selection
            gap = selected_gaps[0]
            if gap.is_enabled:
                disable_action = menu.addAction("Disable gap (remove from video)")
                disable_action.triggered.connect(lambda: self._disable_gap(gap))
            else:
                enable_action = menu.addAction("Enable gap (keep in video)")
                enable_action.triggered.connect(lambda: self._enable_gap(gap))
        
        # If no items selected, try to get item at position
        if not selected_sentences and not selected_gaps:
            item = self.itemAt(position)
            if isinstance(item, SentenceItem):
                if item.is_removed:
                    unmark_action = menu.addAction("Unmark sentence for removal")
                    unmark_action.triggered.connect(lambda: self._unmark_sentence(item))
                else:
                    mark_action = menu.addAction("Mark sentence for removal")
                    mark_action.triggered.connect(lambda: self._mark_sentence(item))
            elif isinstance(item, InBetweenItem):
                if item.is_enabled:
                    disable_action = menu.addAction("Disable gap (remove from video)")
                    disable_action.triggered.connect(lambda: self._disable_gap(item))
                else:
                    enable_action = menu.addAction("Enable gap (keep in video)")
                    enable_action.triggered.connect(lambda: self._enable_gap(item))
            else:
                return  # No valid item
        
        menu.exec(self.mapToGlobal(position))
    
    def _mark_sentence(self, item: SentenceItem):
        """Mark a sentence for removal."""
        item.mark_for_removal()
        self._update_overlap_warnings()
        self.itemChanged.emit(item)
        self._has_unsaved_changes = True
        self._notify_parent_of_changes()
    
    def _unmark_sentence(self, item: SentenceItem):
        """Unmark a sentence for removal."""
        item.unmark_for_removal()
        self._update_overlap_warnings()
        self.itemChanged.emit(item)
        self._has_unsaved_changes = True
        self._notify_parent_of_changes()
    
    def _mark_selected_sentences(self):
        """Mark all selected sentences for removal."""
        selected_items = self.selectedItems()
        selected_sentences = [item for item in selected_items if isinstance(item, SentenceItem)]
        
        for item in selected_sentences:
            if not item.is_removed:
                item.mark_for_removal()
                self.itemChanged.emit(item)
        
        if selected_sentences:
            self._update_overlap_warnings()
            self._has_unsaved_changes = True
            self._notify_parent_of_changes()
    
    def _unmark_selected_sentences(self):
        """Unmark all selected sentences for removal."""
        selected_items = self.selectedItems()
        selected_sentences = [item for item in selected_items if isinstance(item, SentenceItem)]
        
        for item in selected_sentences:
            if item.is_removed:
                item.unmark_for_removal()
                self.itemChanged.emit(item)
        
        if selected_sentences:
            self._update_overlap_warnings()
            self._has_unsaved_changes = True
            self._notify_parent_of_changes()
    
    def _enable_gap(self, item: InBetweenItem):
        """Enable a gap (keep in final video)."""
        item.enable()
        self.itemChanged.emit(item)
        self._has_unsaved_changes = True
        self._notify_parent_of_changes()
    
    def _disable_gap(self, item: InBetweenItem):
        """Disable a gap (remove from final video)."""
        item.disable()
        self.itemChanged.emit(item)
        self._has_unsaved_changes = True
        self._notify_parent_of_changes()
    
    def _enable_selected_gaps(self):
        """Enable all selected gaps."""
        selected_items = self.selectedItems()
        selected_gaps = [item for item in selected_items if isinstance(item, InBetweenItem)]
        
        for item in selected_gaps:
            if not item.is_enabled:
                item.enable()
                self.itemChanged.emit(item)
        
        if selected_gaps:
            self._has_unsaved_changes = True
            self._notify_parent_of_changes()
    
    def _disable_selected_gaps(self):
        """Disable all selected gaps."""
        selected_items = self.selectedItems()
        selected_gaps = [item for item in selected_items if isinstance(item, InBetweenItem)]
        
        for item in selected_gaps:
            if item.is_enabled:
                item.disable()
                self.itemChanged.emit(item)
        
        if selected_gaps:
            self._has_unsaved_changes = True
            self._notify_parent_of_changes()
    
    def _update_overlap_warnings(self):
        """Update yellow highlighting for sentences impacted by removed overlapping sentences."""
        for i, sentence in enumerate(self.sentences):
            has_warning = False
            
            # Check if any overlapping sentence from a different track is marked for removal
            if i in self._overlap_map:
                for overlap_idx in self._overlap_map[i]:
                    if overlap_idx < len(self.sentences):
                        overlapping_sentence = self.sentences[overlap_idx]
                        # If the overlapping sentence is removed and this one isn't,
                        # this sentence will be impacted
                        if overlapping_sentence.is_removed and not sentence.is_removed:
                            has_warning = True
                            break
            
            sentence.set_overlap_warning(has_warning)
    
    def _notify_parent_of_changes(self):
        """Notify parent VideoReviewWindow of unsaved changes."""
        self.sentence_modified.emit()
    
    def highlight_sentence_at_time(self, current_time: float):
        """Highlight the item (sentence or gap) that matches the current video time."""
        # Find item that contains current_time
        for i, item in enumerate(self.all_items):
            if isinstance(item, SentenceItem):
                if item.start_time <= current_time <= item.end_time:
                    if i != self.current_sentence_index:
                        self.current_sentence_index = i
                        # Clear previous selection
                        for j in range(self.count()):
                            self.item(j).setSelected(False)
                        # Select current
                        item.setSelected(True)
                        # Auto-scroll if needed
                        if not self.visualItemRect(item).intersects(self.viewport().rect()):
                            self.scrollToItem(item, QListWidget.ScrollHint.EnsureVisible)
                    return
            elif isinstance(item, InBetweenItem):
                if item.start_time <= current_time <= item.end_time:
                    if i != self.current_sentence_index:
                        self.current_sentence_index = i
                        # Clear previous selection
                        for j in range(self.count()):
                            self.item(j).setSelected(False)
                        # Select current
                        item.setSelected(True)
                        # Auto-scroll if needed
                        if not self.visualItemRect(item).intersects(self.viewport().rect()):
                            self.scrollToItem(item, QListWidget.ScrollHint.EnsureVisible)
                    return
        
        # No item found, clear selection
        if self.current_sentence_index >= 0:
            for j in range(self.count()):
                self.item(j).setSelected(False)
            self.current_sentence_index = -1
    
    def get_modified_sentences(self) -> List[Dict[str, Any]]:
        """Get all sentences with their current modification status."""
        return [item.sentence_data for item in self.sentences]
    
    def get_disabled_gaps(self) -> List[Tuple[float, float]]:
        """Get all disabled gaps (time ranges to remove)."""
        return [(gap.start_time, gap.end_time) for gap in self.gaps if not gap.is_enabled]
    
    def get_enabled_sentences(self) -> List[SentenceItem]:
        """Get all sentences that are NOT marked for removal."""
        return [s for s in self.sentences if not s.is_removed]
    
    def get_removed_sentences(self) -> List[Tuple[float, float]]:
        """Get time ranges of sentences marked for removal."""
        return [(s.start_time, s.end_time) for s in self.sentences if s.is_removed]


class PlaybackControlsWidget(QWidget):
    """Widget for video playback controls."""
    
    play_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    seek_requested = Signal(float)  # position in seconds
    speed_changed = Signal(float)  # playback rate
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.total_duration = 0.0  # Store total duration for seeking
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)  # Reduce margins
        layout.setSpacing(5)  # Reduce spacing
        
        # Play/Pause/Stop buttons
        self.play_button = QPushButton("▶")
        self.play_button.clicked.connect(self._on_play_clicked)
        self.is_playing = False
        
        self.stop_button = QPushButton("⏹")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        
        layout.addWidget(self.play_button)
        layout.addWidget(self.stop_button)
        
        # Seek slider
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setMinimum(0)
        self.seek_slider.setMaximum(1000)
        self.seek_slider.sliderPressed.connect(self._on_slider_pressed)
        self.seek_slider.sliderReleased.connect(self._on_slider_released)
        self.seek_slider.valueChanged.connect(self._on_slider_changed)
        self.slider_pressed = False
        
        layout.addWidget(self.seek_slider)
        
        # Time display
        self.time_label = QLabel("00:00 / 00:00")
        layout.addWidget(self.time_label)
        
        # Speed control
        speed_label = QLabel("Speed:")
        layout.addWidget(speed_label)
        
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5x", "1.0x", "2.0x", "4.0x", "8.0x"])
        self.speed_combo.setCurrentText("1.0x")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        layout.addWidget(self.speed_combo)
        
        layout.addStretch()
        self.setLayout(layout)
        
        # Set maximum height to make controls more compact
        self.setMaximumHeight(60)
        self.setMinimumHeight(50)
    
    def _on_play_clicked(self):
        """Handle play button click."""
        if self.is_playing:
            self.pause_requested.emit()
            self.play_button.setText("▶")
            self.is_playing = False
        else:
            self.play_requested.emit()
            self.play_button.setText("⏸")
            self.is_playing = True
    
    def set_playing(self, playing: bool):
        """Update play button state."""
        self.is_playing = playing
        self.play_button.setText("⏸" if playing else "▶")
    
    def _on_slider_pressed(self):
        """Handle slider press."""
        self.slider_pressed = True
    
    def _on_slider_released(self):
        """Handle slider release."""
        self.slider_pressed = False
        if self.total_duration > 0:
            # Convert slider value (0-1000) to actual seconds
            position = (self.seek_slider.value() / 1000.0) * self.total_duration
            self.seek_requested.emit(position)
    
    def _on_slider_changed(self, value: int):
        """Handle slider value change (only when dragging)."""
        if self.slider_pressed and self.total_duration > 0:
            # Convert slider value (0-1000) to actual seconds
            position = (value / 1000.0) * self.total_duration
            self.seek_requested.emit(position)
    
    def _on_speed_changed(self, text: str):
        """Handle speed combo change."""
        rate = float(text.replace('x', ''))
        self.speed_changed.emit(rate)
    
    def update_position(self, current: float, total: float):
        """Update slider and time display."""
        # Store total duration for seeking calculations
        if total > 0:
            self.total_duration = total
        
        if not self.slider_pressed:
            # Update slider (scale to 0-1000)
            if total > 0:
                slider_value = int((current / total) * 1000)
                self.seek_slider.setValue(slider_value)
        
        # Update time label
        current_str = self._format_time(current)
        total_str = self._format_time(total)
        self.time_label.setText(f"{current_str} / {total_str}")
    
    def _format_time(self, seconds: float) -> str:
        """Format seconds as MM:SS or HH:MM:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"
    
    def set_duration(self, duration: float):
        """Set total duration for slider."""
        # Slider is 0-1000, we'll scale based on duration
        pass  # Duration doesn't affect slider max, we keep it at 1000


class VideoReviewWindow(QMainWindow):
    """Main window for video review and sentence editing."""
    
    def __init__(self, video_path: Path, json_path: Path, original_video_path: Path = None, parent=None):
        super().__init__(parent)
        self.video_path = video_path  # Mixed audio file for UI playback
        self.original_video_path = original_video_path or video_path  # Original file for rendering
        self.json_path = json_path
        self.modifications: List[Dict[str, Any]] = []
        self.has_unsaved_changes = False
        
        self.setWindowTitle("Video Review - Auto Video Edit")
        self.setMinimumSize(1200, 700)
        
        # Create UI components
        self._create_menu_bar()
        self._create_toolbar()
        self._create_status_bar()
        self._create_main_widget()
        
        # Load data
        self._load_json()
        # Note: _load_video will be called after window is shown (in apply_edits.py)
        # This ensures VLC can embed properly
        
        # Setup update timer for position tracking
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._update_position)
        self.update_timer.start(100)  # Update every 100ms
        
        # Keyboard shortcuts
        self._setup_shortcuts()
    
    def _create_menu_bar(self):
        """Create menu bar."""
        menubar = self.menuBar()
        
        file_menu = menubar.addMenu("File")
        
        save_action = QAction("Save", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._save_json)
        file_menu.addAction(save_action)
        
        render_action = QAction("Render", self)
        render_action.setShortcut(QKeySequence("Ctrl+R"))
        render_action.triggered.connect(self._render_video)
        file_menu.addAction(render_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
    
    def _create_toolbar(self):
        """Create toolbar."""
        toolbar = QToolBar("Main Toolbar")
        self.addToolBar(toolbar)
        
        save_action = QAction("💾 Save", self)
        save_action.triggered.connect(self._save_json)
        toolbar.addAction(save_action)
        
        render_action = QAction("🎬 Render", self)
        render_action.triggered.connect(self._render_video)
        toolbar.addAction(render_action)
    
    def _create_status_bar(self):
        """Create status bar."""
        self.status_bar = self.statusBar()
        self.status_bar.showMessage("Ready")
    
    def _create_main_widget(self):
        """Create main widget with split layout."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Splitter for video and sentence list
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Video player (left)
        self.video_player = VideoPlayerWidget()
        splitter.addWidget(self.video_player)
        
        # Sentence list (right)
        self.sentence_list = SentenceListWidget()
        splitter.addWidget(self.sentence_list)
        
        # Set splitter sizes (70% video, 30% sentences)
        splitter.setSizes([700, 300])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        
        layout.addWidget(splitter)
        
        # Playback controls (bottom)
        self.playback_controls = PlaybackControlsWidget()
        from PySide6.QtWidgets import QSizePolicy
        size_policy = QSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed
        )
        self.playback_controls.setSizePolicy(size_policy)
        layout.addWidget(self.playback_controls)
        
        central_widget.setLayout(layout)
        
        # Connect signals
        self.playback_controls.play_requested.connect(self.video_player.play)
        self.playback_controls.pause_requested.connect(self.video_player.pause)
        self.playback_controls.stop_requested.connect(self.video_player.stop)
        self.playback_controls.seek_requested.connect(self.video_player.set_position_seconds)
        self.playback_controls.speed_changed.connect(self.video_player.set_playback_rate)
        
        self.video_player.position_changed.connect(self._on_video_position_changed)
        self.sentence_list.sentence_clicked.connect(self.video_player.set_position_seconds)
        self.sentence_list.sentence_modified.connect(lambda: setattr(self, 'has_unsaved_changes', True))
    
    def _setup_shortcuts(self):
        """Setup keyboard shortcuts."""
        # Space for play/pause
        space_shortcut = QShortcut(QKeySequence("Space"), self)
        space_shortcut.activated.connect(self._toggle_play_pause)
        
        # Left/Right arrows for seeking
        left_shortcut = QShortcut(QKeySequence("Left"), self)
        left_shortcut.activated.connect(lambda: self._seek_relative(-5))
        
        right_shortcut = QShortcut(QKeySequence("Right"), self)
        right_shortcut.activated.connect(lambda: self._seek_relative(5))
    
    def _toggle_play_pause(self):
        """Toggle play/pause."""
        if self.video_player.is_playing():
            self.video_player.pause()
            self.playback_controls.set_playing(False)
        else:
            self.video_player.play()
            self.playback_controls.set_playing(True)
    
    def _seek_relative(self, seconds: float):
        """Seek relative to current position."""
        current = self.video_player.get_position_seconds()
        new_pos = max(0, min(current + seconds, self.video_player.get_duration_seconds()))
        self.video_player.set_position_seconds(new_pos)
    
    def _load_json(self):
        """Load JSON modifications file."""
        try:
            with open(self.json_path, 'r') as f:
                self.modifications = json.load(f)
            
            if not isinstance(self.modifications, list):
                raise ValueError("JSON must contain a list")
            
            # Load sentences into list widget
            self.sentence_list.load_sentences(self.modifications)
            
            self.status_bar.showMessage(f"Loaded {len(self.modifications)} modification(s)")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load JSON file: {e}")
            sys.exit(1)
    
    def _load_video(self):
        """Load video file."""
        try:
            # Ensure window is shown so VLC can embed properly
            if not self.isVisible():
                self.show()
            QApplication.processEvents()  # Process events to ensure window is rendered
            
            self.video_player.load_video(self.video_path)
            self.status_bar.showMessage(f"Loaded video: {self.video_path.name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load video: {e}")
            sys.exit(1)
    
    def _update_position(self):
        """Update playback position display."""
        current = self.video_player.get_position_seconds()
        total = self.video_player.get_duration_seconds()
        
        # Only update if we have valid duration
        if total > 0:
            self.playback_controls.update_position(current, total)
            self.sentence_list.highlight_sentence_at_time(current)
    
    def _on_video_position_changed(self, position: float):
        """Handle video position change."""
        # This is handled by update_timer, but we can use it for immediate updates
        pass
    
    def _save_json(self):
        """Save modifications to JSON file."""
        try:
            # Get modified sentences
            modified_sentences = self.sentence_list.get_modified_sentences()
            
            # Update modifications list with new sentence states
            sentence_dict = {s.get('start_time'): s for s in modified_sentences}
            
            updated_modifications = []
            for mod in self.modifications:
                if mod.get('reason') == 'Sentence' and mod.get('start_time') in sentence_dict:
                    # Update with modified sentence
                    updated_modifications.append(sentence_dict[mod.get('start_time')])
                else:
                    # Keep original
                    updated_modifications.append(mod)
            
            # Sort by start_time
            updated_modifications.sort(key=lambda x: float(x.get('start_time', 0.0)))
            
            # Save to file
            with open(self.json_path, 'w') as f:
                json.dump(updated_modifications, f, indent=2)
            
            self.modifications = updated_modifications
            self.has_unsaved_changes = False
            self.sentence_list._has_unsaved_changes = False
            self.status_bar.showMessage("JSON file saved successfully")
            
            QMessageBox.information(self, "Saved", f"Changes saved to {self.json_path.name}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save JSON file: {e}")
    
    def _render_video(self):
        """Render the processed video."""
        # Collect segments to remove from various sources
        segments_to_remove: List[Tuple[float, float]] = []
        
        # 1. Collect disabled sentences (marked for removal in UI)
        removed_sentences = self.sentence_list.get_removed_sentences()
        segments_to_remove.extend(removed_sentences)
        
        # 2. Collect disabled gaps (gaps that user hasn't enabled)
        # NOTE: Gaps are calculated with padding, so they represent the actual gap boundaries
        # after padding is applied. We should remove these as-is.
        disabled_gaps = self.sentence_list.get_disabled_gaps()
        segments_to_remove.extend(disabled_gaps)
        
        # 3. Find silences that fall within enabled sentences
        enabled_sentences = self.sentence_list.get_enabled_sentences()
        silences_in_sentences = self._find_silences_within_sentences(enabled_sentences)
        segments_to_remove.extend(silences_in_sentences)
        
        # Merge overlapping segments
        segments_to_remove = self._merge_segments(segments_to_remove)
        
        if not segments_to_remove:
            QMessageBox.information(
                self, "No Segments to Remove",
                "No segments are marked for removal. Nothing to render."
            )
            return
        
        # Prompt for output file
        default_output = self.video_path.parent / f"{self.video_path.stem}_processed.mov"
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Processed Video",
            str(default_output),
            "Video Files (*.mkv *.mp4 *.avi *.mov *.webm);;All Files (*)"
        )
        
        if not output_path:
            return
        
        output_path = Path(output_path)
        
        # Create custom render options dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Render Options")
        layout = QVBoxLayout()
        
        # Confirmation message with breakdown
        total_duration = sum(end - start for start, end in segments_to_remove)
        message_text = f"Segments to remove:\n"
        message_text += f"  - Disabled sentences: {len(removed_sentences)}\n"
        message_text += f"  - Disabled gaps: {len(disabled_gaps)}\n"
        message_text += f"  - Silences in enabled sentences: {len(silences_in_sentences)}\n"
        message_text += f"\nTotal: {len(segments_to_remove)} segment(s), {total_duration:.1f} seconds\n\n"
        message_text += f"Output: {output_path.name}"
        
        message_label = QLabel(message_text)
        layout.addWidget(message_label)
        
        # Checkbox for re-encode video
        re_encode_checkbox = QCheckBox("Re-encode video for precise cuts")
        re_encode_checkbox.setToolTip(
            "Re-encoding allows precise cuts at any frame but is slower. "
            "Unchecked uses fast copy method (may have choppiness at cut points)."
        )
        re_encode_checkbox.setChecked(False)  # Default to unchecked (fast copy method)
        layout.addWidget(re_encode_checkbox)
        
        # Dialog buttons
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        dialog.setLayout(layout)
        
        # Show dialog and get result
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        re_encode_video = re_encode_checkbox.isChecked()
        
        # Show progress dialog
        self.progress_dialog = QProgressDialog("Rendering video...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setAutoReset(False)
        
        # Create worker thread (no longer using remove_space_between_sentences flag)
        self.render_worker = RenderWorker(
            self.original_video_path,
            output_path,
            segments_to_remove,
            remove_space_between_sentences=False,  # Deprecated, gaps handled in UI
            modifications=self.modifications,
            re_encode_video=re_encode_video,
            chunk_size=5.0
        )
        
        self.render_worker.progress.connect(self._on_render_progress)
        self.render_worker.finished.connect(self._on_render_finished)
        self.render_worker.error.connect(self._on_render_error)
        
        self.progress_dialog.canceled.connect(self.render_worker.cancel)
        
        # Start render
        self.progress_dialog.show()
        self.render_worker.start()
    
    def _find_silences_within_sentences(self, enabled_sentences: List[SentenceItem]) -> List[Tuple[float, float]]:
        """Find silence segments that fall within enabled sentences.
        
        Args:
            enabled_sentences: List of sentences that are NOT marked for removal
            
        Returns:
            List of (start_time, end_time) tuples for silences within sentences
        """
        silences_in_sentences = []
        
        # Get all silence (dead air) segments from modifications
        silence_segments = [
            (float(mod.get('start_time', 0.0)), float(mod.get('end_time', 0.0)))
            for mod in self.modifications
            if mod.get('reason') == 'dead air'
        ]
        
        # For each silence, check if it falls within any enabled sentence
        for silence_start, silence_end in silence_segments:
            for sentence in enabled_sentences:
                # Check if silence is fully contained within sentence
                if silence_start >= sentence.start_time and silence_end <= sentence.end_time:
                    silences_in_sentences.append((silence_start, silence_end))
                    break  # Found a containing sentence, no need to check others
                # Check if silence partially overlaps with sentence (clip to sentence bounds)
                elif silence_start < sentence.end_time and silence_end > sentence.start_time:
                    # Calculate the overlapping portion
                    overlap_start = max(silence_start, sentence.start_time)
                    overlap_end = min(silence_end, sentence.end_time)
                    if overlap_end > overlap_start:
                        silences_in_sentences.append((overlap_start, overlap_end))
                        break
        
        return silences_in_sentences
    
    def _merge_segments(self, segments: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Merge overlapping or adjacent segments.
        
        Args:
            segments: List of (start_time, end_time) tuples
            
        Returns:
            List of merged (start_time, end_time) tuples
        """
        if not segments:
            return []
        
        # Sort by start time
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
    
    def _on_render_progress(self, percentage: int, message: str):
        """Handle render progress update."""
        self.progress_dialog.setValue(percentage)
        self.progress_dialog.setLabelText(message)
        self.status_bar.showMessage(f"Rendering: {message}")
    
    def _on_render_finished(self, success: bool, message: str):
        """Handle render completion."""
        self.progress_dialog.close()
        
        if success:
            QMessageBox.information(self, "Render Complete", message)
            self.status_bar.showMessage("Render complete")
        else:
            QMessageBox.warning(self, "Render Cancelled", message)
            self.status_bar.showMessage("Render cancelled")
    
    def _on_render_error(self, error_message: str):
        """Handle render error."""
        self.progress_dialog.close()
        QMessageBox.critical(self, "Render Error", f"Failed to render video:\n{error_message}")
        self.status_bar.showMessage("Render failed")
    
    def closeEvent(self, event):
        """Handle window close event."""
        if self.has_unsaved_changes:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes. Do you want to save before closing?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel
            )
            
            if reply == QMessageBox.StandardButton.Save:
                self._save_json()
                event.accept()
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
            else:
                event.accept()
        else:
            event.accept()
        
        # Clean up temporary video file if it exists
        self.video_player.cleanup()
        
        # Clean up temporary video file if it exists
        self.video_player.cleanup()


def main():
    """Main entry point for UI."""
    app = QApplication(sys.argv)
    
    # For testing - would normally get from command line
    if len(sys.argv) < 3:
        print("Usage: video_review_ui.py <video_file> <json_file>")
        sys.exit(1)
    
    video_path = Path(sys.argv[1])
    json_path = Path(sys.argv[2])
    
    window = VideoReviewWindow(video_path, json_path)
    window.show()
    # Use a short timer to ensure the window is fully mapped to X11
    # before VLC tries to embed into it (winId() must be valid)
    QTimer.singleShot(300, window._load_video)
    
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

