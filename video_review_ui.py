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
    QMenuBar, QMenu, QToolBar, QStatusBar, QDialog, QDialogButtonBox
)
from PySide6.QtCore import Qt, QUrl, Signal, QThread, QTimer, QSize
from PySide6.QtGui import QAction, QKeySequence, QShortcut
import vlc
import platform

from video_processor import VideoProcessor


class RenderWorker(QThread):
    """Worker thread for video rendering to keep UI responsive."""
    
    progress = Signal(int, str)  # progress percentage, status message
    finished = Signal(bool, str)  # success, message
    error = Signal(str)  # error message
    
    def __init__(self, input_path: Path, output_path: Path, 
                 segments_to_remove: List[Tuple[float, float]], 
                 chunk_size: float = 5.0):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.segments_to_remove = segments_to_remove
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
            
            # Process video (we can't easily intercept progress from remove_segments,
            # so we'll estimate based on time)
            import time
            start_time = time.time()
            
            processor.remove_segments(
                self.input_path,
                self.output_path,
                self.segments_to_remove,
                chunk_size=self.chunk_size
            )
            
            if self._cancelled:
                self.finished.emit(False, "Render cancelled")
                return
            
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
        self.video_widget = QWidget()
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
    """Widget for displaying and managing sentences."""
    
    sentence_clicked = Signal(float)  # start_time in seconds
    sentence_modified = Signal()  # emitted when a sentence is marked/unmarked
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sentences: List[SentenceItem] = []
        self.current_sentence_index = -1
        self._has_unsaved_changes = False
        
        self.itemClicked.connect(self._on_item_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
    
    def load_sentences(self, modifications: List[Dict[str, Any]]):
        """Load sentences from modifications list."""
        self.clear()
        self.sentences = []
        self._has_unsaved_changes = False
        
        # Filter for sentences only
        sentence_mods = [
            mod for mod in modifications
            if mod.get('modification') in ('NONE', 'REMOVED') and mod.get('reason') == 'Sentence'
        ]
        
        # Sort by start_time
        sentence_mods.sort(key=lambda x: float(x.get('start_time', 0.0)))
        
        for mod in sentence_mods:
            item = SentenceItem(mod)
            self.sentences.append(item)
            self.addItem(item)
    
    def _on_item_clicked(self, item: SentenceItem):
        """Handle sentence click - seek to timestamp."""
        self.sentence_clicked.emit(item.start_time)
        self._center_item(item)
    
    def _center_item(self, item: SentenceItem):
        """Center the clicked item in the view."""
        index = self.row(item)
        self.scrollToItem(item, QListWidget.ScrollHint.PositionAtCenter)
        self.setCurrentItem(item)
    
    def _show_context_menu(self, position):
        """Show context menu for sentence actions."""
        item = self.itemAt(position)
        if not isinstance(item, SentenceItem):
            return
        
        menu = QMenu(self)
        
        if item.is_removed:
            unmark_action = menu.addAction("Unmark for removal")
            unmark_action.triggered.connect(lambda: self._unmark_sentence(item))
        else:
            mark_action = menu.addAction("Mark for removal")
            mark_action.triggered.connect(lambda: self._mark_sentence(item))
        
        menu.exec(self.mapToGlobal(position))
    
    def _mark_sentence(self, item: SentenceItem):
        """Mark a sentence for removal."""
        item.mark_for_removal()
        self.itemChanged.emit(item)
        self._has_unsaved_changes = True
        self._notify_parent_of_changes()
    
    def _unmark_sentence(self, item: SentenceItem):
        """Unmark a sentence for removal."""
        item.unmark_for_removal()
        self.itemChanged.emit(item)
        self._has_unsaved_changes = True
        self._notify_parent_of_changes()
    
    def _notify_parent_of_changes(self):
        """Notify parent VideoReviewWindow of unsaved changes."""
        self.sentence_modified.emit()
    
    def highlight_sentence_at_time(self, current_time: float):
        """Highlight the sentence that matches the current video time."""
        # Find sentence that contains current_time
        for i, item in enumerate(self.sentences):
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
        
        # No sentence found, clear selection
        if self.current_sentence_index >= 0:
            for j in range(self.count()):
                self.item(j).setSelected(False)
            self.current_sentence_index = -1
    
    def get_modified_sentences(self) -> List[Dict[str, Any]]:
        """Get all sentences with their current modification status."""
        return [item.sentence_data for item in self.sentences]


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
    
    def __init__(self, video_path: Path, json_path: Path, parent=None):
        super().__init__(parent)
        self.video_path = video_path
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
        # Collect all REMOVED segments
        removed_segments = []
        for mod in self.modifications:
            if mod.get('modification') == 'REMOVED':
                start_time = float(mod.get('start_time', 0.0))
                end_time = float(mod.get('end_time', 0.0))
                removed_segments.append((start_time, end_time))
        
        if not removed_segments:
            QMessageBox.information(
                self, "No Segments to Remove",
                "No segments are marked for removal. Nothing to render."
            )
            return
        
        # Prompt for output file
        default_output = self.video_path.parent / f"{self.video_path.stem}_processed{self.video_path.suffix}"
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Processed Video",
            str(default_output),
            "Video Files (*.mkv *.mp4 *.avi *.mov *.webm);;All Files (*)"
        )
        
        if not output_path:
            return
        
        output_path = Path(output_path)
        
        # Confirmation dialog
        total_duration = sum(end - start for start, end in removed_segments)
        reply = QMessageBox.question(
            self,
            "Confirm Render",
            f"Remove {len(removed_segments)} segment(s) totaling {total_duration:.1f} seconds?\n\n"
            f"Output: {output_path.name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        # Show progress dialog
        self.progress_dialog = QProgressDialog("Rendering video...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setAutoReset(False)
        
        # Create worker thread
        self.render_worker = RenderWorker(
            self.video_path,
            output_path,
            removed_segments,
            chunk_size=5.0
        )
        
        self.render_worker.progress.connect(self._on_render_progress)
        self.render_worker.finished.connect(self._on_render_finished)
        self.render_worker.error.connect(self._on_render_error)
        
        self.progress_dialog.canceled.connect(self.render_worker.cancel)
        
        # Start render
        self.progress_dialog.show()
        self.render_worker.start()
    
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
    
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

