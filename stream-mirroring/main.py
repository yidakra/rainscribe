#!/usr/bin/env python3
"""
Stream Mirroring Service for rainscribe

This service uses FFmpeg to mirror the input HLS stream, performs transcoding if needed,
and adds WebVTT subtitle files to the output HLS stream.
"""

import os
import sys
import time
import signal
import subprocess
import asyncio
import random
import tempfile
import shlex
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Import shared modules
from shared.reference_clock import get_global_clock, get_time, get_formatted_time
from shared.logging_config import configure_logging
from shared.monitoring import get_metrics_manager

# Configure logging
logger = configure_logging("stream-mirroring")

# Load environment variables
load_dotenv()

# Configuration from environment variables with defaults
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
INPUT_URL = os.getenv("INPUT_URL") or os.getenv("HLS_STREAM_URL")  # Fallback to HLS_STREAM_URL if INPUT_URL is not set
OUTPUT_DIR = f"{SHARED_VOLUME_PATH}/hls"
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
FFMPEG_LOGS_DIR = f"{SHARED_VOLUME_PATH}/logs"
HLS_SEGMENT_TIME = int(os.getenv("FFMPEG_SEGMENT_DURATION", "10"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "6"))
SUBTITLE_SYNC_THRESHOLD = int(os.getenv("SUBTITLE_SYNC_THRESHOLD", "5"))  # Maximum allowed subtitle sync offset in seconds
MAX_RETRIES = int(os.getenv("FFMPEG_MAX_RETRIES", "10"))
RETRY_DELAY = int(os.getenv("FFMPEG_RETRY_DELAY", "5"))  # seconds
JITTER_FACTOR = float(os.getenv("RETRY_JITTER_FACTOR", "0.5"))  # Add randomness to retry delays
USE_COPYTS = os.getenv("FFMPEG_COPYTS", "1") == "1"
START_AT_ZERO = os.getenv("FFMPEG_START_AT_ZERO", "1") == "1"
FFMPEG_EXTRA_OPTIONS = os.getenv("FFMPEG_EXTRA_OPTIONS", "")
USE_PROGRAM_DATE_TIME = os.getenv("FFMPEG_USE_PROGRAM_DATE_TIME", "1") == "1"
# New configuration for output delay
OUTPUT_DELAY_SECONDS = int(os.getenv("VIDEO_OUTPUT_DELAY_SECONDS", "30"))
# Calculate how many segments this represents
OUTPUT_DELAY_SEGMENTS = max(1, OUTPUT_DELAY_SECONDS // HLS_SEGMENT_TIME)

# Get metrics manager
metrics_manager = get_metrics_manager()
sync_metrics = metrics_manager.sync

# Global state
ffmpeg_process = None
restart_count = 0
running = True

def signal_handler(sig, frame):
    """Handle signals to gracefully terminate FFmpeg."""
    global running
    logger.info(f"Received signal {sig}, stopping FFmpeg...")
    running = False
    stop_ffmpeg()

# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def stop_ffmpeg():
    """Stop the FFmpeg process gracefully."""
    global ffmpeg_process
    if ffmpeg_process:
        try:
            logger.info("Stopping FFmpeg process")
            # Try to terminate gracefully first
            ffmpeg_process.terminate()
            # Wait for up to 5 seconds
            for _ in range(50):
                if ffmpeg_process.poll() is not None:
                    break
                time.sleep(0.1)
            # If still running, force kill
            if ffmpeg_process.poll() is None:
                logger.warning("FFmpeg process did not terminate gracefully, killing")
                ffmpeg_process.kill()
        except Exception as e:
            logger.error(f"Error stopping FFmpeg: {e}")
            sync_metrics.record_error("ffmpeg_stop_error")
        finally:
            # Ensure we mark process as not running
            sync_metrics.record_health_check("ffmpeg_running", False)
            ffmpeg_process = None

def build_ffmpeg_command():
    """Build the FFmpeg command for stream mirroring."""
    # Create a timestamp for the log file
    timestamp = int(time.time())
    log_file = f"{FFMPEG_LOGS_DIR}/ffmpeg_{timestamp}.log"
    
    # Determine subtitle input
    subtitle_input = f"{WEBVTT_DIR}/ru/playlist.m3u8"
    
    # Build the FFmpeg command
    cmd = list(filter(None, [
        "ffmpeg",
        
        # Logging options
        "-loglevel", "info",
        
        # Input options for the main stream
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "30",
        "-i", INPUT_URL,
        
        # Input options for subtitles
        "-i", subtitle_input,
        
        # Global options
        "-y",                      # Overwrite output files
        "-copyts",                 # Copy timestamps
        "-start_at_zero",          # Start at zero
        "-avoid_negative_ts", "1", # Avoid negative timestamps
        
        # Stream mapping
        "-map", "0:v:0",          # Map video from input 0
        "-map", "0:a:0",          # Map audio from input 0
        "-map", "1:s?",           # Map subtitles from input 1 if available
        
        # Video codec options (copy by default)
        "-c:v", "copy",
        
        # Audio codec options (copy by default)
        "-c:a", "copy",
        
        # Subtitle codec options
        "-c:s", "webvtt",
        
        # Output HLS settings
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_TIME),
        "-hls_list_size", str(HLS_LIST_SIZE + OUTPUT_DELAY_SEGMENTS),  # Increase list size to accommodate delay
        "-hls_flags", "delete_segments+independent_segments+program_date_time",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%05d.ts",
        
        # Enable subtitle streams in playlist
        "-hls_subtitle_path", f"{OUTPUT_DIR}/subtitles/",
        
        # Synchronization options
        "-use_wallclock_as_timestamps", "1",  # Use system time for timestamps
        
        # Allow all file extensions (needed for .vtt files)
        "-allowed_extensions", "ALL",
        
        # Output file
        f"{OUTPUT_DIR}/playlist.m3u8"
    ]))
    
    logger.info(f"FFmpeg command: {' '.join(cmd)}")
    return cmd, log_file

