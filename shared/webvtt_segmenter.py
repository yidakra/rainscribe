#!/usr/bin/env python3
"""
WebVTT Segmenter Module

This module provides functions for segmenting WebVTT subtitle content
to align with HLS video segments for better synchronization.
"""

import os
import re
import time
import logging
import tempfile
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Any, Generator, Union

# Import local modules
try:
    from shared.reference_clock import get_time as get_reference_time
except ImportError:
    # Fallback to system time if reference clock not available
    get_reference_time = time.time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("webvtt-segmenter")

# Constants
DEFAULT_SEGMENT_DURATION = 6.0  # Default segment duration in seconds
TIME_PATTERN = re.compile(r'(\d{2}):(\d{2}):(\d{2})\.(\d{3})')
CUE_PATTERN = re.compile(r'(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})')
VTT_HEADER = "WEBVTT\n\n"

class WebVTTCue:
    """Represents a single WebVTT cue with timing and text."""
    
    def __init__(self, cue_id: str, start_time: float, end_time: float, text: str):
        """
        Initialize a WebVTT cue.
        
        Args:
            cue_id: The ID of the cue
            start_time: Start time in seconds
            end_time: End time in seconds
            text: The cue text content
        """
        self.cue_id = cue_id
        self.start_time = start_time
        self.end_time = end_time
        self.text = text.strip()
    
    def __repr__(self) -> str:
        return f"WebVTTCue(id={self.cue_id}, {self.start_time:.3f} --> {self.end_time:.3f}, text='{self.text[:20]}...')"
    
    def to_vtt(self, offset: float = 0.0) -> str:
        """
        Convert cue to WebVTT format.
        
        Args:
            offset: Time offset to apply (in seconds)
            
        Returns:
            str: Formatted WebVTT cue
        """
        start_with_offset = max(0, self.start_time + offset)
        end_with_offset = max(start_with_offset + 0.1, self.end_time + offset)
        
        return (
            f"{self.cue_id}\n"
            f"{format_timestamp(start_with_offset)} --> {format_timestamp(end_with_offset)}\n"
            f"{self.text}\n\n"
        )
    
    def overlaps(self, start_time: float, end_time: float) -> bool:
        """
        Check if cue overlaps with the given time range.
        
        Args:
            start_time: Range start time in seconds
            end_time: Range end time in seconds
            
        Returns:
            bool: True if the cue overlaps with the range
        """
        return (self.start_time < end_time) and (self.end_time > start_time)
    
    def clip_to_range(self, start_time: float, end_time: float) -> Optional['WebVTTCue']:
        """
        Create a new cue that's clipped to the given time range.
        
        Args:
            start_time: Range start time in seconds
            end_time: Range end time in seconds
            
        Returns:
            Optional[WebVTTCue]: A new cue clipped to the range, or None if no overlap
        """
        if not self.overlaps(start_time, end_time):
            return None
        
        new_start = max(self.start_time, start_time)
        new_end = min(self.end_time, end_time)
        
        return WebVTTCue(
            cue_id=self.cue_id,
            start_time=new_start,
            end_time=new_end,
            text=self.text
        )

