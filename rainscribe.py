#!/usr/bin/env python3
"""
Live Transcription with Native HLS Subtitle Integration for HLS Streaming

This implementation incorporates methodological improvements for multimedia buffering and parallel processing:
- Systematic collection of 6 10-second audio and video segments prior to stream initiation
- Parallel audio transcription via isolated processing channel to Gladia API
- Synchronized generation of WebVTT segments corresponding to buffered media segments
- Stream commencement from earliest buffered segment after 60-second initialization period
- Continuous media segment accumulation with corresponding caption alignment
- Optimized resource management for sustained operational integrity
"""

import asyncio
import json
import subprocess
import sys
import signal
import os
import time
import aiofiles
from typing import Dict, List, Any, Optional, Set, Deque
from collections import deque
import requests
from websockets.legacy.client import WebSocketClientProtocol, connect as ws_connect
from websockets.exceptions import ConnectionClosedOK
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
import uvicorn
import logging

# === File Access Coordination ===
class FileAccessCoordinator:
    """Coordinates access to files to prevent race conditions."""
    def __init__(self):
        self._locks = {}  # Path-based locks
        self._master_lock = asyncio.Lock()
    
    async def acquire_lock(self, path):
        """Acquire a lock for a specific file path."""
        async with self._master_lock:
            if path not in self._locks:
                self._locks[path] = asyncio.Lock()
        return await self._locks[path].acquire()
    
    def release_lock(self, path):
        """Release a lock for a specific file path."""
        if path in self._locks:
            self._locks[path].release()
    
    async def __aenter__(self):
        """Context manager support."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure all locks are released."""
        async with self._master_lock:
            for lock in self._locks.values():
                if lock.locked():
                    lock.release()

# Create a global file coordinator instance
file_coordinator = FileAccessCoordinator()

async def atomic_file_write(path, content):
    """Write content to a file atomically using a temporary file."""
    temp_path = f"{path}.tmp"
    
    # Ensure parent directory exists
    parent_dir = os.path.dirname(path)
    os.makedirs(parent_dir, exist_ok=True)
    
    try:
        async with aiofiles.open(temp_path, "w", encoding="utf-8") as f:
            await f.write(content)
        os.replace(temp_path, path)  # Atomic operation on most file systems
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except:
                pass  # Best effort cleanup, ignore errors during cleanup
        raise e

async def atomic_file_write_with_retry(path, content, max_retries=3, retry_delay=0.5):
    """Write content to a file atomically with retries for resilience."""
    last_error = None
    for attempt in range(max_retries):
        try:
            await atomic_file_write(path, content)
            return  # Success
        except Exception as e:
            last_error = e
            if attempt == max_retries - 1:  # Last attempt
                break
            
            # Log and retry
            transcription_logger.warning(f"File operation failed (attempt {attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(retry_delay)
    
    # If we get here, all retries failed
    raise last_error

async def safe_read_file(path):
    """Read a file safely with proper locking."""
    async with file_coordinator:
        await file_coordinator.acquire_lock(path)
        try:
            if os.path.exists(path):
                async with aiofiles.open(path, "rb") as f:
                    return await f.read()
            return None
        finally:
            file_coordinator.release_lock(path)

# === Logging Configuration ===
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL
}

# Create different loggers for different types of messages
captions_logger = logging.getLogger('captions')
system_logger = logging.getLogger('system')
transcription_logger = logging.getLogger('transcription')

def setup_logging():
    """Configure the logging system based on environment variables."""
    # Get log levels from environment variables, default to INFO if not set
    captions_level = os.getenv('CAPTIONS_LOG_LEVEL', 'INFO')
    system_level = os.getenv('SYSTEM_LOG_LEVEL', 'INFO')
    transcription_level = os.getenv('TRANSCRIPTION_LOG_LEVEL', 'INFO')

    # Configure handlers and formatters
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # Setup individual loggers
    for logger_name, level in [
        (captions_logger, captions_level),
        (system_logger, system_level),
        (transcription_logger, transcription_level)
    ]:
        logger_name.addHandler(handler)
        logger_name.setLevel(LOG_LEVELS.get(level, logging.INFO))
        logger_name.propagate = False  # Prevent duplicate logging

# === Configuration Constants ===
GLADIA_API_URL = "https://api.gladia.io"
STREAM_URL = os.environ.get(
    "STREAM_URL", 
    "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"
)

HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "10"))
WINDOW_SIZE = int(os.environ.get("WINDOW_SIZE", "12"))
DEBUG_MESSAGES = os.environ.get("DEBUG_MESSAGES", "false").lower() == "true"

# Constants for multimedia buffer initialization
REQUIRED_BUFFER_SEGMENTS = 6  # Number of segments required before stream initialization
SEGMENT_BUFFER_SECONDS = SEGMENT_DURATION * REQUIRED_BUFFER_SEGMENTS  # 60 seconds with 10-second segments
TRANSCRIPTION_BUFFER_MIN = 3  # Minimum number of transcriptions needed (relaxed from 6 to ensure startup)

# Directory structure
HLS_OUTPUT_DIR = OUTPUT_DIR
VIDEO_DIR = os.path.join(HLS_OUTPUT_DIR, "video")
AUDIO_DIR = os.path.join(HLS_OUTPUT_DIR, "audio")
SUBTITLE_BASE_DIR = os.path.join(HLS_OUTPUT_DIR, "subtitles")

# Serving configuration
SERVING_WINDOW_SIZE = 2  # Number of segments in serving playlist
SERVING_DIR = os.path.join(HLS_OUTPUT_DIR, "serving")
SERVING_VIDEO_DIR = os.path.join(SERVING_DIR, "video")
SERVING_AUDIO_DIR = os.path.join(SERVING_DIR, "audio")
SERVING_SUBTITLE_BASE_DIR = os.path.join(SERVING_DIR, "subtitles")

# === Global State Management ===
# Caption storage with controlled memory usage (prevents memory leaks for 24/7 operation)
MAX_CUES_PER_LANGUAGE = 1000
caption_cues = {
    "ru": deque(maxlen=MAX_CUES_PER_LANGUAGE),  # Original Russian captions
    "en": deque(maxlen=MAX_CUES_PER_LANGUAGE),  # English translations
    "nl": deque(maxlen=MAX_CUES_PER_LANGUAGE)   # Dutch translations
}

# Process and timing management
ffmpeg_processes = {}
stream_start_time = None
transcription_start_time = None
first_segment_timestamp = None
segment_time_offset = None

