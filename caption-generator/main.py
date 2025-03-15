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
import asyncio
import tempfile
import shutil
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Optional, Any

# Import shared modules
from shared.webvtt_segmenter import WebVTTSegmenter, WebVTTCue, parse_vtt_content, format_timestamp
from shared.reference_clock import get_global_clock, get_time, get_formatted_time
from shared.offset_calculator import get_global_calculator, add_measurement, get_current_offset
from shared.logging_config import configure_logging
from shared.monitoring import get_metrics_manager

# Configure logging
logger = configure_logging("caption-generator")

# Load environment variables
load_dotenv()

# Configuration
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
TRANSCRIPT_DIR = f"{SHARED_VOLUME_PATH}/transcript"
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
SEGMENT_DURATION = int(os.getenv("WEBVTT_SEGMENT_DURATION", "10"))
BUFFER_DURATION = int(os.getenv("BUFFER_DURATION", "3"))  # Buffer duration in seconds
OFFSET_ADJUSTMENT_INTERVAL = int(os.getenv("OFFSET_ADJUSTMENT_INTERVAL", "30"))  # Check for drift every 30 seconds
DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "0.5"))  # Drift threshold in seconds
SUBTITLE_DISPLAY_WINDOW = int(os.getenv("SUBTITLE_DISPLAY_WINDOW", "30"))  # Keep subtitles active for 30 seconds
MINIMUM_CUE_DURATION = float(os.getenv("MINIMUM_CUE_DURATION", "2.0"))  # Ensure cues stay visible for at least 2 seconds

# Get metrics manager for monitoring
metrics_manager = get_metrics_manager()
sync_metrics = metrics_manager.sync

# Global state
video_start_time = None  # Estimated start time of the video stream
recent_cues = []  # Maintain a list of recent cues
vtt_segmenter = None  # Will be initialized in main()

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

