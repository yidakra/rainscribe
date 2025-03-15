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
import math
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Any, Generator, Union

# Import local modules
try:
    from shared.reference_clock import get_time as get_reference_time, get_global_clock
except ImportError:
    # Fallback to system time if reference clock not available
    get_reference_time = time.time
    get_global_clock = None

# Configure logging
logger = logging.getLogger('webvtt-segmenter')
logger.setLevel(logging.DEBUG)

# Constants from environment variables with defaults
DEFAULT_SEGMENT_DURATION = float(os.getenv("WEBVTT_SEGMENT_DURATION", "10.0"))  # Default segment duration in seconds
DEFAULT_SEGMENT_OVERLAP = float(os.getenv("WEBVTT_SEGMENT_OVERLAP", "1.0"))  # Default overlap between segments in seconds
REFERENCE_TIME_SOURCE = os.getenv("WEBVTT_TIME_SOURCE", "reference_clock")  # 'reference_clock' or 'system'
HLS_CLOCK_SYNC = os.getenv("HLS_CLOCK_SYNC", "true").lower() in ("true", "1", "yes")  # Synchronize with HLS segments

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
        
        # Ensure the text is stripped of any extra whitespace
        clean_text = self.text.strip()
        
        return (
            f"{self.cue_id}\n"
            f"{format_timestamp(start_with_offset)} --> {format_timestamp(end_with_offset)}\n"
            f"{clean_text}\n\n"
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
        overlaps = (self.start_time < end_time) and (self.end_time > start_time)
        # Print all cue timestamps for debugging
        logger.debug(f"Checking cue {self.cue_id} [{self.start_time:.3f} - {self.end_time:.3f}] against segment [{start_time:.3f} - {end_time:.3f}], overlaps: {overlaps}")
        return overlaps
    
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
            
        new_start = max(start_time, self.start_time)
        new_end = min(end_time, self.end_time)
        
        return WebVTTCue(
            self.cue_id,
            new_start,
            new_end,
            self.text
        )

class WebVTTSegmenter:
    """
    Segments WebVTT content into multiple files that align with HLS segments.
    """
    
    def __init__(self, 
                 segment_duration: float = DEFAULT_SEGMENT_DURATION,
                 reference_start_time: Optional[float] = None,
                 segment_overlap: float = DEFAULT_SEGMENT_OVERLAP,
                 use_reference_clock: bool = REFERENCE_TIME_SOURCE == 'reference_clock',
                 sync_with_hls: bool = HLS_CLOCK_SYNC):
        """
        Initialize the segmenter.
        
        Args:
            segment_duration: Duration of each segment in seconds
            reference_start_time: The start time of the first segment
            segment_overlap: How much overlap to add between segments
            use_reference_clock: Whether to use reference clock for timing
            sync_with_hls: Whether to sync segment boundaries with HLS segments
        """
        self.segment_duration = segment_duration
        self.segment_overlap = segment_overlap
        self.use_reference_clock = use_reference_clock
        self.sync_with_hls = sync_with_hls
        
        # Set the reference start time
        if reference_start_time is None:
            # Use the current reference time as the start time
            if self.use_reference_clock and get_global_clock:
                # Use reference clock if available
                reference_start_time = get_global_clock().get_time()
                logger.info(f"Using reference clock time as start time: {reference_start_time:.3f}s")
            else:
                # Fallback to system time
                reference_start_time = time.time()
                logger.info(f"Using system time as start time: {reference_start_time:.3f}s")
                
            # Align to segment boundary if syncing with HLS
            if self.sync_with_hls:
                # Round down to the nearest segment boundary
                reference_start_time = math.floor(reference_start_time / segment_duration) * segment_duration
                logger.info(f"Aligned reference start time to segment boundary: {reference_start_time:.3f}s")
        
        self.reference_start_time = reference_start_time
        self.cues: List[WebVTTCue] = []
    
    def add_cue(self, cue: WebVTTCue) -> None:
        """
        Add a cue to the segmenter.
        
        Args:
            cue: The WebVTT cue to add
        """
        self.cues.append(cue)
    
    def add_cues(self, cues: List[WebVTTCue]) -> None:
        """
        Add multiple cues to the segmenter.
        
        Args:
            cues: List of WebVTT cues to add
        """
        self.cues.extend(cues)
    
    def add_vtt_content(self, vtt_content: str, offset: float = 0.0) -> int:
        """
        Parse and add VTT content to the segmenter.
        
        Args:
            vtt_content: Raw WebVTT content
            offset: Time offset to apply to all cues
            
        Returns:
            int: Number of cues added
        """
        cues = parse_vtt_content(vtt_content, offset)
        self.add_cues(cues)
        return len(cues)
    
    def get_segment_cues(self, segment_index: int) -> List[WebVTTCue]:
        """
        Get all cues that overlap with the specified segment.
        
        Args:
            segment_index: The index of the segment.
            
        Returns:
            A list of WebVTTCue objects that overlap with the segment.
        """
        segment_start_time = self.get_segment_start_time(segment_index)
        segment_end_time = segment_start_time + self.segment_duration
        
        # Log the absolute time range for debugging
        logger.debug(f"Segment {segment_index}: absolute time range [{segment_start_time:.3f} - {segment_end_time:.3f}]")
        
        # Calculate relative time range (seconds from start of video)
        # If reference_start_time is set, use it as the base
        if self.reference_start_time is not None:
            # For regenerate_all_vtt_files, we're using current time as reference_start_time
            # But cues have timestamps relative to video start (0, 1, 2, etc.)
            # So we need to use segment_index to calculate relative time
            relative_start = segment_index * self.segment_duration
            relative_end = (segment_index + 1) * self.segment_duration
            
            # Add overlap to ensure we catch cues at segment boundaries
            relative_start = max(0, relative_start - self.segment_overlap)
            relative_end = relative_end + self.segment_overlap
        else:
            # If no reference time, assume segments start at 0
            relative_start = segment_index * self.segment_duration
            relative_end = (segment_index + 1) * self.segment_duration
        
        logger.debug(f"Segment {segment_index}: relative time range [{relative_start:.3f} - {relative_end:.3f}]")
        
        # Check if we need to adjust the relative time range to match transcript timestamps
        # This is needed when transcript timestamps are much larger than segment indices
        # (e.g., transcript timestamps are 100-1000+ seconds but segments are 0-10)
        if self.cues and len(self.cues) > 0:
            # Check if there's a significant mismatch between cue timestamps and segment times
            avg_cue_start = sum(cue.start_time for cue in self.cues[:min(10, len(self.cues))]) / min(10, len(self.cues))
            if avg_cue_start > relative_end * 10:  # If average cue start time is much larger than segment end time
                logger.info(f"Detected timestamp mismatch: avg cue start {avg_cue_start:.3f} vs segment end {relative_end:.3f}")
                # Find the minimum cue start time to use as a base offset
                min_cue_start = min(cue.start_time for cue in self.cues)
                # Adjust the relative time range to match the cue timestamps
                adjusted_start = min_cue_start + relative_start
                adjusted_end = min_cue_start + relative_end
                logger.debug(f"Adjusted segment {segment_index} time range: [{adjusted_start:.3f} - {adjusted_end:.3f}]")
                relative_start = adjusted_start
                relative_end = adjusted_end
        
        matching_cues = []
        for cue in self.cues:
            if cue.overlaps(relative_start, relative_end):
                matching_cues.append(cue)
        
        logger.info(f"Segment {segment_index}: time range [{relative_start:.3f} - {relative_end:.3f}], found {len(matching_cues)} matching cues out of {len(self.cues)} total cues")
        return matching_cues
    
    def get_segment_content(self, segment_index: int, offset: float = 0.0) -> str:
        """
        Generate WebVTT content for a specific segment.
        
        Args:
            segment_index: The segment index (0-based)
            offset: Additional time offset to apply
            
        Returns:
            str: Formatted WebVTT content for the segment
        """
        segment_cues = self.get_segment_cues(segment_index)
        
        if not segment_cues:
            # Return minimal valid WebVTT file
            return VTT_HEADER
        
        # Generate content
        segment_start = self.get_segment_start_time(segment_index)
        segment_end = segment_start + self.segment_duration
        
        # Add overlap for smoother transitions
        if segment_index > 0:
            segment_start -= self.segment_overlap
        
        content = VTT_HEADER
        
        # X-TIMESTAMP-MAP for HLS compatibility
        if self.sync_with_hls:
            # Add HLS timestamp mapping if enabled
            # See: https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/HTTP_Live_Streaming_Metadata_Spec/7_WebVTT/7_WebVTT.html
            mpegts_value = int(((segment_start * 90000) + 0x100000000) % 0x200000000)
            local_time = format_timestamp(0)  # Local time is always 00:00:00.000
            content += f"X-TIMESTAMP-MAP=MPEGTS:{mpegts_value},LOCAL:{local_time}\n\n"
        
        # Add cues, adjusting timestamps appropriately
        for i, cue in enumerate(segment_cues):
            if self.sync_with_hls:
                # For HLS alignment, we should calculate the relative timestamp
                # but we MUST preserve the original non-zero timestamps to ensure
                # cues appear at the correct time relative to each other
                relative_start = cue.start_time
                relative_end = cue.end_time
                
                # Create a new cue with the adjusted timestamps
                adjusted_cue = WebVTTCue(
                    cue.cue_id,
                    relative_start,
                    relative_end,
                    cue.text
                )
                content += adjusted_cue.to_vtt(offset)
            else:
                # No HLS sync, use cues as is
                content += cue.to_vtt(offset)
        
        return content
    
    def generate_segment_file(self, segment_index: int, output_dir: str, 
                             filename_template: str = "subtitles_{index}.vtt",
                             offset: float = 0.0) -> Optional[str]:
        """
        Generate and write a WebVTT segment file.
        
        Args:
            segment_index: The segment index (0-based)
            output_dir: Directory to write the file to
            filename_template: Template for the filename
            offset: Additional time offset to apply
            
        Returns:
            Optional[str]: Path to the generated file, or None if failed
        """
        content = self.get_segment_content(segment_index, offset)
        
        # Format filename - handle both Python-style and C-style format strings
        if '{index}' in filename_template:
            # Python-style format string
            filename = filename_template.format(index=segment_index)
        elif '%' in filename_template:
            # C-style format string
            filename = filename_template % segment_index
        else:
            # Fallback - just append the index
            filename = f"{filename_template}_{segment_index}"
            
        output_path = os.path.join(output_dir, filename)
        
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)
        
        try:
            # Write to temporary file first for atomicity
            with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=output_dir) as temp_file:
                temp_file.write(content)
                temp_path = temp_file.name
                
            # Set permissions on the temp file to ensure it's readable
            os.chmod(temp_path, 0o644)
                
            # Rename to target (atomic operation)
            os.replace(temp_path, output_path)
            
            logger.debug(f"Generated VTT segment {segment_index} at {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Failed to write segment {segment_index}: {str(e)}")
            # Try to clean up temp file if it exists
            try:
                if 'temp_path' in locals() and os.path.exists(temp_path):
                    os.unlink(temp_path)
            except:
                pass
            return None
    
    def generate_all_segments(self, start_index: int, end_index: int, output_dir: str,
                             filename_template: str = "subtitles_{index}.vtt",
                             offset: float = 0.0) -> List[str]:
        """
        Generate multiple segment files.
        
        Args:
            start_index: First segment index to generate
            end_index: Last segment index to generate (inclusive)
            output_dir: Directory to write files to
            filename_template: Template for the filenames
            offset: Additional time offset to apply
            
        Returns:
            List[str]: Paths to generated files
        """
        generated_files = []
        
        for segment_index in range(start_index, end_index + 1):
            output_path = self.generate_segment_file(
                segment_index, output_dir, filename_template, offset
            )
            if output_path:
                generated_files.append(output_path)
        
        logger.info(f"Generated {len(generated_files)} VTT segment files in {output_dir}")
        return generated_files
    
    def generate_playlist(self, start_index: int, end_index: int, 
                         output_path: str, segment_template: str = "subtitles_{index}.vtt") -> str:
        """
        Generate an HLS playlist (m3u8) for the subtitle segments.
        
        Args:
            start_index: Starting segment index (inclusive)
            end_index: Ending segment index (inclusive)
            output_path: Path to save the playlist file
            segment_template: Template for segment filenames, can be either Python format string with {index} or C-style format string with %d
            
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
            # Handle both Python format strings and C-style format strings
            if "{index}" in segment_template:
                segment_filename = segment_template.format(index=i)
            else:
                segment_filename = segment_template % i
            content += segment_filename + "\n"
        
        # No need to add #EXT-X-ENDLIST for live streams
        
        # Write the playlist file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.info(f"Generated subtitle playlist: {output_path}")
        return output_path

    def get_segment_start_time(self, segment_index: int) -> float:
        """
        Get the start time of a segment in reference time.
        
        Args:
            segment_index: The segment index (0-based)
            
        Returns:
            float: Start time of the segment in seconds
        """
        return self.reference_start_time + (segment_index * self.segment_duration)
    
    def get_segment_for_time(self, timestamp: float) -> int:
        """
        Get the segment index that contains the given timestamp.
        
        Args:
            timestamp: The timestamp in seconds (in reference time)
            
        Returns:
            int: Segment index
        """
        if timestamp < self.reference_start_time:
            # If timestamp is before reference start time, return segment 0
            return 0
            
        # Calculate relative time from reference start
        relative_time = timestamp - self.reference_start_time
        
        # Calculate segment index (floor division)
        segment_index = int(relative_time / self.segment_duration)
        
        return segment_index
    
    def set_reference_start_time(self, reference_start_time: float) -> None:
        """
        Update the reference start time for the segmenter.
        
        Args:
            reference_start_time: New reference start time
        """
        old_start = self.reference_start_time
        self.reference_start_time = reference_start_time
        logger.info(f"Updated reference start time from {old_start:.3f}s to {reference_start_time:.3f}s")

def parse_vtt_content(vtt_content: str, offset: float = 0.0) -> List[WebVTTCue]:
    """
    Parse WebVTT content into cue objects.
    
    Args:
        vtt_content: Raw WebVTT content
        offset: Time offset to apply to all cues
        
    Returns:
        List[WebVTTCue]: List of parsed cues
    """
    if not vtt_content.strip():
        return []
        
    lines = vtt_content.strip().split('\n')
    
    # Skip WEBVTT header if present
    start_line = 0
    if lines and lines[0].startswith("WEBVTT"):
        start_line = 1
        # Skip any header lines until we find an empty line
        while start_line < len(lines) and lines[start_line].strip():
            start_line += 1
        # Skip the empty line
        start_line += 1
    
    cues = []
    current_cue_id = None
    current_timing = None
    current_text = []
    
    for i in range(start_line, len(lines)):
        line = lines[i]
        
        # Empty line indicates end of cue
        if not line.strip() and current_timing:
            cue_text = '\n'.join(current_text).strip()
            # Parse timing
            match = CUE_PATTERN.match(current_timing)
            if match:
                start_ts = parse_timestamp(match.group(1))
                end_ts = parse_timestamp(match.group(2))
                
                # Apply offset
                start_ts += offset
                end_ts += offset
                
                # Create cue
                cue = WebVTTCue(
                    current_cue_id or str(len(cues) + 1),
                    start_ts,
                    end_ts,
                    cue_text
                )
                cues.append(cue)
            
            # Reset for next cue
            current_cue_id = None
            current_timing = None
            current_text = []
            continue
            
        # Check for timing line
        if " --> " in line:
            current_timing = line
            continue
            
        # If we have timing but no ID yet, this might be an ID line
        if current_timing is None and not current_text and current_cue_id is None:
            # This line could be an ID if it's not a timing line
            if " --> " not in line:
                current_cue_id = line.strip()
                continue
        
        # If we have timing, this must be text
        if current_timing is not None:
            current_text.append(line)
    
    # Handle final cue if present
    if current_timing and current_text:
        cue_text = '\n'.join(current_text).strip()
        match = CUE_PATTERN.match(current_timing)
        if match:
            start_ts = parse_timestamp(match.group(1))
            end_ts = parse_timestamp(match.group(2))
            
            # Apply offset
            start_ts += offset
            end_ts += offset
            
            # Create cue
            cue = WebVTTCue(
                current_cue_id or str(len(cues) + 1),
                start_ts,
                end_ts,
                cue_text
            )
            cues.append(cue)
    
    return cues
                
def parse_timestamp(timestamp: str) -> float:
    """
    Parse a WebVTT timestamp into seconds.
    
    Args:
        timestamp: Timestamp in format HH:MM:SS.mmm
        
    Returns:
        float: Timestamp in seconds
    """
    match = TIME_PATTERN.match(timestamp)
    if not match:
        raise ValueError(f"Invalid timestamp format: {timestamp}")
    
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    
    return (hours * 3600) + (minutes * 60) + seconds + (milliseconds / 1000)

def format_timestamp(seconds: float) -> str:
    """
    Format seconds as a WebVTT timestamp.
    
    Args:
        seconds: Time in seconds
        
    Returns:
        str: Formatted timestamp HH:MM:SS.mmm
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
        output_path: Path to write the file to
        
    Returns:
        str: Path to the created file
    """
    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    try:
        with open(output_path, 'w') as f:
            f.write(VTT_HEADER)
        return output_path
    except Exception as e:
        logger.error(f"Failed to create empty VTT file: {str(e)}")
        return ""

def get_current_segment_index(segmenter: Optional[WebVTTSegmenter] = None, 
                            segment_duration: float = DEFAULT_SEGMENT_DURATION,
                            reference_start_time: Optional[float] = None) -> int:
    """
    Get the current segment index based on the reference time.
    
    Args:
        segmenter: An existing segmenter to use, or None to create one
        segment_duration: Duration of each segment in seconds
        reference_start_time: Start time to use if creating a new segmenter
        
    Returns:
        int: The current segment index
    """
    if segmenter is None:
        segmenter = WebVTTSegmenter(
            segment_duration=segment_duration,
            reference_start_time=reference_start_time
        )
    
    # Get current time from reference clock if available
    if REFERENCE_TIME_SOURCE == 'reference_clock' and get_global_clock:
        current_time = get_global_clock().get_time()
    else:
        current_time = time.time()
    
    return segmenter.get_segment_for_time(current_time)

if __name__ == "__main__":
    # Test the segmenter
    logging.basicConfig(level=logging.INFO)
    
    segmenter = WebVTTSegmenter(segment_duration=10.0)
    
    # Add some test cues
    segmenter.add_cue(WebVTTCue("1", 1.0, 4.0, "This is the first subtitle"))
    segmenter.add_cue(WebVTTCue("2", 5.0, 8.0, "This is the second subtitle"))
    segmenter.add_cue(WebVTTCue("3", 9.0, 12.0, "This spans across segments"))
    segmenter.add_cue(WebVTTCue("4", 15.0, 18.0, "This is in the second segment"))
    
    # Generate segments
    test_dir = tempfile.mkdtemp()
    logger.info(f"Generating test segments in {test_dir}")
    
    files = segmenter.generate_all_segments(0, 2, test_dir)
    
    # Generate playlist
    playlist_path = os.path.join(test_dir, "playlist.m3u8")
    segmenter.generate_playlist(0, 2, playlist_path)
    
    print(f"Generated {len(files)} segment files and playlist at {test_dir}")
    
    # Show content of first segment
    with open(files[0], 'r') as f:
        print("\nSegment 0 content:")
        print(f.read()) 