# Synchronization status
ready_to_serve = False
initialization_complete = False

# Serving state
serving_segments = {
    "video": [],
    "audio": [],
    "subtitles": {"ru": [], "en": [], "nl": []}
}
serving_media_sequence = 0  # Current media sequence for serving playlists
delayed_start_time = None   # When we started serving (60s after script start)

# === Streaming Configuration for Gladia ===
STREAMING_CONFIGURATION = {
    "encoding": "wav/pcm",
    "sample_rate": 16000,
    "bit_depth": 16,
    "channels": 1,
    "language_config": {
        "languages": ["ru"],
        "code_switching": False,
    },
    "realtime_processing": {
        "words_accurate_timestamps": True,
        "custom_vocabulary": True,
        "custom_vocabulary_config": {
            "vocabulary": ["tvrain", "дождь", "телеканал", "россия", "украина", "москва", "санкт-петербург"]
        },
        "translation": True,
        "translation_config": {
            "target_languages": ["en", "nl"]  # English and Dutch
        }
    }
}

# === Utility Functions ===
def get_gladia_key() -> str:
    """Retrieve the Gladia API key from environment or command-line."""
    env_key = os.environ.get("GLADIA_API_KEY")
    if env_key:
        return env_key
        
    if len(sys.argv) != 2 or not sys.argv[1]:
        system_logger.error("You must provide a Gladia key as the first argument or set GLADIA_API_KEY environment variable.")
        sys.exit(1)
    return sys.argv[1]

def format_duration(seconds: float) -> str:
    """Format seconds into WebVTT time format: HH:MM:SS.mmm"""
    try:
        if isinstance(seconds, str):
            # Handle epoch-based timestamps
            if ":" in seconds and len(seconds.split(":")) > 2:
                parts = seconds.split(":")
                seconds = float(parts[-2]) * 60 + float(parts[-1])
        
        milliseconds = int(float(seconds) * 1000)
        hours = milliseconds // 3600000
        minutes = (milliseconds % 3600000) // 60000
        secs = (milliseconds % 60000) // 1000
        ms = milliseconds % 1000
        
        # Keep hours reasonable for WebVTT (max 99)
        hours = hours % 100
        
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    except (ValueError, TypeError) as e:
        system_logger.error(f"Invalid timestamp value: {seconds}. Error: {e}")
        return "00:00:00.000"

def init_live_session(config: Dict[str, Any]) -> Dict[str, str]:
    """Initialize a live transcription session with the Gladia API."""
    gladia_key = get_gladia_key()
    system_logger.info("Initializing Gladia live transcription session")
    try:
        response = requests.post(
            f"{GLADIA_API_URL}/v2/live",
            headers={"X-Gladia-Key": gladia_key},
            json=config,
            timeout=10,
        )
        if not response.ok:
            system_logger.error(f"Gladia API error: {response.status_code}: {response.text or response.reason}")
            sys.exit(response.status_code)
        return response.json()
    except requests.exceptions.RequestException as e:
        system_logger.error(f"Failed to initialize Gladia session: {e}")
        sys.exit(1)

def normalize_segment_number(segment_number: int) -> int:
    """Normalize an epoch-based segment number to a relative number."""
    global first_segment_timestamp
    
    if first_segment_timestamp is None:
        first_segment_timestamp = segment_number
        system_logger.info(f"First segment timestamp set to: {first_segment_timestamp}")
    
    return segment_number - first_segment_timestamp

def get_segment_timestamp(segment_number: int) -> float:
    """Convert a segment number to a timestamp (in seconds) relative to stream start."""
    normalized_segment = normalize_segment_number(segment_number)
    return normalized_segment * SEGMENT_DURATION

def cleanup_old_directories():
    """Clean up old output directories to start fresh."""
    try:
        import shutil
        for dir_path in [VIDEO_DIR, AUDIO_DIR, SUBTITLE_BASE_DIR]:
            if os.path.exists(dir_path):
                shutil.rmtree(dir_path)
                system_logger.info(f"Cleaned up directory: {dir_path}")
    except Exception as e:
        system_logger.error(f"Error cleaning up directories: {e}")

def ensure_directories_exist():
    """Ensure all required directories exist."""
    # Create main directories
    for dir_path in [VIDEO_DIR, AUDIO_DIR]:
        os.makedirs(dir_path, exist_ok=True)
    
    # Create subtitle directories for each language
    for lang in caption_cues.keys():
        os.makedirs(os.path.join(SUBTITLE_BASE_DIR, lang), exist_ok=True)
    
    # Create serving directories
    os.makedirs(SERVING_VIDEO_DIR, exist_ok=True)
    os.makedirs(SERVING_AUDIO_DIR, exist_ok=True)
    for lang in caption_cues.keys():
        os.makedirs(os.path.join(SERVING_SUBTITLE_BASE_DIR, lang), exist_ok=True)

# === Transcription Processing ===
async def stream_audio_to_gladia(websocket: WebSocketClientProtocol) -> None:
    """
    Stream audio directly from the source HLS to Gladia for real-time transcription.
    Uses a dedicated FFmpeg instance for low-latency transcription.
    """
    global ffmpeg_processes
    
    # FFmpeg command optimized for real-time streaming to Gladia
    ffmpeg_command = [
        "ffmpeg", "-re",
        "-i", STREAM_URL,
        "-ar", str(STREAMING_CONFIGURATION["sample_rate"]),
        "-ac", str(STREAMING_CONFIGURATION["channels"]),
        "-acodec", "pcm_s16le",
        "-f", "wav",
        "-bufsize", "16K",
        "pipe:1",
    ]
    
    system_logger.info(f"Starting direct audio streaming to Gladia")
    
    process = subprocess.Popen(
        ffmpeg_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10**6,
    )
    
    ffmpeg_processes["gladia_audio"] = process
    
    try:
        # Skip WAV header (44 bytes)
        header = process.stdout.read(44)
        
        while True:
            # Stream raw audio data directly
            audio_chunk = process.stdout.read(4096)  # Use larger chunks for efficiency
            if not audio_chunk:
                stderr = process.stderr.read()
                if stderr:
                    system_logger.error(f"FFmpeg audio streaming error: {stderr.decode()}")
                break
            
            try:
                await websocket.send(audio_chunk)
                await asyncio.sleep(0.01)  # Reduced sleep time for lower latency
            except ConnectionClosedOK:
                system_logger.info("Gladia WebSocket connection closed")
                break
            except Exception as e:
                system_logger.error(f"Error sending audio to Gladia: {e}")
                break
    
    except Exception as e:
        system_logger.error(f"Error in audio streaming: {e}")
    finally:
        try:
            await stop_recording(websocket)
        except Exception as e:
            system_logger.error(f"Error stopping recording: {e}")
        
        if process and process.poll() is None:
            process.terminate()
            system_logger.info("Terminated direct audio streaming process")