async def adjust_timestamps(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adjust the timestamps in the transcription data based on the current offset.
    
    Returns the adjusted data with updated start/end times.
    """
    global video_start_time
    
    # Make a copy to avoid modifying the original
    adjusted_data = data.copy()
    
    # Get current offset from the global calculator
    current_offset = get_current_offset()
    
    # Calculate video_start_time if not already set
    if video_start_time is None:
        reference_start_time = get_global_clock().get_time()
        video_start_time = reference_start_time - current_offset
        logger.info(f"Estimated video start time: {datetime.fromtimestamp(video_start_time).strftime('%Y-%m-%d %H:%M:%S.%f')}")
    
    # For relative time calculation
    current_video_time = get_time() - (current_offset if video_start_time is None else (get_time() - video_start_time))
    
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
    
    # Record metrics for monitoring
    sync_metrics.record_offset(current_offset, "caption_generator")
    
    return adjusted_data

async def process_transcript_file(file_path: str):
    """Process a transcript file and update the WebVTT file."""
    global recent_cues, vtt_segmenter
    
    try:
        # Record start time for performance metrics
        start_time = time.time()
        
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
        start_time_sec = adjusted_data.get("start", 0)
        end_time_sec = adjusted_data.get("end", start_time_sec + 5)  # Default to 5 seconds if end is not provided
        
        # Create a WebVTT cue and add it to the segmenter
        cue = WebVTTCue(
            cue_id=f"cue{int(start_time_sec * 1000)}",
            start_time=start_time_sec,
            end_time=end_time_sec,
            text=text
        )
        vtt_segmenter.add_cue(cue)
        logger.info(f"Added cue: '{text[:30]}...' - Total cues in segmenter: {len(vtt_segmenter.cues)}")
        
        # Store this cue in our recent cues list
        cue_data = {
            "text": text,
            "start": start_time_sec,
            "end": end_time_sec,
            "file_path": file_path,
            "timestamp": current_time
        }
        
        # Add to recent cues and remove old ones
        recent_cues.append(cue_data)
        recent_cues = [cue for cue in recent_cues if (current_time - cue["timestamp"]) < SUBTITLE_DISPLAY_WINDOW]
        
        # Calculate current segment index based on reference time
        current_segment_index = vtt_segmenter.get_segment_for_time(get_time())
        
        # Generate segment files for a window around the current time
        # This ensures the subtitle appears at the right time and stays visible appropriately
        window_range = 3  # Generate segments for current-1 to current+2
        vtt_segmenter.generate_all_segments(
            start_index=max(0, current_segment_index - 1),
            end_index=current_segment_index + 2,
            output_dir=f"{WEBVTT_DIR}/ru",
            filename_template="segment_%05d.vtt"
        )
        
        # Update the HLS manifest for subtitles
        playlist_path = f"{WEBVTT_DIR}/ru/playlist.m3u8"
        vtt_segmenter.generate_playlist(
            start_index=max(0, current_segment_index - 5),  # Include some past segments
            end_index=current_segment_index + 10,  # And future segments
            output_path=playlist_path,
            segment_template="segment_%05d.vtt"
        )
        
        # Update offset based on measured latency if provided
        if "measured_latency" in data:
            measured_latency = data["measured_latency"]
            logger.debug(f"Transcript reported measured latency: {measured_latency:.2f}s")
            
            # Add the measurement to the global offset calculator
            add_measurement(measured_latency)
            
            # Record metrics
            sync_metrics.record_latency(measured_latency, "transcript")
        
        # Record processing time for performance metrics
        processing_time = time.time() - start_time
        sync_metrics.record_processing_time(processing_time, "process_transcript")
        
        logger.info(f"Processed transcript: {text[:50]}... (offset: {get_current_offset():.2f}s)")
        return True  # Indicate file was processed
        
    except Exception as e:
        logger.error(f"Error processing transcript file {file_path}: {e}")
        logger.exception(e)
        sync_metrics.record_error("process_transcript_error")
        return False

async def update_latency_offset():
    """
    Periodically update the latency offset based on recent transcription data.
    This helps compensate for drift between audio and subtitles.
    """
    global video_start_time
    
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
                    current_offset = get_current_offset()
                    drift = avg_latency - current_offset
                    
                    # Record drift metrics
                    sync_metrics.record_offset(current_offset, "before_adjustment")
                    sync_metrics.record_offset(avg_latency, "measured")
                    
                    # Adjust offset if drift exceeds threshold
                    if abs(drift) > DRIFT_THRESHOLD:
                        # Add the latest measurement to the global offset calculator
                        add_measurement(avg_latency)
                        
                        # Log the adjustment
                        new_offset = get_current_offset()
                        logger.info(f"Adjusted offset from {current_offset:.2f}s to {new_offset:.2f}s (drift: {drift:.2f}s)")
                        
                        # Recalculate video_start_time when offset changes significantly
                        if video_start_time is not None:
                            video_start_time = get_global_clock().get_time() - new_offset
                            logger.info(f"Updated video start time: {datetime.fromtimestamp(video_start_time).strftime('%Y-%m-%d %H:%M:%S.%f')}")
                            
                        # Record the adjusted offset
                        sync_metrics.record_offset(new_offset, "after_adjustment")
            
        except Exception as e:
            logger.error(f"Error updating latency offset: {e}")
            sync_metrics.record_error("update_latency_offset_error")
        
        # Sleep before next check
        await asyncio.sleep(OFFSET_ADJUSTMENT_INTERVAL)

async def regenerate_all_vtt_files():
    """
    Regenerate all WebVTT files from the transcript data.
    This is useful when restarting the service or when the offset has changed significantly.
    """
    global vtt_segmenter
    
    start_time = time.time()
    
    try:
        # Create a new WebVTT segmenter with the current time as reference
        vtt_segmenter = WebVTTSegmenter(
            segment_duration=SEGMENT_DURATION,
            reference_start_time=get_global_clock().get_time()
        )
        
        # Get all transcript files
        transcript_files = []
        if os.path.exists(TRANSCRIPT_DIR):
            transcript_files = sorted([
                f for f in os.listdir(TRANSCRIPT_DIR)
                if f.startswith("ru_transcript_") and f.endswith(".json")
            ])
        
        # Process each file
        processed_count = 0
        for file_name in transcript_files:
            if await process_transcript_file(f"{TRANSCRIPT_DIR}/{file_name}"):
                processed_count += 1
        
        # Debug: Print the timestamps of the first 5 cues
        if vtt_segmenter.cues:
            logger.debug(f"First 5 cues timestamps after processing all transcripts:")
            for i, cue in enumerate(vtt_segmenter.cues[:5]):
                logger.debug(f"Cue {i}: start_time={cue.start_time:.3f}, end_time={cue.end_time:.3f}, text='{cue.text[:30]}...'")
        else:
            logger.debug("No cues in the segmenter after processing all transcripts")
        
        # Record performance metrics
        processing_time = time.time() - start_time
        sync_metrics.record_processing_time(processing_time, "regenerate_vtt")
        
        logger.info(f"Regenerated WebVTT files from {processed_count} transcript files")
    except Exception as e:
        logger.error(f"Error regenerating VTT files: {e}")
        sync_metrics.record_error("regenerate_vtt_error")

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
            
            # Update health metrics
            sync_metrics.record_health_check("caption_generator", True)
            
            # Sleep before checking again
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error monitoring transcript directory: {e}")
            sync_metrics.record_health_check("caption_generator", False)
            sync_metrics.record_error("monitor_transcript_error")
            await asyncio.sleep(5)  # Longer sleep on error

def write_vtt_file(output_path, vtt_content):
    """Write a WebVTT file to disk, ensuring it's properly saved."""
    try:
        # Create a temporary file in the same directory
        dir_path = os.path.dirname(output_path)
        os.makedirs(dir_path, exist_ok=True)
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=dir_path) as temp_file:
            temp_path = temp_file.name
            temp_file.write(vtt_content)
        
        # Set permissions to ensure file is readable by nginx
        os.chmod(temp_path, 0o644)
        
        # Rename the temporary file to the target name (atomic operation)
        shutil.move(temp_path, output_path)
        
        # Verify the file exists and has content
        if os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
            if file_size > 0:
                logger.debug(f"Successfully wrote VTT file: {output_path} ({file_size} bytes)")
                return True
            else:
                logger.error(f"VTT file has zero size: {output_path}")
                return False
        else:
            logger.error(f"Failed to create VTT file: {output_path}")
            return False
    except Exception as e:
        logger.error(f"Error writing VTT file {output_path}: {e}")
        return False

async def main():
    """
    Main entry point for the Caption Generator Service.
    """
    global vtt_segmenter
    
    logger.info("Starting Caption Generator Service")
    
    # Ensure directories exist
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    os.makedirs(f"{WEBVTT_DIR}/ru", exist_ok=True)
    
    # Create a WebVTT segmenter with the current time as reference
    vtt_segmenter = WebVTTSegmenter(
        segment_duration=SEGMENT_DURATION,
        reference_start_time=get_global_clock().get_time()
    )
    
    # Start the metrics manager
    metrics_manager.start_auto_save()
    
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