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
INPUT_URL = os.getenv("INPUT_URL")
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
    """Build the FFmpeg command with all options."""
    # Generate timestamp for log file
    timestamp = int(time.time())
    log_file = f"{FFMPEG_LOGS_DIR}/ffmpeg_{timestamp}.log"
    
    # Get reference time to set program-date-time values
    reference_time = get_time()
    reference_date = datetime.fromtimestamp(reference_time)
    
    # Format datetime for FFmpeg
    formatted_datetime = reference_date.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    
    # Create command
    cmd = [
        "ffmpeg",
        "-loglevel", "info",
        
        # Input options
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "30",
        
        # Input stream
        "-i", INPUT_URL,
        
        # Add subtitles input (will be used if webvtt files are available)
        "-i", f"{WEBVTT_DIR}/playlist.m3u8",
        
        # Global options
        "-y",                      # Overwrite output files
    ]
    
    # Add timestamp copying if enabled
    if USE_COPYTS:
        cmd.extend(["-copyts"])
    
    # Start at zero option
    if START_AT_ZERO:
        cmd.extend(["-start_at_zero"])
    
    # Avoid negative timestamps
    cmd.extend(["-avoid_negative_ts", "1"])
    
    # Add extra FFmpeg options if specified
    if FFMPEG_EXTRA_OPTIONS:
        cmd.extend(shlex.split(FFMPEG_EXTRA_OPTIONS))
    
    # Continue with mapping and output options
    cmd.extend([
        # Map audio and video streams
        "-map", "0:v:0",          # Map video from input 0
        "-map", "0:a:0",          # Map audio from input 0
        "-map", "1:s?",           # Map subtitles from input 1 if available
        
        # Video codec options (copy by default)
        "-c:v", "copy",
        
        # Audio codec options (copy by default)
        "-c:a", "copy",
        
        # Subtitle codec options
        "-c:s", "webvtt",
        
        # HLS options
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_TIME),
        "-hls_list_size", str(HLS_LIST_SIZE),
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%d.ts",
    ])
    
    # Add program-date-time if enabled
    if USE_PROGRAM_DATE_TIME:
        cmd.extend([
            "-hls_flags", "program_date_time",
            "-program_date_time", formatted_datetime
        ])
    
    # Output path
    cmd.extend([f"{OUTPUT_DIR}/playlist.m3u8"])
    
    logger.info(f"FFmpeg command: {' '.join(cmd)}")
    return cmd, log_file

async def run_ffmpeg():
    """Run FFmpeg to mirror the stream and add subtitles."""
    global ffmpeg_process, restart_count, running
    
    # Ensure directories exist
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(FFMPEG_LOGS_DIR, exist_ok=True)
    
    # Create master playlist with reference to subtitle tracks
    create_master_playlist()
    
    retries = 0
    while running and retries < MAX_RETRIES:
        try:
            # Build FFmpeg command and get log file path
            ffmpeg_cmd, log_file = build_ffmpeg_command()
            
            logger.info(f"Starting FFmpeg (attempt {retries+1}/{MAX_RETRIES})")
            
            # Start the process
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
            
            logger.info(f"FFmpeg process started (PID: {ffmpeg_process.pid})")
            
            # Wait for process to finish
            while ffmpeg_process.poll() is None:
                # Check if we should exit
                if not running:
                    logger.info("Shutdown requested, stopping FFmpeg")
                    stop_ffmpeg()
                    break
                
                await asyncio.sleep(1)
            
            # Check exit status
            if ffmpeg_process and ffmpeg_process.returncode != 0 and running:
                # FFmpeg exited with error
                logger.error(f"FFmpeg exited with code {ffmpeg_process.returncode}")
                logger.error(f"Check log file: {log_file}")
                sync_metrics.record_error("ffmpeg_exit_error")
                sync_metrics.record_health_check("ffmpeg_running", False)
                
                # Calculate retry delay with jitter to avoid thundering herd
                delay = RETRY_DELAY * (1 + random.uniform(-JITTER_FACTOR, JITTER_FACTOR))
                logger.info(f"Retrying in {delay:.2f} seconds...")
                
                # Increment retry count
                retries += 1
                
                # Wait before retrying
                for _ in range(int(delay)):
                    if not running:
                        break
                    await asyncio.sleep(1)
            else:
                # Normal exit or shutdown requested
                logger.info("FFmpeg process stopped normally or shutdown requested")
                break
                
        except Exception as e:
            logger.error(f"Error running FFmpeg: {e}")
            sync_metrics.record_error("ffmpeg_error")
            sync_metrics.record_health_check("ffmpeg_running", False)
            
            # Calculate retry delay with jitter
            delay = RETRY_DELAY * (1 + random.uniform(-JITTER_FACTOR, JITTER_FACTOR))
            logger.info(f"Retrying in {delay:.2f} seconds...")
            retries += 1
            
            # Wait before retrying
            for _ in range(int(delay)):
                if not running:
                    break
                await asyncio.sleep(1)
    
    if retries >= MAX_RETRIES and running:
        logger.critical(f"Failed to start FFmpeg after {MAX_RETRIES} attempts, giving up")
        sync_metrics.record_error("ffmpeg_max_retries")
    
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
        if os.path.exists(f"{WEBVTT_DIR}/playlist.m3u8"):
            content += "#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID=\"subs\",NAME=\"Subtitles\",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE=\"ru\",URI=\"../webvtt/playlist.m3u8\"\n"
        
        # Write to the file
        with open(master_path, 'w') as f:
            f.write(content)
        
        logger.info(f"Created master playlist at {master_path}")
    except Exception as e:
        logger.error(f"Error creating master playlist: {e}")
        sync_metrics.record_error("master_playlist_error")

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
    
    try:
        # Synchronize reference clock
        reference_clock = get_global_clock()
        reference_clock.sync_once()
        logger.info(f"Reference clock synchronized: {get_formatted_time()}")
        
        # Start metrics server
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