async def process_transcription_messages(websocket: WebSocketClientProtocol) -> None:
    """
    Process transcription and translation messages from Gladia.
    Store transcriptions and prepare for synchronization with video segments.
    """
    global transcription_start_time, segment_time_offset, initialization_complete
    
    transcription_logger.info("Starting to process transcription messages from Gladia")
    
    # Function to normalize and synchronize timestamps
    def normalize_timestamp(ts):
        """Convert Gladia timestamp to stream-relative timestamp."""
        if transcription_start_time is None:
            return float(ts)  # Cannot normalize yet
            
        # Base normalization - relative to first transcript
        normalized = float(ts) - transcription_start_time
        
        # Apply segment offset if available
        if segment_time_offset is not None:
            normalized += segment_time_offset
            
        return normalized
    
    async for message in websocket:
        try:
            content = json.loads(message)
            msg_type = content["type"]
            
            # Handle original Russian transcriptions
            if msg_type == "transcript" and content["data"]["is_final"]:
                utterance = content["data"]["utterance"]
                start = utterance["start"]
                end = utterance["end"]
                text = utterance["text"].strip()
                
                # Initialize timing reference on first transcript
                if transcription_start_time is None:
                    transcription_start_time = float(start)
                    transcription_logger.info(f"Initialized transcription_start_time to {transcription_start_time}")
                    
                    # We need to synchronize with segment timestamps once they're available
                    if first_segment_timestamp is not None:
                        # Simple offset to align transcription with segments
                        segment_time_offset = 0  # Start with no offset
                        transcription_logger.info(f"Timing references initialized - first transcript at {start}s, first segment at {first_segment_timestamp}")
                
                # Normalize timestamps to stream timeline
                stream_relative_start = normalize_timestamp(start)
                stream_relative_end = normalize_timestamp(end)
                
                # Log transcription data
                captions_logger.info(f"[RU] {format_duration(stream_relative_start)} --> {format_duration(stream_relative_end)} | {text}")
                
                # Store the cue with normalized stream timestamps
                await store_caption_cue("ru", stream_relative_start, stream_relative_end, text)
                
                # Assess transcription buffer status against initialization threshold
                if not initialization_complete and len(caption_cues["ru"]) >= TRANSCRIPTION_BUFFER_MIN:
                    initialization_complete = True
                    transcription_logger.info(f"Transcription buffer threshold achieved: {len(caption_cues['ru'])} cues accumulated")
            
            # Handle translations (English and Dutch)
            elif msg_type == "translation":
                try:
                    # Format 1: Complete structure with translated_utterance
                    if "utterance" in content["data"] and "translated_utterance" in content["data"]:
                        utterance = content["data"]["utterance"]
                        start = utterance["start"]
                        end = utterance["end"]
                        
                        translated_utterance = content["data"]["translated_utterance"]
                        text = translated_utterance["text"].strip()
                        lang = content["data"]["target_language"]
                        
                        # Normalize timestamps
                        stream_relative_start = normalize_timestamp(start)
                        stream_relative_end = normalize_timestamp(end)
                        
                        if lang in ["en", "nl"] and text:
                            captions_logger.info(f"[{lang.upper()}] {format_duration(stream_relative_start)} --> {format_duration(stream_relative_end)} | {text}")
                            await store_caption_cue(lang, stream_relative_start, stream_relative_end, text)
                    
                    # Format 2: Alternative structure (backup compatibility)
                    elif "translation" in content["data"]:
                        translation = content["data"]["translation"]
                        
                        # Get timestamps from either nested or outer level
                        if "start" in translation and "end" in translation:
                            start = translation["start"]
                            end = translation["end"]
                        else:
                            start = content["data"]["start"]
                            end = content["data"]["end"]
                        
                        # Normalize timestamps
                        stream_relative_start = normalize_timestamp(start)
                        stream_relative_end = normalize_timestamp(end)
                        
                        text = translation["text"].strip()
                        lang = translation["target_language"]
                        
                        if lang in ["en", "nl"] and text:
                            captions_logger.info(f"[{lang.upper()}] {format_duration(stream_relative_start)} --> {format_duration(stream_relative_end)} | {text}")
                            await store_caption_cue(lang, stream_relative_start, stream_relative_end, text)
                
                except Exception as e:
                    transcription_logger.error(f"Error processing translation: {e}")
                    transcription_logger.error(f"Translation message content: {json.dumps(content, indent=2)}")
            
            # Debug end-of-session message
            elif msg_type == "post_final_transcript":
                transcription_logger.info("\n#### End of session ####\n")
                transcription_logger.debug(json.dumps(content, indent=2, ensure_ascii=False))
        
        except json.JSONDecodeError:
            transcription_logger.error("Failed to decode message from Gladia")
        except Exception as e:
            transcription_logger.error(f"Error processing message from Gladia: {e}")

async def store_caption_cue(language, start_time, end_time, text):
    """Store a caption cue in memory and update corresponding VTT files if needed."""
    try:
        # Ensure valid timestamps
        start_time = float(start_time)
        end_time = float(end_time)
        
        if end_time <= start_time:
            transcription_logger.warning(f"Invalid timestamps: {start_time} -> {end_time}, adjusting end time")
            end_time = start_time + 1.0  # Ensure at least 1 second duration
        
        # Add to in-memory caption store
        caption_cues[language].append({
            "start": start_time,
            "end": end_time,
            "text": text
        })
        
        # Log caption storage for debugging
        transcription_logger.debug(f"Stored {language} caption: {format_duration(start_time)} -> {format_duration(end_time)}: {text[:30]}...")
        transcription_logger.debug(f"Total {language} captions in memory: {len(caption_cues[language])}")
        
        # For any existing segments that might contain this caption, update their VTT files
        if first_segment_timestamp is not None:
            await update_overlapping_vtt_segments(language, start_time, end_time)
        else:
            transcription_logger.warning("Cannot update VTT segments: first_segment_timestamp not initialized")
    except Exception as e:
        transcription_logger.error(f"Error storing caption cue: {e}")