class WebVTTSegmenter:
    """Segments WebVTT content into HLS-compatible chunks."""
    
    def __init__(self, 
                 segment_duration: float = DEFAULT_SEGMENT_DURATION,
                 reference_start_time: Optional[float] = None,
                 segment_overlap: float = 0.5):
        """
        Initialize the WebVTT segmenter.
        
        Args:
            segment_duration: Duration of each segment in seconds
            reference_start_time: Reference start time, if None uses current time
            segment_overlap: How much to overlap cues across segment boundaries
        """
        self.segment_duration = segment_duration
        self.reference_start_time = reference_start_time or get_reference_time()
        self.segment_overlap = segment_overlap
        self.cues: List[WebVTTCue] = []
        
    def add_cue(self, cue: WebVTTCue) -> None:
        """
        Add a cue to the segmenter.
        
        Args:
            cue: The WebVTTCue to add
        """
        self.cues.append(cue)
        self.cues.sort(key=lambda c: c.start_time)  # Keep cues sorted by start time
    
    def add_cues(self, cues: List[WebVTTCue]) -> None:
        """
        Add multiple cues to the segmenter.
        
        Args:
            cues: List of WebVTTCue objects to add
        """
        self.cues.extend(cues)
        self.cues.sort(key=lambda c: c.start_time)  # Keep cues sorted by start time
    
    def add_vtt_content(self, vtt_content: str, offset: float = 0.0) -> int:
        """
        Parse VTT content and add all cues to the segmenter.
        
        Args:
            vtt_content: WebVTT content as string
            offset: Time offset to apply to all cues (in seconds)
            
        Returns:
            int: Number of cues added
        """
        cues = parse_vtt_content(vtt_content, offset)
        self.add_cues(cues)
        return len(cues)
    
    def get_segment_cues(self, segment_index: int) -> List[WebVTTCue]:
        """
        Get cues for a specific segment.
        
        Args:
            segment_index: The segment index
            
        Returns:
            List[WebVTTCue]: Cues that belong in this segment
        """
        segment_start_time = self.get_segment_start_time(segment_index)
        segment_end_time = segment_start_time + self.segment_duration
        
        # Include slight overlap to handle cues at segment boundaries
        search_start = segment_start_time - self.segment_overlap
        search_end = segment_end_time + self.segment_overlap
        
        # Find cues that overlap with this segment's time range
        return [
            cue for cue in self.cues
            if cue.overlaps(search_start, search_end)
        ]
    
    def get_segment_content(self, segment_index: int, offset: float = 0.0) -> str:
        """
        Generate WebVTT content for a specific segment.
        
        Args:
            segment_index: The segment index
            offset: Additional time offset to apply (in seconds)
            
        Returns:
            str: WebVTT content for this segment
        """
        segment_cues = self.get_segment_cues(segment_index)
        
        # Start with WebVTT header
        content = VTT_HEADER
        
        # Segment metadata
        segment_start_time = self.get_segment_start_time(segment_index)
        content += f"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{int(segment_start_time * 90000)}\n\n"
        
        # Adjust timestamps relative to segment start
        for i, cue in enumerate(segment_cues):
            # Create a copy of the cue with adjusted times
            segment_relative_start = cue.start_time - segment_start_time
            segment_relative_end = cue.end_time - segment_start_time
            relative_cue = WebVTTCue(
                cue_id=f"{segment_index}-{i+1}",
                start_time=segment_relative_start,
                end_time=segment_relative_end,
                text=cue.text
            )
            content += relative_cue.to_vtt(offset)
        
        return content
    
    def generate_segment_file(self, segment_index: int, output_dir: str, 
                             filename_template: str = "subtitles_{index}.vtt",
                             offset: float = 0.0) -> Optional[str]:
        """
        Generate a WebVTT segment file.
        
        Args:
            segment_index: The segment index
            output_dir: Directory to save the segment file
            filename_template: Template for segment filenames
            offset: Additional time offset to apply (in seconds)
            
        Returns:
            Optional[str]: Path to the generated file, or None if no cues
        """
        segment_cues = self.get_segment_cues(segment_index)
        if not segment_cues:
            logger.debug(f"No cues for segment {segment_index}, skipping file generation")
            return None
        
        # Generate filename
        filename = filename_template.format(index=segment_index)
        filepath = os.path.join(output_dir, filename)
        
        # Ensure directory exists
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate and write the segment content
        content = self.get_segment_content(segment_index, offset)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.debug(f"Generated segment file: {filepath} with {len(segment_cues)} cues")
        return filepath
    
    def generate_all_segments(self, start_index: int, end_index: int, output_dir: str,
                             filename_template: str = "subtitles_{index}.vtt",
                             offset: float = 0.0) -> List[str]:
        """
        Generate segment files for a range of indices.
        
        Args:
            start_index: Starting segment index (inclusive)
            end_index: Ending segment index (inclusive)
            output_dir: Directory to save segment files
            filename_template: Template for segment filenames
            offset: Additional time offset to apply (in seconds)
            
        Returns:
            List[str]: Paths to the generated files
        """
        generated_files = []
        
        for i in range(start_index, end_index + 1):
            filepath = self.generate_segment_file(
                segment_index=i,
                output_dir=output_dir,
                filename_template=filename_template,
                offset=offset
            )
            if filepath:
                generated_files.append(filepath)
        
        return generated_files
    
    def generate_playlist(self, start_index: int, end_index: int, 
                         output_path: str, segment_template: str = "subtitles_{index}.vtt") -> str:
        """
        Generate an HLS playlist (m3u8) for the subtitle segments.
        
        Args:
            start_index: Starting segment index (inclusive)
            end_index: Ending segment index (inclusive)
            output_path: Path to save the playlist file
            segment_template: Template for segment filenames
            
        Returns:
            str: Path to the generated playlist file
        """
        # Ensure directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Generate m3u8 content
        content = "#EXTM3U\n"
        content += f"#EXT-X-TARGETDURATION:{int(self.segment_duration)}\n"
        content += "#EXT-X-VERSION:3\n"
        content += f"#EXT-X-MEDIA-SEQUENCE:{start_index}\n"
        
        for i in range(start_index, end_index + 1):
            content += f"#EXTINF:{self.segment_duration:.3f},\n"
            content += segment_template.format(index=i) + "\n"
        
        # No need to add #EXT-X-ENDLIST for live streams
        
        # Write the playlist file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.info(f"Generated subtitle playlist: {output_path}")
        return output_path
    
    def get_segment_start_time(self, segment_index: int) -> float:
        """
        Calculate the start time for a segment.
        
        Args:
            segment_index: The segment index
            
        Returns:
            float: The start time in seconds
        """
        return self.reference_start_time + (segment_index * self.segment_duration)
    
    def get_segment_for_time(self, timestamp: float) -> int:
        """
        Calculate which segment a specific timestamp belongs to.
        
        Args:
            timestamp: The timestamp in seconds
            
        Returns:
            int: The segment index
        """
        if timestamp < self.reference_start_time:
            return 0
        
        return int((timestamp - self.reference_start_time) / self.segment_duration)

