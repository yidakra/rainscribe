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
import subprocess
import signal
import tempfile
import ffmpeg
from dotenv import load_dotenv
import time
import shlex
import random

# Import shared modules
from shared.reference_clock import get_global_clock, get_time, get_formatted_time
from shared.logging_config import configure_logging
from shared.monitoring import get_metrics_manager

# Configure logging
logger = configure_logging("audio-extractor")

# Load environment variables
load_dotenv()

# Configuration from environment variables with defaults
HLS_STREAM_URL = os.getenv("HLS_STREAM_URL")
AUDIO_OUTPUT_MODE = os.getenv("AUDIO_OUTPUT_MODE", "chunks")  # "pipe" or "chunks"
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
BIT_DEPTH = int(os.getenv("AUDIO_BIT_DEPTH", "16"))
CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
AUDIO_PIPE_PATH = f"{SHARED_VOLUME_PATH}/audio_stream"
AUDIO_CHUNKS_DIR = f"{SHARED_VOLUME_PATH}/audio"
AUDIO_CHUNK_DURATION = int(os.getenv("AUDIO_CHUNK_DURATION", "10"))  # seconds
MAX_RETRIES = int(os.getenv("FFMPEG_MAX_RETRIES", "10"))
RETRY_DELAY = int(os.getenv("FFMPEG_RETRY_DELAY", "5"))  # seconds
BUFFER_SIZE = int(os.getenv("FFMPEG_BUFFER_SIZE", f"{BIT_DEPTH * SAMPLE_RATE * CHANNELS // 4}"))  # Default to 1/4 second
FFMPEG_EXTRA_OPTIONS = os.getenv("FFMPEG_EXTRA_OPTIONS", "")
JITTER_FACTOR = float(os.getenv("RETRY_JITTER_FACTOR", "0.5"))  # Add randomness to retry delays

# Get metrics manager
metrics_manager = get_metrics_manager()
sync_metrics = metrics_manager.sync

# Global variables
ffmpeg_process = None
restart_count = 0
running = True

def handle_sigterm(signum, frame):
    """Handle SIGTERM to gracefully shutdown."""
    global running
    logger.info("Received shutdown signal, stopping audio extraction")
    running = False
    stop_ffmpeg()

# Register signal handlers
signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

def stop_ffmpeg():
    """Stop the FFmpeg process gracefully."""
    global ffmpeg_process
    if ffmpeg_process:
        try:
            logger.info("Stopping FFmpeg process")
            # Try to send SIGTERM first for graceful shutdown
            ffmpeg_process.terminate()
            try:
                # Wait a bit for graceful shutdown
                asyncio.get_event_loop().run_until_complete(
                    asyncio.wait_for(ffmpeg_process.wait(), timeout=3.0)
                )
            except asyncio.TimeoutError:
                # If it doesn't respond, force kill
                logger.warning("FFmpeg didn't terminate gracefully, killing process")
                ffmpeg_process.kill()
        except Exception as e:
            logger.error(f"Error stopping FFmpeg: {e}")
        finally:
            ffmpeg_process = None
            sync_metrics.record_health_check("ffmpeg_running", False)

async def build_ffmpeg_command(mode="chunks"):
    """
    Build the FFmpeg command for audio extraction.
    
    Args:
        mode: The output mode, either "pipe" or "chunks"
        
    Returns:
        list: The FFmpeg command as a list of arguments
    """
    # Base command with input options
    cmd = [
        "ffmpeg", "-y",
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "30",
        "-i", HLS_STREAM_URL,
        "-vn",  # Disable video
    ]
    
    # Add extra FFmpeg options if specified
    if FFMPEG_EXTRA_OPTIONS:
        cmd.extend(shlex.split(FFMPEG_EXTRA_OPTIONS))
    
    # Audio format options
    cmd.extend([
        "-ar", str(SAMPLE_RATE),    # Sample rate
        "-ac", str(CHANNELS),       # Channels
        "-sample_fmt", f"s{BIT_DEPTH}",  # Sample format
        "-bufsize", str(BUFFER_SIZE),
    ])
    
    # Always use chunks mode
    # Output as audio chunks
    timestamp = int(time.time())
    chunk_pattern = f"{AUDIO_CHUNKS_DIR}/audio_%d_{timestamp}.wav"
    cmd.extend([
        "-f", "segment",
        "-segment_time", str(AUDIO_CHUNK_DURATION),
        "-segment_format", "wav",
        "-reset_timestamps", "1",
        chunk_pattern
    ])
    
    logger.info(f"FFmpeg command: {' '.join(cmd)}")
    return cmd