async def update_overlapping_vtt_segments(language, start_time, end_time):
    """Update any VTT segments that would contain this caption timespan."""
    try:
        # Get current video segments from playlist
        video_playlist_path = os.path.join(VIDEO_DIR, "playlist.m3u8")
        if not os.path.exists(video_playlist_path):
            transcription_logger.warning(f"Video playlist not found, cannot update VTT segments")
            return
        
        current_segments = []
        async with aiofiles.open(video_playlist_path, 'r') as f:
            content = await f.read()
            for line in content.splitlines():
                if line.strip().endswith(".ts"):
                    seg_num = int(line.strip().replace("segment", "").replace(".ts", ""))
                    current_segments.append(seg_num)
        
        if not current_segments:
            transcription_logger.warning(f"No segments found in playlist, cannot update VTT segments")
            return
            
        transcription_logger.debug(f"Found {len(current_segments)} current segments: {current_segments}")
        transcription_logger.debug(f"Checking for segments overlapping with caption: {format_duration(start_time)} -> {format_duration(end_time)}")
        
        # For each segment, check if it overlaps with the caption timespan
        segments_updated = []
        for seg_num in current_segments:
            segment_start = (seg_num - first_segment_timestamp) * SEGMENT_DURATION
            segment_end = segment_start + SEGMENT_DURATION
            
            transcription_logger.debug(f"Checking segment {seg_num}: {format_duration(segment_start)} -> {format_duration(segment_end)}")
            
            # Check for overlap with caption timespan (use more flexible matching)
            if (start_time >= segment_start - 5 and start_time < segment_end + 5) or \
               (end_time > segment_start - 5 and end_time <= segment_end + 5) or \
               (start_time <= segment_start + 5 and end_time >= segment_end - 5):
                
                transcription_logger.debug(f"Found overlap! Updating {language} segment {seg_num}")
                # This segment needs to be updated
                success = await create_vtt_segment(seg_num, language)
                if success:
                    segments_updated.append(seg_num)
        
        # If no segments were updated due to the flexible matching, update the latest segment as fallback
        if not segments_updated and current_segments:
            latest_segment = max(current_segments)
            transcription_logger.info(f"No overlapping segments found, updating latest segment {latest_segment} as fallback")
            await create_vtt_segment(latest_segment, language)
            segments_updated.append(latest_segment)
        
        # Update the subtitle playlist after any changes
        if segments_updated:
            transcription_logger.debug(f"Updated segments {segments_updated}, updating subtitle playlist")
            await update_subtitle_playlist(language)
        else:
            transcription_logger.warning(f"No segments were updated for caption at {format_duration(start_time)}")
    
    except Exception as e:
        transcription_logger.error(f"Error updating overlapping VTT segments: {e}")

async def stop_recording(websocket: WebSocketClientProtocol) -> None:
    """Send a stop recording signal to Gladia."""
    system_logger.info("Ending the recording session...")
    try:
        await websocket.send(json.dumps({"type": "stop_recording"}))
        await asyncio.sleep(0.5)  # Give it time to process
    except Exception as e:
        system_logger.error(f"Error sending stop recording signal: {e}")

# === HLS and Subtitle Generation ===
async def create_hls_stream():
    """
    Create the HLS stream with separate audio and video tracks.
    This FFmpeg instance handles segment creation independently from transcription.
    """
    global ffmpeg_processes, stream_start_time
    
    # Set up directories
    ensure_directories_exist()
    
    # FFmpeg command for HLS segment creation
    ffmpeg_command = [
        "ffmpeg", "-y",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", STREAM_URL,
        # Audio output
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-map", "0:a",
        "-f", "hls",
        "-hls_time", str(SEGMENT_DURATION),
        "-hls_list_size", str(WINDOW_SIZE),
        "-hls_flags", "delete_segments+independent_segments+append_list+split_by_time",
        "-hls_segment_type", "mpegts",
        "-hls_allow_cache", "0",
        "-hls_start_number_source", "epoch",
        "-hls_segment_filename", os.path.join(AUDIO_DIR, "segment%d.ts"),
        os.path.join(AUDIO_DIR, "playlist.m3u8"),
        # Video output
        "-map", "0:v",
        "-c:v", "copy",
        "-f", "hls",
        "-hls_time", str(SEGMENT_DURATION),
        "-hls_list_size", str(WINDOW_SIZE),
        "-hls_flags", "delete_segments+independent_segments+append_list+split_by_time",
        "-hls_segment_type", "mpegts",
        "-hls_allow_cache", "0",
        "-hls_start_number_source", "epoch",
        "-hls_segment_filename", os.path.join(VIDEO_DIR, "segment%d.ts"),
        os.path.join(VIDEO_DIR, "playlist.m3u8")
    ]

    system_logger.info("Starting FFmpeg for HLS stream generation")
    system_logger.debug(f"FFmpeg Command: {' '.join(ffmpeg_command)}")
    
    try:
        # Start FFmpeg process with real-time error output
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        
        ffmpeg_processes["hls_generator"] = process
        stream_start_time = time.time()
        
        # Create initial master playlist
        await create_master_playlist()
        
        # Monitor FFmpeg output in real-time
        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                system_logger.error("FFmpeg process ended unexpectedly")
                raise RuntimeError("FFmpeg process failed")
            if line:
                if DEBUG_MESSAGES:
                    system_logger.debug(f"FFmpeg: {line.strip()}")
            
            # Check if FFmpeg process has failed
            if process.poll() is not None:
                stderr = process.stderr.read()
                system_logger.error(f"FFmpeg process failed: {stderr}")
                break
            
            await asyncio.sleep(0.1)
    
    except Exception as e:
        system_logger.error(f"Error in HLS stream generation: {e}")
        raise
    
    finally:
        # Cleanup processes
        if process and process.poll() is None:
            process.terminate()
            system_logger.info("Terminated HLS generation process")

