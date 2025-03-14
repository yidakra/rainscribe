#!/usr/bin/env python3
"""
Caption Generator Service for rainscribe

This service monitors the transcript directory for new transcription files,
processes them, and generates WebVTT subtitle files with precise synchronization.
"""

import os
import sys
import json
import time
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Optional, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("caption-generator")

# Load environment variables
load_dotenv()

# Configuration
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
TRANSCRIPT_DIR = f"{SHARED_VOLUME_PATH}/transcript"
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
REFERENCE_CLOCK_FILE = f"{SHARED_VOLUME_PATH}/reference_clock.json"
SEGMENT_DURATION = int(os.getenv("WEBVTT_SEGMENT_DURATION", "10"))
BUFFER_DURATION = int(os.getenv("BUFFER_DURATION", "3"))  # Buffer duration in seconds
OFFSET_ADJUSTMENT_INTERVAL = int(os.getenv("OFFSET_ADJUSTMENT_INTERVAL", "30"))  # Check for drift every 30 seconds
DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "0.5"))  # Drift threshold in seconds
SUBTITLE_DISPLAY_WINDOW = int(os.getenv("SUBTITLE_DISPLAY_WINDOW", "30"))  # Keep subtitles active for 30 seconds
MINIMUM_CUE_DURATION = float(os.getenv("MINIMUM_CUE_DURATION", "2.0"))  # Ensure cues stay visible for at least 2 seconds

# Global state
current_offset = 0.0  # Initial latency offset
reference_start_time = None
video_start_time = None  # Estimated start time of the video stream
recent_cues = []  # Maintain a list of recent cues

def format_timestamp(seconds: float) -> str:
    """Format a timestamp in seconds to WebVTT format (HH:MM:SS.mmm)."""
    # Ensure no negative timestamps
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    milliseconds = int((seconds - int(seconds)) * 1000)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{milliseconds:03d}"

def create_webvtt_header() -> str:
    """Create the WebVTT header."""
    return "WEBVTT\n\n"

def create_webvtt_cue(start: float, end: float, text: str, cue_id: int = None) -> str:
    """Create a WebVTT cue with optional ID."""
    cue = ""
    if cue_id is not None:
        cue += f"{cue_id}\n"
    cue += f"{format_timestamp(start)} --> {format_timestamp(end)}\n"
    cue += f"{text}\n\n"
    return cue

