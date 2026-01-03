"""JSON logging module for tracking video modifications."""

import json
from typing import List, Dict, Any
from pathlib import Path


class JSONLogger:
    """Handles generation of JSON logs for video modifications."""
    
    def __init__(self):
        self.modifications: List[Dict[str, Any]] = []
    
    def add_removal(self, start_time: float, end_time: float, reason: str = "dead air"):
        """Add a removal entry to the log.
        
        Args:
            start_time: Start timestamp of removed segment (seconds)
            end_time: End timestamp of removed segment (seconds)
            reason: Reason for removal (default: "dead air")
        """
        duration = end_time - start_time
        entry = {
            "start_time": round(start_time, 3),
            "end_time": round(end_time, 3),
            "modification": "REMOVED",
            "reason": reason,
            "duration": round(duration, 3)
        }
        self.modifications.append(entry)
    
    def add_categorization(self, start_time: float, end_time: float, reason: str):
        """Add a categorization entry to the log.
        
        Args:
            start_time: Start timestamp of categorized segment (seconds)
            end_time: End timestamp of categorized segment (seconds)
            reason: Category description/topic
        """
        duration = end_time - start_time
        entry = {
            "start_time": round(start_time, 3),
            "end_time": round(end_time, 3),
            "modification": "CATEGORIZED",
            "reason": reason,
            "duration": round(duration, 3)
        }
        self.modifications.append(entry)
    
    def get_modifications(self) -> List[Dict[str, Any]]:
        """Get all logged modifications.
        
        Returns:
            List of modification dictionaries
        """
        return sorted(self.modifications, key=lambda x: x["start_time"])
    
    def save(self, output_path: Path):
        """Save modifications to JSON file.
        
        Args:
            output_path: Path to output JSON file
        """
        output_path = Path(output_path)
        with open(output_path, 'w') as f:
            json.dump(self.get_modifications(), f, indent=2)
    
    def clear(self):
        """Clear all logged modifications."""
        self.modifications = []

