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

# Configuration
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
INPUT_URL = os.getenv("INPUT_URL")
OUTPUT_DIR = f"{SHARED_VOLUME_PATH}/hls"
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
HLS_SEGMENT_TIME = int(os.getenv("HLS_SEGMENT_TIME", "10"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "6"))
SUBTITLE_SYNC_THRESHOLD = int(os.getenv("SUBTITLE_SYNC_THRESHOLD", "5"))  # Maximum allowed subtitle sync offset in seconds

# Get metrics manager
metrics_manager = get_metrics_manager()
sync_metrics = metrics_manager.sync

# Global state
ffmpeg_process = None

def signal_handler(sig, frame):
    """Handle signals to gracefully terminate FFmpeg."""
    global ffmpeg_process
    logger.info(f"Received signal {sig}, stopping FFmpeg...")
    if ffmpeg_process:
        try:
            # Try to terminate gracefully first
            ffmpeg_process.terminate()
            # Wait for up to 5 seconds
            for _ in range(50):
                if ffmpeg_process.poll() is not None:
                    break
                time.sleep(0.1)
            # If still running, force kill
            if ffmpeg_process.poll() is None:
                ffmpeg_process.kill()
        except Exception as e:
            logger.error(f"Error stopping FFmpeg: {e}")
            sync_metrics.record_error("ffmpeg_stop_error")
    sys.exit(0)

def run_ffmpeg():
    """Run FFmpeg to mirror the stream and add subtitles."""
    global ffmpeg_process
    
    # Ensure output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Create master playlist with reference to subtitle tracks
    create_master_playlist()
    
    start_time = time.time()
    
    # Set FFmpeg command
    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "info",
        
        # Input stream
        "-i", INPUT_URL,
        
        # Add subtitles input (will be used if webvtt files are available)
        "-i", f"{WEBVTT_DIR}/ru/playlist.m3u8",
        
        # Global options
        "-y",                     # Overwrite output files
        "-re",                    # Read input at native frame rate
        "-copyts",                # Copy timestamps
        "-start_at_zero",         # Start timestamp at zero
        "-avoid_negative_ts", "1", # Avoid negative timestamps
        
        # Map audio and video streams
        "-map", "0:v:0",          # Map video from input 0
        "-map", "0:a:0",          # Map audio from input 0
        "-map", "1:s?",           # Map subtitles from input 1 if available
        
        # Video settings (copy by default, but can be changed for transcoding)
        "-c:v", "copy",           # Just copy the video codec
        
        # Audio settings
        "-c:a", "copy",           # Just copy the audio codec
        
        # Subtitle settings
        "-c:s", "webvtt",         # Copy WebVTT subtitles
        
        # Output HLS settings
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_TIME),
        "-hls_list_size", str(HLS_LIST_SIZE),
        "-hls_flags", "independent_segments+delete_segments+program_date_time",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%05d.ts",
        
        # Enable subtitle streams in playlist
        "-hls_subtitle_path", f"{WEBVTT_DIR}/ru/",
        "-hls_flags", "independent_segments+delete_segments+program_date_time+discont_start", 
        "-hls_playlist_type", "event",
        "-strftime", "1",         # Use strftime for naming
        
        # Synchronization options
        "-use_wallclock_as_timestamps", "1",  # Use system time for timestamps
        
        # Output file
        f"{OUTPUT_DIR}/playlist.m3u8"
    ]
    
    # Log FFmpeg command
    logger.info(f"Starting FFmpeg with command: {' '.join(ffmpeg_cmd)}")
    
    try:
        # Start FFmpeg
        ffmpeg_process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Record start time in metrics
        sync_metrics.metrics.add_metric("ffmpeg_start_time", start_time)
        sync_metrics.record_health_check("ffmpeg_process", True)
        
        # Log FFmpeg output
        for line in ffmpeg_process.stdout:
            line = line.strip()
            if line:
                logger.info(f"FFmpeg: {line}")
                
                # Record segment creation events for metrics
                if "Opening" in line and "for writing" in line and "segment" in line:
                    try:
                        segment_num = int(line.split("segment_")[1].split(".")[0])
                        sync_metrics.metrics.add_metric("latest_segment", segment_num)
                        sync_metrics.metrics.add_metric("segment_creation_time", time.time())
                    except (ValueError, IndexError):
                        pass
        
        # Wait for FFmpeg to complete
        return_code = ffmpeg_process.wait()
        logger.info(f"FFmpeg exited with code {return_code}")
        
        # Record exit in metrics
        sync_metrics.record_health_check("ffmpeg_process", False)
        sync_metrics.metrics.add_metric("ffmpeg_exit_code", return_code)
        sync_metrics.metrics.add_metric("ffmpeg_exit_time", time.time())
        
        return return_code
    except Exception as e:
        logger.error(f"Error running FFmpeg: {e}")
        sync_metrics.record_error("ffmpeg_error")
        sync_metrics.record_health_check("ffmpeg_process", False)
        return 1