async def create_master_playlist():
    """Create the master playlist with subtitle tracks."""
    master_playlist_path = os.path.join(HLS_OUTPUT_DIR, "master.m3u8")
    
    # Create subtitle directories
    for lang in caption_cues.keys():
        subtitle_dir = os.path.join(SUBTITLE_BASE_DIR, lang)
        os.makedirs(subtitle_dir, exist_ok=True)
    
    # Build the master playlist content
    content = "#EXTM3U\n#EXT-X-VERSION:3\n"
    content += "#EXT-X-INDEPENDENT-SEGMENTS\n"
    
    # Audio track
    content += '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio/playlist.m3u8"\n\n'
    
    # Subtitle tracks with explicit MIME type
    lang_names = {"ru": "Russian", "en": "English", "nl": "Dutch"}
    for lang, name in lang_names.items():
        default = "YES" if lang == "ru" else "NO"
        content += f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",DEFAULT={default},AUTOSELECT=YES,' + \
                  f'FORCED=NO,LANGUAGE="{lang}",URI="subtitles/{lang}/playlist.m3u8",CHARACTERISTICS="public.accessibility.transcribes-spoken-dialog"\n'
    
    # Add stream info with explicit subtitle codecs
    content += '\n#EXT-X-STREAM-INF:BANDWIDTH=2500000,CODECS="avc1.64001f,mp4a.40.2,wvtt",AUDIO="audio",SUBTITLES="subs"\n'
    content += 'video/playlist.m3u8\n'
    
    # Write master playlist with retries
    await atomic_file_write_with_retry(master_playlist_path, content)
    
    system_logger.info("Created master playlist with subtitle tracks and WebVTT codec")

async def create_vtt_segment(segment_number, language="ru"):
    """Create a WebVTT segment file for the given segment number and language."""
    if first_segment_timestamp is None:
        transcription_logger.warning(f"Cannot create VTT segment: first_segment_timestamp not initialized")
        return False
        
    try:
        # Calculate absolute segment time window
        segment_start_time = (segment_number - first_segment_timestamp) * SEGMENT_DURATION
        segment_end_time = segment_start_time + SEGMENT_DURATION
        
        transcription_logger.debug(f"Creating {language} VTT for segment {segment_number}")
        transcription_logger.debug(f"Segment time window: {format_duration(segment_start_time)} -> {format_duration(segment_end_time)}")
        
        content = "WEBVTT\n\n"
        cue_index = 1
        
        # Find cues that overlap with this segment's time window
        for cue in caption_cues[language]:
            try:
                cue_start = float(cue["start"])
                cue_end = float(cue["end"])
                
                # Skip invalid cues
                if cue_end <= cue_start:
                    transcription_logger.warning(f"Skipping invalid cue: start={cue_start}, end={cue_end}")
                    continue
                
                # Strict overlap check - the cue must actually overlap with this segment
                if cue_start < segment_end_time and cue_end > segment_start_time:
                    # Calculate relative timing and clamp to segment boundaries
                    relative_start = max(0.0, cue_start - segment_start_time)
                    relative_end = min(SEGMENT_DURATION, cue_end - segment_start_time)
                    
                    # Handle case where cue carries over from previous segment
                    if cue_start < segment_start_time:
                        relative_start = 0.0
                    
                    # Handle case where cue carries over to next segment
                    if cue_end > segment_end_time:
                        relative_end = float(SEGMENT_DURATION)
                    
                    transcription_logger.debug(f"Adding cue: {format_duration(relative_start)} -> {format_duration(relative_end)}")
                    transcription_logger.debug(f"Text: {cue['text']}")
                    
                    content += f"{cue_index}\n"
                    content += f"{format_duration(relative_start)} --> {format_duration(relative_end)}\n"
                    content += f"{cue['text']}\n\n"
                    cue_index += 1
            except (ValueError, KeyError) as e:
                transcription_logger.error(f"Error processing cue: {e}")
                continue
        
        # Write the segment file atomically
        segment_path = os.path.join(SUBTITLE_BASE_DIR, language, f"segment{segment_number}.vtt")
        await atomic_file_write_with_retry(segment_path, content)
            
        transcription_logger.debug(f"Created {language} segment {segment_number} with {cue_index-1} cues")
        return True
        
    except Exception as e:
        transcription_logger.error(f"Error in create_vtt_segment: {str(e)}")
        return False

async def update_subtitle_playlist(language="ru"):
    """
    Update the subtitle playlist for the given language.
    Ensures subtitle segments match video segments exactly.
    """
    subtitle_dir = os.path.join(SUBTITLE_BASE_DIR, language)
    os.makedirs(subtitle_dir, exist_ok=True)
    playlist_path = os.path.join(subtitle_dir, "playlist.m3u8")

    # Get video playlist state - this is critical for synchronization
    video_playlist = os.path.join(VIDEO_DIR, "playlist.m3u8")
    media_sequence = 0
    segments = []
    
    if os.path.exists(video_playlist):
        async with aiofiles.open(video_playlist, 'r') as f:
            content = await f.read()
            for line in content.splitlines():
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    media_sequence = int(line.strip().split(":")[1])
                elif line.strip().endswith(".ts"):
                    seg_num = int(line.strip().replace("segment", "").replace(".ts", ""))
                    segments.append(seg_num)

    # Create matching subtitle playlist with EXACTLY the same segments as video
    content = "#EXTM3U\n#EXT-X-VERSION:3\n"
    content += "#EXT-X-INDEPENDENT-SEGMENTS\n"  # Add independent segments directive
    content += f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n"
    content += f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n"

    # Ensure we reference the exact same segments in the same order as video playlist
    for seg_num in segments:
        content += f"#EXTINF:{SEGMENT_DURATION}.0,\n"
        content += f"segment{seg_num}.vtt\n"

    # Write playlist atomically with retries
    await atomic_file_write_with_retry(playlist_path, content)
    
    system_logger.debug(f"Updated {language} subtitle playlist (media_sequence: {media_sequence}, segments: {segments})")