async def extract_audio():
    """
    Extract audio from HLS stream and write to a named pipe or chunks
    for processing by the transcription service.
    """
    global ffmpeg_process, restart_count, running
    
    # Ensure directories exist
    os.makedirs(SHARED_VOLUME_PATH, exist_ok=True)
    os.makedirs(AUDIO_CHUNKS_DIR, exist_ok=True)
    
    # Create named pipe if using pipe mode and it doesn't exist
    if AUDIO_OUTPUT_MODE == "pipe" and not os.path.exists(AUDIO_PIPE_PATH):
        try:
            os.mkfifo(AUDIO_PIPE_PATH)
            logger.info(f"Created named pipe at {AUDIO_PIPE_PATH}")
        except Exception as e:
            logger.error(f"Failed to create named pipe: {e}")
            return
    
    logger.info(f"Starting audio extraction from {HLS_STREAM_URL}")
    logger.info(f"Audio format: {SAMPLE_RATE}Hz, {BIT_DEPTH}-bit, {CHANNELS} channel(s)")
    logger.info(f"Output mode: {AUDIO_OUTPUT_MODE}")
    
    # Record start time for metrics
    start_time = get_time()
    sync_metrics.metrics.add_metric("extraction_start_time", start_time)
    
    retries = 0
    
    while running and retries < MAX_RETRIES:
        try:
            # Build FFmpeg command
            ffmpeg_cmd = await build_ffmpeg_command("chunks")  # Force chunks mode
            
            # Create temporary log file for FFmpeg output
            with tempfile.NamedTemporaryFile(mode='w+', prefix='ffmpeg_', suffix='.log', delete=False) as log_file:
                log_path = log_file.name
                
                # Start FFmpeg process
                logger.info(f"Starting FFmpeg (attempt {retries+1}/{MAX_RETRIES})")
                
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                logger.info(f"FFmpeg process started (PID: {ffmpeg_process.pid})")
                restart_count += 1
                sync_metrics.metrics.add_metric("ffmpeg_restarts", restart_count)
                sync_metrics.record_health_check("ffmpeg_running", True)
                
                # Collect output asynchronously
                stderr_task = asyncio.create_task(ffmpeg_process.stderr.read())
                
                # Monitor the process
                exit_code = await ffmpeg_process.wait()
                
                # Get stderr output
                stderr = await stderr_task
                stderr_str = stderr.decode()
                
                # Log the output to our temp file
                with open(log_path, 'w') as log_file:
                    log_file.write(stderr_str)
                    
                # Check exit code
                if exit_code != 0 and running:
                    logger.error(f"FFmpeg exited with code {exit_code}")
                    logger.error(f"FFmpeg log: {log_path}")
                    logger.error(f"FFmpeg stderr: {stderr_str[:500]}...")  # Log first 500 chars
                    sync_metrics.record_error("ffmpeg_exit_error")
                    
                    # Calculate retry delay with jitter to avoid thundering herd
                    delay = RETRY_DELAY * (1 + random.uniform(-JITTER_FACTOR, JITTER_FACTOR))
                    logger.info(f"Retrying in {delay:.2f} seconds...")
                    
                    # If we've successfully waited, increment retry count
                    retries += 1
                    sync_metrics.record_health_check("ffmpeg_running", False)
                    
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
            logger.error(f"Error in audio extraction: {e}")
            sync_metrics.record_error("extraction_error")
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
        stop_ffmpeg()
    
    logger.info("Audio extraction stopped")

async def health_check():
    """
    Periodically check the health of the FFmpeg process.
    """
    while running:
        try:
            if ffmpeg_process and ffmpeg_process.returncode is None:
                # Process is running
                sync_metrics.record_health_check("ffmpeg_running", True)
            else:
                # Process is not running
                sync_metrics.record_health_check("ffmpeg_running", False)
            
            # Update metrics
            sync_metrics.metrics.add_metric("ffmpeg_restarts", restart_count)
        except Exception as e:
            logger.error(f"Error in health check: {e}")
        
        # Check every 30 seconds
        for _ in range(30):
            if not running:
                break
            await asyncio.sleep(1)

async def main():
    """
    Main entry point.
    """
    if not HLS_STREAM_URL:
        logger.error("HLS_STREAM_URL environment variable is not set")
        return
    
    try:
        # Start tasks
        extraction_task = asyncio.create_task(extract_audio())
        health_task = asyncio.create_task(health_check())
        
        # Wait for tasks to complete
        await asyncio.gather(extraction_task, health_task)
        
    except asyncio.CancelledError:
        logger.info("Main task cancelled")
    except Exception as e:
        logger.error(f"Error in main task: {e}")
    finally:
        # Ensure FFmpeg process is stopped
        stop_ffmpeg()
        
        # Log exit
        logger.info("Audio extractor service exiting")

if __name__ == "__main__":
    try:
        # Run the event loop
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, exiting")
    finally:
        # Ensure we clean up
        if ffmpeg_process:
            stop_ffmpeg() 