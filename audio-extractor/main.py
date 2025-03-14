#!/usr/bin/env python3
"""
Audio Extractor Service for rainscribe

This service captures an HLS stream and extracts audio in real-time.
The extracted audio is saved as a continuous stream of PCM data
that can be consumed by the Transcription & Translation Service.
"""

import os
import sys
import asyncio
import logging
import subprocess
import ffmpeg
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("audio-extractor")

# Load environment variables
load_dotenv()

# Configuration
HLS_STREAM_URL = os.getenv("HLS_STREAM_URL")
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
BIT_DEPTH = int(os.getenv("AUDIO_BIT_DEPTH", "16"))
CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
AUDIO_PIPE_PATH = f"{SHARED_VOLUME_PATH}/audio_stream"

async def extract_audio():
    """
    Extract audio from HLS stream and write to a named pipe for processing
    by the transcription service.
    """
    # Ensure shared volume directory exists
    os.makedirs(SHARED_VOLUME_PATH, exist_ok=True)
    
    # Create named pipe if it doesn't exist
    if not os.path.exists(AUDIO_PIPE_PATH):
        os.mkfifo(AUDIO_PIPE_PATH)
    
    logger.info(f"Starting audio extraction from {HLS_STREAM_URL}")
    logger.info(f"Audio format: {SAMPLE_RATE}Hz, {BIT_DEPTH}-bit, {CHANNELS} channel(s)")
    
    try:
        # Build FFmpeg command
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-re",
            "-i", HLS_STREAM_URL,
            "-vn",  # Disable video
            "-ar", str(SAMPLE_RATE),  # Audio sample rate
            "-ac", str(CHANNELS),  # Audio channels
            "-f", "wav",  # Output format
            "-bufsize", f"{BIT_DEPTH * SAMPLE_RATE * CHANNELS // 8}",
            AUDIO_PIPE_PATH
        ]
        
        # Start FFmpeg process
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        logger.info("Audio extraction process started")
        
        # Wait for the process to complete
        stdout, stderr = await process.communicate()
        
        # Log any errors
        if process.returncode != 0:
            logger.error(f"FFmpeg error: {stderr.decode()}")
            return
            
    except Exception as e:
        logger.error(f"Error extracting audio: {e}")
        sys.exit(1)

async def health_check():
    """
    Periodic health check to ensure the FFmpeg process is running.
    Restarts the process if necessary.
    """
    while True:
        try:
            # Simple health check - just check if the pipe exists
            if not os.path.exists(AUDIO_PIPE_PATH):
                logger.warning("Audio pipe not found, recreating...")
                os.mkfifo(AUDIO_PIPE_PATH)
            
            # Check if any ffmpeg processes are running
            result = subprocess.run(
                ["pgrep", "ffmpeg"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE
            )
            
            if result.returncode != 0:
                logger.warning("FFmpeg process not running, restarting extraction...")
                asyncio.create_task(extract_audio())
                
        except Exception as e:
            logger.error(f"Health check error: {e}")
            
        # Check every 30 seconds
        await asyncio.sleep(30)

async def main():
    """
    Main entry point for the Audio Extractor Service.
    """
    logger.info("Starting Audio Extractor Service")
    
    # Start the initial audio extraction process
    extraction_task = asyncio.create_task(extract_audio())
    
    # Start health check
    health_task = asyncio.create_task(health_check())
    
    # Wait for both tasks
    await asyncio.gather(extraction_task, health_task)

if __name__ == "__main__":
    asyncio.run(main()) 