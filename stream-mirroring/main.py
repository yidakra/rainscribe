#!/usr/bin/env python3
"""
Stream Mirroring Service for rainscribe

This service uses FFmpeg to mirror the input HLS stream, performs transcoding if needed,
and adds WebVTT subtitle files to the output HLS stream.
"""

import os
import sys
import time
import json
import logging
import signal
import subprocess
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("stream-mirroring")

# Load environment variables
load_dotenv()

# Configuration
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
INPUT_URL = os.getenv("INPUT_URL")
OUTPUT_DIR = f"{SHARED_VOLUME_PATH}/hls"
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
REFERENCE_CLOCK_FILE = f"{SHARED_VOLUME_PATH}/reference_clock.json"
HLS_SEGMENT_TIME = int(os.getenv("HLS_SEGMENT_TIME", "10"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "6"))
SUBTITLE_SYNC_THRESHOLD = int(os.getenv("SUBTITLE_SYNC_THRESHOLD", "5"))  # Maximum allowed subtitle sync offset in seconds

# Global state
ffmpeg_process = None
reference_start_time = None

def save_reference_clock():
    """Save the reference clock to the shared file."""
    global reference_start_time
    try:
        current_time = time.time()
        if reference_start_time is None:
            reference_start_time = current_time
        
        with open(REFERENCE_CLOCK_FILE, "w") as f:
            json.dump({
                "start_time": reference_start_time,
                "creation_timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }, f)
        logger.info(f"Saved reference start time: {reference_start_time}")
    except Exception as e:
        logger.error(f"Error saving reference clock: {e}")

def load_reference_clock():
    """Load the reference clock from the shared file."""
    global reference_start_time
    try:
        if os.path.exists(REFERENCE_CLOCK_FILE):
            with open(REFERENCE_CLOCK_FILE, "r") as f:
                data = json.load(f)
                reference_start_time = data.get("start_time")
                logger.info(f"Loaded reference start time: {reference_start_time}")
        else:
            # If file doesn't exist, create it
            save_reference_clock()
    except Exception as e:
        logger.error(f"Error loading reference clock: {e}")
        # If an error occurs, create a new reference clock
        save_reference_clock()

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
    sys.exit(0)

def run_ffmpeg():
    """Run FFmpeg to mirror the stream and add subtitles."""
    global ffmpeg_process
    
    # Ensure output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Create master playlist with reference to subtitle tracks
    create_master_playlist()
    
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
        
        # Log FFmpeg output
        for line in ffmpeg_process.stdout:
            line = line.strip()
            if line:
                logger.info(f"FFmpeg: {line}")
        
        # Wait for FFmpeg to complete
        return_code = ffmpeg_process.wait()
        logger.info(f"FFmpeg exited with code {return_code}")
        return return_code
    except Exception as e:
        logger.error(f"Error running FFmpeg: {e}")
        return 1

def create_master_playlist():
    """Create a master playlist that includes subtitle tracks."""
    try:
        # Create a master playlist with subtitle tracks
        with open(f"{OUTPUT_DIR}/master.m3u8", "w") as f:
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n")
            
            # Add program date time for synchronization
            if reference_start_time:
                program_date = datetime.fromtimestamp(reference_start_time).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                f.write(f"#EXT-X-PROGRAM-DATE-TIME:{program_date}\n")
            
            # Define subtitle track
            f.write('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Russian",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE="ru",URI="ru/playlist.m3u8"\n')
            
            # Define the main video+audio stream with subtitle group
            f.write('#EXT-X-STREAM-INF:BANDWIDTH=3000000,CODECS="avc1.4d401f,mp4a.40.2",SUBTITLES="subs"\n')
            f.write("playlist.m3u8\n")
            
        logger.info("Created master playlist with subtitle tracks")
    except Exception as e:
        logger.error(f"Error creating master playlist: {e}")

def main():
    """Main entry point for the Stream Mirroring Service."""
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("Starting Stream Mirroring Service")
    
    # Load or create reference clock
    load_reference_clock()
    
    # Run FFmpeg and restart if it crashes
    while True:
        return_code = run_ffmpeg()
        if return_code != 0:
            logger.error(f"FFmpeg crashed with code {return_code}, restarting in 5 seconds...")
            time.sleep(5)
        else:
            logger.info("FFmpeg exited gracefully, stopping service")
            break

if __name__ == "__main__":
    main() 