async def monitor_segments_and_create_vtt():
    """
    Monitor video segments and create corresponding VTT segments.
    This ensures subtitle segments are created for every video segment.
    """
    global first_segment_timestamp, ready_to_serve, segment_time_offset
    
    processed_segments = set()
    retry_count = 0
    max_retries = 10
    
    while True:
        try:
            # Get current video segments
            video_playlist = os.path.join(VIDEO_DIR, "playlist.m3u8")
            if not os.path.exists(video_playlist):
                if retry_count < max_retries:
                    system_logger.info("Video playlist not found, waiting...")
                    retry_count += 1
                    await asyncio.sleep(1)
                    continue
                else:
                    system_logger.error(f"Video playlist not found after {max_retries} attempts")
                    return
            
            retry_count = 0  # Reset retry count when successful
            
            current_segments = []
            async with aiofiles.open(video_playlist, 'r') as f:
                content = await f.read()
                for line in content.splitlines():
                    if line.strip().endswith(".ts"):
                        seg_num = int(line.strip().replace("segment", "").replace(".ts", ""))
                        current_segments.append(seg_num)
            
            # Proceed only when segment data is available for synchronization
            if not current_segments:
                system_logger.info("Waiting for initial segment creation to establish temporal reference frame...")
                await asyncio.sleep(1)
                continue
            
            # Initialize first_segment_timestamp if not set
            if first_segment_timestamp is None and current_segments:
                first_segment_timestamp = min(current_segments)
                system_logger.info(f"Initialized first_segment_timestamp to {first_segment_timestamp}")
                
                # Important: Synchronize timing references
                if transcription_start_time is not None:
                    # Initialize with a simpler approach - just using normalized timestamps
                    segment_time_offset = 0
                    system_logger.info(f"Initialized segment_time_offset to 0 for simplified timestamp normalization")
                    system_logger.info(f"Transcription start time: {transcription_start_time}, First segment: {first_segment_timestamp}")
            
            system_logger.info(f"Current segments: {current_segments}")
            system_logger.info(f"Processed segments: {processed_segments}")
            
            # Force recreation of all subtitle segments periodically to ensure they have the latest captions
            force_update_all = len(processed_segments) % 10 == 0
            if force_update_all:
                system_logger.info("Periodic full update of all subtitle segments")
            
            # Process new or updated segments
            for seg_num in current_segments:
                if seg_num not in processed_segments or force_update_all:
                    if seg_num not in processed_segments:
                        system_logger.info(f"Processing new segment: {seg_num}")
                    else:
                        system_logger.info(f"Refreshing segment: {seg_num}")
                    
                    # Create VTT segments for all languages
                    all_successful = True
                    for lang in caption_cues.keys():
                        success = await create_vtt_segment(seg_num, lang)
                        if success:
                            await update_subtitle_playlist(lang)
                        else:
                            all_successful = False
                    
                    if seg_num not in processed_segments:
                        processed_segments.add(seg_num)
                    
                    # Validate buffer initialization criteria prior to service commencement
                    if not ready_to_serve and len(processed_segments) >= REQUIRED_BUFFER_SEGMENTS:
                        if initialization_complete and all_successful:  # Verify transcription data availability
                            ready_to_serve = True
                            system_logger.info(f"Buffer initialization complete: {len(processed_segments)} segments with synchronized transcriptions")
            
            # Clean up old segments
            if current_segments:
                min_segment = min(current_segments)
                processed_segments = {s for s in processed_segments if s >= min_segment}
            
            await asyncio.sleep(1)  # Check every second
            
        except Exception as e:
            system_logger.error(f"Error in segment monitoring: {str(e)}")
            await asyncio.sleep(1)

# === FastAPI Server ===
app = FastAPI()