def create_master_playlist():
    """Create a master playlist that includes subtitle tracks."""
    try:
        # Get reference time from the global clock
        reference_time = get_global_clock().get_time()
        
        # Create a master playlist with subtitle tracks
        with open(f"{OUTPUT_DIR}/master.m3u8", "w") as f:
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n")
            
            # Add program date time for synchronization
            program_date = datetime.fromtimestamp(reference_time).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            f.write(f"#EXT-X-PROGRAM-DATE-TIME:{program_date}\n")
            
            # Define subtitle track
            f.write('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Russian",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE="ru",URI="ru/playlist.m3u8"\n')
            
            # Define the main video+audio stream with subtitle group
            f.write('#EXT-X-STREAM-INF:BANDWIDTH=3000000,CODECS="avc1.4d401f,mp4a.40.2",SUBTITLES="subs"\n')
            f.write("playlist.m3u8\n")
            
        logger.info("Created master playlist with subtitle tracks")
        
        # Record in metrics
        sync_metrics.metrics.add_metric("master_playlist_created", time.time())
        
    except Exception as e:
        logger.error(f"Error creating master playlist: {e}")
        sync_metrics.record_error("master_playlist_error")

async def monitor_health():
    """Periodically check system health and update metrics."""
    while True:
        try:
            # Check if FFmpeg is running
            if ffmpeg_process:
                is_running = ffmpeg_process.poll() is None
                sync_metrics.record_health_check("ffmpeg_running", is_running)
                
                # Check how long FFmpeg has been running
                if is_running:
                    start_time = sync_metrics.metrics.get_latest("ffmpeg_start_time")
                    if start_time:
                        runtime = time.time() - start_time[1]
                        sync_metrics.metrics.add_metric("ffmpeg_runtime", runtime)
            
            # Check if subtitle files are being generated
            try:
                if os.path.exists(f"{WEBVTT_DIR}/ru"):
                    vtt_files = [f for f in os.listdir(f"{WEBVTT_DIR}/ru") if f.endswith(".vtt")]
                    sync_metrics.metrics.add_metric("vtt_file_count", len(vtt_files))
                    
                    # Check playlist file
                    playlist_path = f"{WEBVTT_DIR}/ru/playlist.m3u8"
                    if os.path.exists(playlist_path):
                        last_modified = os.path.getmtime(playlist_path)
                        age = time.time() - last_modified
                        sync_metrics.metrics.add_metric("subtitle_playlist_age", age)
            except Exception as e:
                logger.error(f"Error checking subtitle files: {e}")
            
            # Record service as healthy
            sync_metrics.record_health_check("stream_mirroring_service", True)
            
        except Exception as e:
            logger.error(f"Health check error: {e}")
            sync_metrics.record_health_check("stream_mirroring_service", False)
            sync_metrics.record_error("health_check_error")
        
        # Run health check every 30 seconds
        await asyncio.sleep(30)

async def run_async_tasks():
    """Run asynchronous tasks like health monitoring."""
    # Create and start the health monitoring task
    health_task = asyncio.create_task(monitor_health())
    
    # Wait for the task to complete (it should run indefinitely)
    await health_task

def main():
    """Main entry point for the Stream Mirroring Service."""
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Starting Stream Mirroring Service")
    
    # Initialize the reference clock
    reference_clock = get_global_clock()
    reference_clock.sync_once()  # Synchronize with NTP servers
    logger.info(f"Reference clock initialized: {get_formatted_time()}")
    
    # Start the metrics manager
    metrics_manager.start_auto_save()
    
    # Start async tasks in a separate thread
    import threading
    asyncio_thread = threading.Thread(
        target=lambda: asyncio.run(run_async_tasks()),
        daemon=True
    )
    asyncio_thread.start()
    
    # Run FFmpeg and restart if it crashes
    while True:
        return_code = run_ffmpeg()
        if return_code != 0:
            logger.error(f"FFmpeg crashed with code {return_code}, restarting in 5 seconds...")
            sync_metrics.record_error("ffmpeg_crash")
            time.sleep(5)
        else:
            logger.info("FFmpeg exited gracefully, stopping service")
            break

if __name__ == "__main__":
    main() 