async def run_ffmpeg():
    """Run FFmpeg to mirror the stream and add subtitles."""
    global ffmpeg_process, restart_count, running
    
    # Ensure directories exist
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(FFMPEG_LOGS_DIR, exist_ok=True)
    os.makedirs(f"{OUTPUT_DIR}/subtitles", exist_ok=True)
    
    # Create master playlist with reference to subtitle tracks
    create_master_playlist()
    
    retries = 0
    while running and retries < MAX_RETRIES:
        try:
            # Build FFmpeg command and get log file path
            ffmpeg_cmd, log_file = build_ffmpeg_command()
            
            # Debug: Print each item in the command
            logger.info("FFmpeg command items:")
            for i, item in enumerate(ffmpeg_cmd):
                logger.info(f"  Item {i}: {repr(item)}")
            
            # Filter out None values from the command
            ffmpeg_cmd = [str(item) if item is not None else "NONE_VALUE" for item in ffmpeg_cmd]
            
            logger.info(f"Starting FFmpeg (attempt {retries+1}/{MAX_RETRIES})")
            
            # Start the process
            try:
                with open(log_file, 'w') as f:
                    ffmpeg_process = subprocess.Popen(
                        ffmpeg_cmd,
                        stdout=f,
                        stderr=subprocess.STDOUT,
                        bufsize=1,
                        universal_newlines=True
                    )
                
                # Record start time for metrics
                start_time = time.time()
                restart_count += 1
                
                # Update metrics
                sync_metrics.metrics.add_metric("stream_start_time", start_time)
                sync_metrics.metrics.add_metric("ffmpeg_restarts", restart_count)
                sync_metrics.record_health_check("ffmpeg_running", True)
                
                logger.info(f"FFmpeg process started (PID: {ffmpeg_process.pid if ffmpeg_process else 'Unknown'})")
                
                # Wait for process to finish
                while ffmpeg_process and ffmpeg_process.poll() is None:
                    # Check if we should exit
                    if not running:
                        logger.info("Shutdown requested, stopping FFmpeg")
                        stop_ffmpeg()
                        break
                    
                    await asyncio.sleep(1)
                
                # Check process exit code
                if ffmpeg_process:
                    exit_code = ffmpeg_process.returncode
                    logger.info(f"FFmpeg process exited with code {exit_code}")
                    
                    if exit_code != 0:
                        logger.error(f"FFmpeg failed with exit code {exit_code}")
                        with open(log_file, 'r') as f:
                            last_lines = f.readlines()[-20:]  # Get last 20 lines
                            logger.error(f"Last FFmpeg log lines: {''.join(last_lines)}")
                else:
                    logger.error("FFmpeg process was None, possible initialization error")
            except Exception as e:
                logger.error(f"Error starting FFmpeg process: {str(e)}")
                sync_metrics.record_health_check("ffmpeg_running", False)
                
            # If we get here, the process has exited or we caught an exception
            sync_metrics.record_health_check("ffmpeg_running", False)
            
            # Retry with exponential backoff
            retries += 1
            retry_delay = min(RETRY_DELAY * (2 ** (retries - 1)) + random.uniform(0, 1), RETRY_DELAY * 2)
            logger.info(f"Retrying in {retry_delay:.2f} seconds...")
            
            # Wait before retrying
            await asyncio.sleep(retry_delay)
            
        except Exception as e:
            logger.error(f"Error running FFmpeg: {str(e)}")
            sync_metrics.record_health_check("ffmpeg_running", False)
            
            # Retry with exponential backoff
            retries += 1
            retry_delay = min(RETRY_DELAY * (2 ** (retries - 1)) + random.uniform(0, 1), RETRY_DELAY * 2)
            logger.info(f"Retrying in {retry_delay:.2f} seconds...")
            
            # Wait before retrying
            await asyncio.sleep(retry_delay)
    
    # If we've exhausted all retries, log a critical error
    if retries >= MAX_RETRIES:
        logger.critical(f"Failed to run FFmpeg after {MAX_RETRIES} attempts. Giving up.")
    
    logger.info("Stream mirroring stopped")