@app.get("/")
async def root():
    """Serve a minimal page that auto-redirects to the player."""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta http-equiv="refresh" content="0;URL='/player.html'" />
    </head>
    <body>
        <p>Redirecting to player...</p>
    </body>
    </html>
    """)

@app.get("/player.html")
async def player_page():
    """Serve the video player page."""
    return HTMLResponse(await generate_player_html())

@app.get("/master.m3u8")
async def master_playlist():
    """Serve the master playlist from the serving directory."""
    global ready_to_serve
    
    if not ready_to_serve:
        return PlainTextResponse(content="Media buffer initialization in progress", status_code=404)
    
    file_path = os.path.join(SERVING_DIR, "master.m3u8")
    if not os.path.exists(file_path):
        return PlainTextResponse(content="Playlist not found", status_code=404)
        
    return FileResponse(
        path=file_path,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS"
        }
    )

@app.get("/{file_path:path}")
async def serve_file(file_path: str):
    """Serve files from the serving directory."""
    global ready_to_serve
    
    # Restrict access to primary playlists until buffer initialization is complete
    if file_path in ["video/playlist.m3u8", "audio/playlist.m3u8"] and not ready_to_serve:
        return PlainTextResponse(content="Media buffer initialization in progress", status_code=404)
    
    # Try serving from serving directory first
    full_path = os.path.join(SERVING_DIR, file_path)
    
    # If not in serving directory and it's a segment file, try the main output directory
    if not os.path.exists(full_path) and (file_path.endswith(".ts") or file_path.endswith(".vtt")):
        full_path = os.path.join(HLS_OUTPUT_DIR, file_path)
    
    if not os.path.exists(full_path):
        return PlainTextResponse(content="File not found", status_code=404)
    
    # Special handling for VTT files to ensure proper UTF-8 encoding and atomic reading
    if file_path.endswith(".vtt"):
        content = await safe_read_file(full_path)
        if content is None:
            return PlainTextResponse(content="File not found", status_code=404)
            
        return Response(
            content=content,
            media_type="text/vtt; charset=utf-8",
            headers={
                "Content-Length": str(len(content)),
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )
    
    # Handle other file types normally
    content_type = "application/octet-stream"
    if file_path.endswith(".m3u8"):
        content_type = "application/vnd.apple.mpegurl"
    elif file_path.endswith(".ts"):
        content_type = "video/mp2t"
    
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    
    return FileResponse(
        path=full_path,
        media_type=content_type,
        headers=headers
    )

@app.options("/{file_path:path}")
async def options_handler(file_path: str):
    """Handle OPTIONS requests for CORS preflight."""
    return PlainTextResponse(
        content="",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Max-Age": "86400"  # 24 hours
        }
    )

async def generate_player_html():
    """Generate a minimal HTML player supporting HLS with captions."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>TV Rain Live Stream</title>
    <style>
        body {
            margin: 0;
            padding: 0;
            background: #000;
            color: #fff;
            font-family: Arial, sans-serif;
            overflow: hidden;
        }
        .player-container {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
        }
        video {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .controls {
            position: absolute;
            bottom: 60px;
            left: 0;
            width: 100%;
            display: flex;
            justify-content: center;
            gap: 10px;
            z-index: 10;
        }
        button {
            padding: 8px 15px;
            background: rgba(0,0,0,0.7);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        button:hover {
            background: rgba(0,0,0,0.9);
            border-color: rgba(255,255,255,0.5);
        }
        button.active {
            background: rgba(40,120,200,0.7);
            border-color: rgba(255,255,255,0.7);
        }
        /* Enhance subtitle display */
        ::cue {
            background-color: rgba(0, 0, 0, 0.7);
            color: white;
            font-size: 1.2em;
        }
    </style>
</head>
<body>
    <div class="player-container">
        <video id="video" controls autoplay></video>
        
        <div class="controls">
            <button onclick="player.selectTextTrack('ru')" id="btn-ru">Russian</button>
            <button onclick="player.selectTextTrack('en')" id="btn-en">English</button>
            <button onclick="player.selectTextTrack('nl')" id="btn-nl">Dutch</button>
            <button onclick="player.disableTextTrack()" id="btn-none">No Subtitles</button>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.4.12"></script>
    <script>
        const player = {
            hlsInstance: null,
            videoElement: document.getElementById('video'),
            
            init() {
                if (!Hls.isSupported()) {
                    console.error('HLS.js is not supported in this browser');
                    return;
                }
                
                this.hlsInstance = new Hls({
                    debug: false,
                    enableWebVTT: true,
                    renderTextTracksNatively: true,
                    startLevel: -1,
                    defaultAudioCodec: 'mp4a.40.2',
                    maxBufferLength: 30,
                    maxMaxBufferLength: 600,
                    startPosition: 0,
                    liveSyncDurationCount: 3,
                    liveMaxLatencyDurationCount: 10,
                    enableWorker: true,
                    lowLatencyMode: true,
                    backBufferLength: 90
                });
                
                this.setupEventListeners();
                this.loadStream();
            },
            
            loadStream() {
                const manifestUrl = 'master.m3u8';
                console.log(`Loading manifest: ${manifestUrl}`);
                
                this.hlsInstance.loadSource(manifestUrl);
                this.hlsInstance.attachMedia(this.videoElement);
            },
            
            setupEventListeners() {
                this.hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
                    console.log('Manifest parsed, attempting playback...');
                    this.videoElement.play()
                        .then(() => {
                            console.log('Playback started');
                            this.selectTextTrack('ru');
                        })
                        .catch(error => {
                            console.error(`Playback failed: ${error.message}`);
                        });
                });
                
                // Add debug events for subtitle tracking
                this.hlsInstance.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, (_, data) => {
                    console.log('Subtitle tracks updated:', data.subtitleTracks);
                });
                
                this.hlsInstance.on(Hls.Events.SUBTITLE_TRACK_LOADED, (_, data) => {
                    console.log('Subtitle track loaded:', data);
                });
                
                this.hlsInstance.on(Hls.Events.ERROR, (event, data) => {
                    if (data.fatal) {
                        console.error(`Fatal error: ${data.type} - ${data.details}`);
                        switch(data.type) {
                            case Hls.ErrorTypes.NETWORK_ERROR:
                                this.hlsInstance.startLoad();
                                break;
                            case Hls.ErrorTypes.MEDIA_ERROR:
                                this.hlsInstance.recoverMediaError();
                                break;
                            default:
                                this.reloadPlayer();
                                break;
                        }
                    }
                });
            },
            
            selectTextTrack(language) {
                if (!this.hlsInstance) return;
                
                const tracks = this.hlsInstance.subtitleTracks;
                console.log('Available subtitle tracks:', tracks);
                
                const trackId = tracks.findIndex(track => track.lang === language);
                
                if (trackId !== -1) {
                    this.hlsInstance.subtitleTrack = trackId;
                    console.log(`Selected ${language} subtitles (track ${trackId})`);
                    
                    document.querySelectorAll('.controls button').forEach(btn => {
                        btn.classList.remove('active');
                    });
                    document.getElementById(`btn-${language}`).classList.add('active');
                } else {
                    console.warn(`No subtitle track found for language: ${language}`);
                }
            },
            
            disableTextTrack() {
                if (!this.hlsInstance) return;
                
                this.hlsInstance.subtitleTrack = -1;
                console.log('Disabled subtitles');
                
                document.querySelectorAll('.controls button').forEach(btn => {
                    btn.classList.remove('active');
                });
                document.getElementById('btn-none').classList.add('active');
            },
            
            reloadPlayer() {
                console.log('Reloading player...');
                if (this.hlsInstance) {
                    this.hlsInstance.destroy();
                }
                this.init();
            }
        };
        
        // Initialize player when the page loads
        window.addEventListener('load', () => {
            player.init();
        });
    </script>