def load_reference_clock() -> float:
    """Load the reference clock from the shared file."""
    try:
        if os.path.exists(REFERENCE_CLOCK_FILE):
            with open(REFERENCE_CLOCK_FILE, "r") as f:
                data = json.load(f)
                return data.get("start_time", time.time())
        else:
            logger.warning("Reference clock file not found, creating a new one")
            start_time = time.time()
            with open(REFERENCE_CLOCK_FILE, "w") as f:
                json.dump({"start_time": start_time, "creation_timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
            return start_time
    except Exception as e:
        logger.error(f"Error loading reference clock: {e}")
        return time.time()

async def adjust_timestamps(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adjust the timestamps in the transcription data based on the current offset.
    
    Returns the adjusted data with updated start/end times.
    """
    global video_start_time
    
    # Make a copy to avoid modifying the original
    adjusted_data = data.copy()
    
    # Calculate video_start_time if not already set
    if video_start_time is None and reference_start_time is not None:
        video_start_time = reference_start_time - current_offset
        logger.info(f"Estimated video start time: {datetime.fromtimestamp(video_start_time).strftime('%Y-%m-%d %H:%M:%S.%f')}")
    
    # For relative time calculation
    current_video_time = time.time() - (current_offset if video_start_time is None else (time.time() - video_start_time))
    
    # Apply the current offset to adjust for drift
    if "original_start" in data and "original_end" in data:
        # Use original timestamps if they exist (from transcription service)
        original_start = data["original_start"]
        original_end = data["original_end"]
        
        # Adjust timestamps to match current video time
        # We want subtitle timestamps to be relative to current playback time
        adjusted_data["start"] = max(0, original_start - current_offset)
        
        # Ensure minimum duration for better visibility
        adjusted_data["end"] = max(adjusted_data["start"] + MINIMUM_CUE_DURATION, original_end - current_offset)
    else:
        # Fall back to regular timestamps with offset
        adjusted_data["start"] = max(0, data["start"] - current_offset)
        adjusted_data["end"] = max(adjusted_data["start"] + MINIMUM_CUE_DURATION, data["end"] - current_offset)
    
    # Also adjust word-level timestamps if they exist
    if "words" in adjusted_data and adjusted_data["words"]:
        for i, word in enumerate(adjusted_data["words"]):
            if "original_start" in word and "original_end" in word:
                word["start"] = max(0, word["original_start"] - current_offset)
                word["end"] = max(word["start"] + 0.1, word["original_end"] - current_offset)  # Ensure minimum word duration
            else:
                word["start"] = max(0, word["start"] - current_offset)
                word["end"] = max(word["start"] + 0.1, word["end"] - current_offset)
    
    # Log the timestamp adjustment for debugging
    logger.debug(f"Adjusted timestamp: original={data.get('original_start', data.get('start', 0)):.2f}, " +
                f"adjusted={adjusted_data['start']:.2f}, current_offset={current_offset:.2f}")
    
    return adjusted_data

async def process_transcript_file(file_path: str):
    """Process a transcript file and update the WebVTT file."""
    global current_offset, recent_cues
    
    try:
        # Read the transcript file
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Skip processing if the file is too recent (apply buffering)
        current_time = time.time()
        if "processing_time" in data:
            file_age = current_time - data["processing_time"]
            if file_age < BUFFER_DURATION:
                logger.debug(f"File {file_path} is too recent ({file_age:.2f}s), buffering for later processing")
                return False  # Indicate file was not processed
        
        # Adjust timestamps based on the current offset
        adjusted_data = await adjust_timestamps(data)
        
        # Extract the necessary information
        text = adjusted_data.get("text", "")
        start_time = adjusted_data.get("start", 0)
        end_time = adjusted_data.get("end", start_time + 5)  # Default to 5 seconds if end is not provided
        
        # For word-level timestamps (also adjusted)
        words = adjusted_data.get("words", [])
        
        # Store this cue in our recent cues list
        cue_data = {
            "text": text,
            "start": start_time,
            "end": end_time,
            "file_path": file_path,
            "timestamp": current_time
        }
        
        # Add to recent cues and remove old ones
        recent_cues.append(cue_data)
        recent_cues = [cue for cue in recent_cues if (current_time - cue["timestamp"]) < SUBTITLE_DISPLAY_WINDOW]
        
        # Create multiple segment files for this cue to ensure it appears in the right window
        # This helps keep subtitles visible longer and improves synchronization
        current_segment_index = int((current_time - reference_start_time) / SEGMENT_DURATION)
        
        # Create a window of segment files to ensure the subtitle is visible
        for window_offset in range(-1, 3):  # Create segments from current-1 to current+2
            segment_index = max(0, current_segment_index + window_offset)
            segment_file = f"{WEBVTT_DIR}/ru/segment_{segment_index:05d}.vtt"
            
            # Ensure the directory exists
            os.makedirs(os.path.dirname(segment_file), exist_ok=True)
            
            # Create or rewrite the WebVTT file
            # We'll rewrite the entire file to ensure proper formatting
            cues = []
            
            # If the file exists, read existing cues
            if os.path.exists(segment_file):
                with open(segment_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Skip header
                    if content.startswith("WEBVTT"):
                        parts = content.split("\n\n")
                        # First part is header, skip it
                        for part in parts[1:]:
                            if part.strip():  # Skip empty parts
                                cues.append(part)
            
            # Prepare the new cue
            adjusted_start = start_time + (window_offset * SEGMENT_DURATION)
            adjusted_end = end_time + (window_offset * SEGMENT_DURATION)
            
            cue_id = f"cue{int(start_time * 1000)}"  # Use timestamp as cue ID with prefix
            cue_text = create_webvtt_cue(adjusted_start, adjusted_end, text, cue_id).strip()
            
            # Check if we already have this cue (based on cue_id)
            existing_cue_index = -1
            for i, cue in enumerate(cues):
                if cue_id in cue:
                    existing_cue_index = i
                    break
            
            # Replace or append the cue
            if existing_cue_index >= 0:
                cues[existing_cue_index] = cue_text
            else:
                cues.append(cue_text)
            
            # Write back the file with header and all cues
            with open(segment_file, "w", encoding="utf-8") as f:
                f.write(create_webvtt_header())
                for cue in cues:
                    f.write(cue + "\n\n")
            
            if window_offset == 0:
                logger.info(f"Updated {segment_file} with cue: {text[:50]}... (offset: {current_offset:.2f}s)")
        
        # Update the HLS manifest for subtitles
        await update_hls_manifest()
        
        # Update offset based on measured latency if provided
        if "measured_latency" in data:
            measured_latency = data["measured_latency"]
            logger.debug(f"Transcript reported measured latency: {measured_latency:.2f}s")
        
        return True  # Indicate file was processed
        
    except Exception as e:
        logger.error(f"Error processing transcript file {file_path}: {e}")
        logger.exception(e)
        return False

async def update_hls_manifest():
    """Update the HLS manifest for Russian subtitles."""
    try:
        # Path to the manifest
        manifest_path = f"{WEBVTT_DIR}/ru/playlist.m3u8"
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        
        # Find all segment files
        segment_files = []
        if os.path.exists(f"{WEBVTT_DIR}/ru"):
            segment_files = sorted([
                f for f in os.listdir(f"{WEBVTT_DIR}/ru")
                if f.startswith("segment_") and f.endswith(".vtt")
            ])
        
        # Calculate current segment index based on reference time
        current_time = time.time()
        current_segment_index = int((current_time - reference_start_time) / SEGMENT_DURATION)
        
        # Only include segments that are relevant to current playback
        # Keep a window of past and future segments to ensure smooth playback
        relevant_segment_files = []
        for segment_file in segment_files:
            try:
                segment_index = int(segment_file.split("_")[1].split(".")[0])
                # Keep segments that are within a reasonable window of the current playback time
                if current_segment_index - 10 <= segment_index <= current_segment_index + 10:
                    relevant_segment_files.append(segment_file)
            except (ValueError, IndexError):
                # Skip files with invalid naming
                continue
        
        # Create the manifest file
        with open(manifest_path, "w", encoding="utf-8") as f:
            # Write the header
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n")
            f.write(f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n")
            f.write("#EXT-X-MEDIA-SEQUENCE:0\n")
            
            # Calculate the program date time for synchronization
            if reference_start_time:
                program_date = datetime.fromtimestamp(reference_start_time).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                f.write(f"#EXT-X-PROGRAM-DATE-TIME:{program_date}\n")
            
            # Write segment info for relevant segments
            for segment_file in relevant_segment_files:
                try:
                    segment_index = int(segment_file.split("_")[1].split(".")[0])
                    segment_duration = SEGMENT_DURATION
                    
                    # Add program date time to each segment for precise synchronization
                    if reference_start_time:
                        segment_start_time = reference_start_time + (segment_index * SEGMENT_DURATION)
                        segment_date = datetime.fromtimestamp(segment_start_time).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        f.write(f"#EXT-X-PROGRAM-DATE-TIME:{segment_date}\n")
                    
                    f.write(f"#EXTINF:{segment_duration:.3f},\n")
                    f.write(f"{segment_file}\n")
                except (ValueError, IndexError):
                    # Skip files with invalid naming
                    continue
            
            # Don't add endlist for live streams
            # f.write("#EXT-X-ENDLIST\n")
            
        logger.debug(f"Updated HLS manifest with {len(relevant_segment_files)} segments")
        
    except Exception as e:
        logger.error(f"Error updating HLS manifest: {e}")
        logger.exception(e)

async def update_latency_offset():
    """
    Periodically update the latency offset based on recent transcription data.
    This helps compensate for drift between audio and subtitles.
    """
    global current_offset, reference_start_time, video_start_time
    
    while True:
        try:
            # Get the list of recent transcription files
            recent_files = []
            if os.path.exists(TRANSCRIPT_DIR):
                all_files = sorted([
                    f for f in os.listdir(TRANSCRIPT_DIR)
                    if f.startswith("ru_transcript_") and f.endswith(".json")
                ], reverse=True)  # Get most recent first
                
                # Take only the 5 most recent files
                recent_files = all_files[:5]
            
            if recent_files:
                # Calculate the average measured latency from recent files
                total_latency = 0
                count = 0
                
                for file_name in recent_files:
                    try:
                        with open(f"{TRANSCRIPT_DIR}/{file_name}", "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if "measured_latency" in data:
                                total_latency += data["measured_latency"]
                                count += 1
                    except Exception as e:
                        logger.error(f"Error reading latency from {file_name}: {e}")
                
                if count > 0:
                    avg_latency = total_latency / count
                    drift = avg_latency - current_offset
                    
                    # Adjust offset if drift exceeds threshold
                    if abs(drift) > DRIFT_THRESHOLD:
                        # Use a weighted adjustment to avoid sudden changes
                        new_offset = current_offset + (drift * 0.3)  # 30% adjustment
                        logger.info(f"Adjusting offset from {current_offset:.2f}s to {new_offset:.2f}s (drift: {drift:.2f}s)")
                        current_offset = new_offset
                        
                        # Recalculate video_start_time when offset changes significantly
                        if video_start_time is not None:
                            video_start_time = reference_start_time - current_offset
                            logger.info(f"Updated video start time: {datetime.fromtimestamp(video_start_time).strftime('%Y-%m-%d %H:%M:%S.%f')}")
                            
                        # Regenerate WebVTT files with the new offset
                        await regenerate_all_vtt_files()
            
        except Exception as e:
            logger.error(f"Error updating latency offset: {e}")
        
        # Sleep before next check
        await asyncio.sleep(OFFSET_ADJUSTMENT_INTERVAL)

async def regenerate_all_vtt_files():
    """
    Regenerate all WebVTT files from the transcript data.
    This is useful when restarting the service or when the offset has changed significantly.
    """
    # Clear existing WebVTT files
    if os.path.exists(f"{WEBVTT_DIR}/ru"):
        for file_name in os.listdir(f"{WEBVTT_DIR}/ru"):
            if file_name.endswith(".vtt"):
                try:
                    os.remove(f"{WEBVTT_DIR}/ru/{file_name}")
                except Exception as e:
                    logger.error(f"Error removing {file_name}: {e}")
    
    # Get all transcript files
    transcript_files = []
    if os.path.exists(TRANSCRIPT_DIR):
        transcript_files = sorted([
            f for f in os.listdir(TRANSCRIPT_DIR)
            if f.startswith("ru_transcript_") and f.endswith(".json")
        ])
    
    # Process each file
    for file_name in transcript_files:
        await process_transcript_file(f"{TRANSCRIPT_DIR}/{file_name}")
    
    logger.info(f"Regenerated WebVTT files from {len(transcript_files)} transcript files")

async def monitor_transcript_directory():
    """
    Monitor the transcript directory for new transcription files
    and process them to update WebVTT files.
    """
    # Dictionary to track processed files
    processed_files = {}
    
    while True:
        try:
            # Get the list of transcript files
            if os.path.exists(TRANSCRIPT_DIR):
                transcript_files = [
                    f for f in os.listdir(TRANSCRIPT_DIR)
                    if f.startswith("ru_transcript_") and f.endswith(".json")
                ]
                
                for file_name in transcript_files:
                    file_path = f"{TRANSCRIPT_DIR}/{file_name}"
                    
                    # Get file modification time
                    mtime = os.path.getmtime(file_path)
                    
                    # Process if the file is new or has been modified
                    if file_name not in processed_files or mtime > processed_files[file_name]:
                        logger.debug(f"Processing transcript file: {file_name}")
                        processed = await process_transcript_file(file_path)
                        
                        # If file was processed, update the processed_files dictionary
                        if processed:
                            processed_files[file_name] = mtime
            
            # Sleep before checking again
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error monitoring transcript directory: {e}")
            await asyncio.sleep(5)  # Longer sleep on error

async def main():
    """
    Main entry point for the Caption Generator Service.
    """
    global reference_start_time
    
    logger.info("Starting Caption Generator Service")
    
    # Ensure directories exist
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    os.makedirs(f"{WEBVTT_DIR}/ru", exist_ok=True)
    
    # Load reference clock
    reference_start_time = load_reference_clock()
    logger.info(f"Loaded reference start time: {reference_start_time}")
    
    # Start the offset update task
    offset_task = asyncio.create_task(update_latency_offset())
    
    # Regenerate all WebVTT files with the current offset
    await regenerate_all_vtt_files()
    
    # Start monitoring the transcript directory
    monitor_task = asyncio.create_task(monitor_transcript_directory())
    
    # Wait for both tasks
    await asyncio.gather(offset_task, monitor_task)

if __name__ == "__main__":
    asyncio.run(main()) 