def create_master_playlist():
    """Create a master playlist that includes the video and subtitle tracks."""
    try:
        master_path = f"{OUTPUT_DIR}/master.m3u8"
        
        content = "#EXTM3U\n"
        content += "#EXT-X-VERSION:3\n"
        
        # Add video/audio variant
        content += "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        content += "playlist.m3u8\n"
        
        # Add subtitle tracks if they exist
        if os.path.exists(f"{WEBVTT_DIR}/ru/playlist.m3u8"):
            # Use the subtitles directory which has symlinks to the actual subtitle files
            content += "#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID=\"subs\",NAME=\"Subtitles\",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE=\"ru\",URI=\"subtitles/playlist.m3u8\"\n"
        
        # Write to the file
        with open(master_path, 'w') as f:
            f.write(content)
        
        logger.info(f"Created master playlist at {master_path}")
        
        # Create or update a delayed playlist
        create_delayed_playlist()
    except Exception as e:
        logger.error(f"Error creating master playlist: {e}")
        sync_metrics.record_error("master_playlist_error")

def create_delayed_playlist():
    """Create a delayed playlist that references older segments to allow time for captions to be generated."""
    try:
        original_playlist = f"{OUTPUT_DIR}/playlist.m3u8"
        delayed_playlist = f"{OUTPUT_DIR}/delayed_playlist.m3u8"
        
        # Check if the original playlist exists
        if not os.path.exists(original_playlist):
            logger.warning(f"Original playlist {original_playlist} does not exist yet, skipping delayed playlist creation")
            return
        
        with open(original_playlist, 'r') as f:
            lines = f.readlines()
        
        # Parse the playlist to find segment indices
        segments = []
        for i, line in enumerate(lines):
            if line.strip().endswith('.ts'):
                segment_name = line.strip()
                segments.append((i, segment_name))
        
        # If we have enough segments, create a delayed playlist
        if len(segments) > OUTPUT_DELAY_SEGMENTS:
            # Start with the header lines (up to the first segment)
            if segments:
                header_lines = lines[:segments[0][0]]
            else:
                header_lines = lines
                
            # Collect the delayed segments (skip the most recent ones)
            delayed_segments = segments[:-OUTPUT_DELAY_SEGMENTS] if OUTPUT_DELAY_SEGMENTS > 0 else segments
            
            # Create the new playlist content
            new_content = ''.join(header_lines)
            
            # Add the delayed segments and their metadata
            for segment_idx, segment_name in delayed_segments:
                # Add the EXTINF line before the segment
                extinf_line = lines[segment_idx - 1]
                # If there's a program date time, include that too
                if segment_idx > 1 and lines[segment_idx - 2].startswith('#EXT-X-PROGRAM-DATE-TIME'):
                    new_content += lines[segment_idx - 2]
                new_content += extinf_line
                new_content += lines[segment_idx]
            
            # Write the delayed playlist
            with open(delayed_playlist, 'w') as f:
                f.write(new_content)
            
            logger.info(f"Created delayed playlist at {delayed_playlist} with {len(delayed_segments)} segments, delay of {OUTPUT_DELAY_SECONDS}s")
            
            # Create symbolic link from default playlist.m3u8 to delayed_playlist.m3u8
            # This ensures clients using the standard playlist name get the delayed version
            delayed_playlist_symlink = f"{OUTPUT_DIR}/delayed_symlink.m3u8"
            try:
                # Create a temporary symlink first
                if os.path.exists(delayed_playlist_symlink):
                    os.remove(delayed_playlist_symlink)
                os.symlink(os.path.basename(delayed_playlist), delayed_playlist_symlink)
                # Then atomically rename it
                os.rename(delayed_playlist_symlink, original_playlist)
                logger.info(f"Updated playlist.m3u8 to point to delayed version")
            except Exception as e:
                logger.error(f"Error creating symlink to delayed playlist: {e}")
        else:
            logger.info(f"Not enough segments yet for delayed playlist, need {OUTPUT_DELAY_SEGMENTS}, have {len(segments)}")
            
    except Exception as e:
        logger.error(f"Error creating delayed playlist: {e}")
        sync_metrics.record_error("delayed_playlist_error")