def parse_vtt_content(vtt_content: str, offset: float = 0.0) -> List[WebVTTCue]:
    """
    Parse WebVTT content and extract cues.
    
    Args:
        vtt_content: The WebVTT content as a string
        offset: Time offset to apply to all cues (in seconds)
        
    Returns:
        List[WebVTTCue]: List of parsed cues
    """
    lines = vtt_content.strip().split('\n')
    cues = []
    
    # Skip header (WEBVTT)
    i = 0
    while i < len(lines) and not re.match(CUE_PATTERN, lines[i]):
        i += 1
    
    # Parse cues
    while i < len(lines):
        # Skip empty lines
        if not lines[i].strip():
            i += 1
            continue
        
        # Look for cue timing
        cue_match = re.match(CUE_PATTERN, lines[i])
        if cue_match:
            start_str, end_str = cue_match.groups()
            start_time = parse_timestamp(start_str) + offset
            end_time = parse_timestamp(end_str) + offset
            
            # Cue ID is the line before timing, if it exists and not another timing
            cue_id = str(len(cues) + 1)
            if i > 0 and not re.match(CUE_PATTERN, lines[i-1]) and lines[i-1].strip():
                cue_id = lines[i-1].strip()
                
            # Collect cue text (all lines until empty line or next cue)
            cue_text = []
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(CUE_PATTERN, lines[i]):
                cue_text.append(lines[i])
                i += 1
            
            # Create cue and add to list
            cue = WebVTTCue(
                cue_id=cue_id,
                start_time=start_time,
                end_time=end_time,
                text='\n'.join(cue_text)
            )
            cues.append(cue)
        else:
            i += 1
    
    return cues

def parse_timestamp(timestamp: str) -> float:
    """
    Parse a WebVTT timestamp to seconds.
    
    Args:
        timestamp: Timestamp in format "HH:MM:SS.mmm"
        
    Returns:
        float: Time in seconds
    """
    match = TIME_PATTERN.match(timestamp)
    if not match:
        raise ValueError(f"Invalid timestamp format: {timestamp}")
    
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0

def format_timestamp(seconds: float) -> str:
    """
    Format seconds as a WebVTT timestamp.
    
    Args:
        seconds: Time in seconds
        
    Returns:
        str: Formatted timestamp "HH:MM:SS.mmm"
    """
    if seconds < 0:
        seconds = 0
    
    hours = int(seconds / 3600)
    seconds %= 3600
    minutes = int(seconds / 60)
    seconds %= 60
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"

def create_empty_vtt_file(output_path: str) -> str:
    """
    Create an empty WebVTT file.
    
    Args:
        output_path: Path to save the file
        
    Returns:
        str: Path to the created file
    """
    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # Write basic WebVTT header
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(VTT_HEADER)
    
    logger.debug(f"Created empty WebVTT file: {output_path}")
    return output_path

if __name__ == "__main__":
    # Test the WebVTT segmenter
    # Create some sample cues
    cues = [
        WebVTTCue("1", 0.5, 3.5, "Hello, this is the first subtitle"),
        WebVTTCue("2", 4.0, 6.5, "This is the second subtitle"),
        WebVTTCue("3", 6.0, 9.0, "This overlaps segments"),
        WebVTTCue("4", 10.0, 13.0, "This is in a later segment"),
    ]
    
    # Create a segmenter with 5-second segments
    segmenter = WebVTTSegmenter(segment_duration=5.0, reference_start_time=0.0)
    segmenter.add_cues(cues)
    
    # Create a temporary directory for output
    with tempfile.TemporaryDirectory() as tmpdir:
        # Generate segments
        segments = segmenter.generate_all_segments(0, 2, tmpdir)
        print(f"Generated {len(segments)} segment files:")
        for seg in segments:
            print(f"  - {os.path.basename(seg)}")
        
        # Generate playlist
        playlist = segmenter.generate_playlist(0, 2, os.path.join(tmpdir, "subtitles.m3u8"))
        print(f"Generated playlist: {os.path.basename(playlist)}")
        
        # Print contents of first segment
        if segments:
            with open(segments[0], 'r', encoding='utf-8') as f:
                print("\nFirst segment content:")
                print(f.read()) 