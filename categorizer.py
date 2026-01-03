"""Categorization module for analyzing transcripts and identifying topic sections."""

from typing import List, Dict, Any, Tuple
import os
import json


class Categorizer:
    """Handles categorization of video segments using OpenAI API."""
    
    def __init__(self, model: str = "gpt-4", api_key: str = None):
        """Initialize categorizer with OpenAI API.
        
        Args:
            model: OpenAI model to use ("gpt-4", "gpt-3.5-turbo", etc.)
            api_key: OpenAI API key (defaults to OPENAI_API_KEY environment variable)
        """
        self.model = model
        self.api_key = api_key or os.getenv('OPENAI_API_KEY')
        
        if not self.api_key:
            raise RuntimeError(
                "OpenAI API key not found. Set OPENAI_API_KEY environment variable "
                "or pass api_key parameter."
            )
    
    def _chunk_transcript(self, segments: List[Dict[str, Any]], max_chunk_duration: float = 600.0) -> List[List[Dict[str, Any]]]:
        """Chunk transcript into manageable pieces for API calls.
        
        Args:
            segments: List of transcript segments
            max_chunk_duration: Maximum duration per chunk in seconds (default: 10 minutes)
            
        Returns:
            List of chunked segment lists
        """
        if not segments:
            return []
        
        chunks = []
        current_chunk = []
        current_duration = 0.0
        
        for segment in segments:
            segment_duration = segment['end'] - segment['start']
            
            # If adding this segment would exceed max duration, start a new chunk
            if current_chunk and current_duration + segment_duration > max_chunk_duration:
                chunks.append(current_chunk)
                current_chunk = [segment]
                current_duration = segment_duration
            else:
                current_chunk.append(segment)
                current_duration += segment_duration
        
        # Add the last chunk
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks
    
    def _create_prompt(self, segments: List[Dict[str, Any]]) -> str:
        """Create prompt for OpenAI API to categorize transcript segments.
        
        Args:
            segments: List of transcript segments to categorize
            
        Returns:
            Prompt string for the API
        """
        # Build transcript text with timestamps
        transcript_lines = []
        for seg in segments:
            transcript_lines.append(f"[{seg['start']:2f} - {seg['end']:2f}] {seg['text']}")
        
        transcript_text = "\n".join(transcript_lines)
        
        prompt = f"""Analyze the following video transcript and identify distinct topic sections. For each minor topic or theme, provide:

1. Start time (in seconds)
2. End time (in seconds)  
3. A brief description of the topic/content covered (4-15 words)

Focus on identifying granular topic breaks where the content shifts to a new subject.
This could even be a 5 second sidebar discussion. For coding tutorials, this might be:
- Introduction to concept of recursion
- Mention of a few use cases for recursion
- Comment about my cat
- Walk through example of recursion (factorial)
- Distracted by an error in the recursion example
- Finishing up the recursion example

Transcript:
{transcript_text}

Respond with a JSON object containing a "categories" array. Each item in the array should be an object with "start_time", "end_time", and "description" fields. The sections should cover the entire transcript without gaps or overlaps.

Example format:
{{
  "categories": [
    {{"start_time": 0.0, "end_time": 25.5, "description": "Introduction to concept of recursion"}},
    {{"start_time": 25.5, "end_time": 40.2, "description": "Mention of a few use cases for recursion"}},
    {{"start_time": 40.2, "end_time": 45.7, "description": "Comment about my cat"}},
  ]
}}

JSON response:"""
        
        return prompt
    
    def _call_openai_api(self, prompt: str) -> List[Dict[str, Any]]:
        """Call OpenAI API to categorize transcript.
        
        Args:
            prompt: Prompt string for the API
            
        Returns:
            List of category dictionaries with start_time, end_time, description
        """
        try:
            from openai import OpenAI
            
            client = OpenAI(api_key=self.api_key)
            
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that analyzes video transcripts and identifies topic sections. Always respond with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,  # Lower temperature for more consistent categorization
            )
            
            # Parse response
            content = response.choices[0].message.content
            
            # Debug: Print first 500 chars of response
            print(f"  API response (first 500 chars): {content[:500]}")
            
            try:
                # Try to parse as JSON object first
                result = json.loads(content)
                
                # If it's an object with a key, try to extract array
                if isinstance(result, dict):
                    # Look for common keys that might contain the array
                    for key in ['categories', 'sections', 'topics', 'result']:
                        if key in result and isinstance(result[key], list):
                            return result[key]
                    # If no array found, log what we got
                    print(f"  Warning: JSON object keys: {list(result.keys())}")
                    return []
                elif isinstance(result, list):
                    return result
                else:
                    print(f"  Warning: Unexpected JSON type: {type(result)}")
                    return []
            except json.JSONDecodeError as e:
                print(f"  Warning: JSON decode error: {e}")
                # Try to extract JSON array from text response
                # Sometimes the API wraps it in markdown code blocks
                import re
                json_match = re.search(r'\[.*\]', content, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                raise ValueError(f"Failed to parse JSON from API response: {content}")
                
        except ImportError:
            raise RuntimeError(
                "OpenAI library not installed. Install with: pip install openai"
            )
        except Exception as e:
            raise RuntimeError(f"OpenAI API call failed: {e}")
    
    def categorize(self, transcript_segments: List[Dict[str, Any]], video_duration: float) -> List[Tuple[float, float, str]]:
        """Categorize transcript segments into topic sections.
        
        Args:
            transcript_segments: List of transcript segments with 'start', 'end', 'text'
            video_duration: Total video duration in seconds
            
        Returns:
            List of (start_time, end_time, category_description) tuples
        """
        if not transcript_segments:
            return []
        
        # Chunk transcript to stay within API token limits
        chunks = self._chunk_transcript(transcript_segments)
        
        all_categories = []
        
        for chunk_idx, chunk in enumerate(chunks):
            print(f"  Categorizing chunk {chunk_idx + 1}/{len(chunks)}...")
            
            # Create prompt for this chunk
            prompt = self._create_prompt(chunk)
            
            # Call API
            try:
                categories = self._call_openai_api(prompt)
                
                if not categories:
                    print(f"  Warning: No categories returned for chunk {chunk_idx + 1}")
                
                # Convert to tuples and add to results
                for cat in categories:
                    start_time = float(cat.get('start_time', 0.0))
                    end_time = float(cat.get('end_time', video_duration))
                    description = cat.get('description', '').strip()
                    
                    if description:  # Only add non-empty categories
                        all_categories.append((start_time, end_time, description))
                        
            except Exception as e:
                print(f"  Error: Failed to categorize chunk {chunk_idx + 1}: {e}")
                import traceback
                traceback.print_exc()
                # Continue with next chunk
                continue
        
        # Sort by start time
        all_categories.sort(key=lambda x: x[0])
        
        # Merge overlapping or adjacent categories if needed
        # (Simple approach: just return sorted list, API should handle boundaries)
        
        return all_categories