async def monitor_health():
    """Periodically check the health of the FFmpeg process and update metrics."""
    global ffmpeg_process
    
    while running:
        try:
            # Check if FFmpeg is running
            if ffmpeg_process and ffmpeg_process.poll() is None:
                # Process is running
                sync_metrics.record_health_check("ffmpeg_running", True)
            else:
                # Process is not running and not a normal exit
                sync_metrics.record_health_check("ffmpeg_running", False)
                
            # Record system clock and reference clock time
            system_time = time.time()
            reference_time = get_time()
            
            # Calculate clock offset
            clock_offset = reference_time - system_time
            
            # Update metrics
            sync_metrics.metrics.add_metric("system_time", system_time)
            sync_metrics.metrics.add_metric("reference_time", reference_time)
            sync_metrics.metrics.add_metric("clock_offset", clock_offset)
            sync_metrics.metrics.add_metric("ffmpeg_restarts", restart_count)
            
            # Check and update the master playlist periodically
            if (not ffmpeg_process or ffmpeg_process.poll() is not None) and running:
                create_master_playlist()
            
            # Also update the delayed playlist
            create_delayed_playlist()
            
        except Exception as e:
            logger.error(f"Error in health check: {e}")
            sync_metrics.record_error("health_check_error")
        
        # Check every 30 seconds
        for _ in range(30):
            if not running:
                break
            await asyncio.sleep(1)

async def run_async_tasks():
    """Run all async tasks."""
    try:
        # Create tasks
        ffmpeg_task = asyncio.create_task(run_ffmpeg())
        health_task = asyncio.create_task(monitor_health())
        
        # Wait for tasks to complete
        await asyncio.gather(ffmpeg_task, health_task)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled")
    except Exception as e:
        logger.error(f"Error in async tasks: {e}")
    finally:
        stop_ffmpeg()

def main():
    """Main entry point."""
    global running
    
    # Check required configuration
    if not INPUT_URL:
        logger.error("INPUT_URL environment variable is not set")
        sys.exit(1)
    
    logger.info(f"Starting Stream Mirroring Service")
    logger.info(f"Input: {INPUT_URL}")
    logger.info(f"Output directory: {OUTPUT_DIR}")
    logger.info(f"Output delay: {OUTPUT_DELAY_SECONDS}s ({OUTPUT_DELAY_SEGMENTS} segments)")
    
    try:
        # Get reference clock
        reference_clock = get_global_clock()
        
        # Try to synchronize, but continue even if it fails
        try:
            # Try to sync, but don't wait for too many retries
            sync_success = reference_clock.sync_once()
            if sync_success:
                logger.info(f"Reference clock synchronized: {get_formatted_time()}")
            else:
                logger.warning("Failed to sync with NTP servers, but continuing anyway")
            
            # Debug metrics_manager
            logger.info(f"Metrics manager: {metrics_manager}")
            logger.info(f"Metrics manager dir: {dir(metrics_manager)}")
        except Exception as e:
            logger.warning(f"Failed to synchronize reference clock: {e}. Continuing anyway.")
        
        # Force continue even if sync fails
        logger.info(f"Proceeding with reference clock time: {get_formatted_time()}")
        
        # Start metrics server if available
        if hasattr(metrics_manager, 'start_server'):
            metrics_manager.start_server()
        
        # Run async tasks
        asyncio.run(run_async_tasks())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        running = False
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
    finally:
        # Ensure FFmpeg is stopped
        stop_ffmpeg()
        logger.info("Stream mirroring service exiting")

if __name__ == "__main__":
    main() 