</body>
</html>
"""

# === Main Application Flow ===
async def transcription_main():
    """Main function to coordinate the transcription and HLS generation process."""
    global ffmpeg_processes, ready_to_serve
    
    system_logger.info("\n===== Starting Rainscribe with native HLS subtitle integration =====")
    
    # Setup logging first
    setup_logging()
    
    # Clear existing files and create directories
    cleanup_old_directories()
    ensure_directories_exist()
    
    try:
        # Start web server first
        web_server_task = asyncio.create_task(start_web_server())
        await asyncio.sleep(1)  # Give the web server a moment to start
        
        # Start FFmpeg for HLS generation
        hls_task = asyncio.create_task(create_hls_stream())
        await asyncio.sleep(2)  # Give FFmpeg time to start creating segments
        
        # Initialize Gladia transcription session
        response = init_live_session(STREAMING_CONFIGURATION)
        transcription_logger.info(f"Gladia session initialized: {response['id']}")
        
        # Start transcription and VTT generation
        async with ws_connect(response["url"]) as websocket:
            transcription_logger.info("\n===== Transcription session started =====")
            
            # Start tasks in parallel
            tasks = [
                web_server_task,
                hls_task,
                asyncio.create_task(process_transcription_messages(websocket)),
                asyncio.create_task(stream_audio_to_gladia(websocket)),
                asyncio.create_task(monitor_segments_and_create_vtt()),
                asyncio.create_task(manage_drip_feed())  # Add drip-feed task
            ]
            
            # Wait for any task to complete (which shouldn't happen unless there's an error)
            await asyncio.gather(*tasks, return_exceptions=True)
            
    except asyncio.CancelledError:
        system_logger.info("Tasks cancelled - shutting down...")
    except Exception as e:
        system_logger.error(f"Error in main process: {e}")
    finally:
        # Cleanup all processes
        for name, process in ffmpeg_processes.items():
            if process and process.poll() is None:
                process.terminate()
                system_logger.info(f"Terminated {name} process")

async def start_web_server():
    """Start the FastAPI web server."""
    config = uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()

# === Signal Handling ===
def handle_exit(*args):
    """Handle exit signals gracefully."""
    system_logger.info("Received exit signal, cleaning up...")
    for name, process in ffmpeg_processes.items():
        if process and process.poll() is None:
            process.terminate()
            system_logger.info(f"Terminated {name} process")
    sys.exit(0)

# === Drip-Feed Management ===
async def manage_drip_feed():
    """
    Manages the drip-feed of segments from buffer to serving playlists,
    maintaining a constant 60-second delay behind the source stream.
    """
    global ready_to_serve, delayed_start_time, serving_media_sequence, serving_segments
    
    # Wait until buffer initialization is complete
    while not (len(processed_segments) >= REQUIRED_BUFFER_SEGMENTS and initialization_complete):
        await asyncio.sleep(1)
    
    # Track the first segment we'll serve
    first_serving_segment = min(processed_segments)
    delayed_start_time = time.time()
    system_logger.info(f"Starting drip-feed with first segment: {first_serving_segment}")
    
    # Initialize serving segment with the first segment
    serving_segments["video"] = [first_serving_segment]
    serving_segments["audio"] = [first_serving_segment]
    for lang in caption_cues.keys():
        serving_segments["subtitles"][lang] = [first_serving_segment]
    
    # Create initial serving playlists
    await create_serving_master_playlist()
    await update_serving_media_playlists()
    
    # Signal that we're ready to serve
    ready_to_serve = True
    
    # Drip-feed loop - add a new segment every SEGMENT_DURATION seconds
    next_segment_time = delayed_start_time + SEGMENT_DURATION
    next_segment_index = 1  # Index relative to first_serving_segment
    
    while True:
        try:
            # Wait until it's time for the next segment
            now = time.time()
            if now < next_segment_time:
                await asyncio.sleep(0.1)
                continue
            
            # Time to add the next segment
            next_segment = first_serving_segment + next_segment_index
            
            # Check if this segment exists and has subtitles
            video_segment_path = os.path.join(VIDEO_DIR, f"segment{next_segment}.ts") 
            if not os.path.exists(video_segment_path):
                system_logger.warning(f"Segment {next_segment} not ready, waiting...")
                await asyncio.sleep(0.5)
                continue
                
            # Update serving segments lists - maintain SERVING_WINDOW_SIZE
            for media_type in ["video", "audio"]:
                serving_segments[media_type].append(next_segment)
                if len(serving_segments[media_type]) > SERVING_WINDOW_SIZE:
                    serving_segments[media_type].pop(0)
                    serving_media_sequence += 1
            
            # Update subtitle segments
            for lang in caption_cues.keys():
                serving_segments["subtitles"][lang].append(next_segment)
                if len(serving_segments["subtitles"][lang]) > SERVING_WINDOW_SIZE:
                    serving_segments["subtitles"][lang].pop(0)
            
            # Update all serving playlists
            await update_serving_media_playlists()
            
            system_logger.info(f"Added segment {next_segment} to serving playlists (sequence: {serving_media_sequence})")
            
            # Schedule next segment
            next_segment_time += SEGMENT_DURATION
            next_segment_index += 1
            
        except Exception as e:
            system_logger.error(f"Error in drip feed: {e}")
            await asyncio.sleep(1)

async def create_serving_master_playlist():
    """Create a master playlist for the serving stream."""
    master_playlist_path = os.path.join(SERVING_DIR, "master.m3u8")
    
    content = "#EXTM3U\n#EXT-X-VERSION:3\n"
    content += "#EXT-X-INDEPENDENT-SEGMENTS\n\n"
    
    # Audio track
    content += f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio/playlist.m3u8"\n\n'
    
    # Subtitle tracks
    lang_names = {"ru": "Russian", "en": "English", "nl": "Dutch"}
    for lang, name in lang_names.items():
        default = "YES" if lang == "ru" else "NO"
        content += f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",DEFAULT={default},AUTOSELECT=YES,' + \
                  f'FORCED=NO,LANGUAGE="{lang}",URI="subtitles/{lang}/playlist.m3u8",CHARACTERISTICS="public.accessibility.transcribes-spoken-dialog"\n'
    
    # Add stream info
    content += f'\n#EXT-X-STREAM-INF:BANDWIDTH=2500000,CODECS="avc1.64001f,mp4a.40.2,wvtt",AUDIO="audio",SUBTITLES="subs"\n'
    content += 'video/playlist.m3u8\n'
    
    await atomic_file_write_with_retry(master_playlist_path, content)
    system_logger.info("Created serving master playlist")

async def update_serving_media_playlists():
    """Update all serving playlists (video, audio, subtitles)."""
    # Update video playlist
    await update_serving_playlist("video", "ts")
    
    # Update audio playlist
    await update_serving_playlist("audio", "ts")
    
    # Update subtitle playlists
    for lang in caption_cues.keys():
        await update_serving_playlist(f"subtitles/{lang}", "vtt")

async def update_serving_playlist(media_type, extension):
    """Update a specific serving playlist."""
    if "/" in media_type:
        # Handle subtitle directories
        playlist_path = os.path.join(SERVING_DIR, f"{media_type}/playlist.m3u8")
        segment_key = "subtitles/" + media_type.split("/")[1]
        segments = serving_segments["subtitles"][media_type.split("/")[1]]
    else:
        playlist_path = os.path.join(SERVING_DIR, f"{media_type}/playlist.m3u8")
        segment_key = media_type
        segments = serving_segments[media_type]
    
    # Create playlist content
    content = "#EXTM3U\n#EXT-X-VERSION:3\n"
    content += "#EXT-X-INDEPENDENT-SEGMENTS\n"
    content += f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n"
    content += f"#EXT-X-MEDIA-SEQUENCE:{serving_media_sequence}\n"
    
    # Add each segment
    for seg_num in segments:
        content += f"#EXTINF:{SEGMENT_DURATION}.0,\n"
        
        # For serving playlists, we use symbolic links to the original segments
        content += f"segment{seg_num}.{extension}\n"
        
        # Ensure symbolic link exists (create it if not)
        source_path = os.path.join(HLS_OUTPUT_DIR, f"{media_type}/segment{seg_num}.{extension}")
        link_path = os.path.join(SERVING_DIR, f"{media_type}/segment{seg_num}.{extension}")
        
        # Create parent directory if needed
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        
        # Create a hard link (copy) rather than symlink for simplicity
        if not os.path.exists(link_path) and os.path.exists(source_path):
            try:
                await asyncio.to_thread(os.link, source_path, link_path)
            except OSError:
                # If hard link fails (e.g., cross-device), just copy the file
                import shutil
                await asyncio.to_thread(shutil.copy2, source_path, link_path)
    
    await atomic_file_write_with_retry(playlist_path, content)

if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    try:
        asyncio.run(transcription_main())
    except KeyboardInterrupt:
        system_logger.info("\nShutting down gracefully...")
        sys.exit(0)