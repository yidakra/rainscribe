#!/usr/bin/env python3
"""
Live Transcription with Native HLS Subtitle Integration for HLS Streaming

Improvements:
- Extracts audio from HLS stream using FFmpeg and streams it to Gladia API
- Receives real-time transcriptions and translations via WebSocket
- Creates segmented WebVTT files that align with HLS segments
- Uses native HLS subtitle capabilities instead of WebSockets for playback
- Provides proper subtitle synchronization for viewers joining mid-stream
- Maintains a sliding window of subtitle segments to match video segments
- Supports multiple languages (Russian + English and Dutch translations)
- Simplified player interface relying on native caption features
- Handles epoch-based segment numbering for proper timing alignment
- Automatically cleans up old segments to prevent disk space issues
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
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import uvicorn

# === Logging Setup ===
class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, data):
        for f in self.files:
            f.write(data)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()
    def isatty(self):
        return False

LOG_FILENAME = "rainscribe_run.log"
log_file = open(LOG_FILENAME, "w", encoding="utf-8")
sys.stdout = Tee(sys.stdout, log_file)
sys.stderr = Tee(sys.stderr, log_file)

# === Configuration Constants ===
GLADIA_API_URL = "https://api.gladia.io"
EXAMPLE_HLS_STREAM_URL = os.environ.get(
    "STREAM_URL", 
    "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"
)

MIN_CUES = int(os.environ.get("MIN_CUES", "2"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

# Directory for HLS output
HLS_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

# HLS configuration
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "10"))  # 10 seconds per segment
WINDOW_SIZE = int(os.environ.get("WINDOW_SIZE", "6"))            # 5 segments in the playlist

# Debug flag
DEBUG_MESSAGES = os.environ.get("DEBUG_MESSAGES", "false").lower() == "true"

# === Global In-Memory Storage for Caption Cues ===
# Modified to use deque with max length to prevent memory leaks in 24/7 streaming
MAX_CUES_PER_LANGUAGE = 1000  # Adjust as needed
caption_cues = {
    "ru": deque(maxlen=MAX_CUES_PER_LANGUAGE),  # Original Russian captions
    "en": deque(maxlen=MAX_CUES_PER_LANGUAGE),  # English translations
    "nl": deque(maxlen=MAX_CUES_PER_LANGUAGE)   # Dutch translations
}

# Global process handles
ffmpeg_audio_process = None
stream_start_time = None  # Track when the stream started
transcription_start_time = None  # Track when transcription started
first_segment_timestamp = None  # First video segment timestamp
segment_counter = 0  # Counter for segment processing

# Add new global variable
segment_time_offset = None  # Tracks time offset between transcription and segments

# === Streaming Configuration ===
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
            "vocabulary": ["Example", "Custom", "Words"]
        },
        "translation": True,
        "translation_config": {
            "target_languages": ["en", "nl"]  # English and Dutch
        }
    }
}

# === Utility Functions ===
def format_duration(seconds: float) -> str:
    """Format seconds into WebVTT time format: HH:MM:SS.mmm"""
    if isinstance(seconds, str):
        # Handle epoch-based timestamps (e.g., "4841161:17:30.000")
        if ":" in seconds and len(seconds.split(":")) > 2:
            # Extract just the MM:SS.mmm part from the end
            parts = seconds.split(":")
            seconds = float(parts[-2]) * 60 + float(parts[-1])
    
    # Convert to milliseconds
    try:
        milliseconds = int(float(seconds) * 1000)
    except (ValueError, TypeError):
        print(f"Invalid timestamp value: {seconds}")
        milliseconds = 0
    
    # Calculate hours, minutes, seconds
    hours = milliseconds // 3600000
    minutes = (milliseconds % 3600000) // 60000
    secs = (milliseconds % 60000) // 1000
    ms = milliseconds % 1000
    
    # Keep hours reasonable for WebVTT (max 99)
    hours = hours % 100
    
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"

def get_gladia_key() -> str:
    """Retrieve the Gladia API key from environment or command-line."""
    env_key = os.environ.get("GLADIA_API_KEY")
    if env_key:
        return env_key
        
    if len(sys.argv) != 2 or not sys.argv[1]:
        print("You must provide a Gladia key as the first argument or set GLADIA_API_KEY environment variable.")
        sys.exit(1)
    return sys.argv[1]

def init_live_session(config: Dict[str, Any]) -> Dict[str, str]:
    """Initialize a live transcription session with the Gladia API."""
    gladia_key = get_gladia_key()
    response = requests.post(
        f"{GLADIA_API_URL}/v2/live",
        headers={"X-Gladia-Key": gladia_key},
        json=config,
        timeout=3,
    )
    if not response.ok:
        print(f"{response.status_code}: {response.text or response.reason}")
        sys.exit(response.status_code)
    return response.json()

def normalize_segment_number(segment_number: int) -> int:
    """
    Normalize an epoch-based segment number to a smaller, relative number.
    This helps with large epoch-based segment numbers.
    """
    global first_segment_timestamp
    
    if first_segment_timestamp is None:
        first_segment_timestamp = segment_number
        print(f"First segment timestamp set to: {first_segment_timestamp}")
    
    # Return segment number relative to the first segment we've seen
    return segment_number - first_segment_timestamp

def get_segment_timestamp(segment_number: int) -> float:
    """
    Convert a segment number to a timestamp (in seconds) relative to stream start.
    This is crucial for mapping segments to transcription times.
    """
    normalized_segment = normalize_segment_number(segment_number)
    return normalized_segment * SEGMENT_DURATION

async def create_vtt_segment(segment_number, language="ru"):
    """
    Create a WebVTT segment file for the given segment number and language.
    Each segment covers a specified duration of time based on segment number.
    """
    if first_segment_timestamp is None:
        print(f"Cannot create VTT segment: first_segment_timestamp not initialized")
        return False
        
    try:
        # Calculate absolute segment time window
        segment_start_time = (segment_number - first_segment_timestamp) * SEGMENT_DURATION
        segment_end_time = segment_start_time + SEGMENT_DURATION
        
        print(f"\nCreating {language} VTT for segment {segment_number}")
        print(f"Segment time window: {format_duration(segment_start_time)} -> {format_duration(segment_end_time)}")
        
        content = "WEBVTT\n\n"
        cue_index = 1
        
        # Find cues that overlap with this segment's time window
        for cue in caption_cues[language]:
            try:
                cue_start = float(cue["start"])
                cue_end = float(cue["end"])
                
                # Skip invalid cues
                if cue_end <= cue_start:
                    continue
                
                # Check if cue overlaps with segment window (using absolute times)
                if (cue_start >= segment_start_time and cue_start < segment_end_time) or \
                   (cue_end > segment_start_time and cue_end <= segment_end_time) or \
                   (cue_start <= segment_start_time and cue_end >= segment_end_time):
                    
                    # Calculate cue timing relative to segment start
                    relative_start = max(0, cue_start - segment_start_time)
                    relative_end = min(SEGMENT_DURATION, cue_end - segment_start_time)
                    
                    print(f"Adding cue: {format_duration(relative_start)} -> {format_duration(relative_end)}")
                    print(f"Text: {cue['text']}")
                    
                    content += f"{cue_index}\n"
                    content += f"{format_duration(relative_start)} --> {format_duration(relative_end)}\n"
                    content += f"{cue['text']}\n\n"
                    cue_index += 1
            except (ValueError, KeyError) as e:
                print(f"Error processing cue: {e}")
                continue
        
        # Write the segment file even if empty (required for HLS)
        segment_path = os.path.join(HLS_OUTPUT_DIR, "subtitles", language, f"segment{segment_number}.vtt")
        async with aiofiles.open(segment_path, "w", encoding="utf-8") as f:
            await f.write(content)
            
        print(f"Created {language} segment {segment_number} with {cue_index-1} cues")
        return True
        
    except Exception as e:
        print(f"Error in create_vtt_segment: {str(e)}")
        return False

async def update_subtitle_playlist(language="ru"):
    """
    Update the subtitle playlist for the given language.
    Ensures subtitle segments match video segments exactly.
    """
    subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", language)
    os.makedirs(subtitle_dir, exist_ok=True)
    playlist_path = os.path.join(subtitle_dir, "playlist.m3u8")

    # Get video playlist state - this is critical for synchronization
    video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
    media_sequence = 0
    segments = []
    
    if os.path.exists(video_playlist):
        with open(video_playlist, 'r') as f:
            for line in f:
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    media_sequence = int(line.strip().split(":")[1])
                elif line.strip().endswith(".ts"):
                    seg_num = int(line.strip().replace("segment", "").replace(".ts", ""))
                    segments.append(seg_num)

    # Create matching subtitle playlist with EXACTLY the same segments as video
    content = "#EXTM3U\n#EXT-X-VERSION:3\n"
    content += f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n"
    content += f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n"

    # Ensure we reference the exact same segments in the same order as video playlist
    for seg_num in segments:
        content += f"#EXTINF:{SEGMENT_DURATION}.0,\n"
        content += f"segment{seg_num}.vtt\n"

    async with aiofiles.open(playlist_path, "w", encoding="utf-8") as f:
        await f.write(content)
    
    print(f"Updated {language} subtitle playlist (media_sequence: {media_sequence}, segments: {segments})")

async def create_master_playlist():
    """
    Create the master playlist with subtitle tracks.
    """
    master_playlist_path = os.path.join(HLS_OUTPUT_DIR, "master.m3u8")
    
    # Create subtitle directories
    for lang in caption_cues.keys():
        subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang)
        os.makedirs(subtitle_dir, exist_ok=True)
    
    content = "#EXTM3U\n#EXT-X-VERSION:3\n\n"
    content += '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio/playlist.m3u8"\n'
    
    # Add subtitle tracks
    lang_names = {"ru": "Russian", "en": "English", "nl": "Dutch"}
    for lang, name in lang_names.items():
        default = "YES" if lang == "ru" else "NO"
        content += f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",DEFAULT={default},AUTOSELECT=YES,FORCED=NO,LANGUAGE="{lang}",URI="subtitles/{lang}/playlist.m3u8"\n'
    
    # Add stream info with subtitles
    content += '\n#EXT-X-STREAM-INF:BANDWIDTH=2500000,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio",SUBTITLES="subs"\n'
    content += 'video/playlist.m3u8\n'
    
    # Write master playlist
    async with aiofiles.open(master_playlist_path, "w") as f:
        await f.write(content)
    
    print("Created master playlist with subtitle tracks")

async def monitor_segments_and_create_vtt():
    """
    Monitor video segments and create corresponding VTT segments.
    """
    global first_segment_timestamp, segment_counter
    processed_segments = set()
    
    while True:
        try:
            # Get current video segments
            video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
            if not os.path.exists(video_playlist):
                print("Video playlist not found, waiting...")
                await asyncio.sleep(1)
                continue
                
            current_segments = []
            async with aiofiles.open(video_playlist, 'r') as f:
                content = await f.read()
                for line in content.splitlines():
                    if line.strip().endswith(".ts"):
                        seg_num = int(line.strip().replace("segment", "").replace(".ts", ""))
                        current_segments.append(seg_num)
            
            if not current_segments:
                print("No segments found in playlist, waiting...")
                await asyncio.sleep(1)
                continue
            
            # Initialize first_segment_timestamp if not set
            if first_segment_timestamp is None and current_segments:
                first_segment_timestamp = min(current_segments)
                print(f"Initialized first_segment_timestamp to {first_segment_timestamp}")
                
            print(f"Current segments: {current_segments}")
            print(f"Processed segments: {processed_segments}")
            print(f"First segment timestamp: {first_segment_timestamp}")
            
            # Process new segments
            for seg_num in current_segments:
                if seg_num not in processed_segments:
                    print(f"Processing new segment: {seg_num}")
                    
                    # Create VTT segments for all languages
                    for lang in caption_cues.keys():
                        success = await create_vtt_segment(seg_num, lang)
                        if success:
                            await update_subtitle_playlist(lang)
                    
                    processed_segments.add(seg_num)
            
            # Clean up old segments
            min_segment = min(current_segments)
            processed_segments = {s for s in processed_segments if s >= min_segment}
            
            await asyncio.sleep(1)  # Check every second
            
        except Exception as e:
            print(f"Error in segment monitoring: {str(e)}")
            await asyncio.sleep(1)

async def append_vtt_cue(language, start_time, end_time, text):
    """
    Append a new cue to the caption_cues list for the specified language.
    Updated to handle epoch-based timing and correctly map to video segments.
    """
    try:
        # Ensure we have proper number types
        start_time = float(start_time)
        end_time = float(end_time)
        
        # Ensure we have valid timestamps
        if end_time <= start_time:
            print(f"Invalid timestamps: {start_time} -> {end_time}, skipping")
            return
            
        # Add to in-memory caption store
        caption_cues[language].append({
            "start": start_time,
            "end": end_time,
            "text": text
        })
        
        print(f"[{language}] Added cue: {format_duration(start_time)} --> {format_duration(end_time)}")
        print(f"Text: {text}")
        print(f"Total cues for {language}: {len(caption_cues[language])}")
        
        # Find the video segment that would contain this caption
        if first_segment_timestamp is not None:
            # Get current video segment info
            video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
            current_segments = []
            
            if os.path.exists(video_playlist):
                with open(video_playlist, 'r') as f:
                    for line in f:
                        if line.strip().endswith(".ts"):
                            seg_num = int(line.strip().replace("segment", "").replace(".ts", ""))
                            current_segments.append(seg_num)
            
            # If we have segments, update VTT files for ones that overlap with this caption
            if current_segments:
                for seg_num in current_segments:
                    # Calculate segment time relative to stream start
                    segment_time = (seg_num - first_segment_timestamp) * SEGMENT_DURATION
                    segment_end = segment_time + SEGMENT_DURATION
                    
                    # If caption overlaps with this segment's time window, update it
                    if (start_time >= segment_time and start_time < segment_end) or \
                       (end_time > segment_time and end_time <= segment_end) or \
                       (start_time <= segment_time and end_time >= segment_end):
                        print(f"Caption overlaps with segment {seg_num}")
                        print(f"Segment window: {format_duration(segment_time)} -> {format_duration(segment_end)}")
                        
                        # Create VTT content
                        content = "WEBVTT\n\n"
                        cue_index = 1
                        
                        # Find all cues that overlap with this segment
                        for cue in caption_cues[language]:
                            cue_start = float(cue["start"])
                            cue_end = float(cue["end"])
                            
                            # Skip invalid cues
                            if cue_end <= cue_start:
                                continue
                            
                            # Check if cue overlaps with segment window
                            if (cue_start >= segment_time and cue_start < segment_end) or \
                               (cue_end > segment_time and cue_end <= segment_end) or \
                               (cue_start <= segment_time and cue_end >= segment_end):
                                
                                # Calculate cue timing relative to segment start
                                relative_start = max(0, cue_start - segment_time)
                                relative_end = min(SEGMENT_DURATION, cue_end - segment_time)
                                
                                print(f"Adding cue: {format_duration(relative_start)} -> {format_duration(relative_end)}")
                                print(f"Text: {cue['text']}")
                                
                                content += f"{cue_index}\n"
                                content += f"{format_duration(relative_start)} --> {format_duration(relative_end)}\n"
                                content += f"{cue['text']}\n\n"
                                cue_index += 1
                        
                        # Write the VTT file
                        subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", language)
                        os.makedirs(subtitle_dir, exist_ok=True)
                        segment_path = os.path.join(subtitle_dir, f"segment{seg_num}.vtt")
                        
                        with open(segment_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        
                        print(f"Created {language} segment {seg_num} with {cue_index-1} cues")
                
                # Always update playlist after adding captions
                await update_subtitle_playlist(language)
        
    except Exception as e:
        print(f"Error in append_vtt_cue: {str(e)}")
        print(f"Failed to append cue: {start_time} -> {end_time}: {text}")

# === FastAPI Server ===
app = FastAPI()

@app.get("/")
async def root():
    """Serve the index.html page."""
    content = await generate_index_html()
    return HTMLResponse(
        content=content,
        status_code=200,
        headers={
            "Content-Type": "text/html",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@app.get("/index.html")
async def index():
    """Serve the index.html page."""
    return await root()

@app.get("/master.m3u8")
async def master_playlist():
    """Serve the master playlist with subtitle tracks."""
    file_path = os.path.join(HLS_OUTPUT_DIR, "master.m3u8")
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

@app.get("/subtitles/{lang}/playlist.m3u8")
async def subtitle_playlist(lang: str):
    """Serve the subtitle playlist for a specific language."""
    if lang not in caption_cues:
        return PlainTextResponse(content="Language not found", status_code=404)
    
    playlist_path = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang, "playlist.m3u8")
    if not os.path.exists(playlist_path):
        # Create empty playlist if it doesn't exist yet
        subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang)
        os.makedirs(subtitle_dir, exist_ok=True)
        
        content = "#EXTM3U\n"
        content += "#EXT-X-VERSION:3\n"
        content += f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n"
        content += "#EXT-X-MEDIA-SEQUENCE:0\n"
        
        async with aiofiles.open(playlist_path, "w") as f:
            await f.write(content)
    
    return FileResponse(
        path=playlist_path,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Origin, Content-Type, Accept"
        }
    )

@app.get("/subtitles/{lang}/segment{segment_num}.vtt")
async def subtitle_segment(lang: str, segment_num: int):
    """Serve a specific subtitle segment."""
    if lang not in caption_cues:
        return PlainTextResponse(content="Language not found", status_code=404)
    
    segment_path = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang, f"segment{segment_num}.vtt")
    if not os.path.exists(segment_path):
        # Create an empty segment if it doesn't exist
        await create_vtt_segment(segment_num, lang)
    
    return FileResponse(
        path=segment_path,
        media_type="text/vtt",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@app.get("/{file_path:path}")
async def serve_file(file_path: str):
    """Serve files from the output directory."""
    full_path = os.path.join(HLS_OUTPUT_DIR, file_path)
    if not os.path.exists(full_path):
        return PlainTextResponse(content="File not found", status_code=404)
    
    # Determine content type based on file extension
    content_type = "application/octet-stream"
    if file_path.endswith(".m3u8"):
        content_type = "application/vnd.apple.mpegurl"
    elif file_path.endswith(".ts"):
        content_type = "video/mp2t"
    elif file_path.endswith(".m4s"):
        content_type = "video/iso.segment"
    elif file_path.endswith(".mp4"):
        content_type = "video/mp4"
    elif file_path.endswith(".vtt"):
        content_type = "text/vtt"
    
    # Add CORS and cache control headers for all HLS-related files
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

@app.get("/video/playlist.m3u8")
async def video_playlist():
    """Serve the video playlist."""
    playlist_path = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
    if not os.path.exists(playlist_path):
        return PlainTextResponse(content="Video playlist not found", status_code=404)
    
    return FileResponse(
        path=playlist_path,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Origin, Content-Type, Accept"
        }
    )

@app.get("/audio/playlist.m3u8")
async def audio_playlist():
    """Serve the audio playlist."""
    playlist_path = os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8")
    if not os.path.exists(playlist_path):
        return PlainTextResponse(content="Audio playlist not found", status_code=404)
    
    return FileResponse(
        path=playlist_path,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Origin, Content-Type, Accept"
        }
    )

@app.get("/video/segment{segment_num}.ts")
async def video_segment(segment_num: int):
    """Serve a video segment."""
    segment_path = os.path.join(HLS_OUTPUT_DIR, "video", f"segment{segment_num}.ts")
    if not os.path.exists(segment_path):
        return PlainTextResponse(content="Video segment not found", status_code=404)
    
    return FileResponse(
        path=segment_path,
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Origin, Content-Type, Accept"
        }
    )

@app.get("/audio/segment{segment_num}.ts")
async def audio_segment(segment_num: int):
    """Serve an audio segment."""
    segment_path = os.path.join(HLS_OUTPUT_DIR, "audio", f"segment{segment_num}.ts")
    if not os.path.exists(segment_path):
        return PlainTextResponse(content="Audio segment not found", status_code=404)
    
    return FileResponse(
        path=segment_path,
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Origin, Content-Type, Accept"
        }
    )

async def generate_index_html():
    """Generate an index.html file with an HLS player supporting native captions."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>TV Rain Live Stream</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            padding: 20px;
            background: #f4f4f4;
            color: #333;
            margin: 0;
        }
        .player-container {
            max-width: 960px;
            margin: 0 auto;
            background: #fff;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #2c3e50;
            text-align: center;
            margin-bottom: 20px;
        }
        video {
            width: 100%;
            height: auto;
            border-radius: 4px;
        }
        .controls {
            margin-top: 15px;
            display: flex;
            justify-content: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        button {
            padding: 8px 15px;
            background: #3498db;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        button:hover {
            background: #2980b9;
        }
        button.active {
            background: #27ae60;
        }
        .debug-panel {
            margin-top: 15px;
            padding: 10px;
            background: #f9f9f9;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 13px;
            max-height: 150px;
            overflow-y: auto;
        }
        .status {
            margin-top: 15px;
            padding: 8px;
            border-radius: 4px;
            background: #f8f9fa;
            text-align: center;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="player-container">
        <h1>TV Rain Live Stream</h1>
        <video id="video" controls autoplay></video>
        
        <div class="controls">
            <button onclick="player.selectTextTrack('ru')" id="btn-ru">Russian</button>
            <button onclick="player.selectTextTrack('en')" id="btn-en">English</button>
            <button onclick="player.selectTextTrack('nl')" id="btn-nl">Dutch</button>
            <button onclick="player.disableTextTrack()" id="btn-none">No Subtitles</button>
            <button onclick="player.reloadPlayer()" id="btn-reload">Reload Player</button>
        </div>
        
        <div class="status" id="status">Loading stream...</div>
        
        <div class="debug-panel" id="debug-panel"></div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.4.12"></script>
    <script>
        const debugPanel = document.getElementById('debug-panel');
        function log(message) {
            console.log(message);
            const time = new Date().toTimeString().split(' ')[0];
            debugPanel.innerHTML += `[${time}] ${message}<br>`;
            debugPanel.scrollTop = debugPanel.scrollHeight;
        }
        
        const player = {
            hlsInstance: null,
            videoElement: document.getElementById('video'),
            statusElement: document.getElementById('status'),
            
            init() {
                if (!Hls.isSupported()) {
                    this.statusElement.textContent = 'HLS.js is not supported in this browser';
                    return;
                }
                
                this.hlsInstance = new Hls({
                    debug: true,
                    enableWebVTT: true,
                    manifestLoadingTimeOut: 20000,
                    manifestLoadingMaxRetry: 3,
                    manifestLoadingRetryDelay: 500,
                    levelLoadingTimeOut: 20000,
                    levelLoadingMaxRetry: 3,
                    levelLoadingRetryDelay: 500,
                    fragLoadingTimeOut: 20000,
                    fragLoadingMaxRetry: 3,
                    fragLoadingRetryDelay: 500,
                    startLevel: -1,
                    defaultAudioCodec: 'mp4a.40.2',
                    maxBufferLength: 30,
                    maxMaxBufferLength: 600,
                    startPosition: -1,
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
                log(`Loading manifest: ${manifestUrl}`);
                
                this.hlsInstance.loadSource(manifestUrl);
                this.hlsInstance.attachMedia(this.videoElement);
            },
            
            setupEventListeners() {
                this.hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
                    log('Manifest parsed, attempting playback...');
                    this.videoElement.play()
                        .then(() => {
                            this.statusElement.textContent = 'Playing stream';
                            log('Playback started');
                            this.selectTextTrack('ru');
                        })
                        .catch(error => {
                            log(`Playback failed: ${error.message}`);
                            this.statusElement.textContent = 'Click play to start';
                        });
                });
                
                this.hlsInstance.on(Hls.Events.ERROR, (event, data) => {
                    if (data.fatal) {
                        log(`Fatal error: ${data.type} - ${data.details}`);
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
                
                this.videoElement.addEventListener('error', (e) => {
                    log(`Video error: ${e.message}`);
                    this.statusElement.textContent = 'Video error - try reloading';
                });
            },
            
            selectTextTrack(language) {
                if (!this.hlsInstance) return;
                
                const tracks = this.hlsInstance.subtitleTracks;
                const trackId = tracks.findIndex(track => track.lang === language);
                
                if (trackId !== -1) {
                    this.hlsInstance.subtitleTrack = trackId;
                    log(`Selected ${language} subtitles`);
                    this.statusElement.textContent = `Playing with ${language.toUpperCase()} subtitles`;
                    
                    document.querySelectorAll('.controls button').forEach(btn => {
                        btn.classList.remove('active');
                    });
                    document.getElementById(`btn-${language}`).classList.add('active');
                },
            
            disableTextTrack() {
                if (!this.hlsInstance) return;
                
                this.hlsInstance.subtitleTrack = -1;
                log('Disabled subtitles');
                this.statusElement.textContent = 'Playing without subtitles';
                
                document.querySelectorAll('.controls button').forEach(btn => {
                    btn.classList.remove('active');
                });
                document.getElementById('btn-none').classList.add('active');
            },
            
            reloadPlayer() {
                log('Reloading player...');
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

# === Audio & Transcription Handling ===
async def stream_audio_from_hls(socket: WebSocketClientProtocol, hls_url: str) -> None:
    """
    Launch FFmpeg to stream audio from the HLS URL to Gladia via WebSocket.
    """
    global ffmpeg_audio_process
    
    # Use the audio playlist instead of the master playlist
    audio_playlist = os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8")
    
    # Wait for the audio playlist to be created
    while not os.path.exists(audio_playlist):
        print("Waiting for audio playlist to be created...")
        await asyncio.sleep(1)
    
    print(f"Audio playlist found at {audio_playlist}")
    
    ffmpeg_command = [
        "ffmpeg", "-re",
        "-i", audio_playlist,
        "-ar", str(STREAMING_CONFIGURATION["sample_rate"]),
        "-ac", str(STREAMING_CONFIGURATION["channels"]),
        "-acodec", "pcm_s16le",  # Ensure we output raw PCM
        "-f", "wav",
        "-bufsize", "16K",
        "pipe:1",
    ]
    
    print(f"Starting audio streaming FFmpeg: {' '.join(ffmpeg_command)}")
    
    ffmpeg_audio_process = subprocess.Popen(
        ffmpeg_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10**6,
    )
    
    print("Started FFmpeg process for audio streaming to Gladia")
    
    chunk_size = int(
        STREAMING_CONFIGURATION["sample_rate"]
        * (STREAMING_CONFIGURATION["bit_depth"] / 8)
        * STREAMING_CONFIGURATION["channels"]
        * 0.1  # 100ms chunks
    )
    
    while True:
        audio_chunk = ffmpeg_audio_process.stdout.read(chunk_size)
        if not audio_chunk:
            stderr = ffmpeg_audio_process.stderr.read()
            if stderr:
                print(f"FFmpeg audio streaming error: {stderr.decode()}")
            break
        try:
            await socket.send(audio_chunk)
            await asyncio.sleep(0.1)
        except ConnectionClosedOK:
            print("Gladia WebSocket connection closed")
            break
    
    print("Finished sending audio data")
    try:
        await stop_recording(socket)
    except Exception as e:
        print(f"Error stopping recording: {e}")
    
    if ffmpeg_audio_process:
        ffmpeg_audio_process.terminate()

async def process_messages_from_socket(socket: WebSocketClientProtocol) -> None:
    """
    Process transcription and translation messages from Gladia.
    For each final transcript and translation, create corresponding VTT segments.
    """
    global transcription_start_time, segment_time_offset
    
    async for message in socket:
        content = json.loads(message)
        msg_type = content["type"]
        
        if msg_type == "transcript" and content["data"]["is_final"]:
            utterance = content["data"]["utterance"]
            start = utterance["start"]
            end = utterance["end"]
            text = utterance["text"].strip()
            
            # Initialize timing reference and sync with segments
            if transcription_start_time is None:
                transcription_start_time = float(start)
                if first_segment_timestamp is not None:
                    segment_time_offset = first_segment_timestamp * SEGMENT_DURATION
                print(f"First transcript at {start}s, stream time reference initialized")
                print(f"Segment time offset: {segment_time_offset}")
            
            # Convert timestamps to be relative to segment timeline
            stream_relative_start = float(start) - transcription_start_time
            if segment_time_offset is not None:
                stream_relative_start += segment_time_offset
            stream_relative_end = float(end) - transcription_start_time
            if segment_time_offset is not None:
                stream_relative_end += segment_time_offset
            
            print(f"[Original] {format_duration(stream_relative_start)} --> {format_duration(stream_relative_end)} | {text}")
            await append_vtt_cue("ru", stream_relative_start, stream_relative_end, text)
            
        # Handle translations
        elif msg_type == "translation":
            try:
                # Format 1: Complete structure with translated_utterance
                if "utterance" in content["data"] and "translated_utterance" in content["data"]:
                    # Get the original utterance for timing
                    utterance = content["data"]["utterance"]
                    start = utterance["start"]
                    end = utterance["end"]
                    
                    # Get the translated text from translated_utterance
                    translated_utterance = content["data"]["translated_utterance"]
                    text = translated_utterance["text"].strip()
                    lang = content["data"]["target_language"]
                    
                    # Calculate stream-relative timestamps
                    stream_relative_start = float(start) - transcription_start_time
                    stream_relative_end = float(end) - transcription_start_time
                    
                    if lang in ["en", "nl"]:
                        print(f"[{lang.upper()}] {format_duration(stream_relative_start)} --> {format_duration(stream_relative_end)} | {text}")
                        await append_vtt_cue(lang, stream_relative_start, stream_relative_end, text)
                        
                # Format 2: Other formats - kept for fallback compatibility
                elif "translation" in content["data"]:
                    translation = content["data"]["translation"]
                    # Get timestamps from either nested or outer level
                    if "start" in translation and "end" in translation:
                        start = translation["start"]
                        end = translation["end"]
                    else:
                        start = content["data"]["start"]
                        end = content["data"]["end"]
                    
                    # Calculate stream-relative timestamps
                    stream_relative_start = float(start) - transcription_start_time
                    stream_relative_end = float(end) - transcription_start_time
                    
                    text = translation["text"].strip()
                    lang = translation["target_language"]
                    if lang in ["en", "nl"]:
                        print(f"[{lang.upper()}] {format_duration(stream_relative_start)} --> {format_duration(stream_relative_end)} | {text}")
                        await append_vtt_cue(lang, stream_relative_start, stream_relative_end, text)
                
                # Handle any other formats
                else:
                    print(f"Unknown translation format: {json.dumps(content, indent=2)}")
            except Exception as e:
                print(f"Error processing translation: {e}")
                print(f"Message content was: {json.dumps(content, indent=2)}")
        
        if msg_type == "post_final_transcript":
            print("\n################ End of session ################\n")
            print(json.dumps(content, indent=2, ensure_ascii=False))

async def stop_recording(websocket: WebSocketClientProtocol) -> None:
    """Send a stop recording signal to Gladia."""
    print(">>>>> Ending the recording...")
    try:
        await websocket.send(json.dumps({"type": "stop_recording"}))
    except Exception as e:
        print(f"Error sending stop recording signal: {e}")
    await asyncio.sleep(0)
    
    # Clean up processes
    if ffmpeg_audio_process:
        ffmpeg_audio_process.terminate()

# === FFmpeg HLS Output ===
async def start_ffmpeg_hls():
    """
    Use FFmpeg to create a live HLS stream with separate audio and video tracks.
    """
    os.makedirs(HLS_OUTPUT_DIR, exist_ok=True)
    
    # Create output directories
    os.makedirs(os.path.join(HLS_OUTPUT_DIR, "audio"), exist_ok=True)
    os.makedirs(os.path.join(HLS_OUTPUT_DIR, "video"), exist_ok=True)
    
    try:
        ffmpeg_command = [
            "ffmpeg", "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", EXAMPLE_HLS_STREAM_URL,
            # Audio output
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-map", "0:a",
            "-f", "hls",
            "-hls_time", str(SEGMENT_DURATION),
            "-hls_list_size", str(WINDOW_SIZE),
            "-hls_flags", "delete_segments+independent_segments+program_date_time",
            "-hls_segment_type", "mpegts",
            "-hls_allow_cache", "0",
            "-hls_start_number_source", "epoch",
            "-hls_segment_filename", os.path.join(HLS_OUTPUT_DIR, "audio", "segment%d.ts"),
            os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8"),
            # Video output
            "-map", "0:v",
            "-c:v", "copy",
            "-f", "hls",
            "-hls_time", str(SEGMENT_DURATION),
            "-hls_list_size", str(WINDOW_SIZE),
            "-hls_flags", "delete_segments+independent_segments+program_date_time",
            "-hls_segment_type", "mpegts",
            "-hls_allow_cache", "0",
            "-hls_start_number_source", "epoch",
            "-hls_segment_filename", os.path.join(HLS_OUTPUT_DIR, "video", "segment%d.ts"),
            os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
        ]

        print("Starting FFmpeg for HLS stream...")
        print(f"FFmpeg Command: {' '.join(ffmpeg_command)}")
        
        # Start FFmpeg process with real-time error output
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        
        # Create initial master playlist
        await create_master_playlist()
        
        # Monitor FFmpeg output in real-time
        async def monitor_ffmpeg():
            while True:
                line = ffmpeg_process.stderr.readline()
                if not line and ffmpeg_process.poll() is not None:
                    print("FFmpeg process ended unexpectedly")
                    raise RuntimeError("FFmpeg process failed")
                if line:
                    print(f"FFmpeg: {line.strip()}")
                await asyncio.sleep(0.1)
        
        # Start monitoring task
        monitor_task = asyncio.create_task(monitor_ffmpeg())
        
        # Monitor the playlists
        audio_playlist = os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8")
        video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
        timeout = 30  # 30 seconds timeout
        start_time = time.time()
        
        while not (os.path.exists(audio_playlist) and os.path.exists(video_playlist)):
            if time.time() - start_time > timeout:
                raise TimeoutError("Failed to generate playlists within timeout")
            
            # Check if FFmpeg process has failed
            if ffmpeg_process.poll() is not None:
                stderr = ffmpeg_process.stderr.read()
                raise RuntimeError(f"FFmpeg process failed: {stderr}")
            
            print("Waiting for playlists to be created...")
            await asyncio.sleep(1)
        
        print("HLS stream is ready")
        await monitor_task  # Keep monitoring FFmpeg output
        
    except Exception as e:
        print(f"Error in start_ffmpeg_hls: {e}")
        if 'ffmpeg_process' in locals():
            stderr = ffmpeg_process.stderr.read()
            print(f"FFmpeg error output: {stderr}")
        raise
    
    finally:
        # Cleanup processes
        if 'ffmpeg_process' in locals():
            ffmpeg_process.terminate()

# === Main Transcription Flow ===
async def transcription_main():
    """
    Main function to coordinate the transcription and HLS generation process.
    """
    print("\nStarting Rainscribe with native HLS subtitle integration")
    
    # Clear existing files and create directories (unchanged)
    ...
    
    # Start FFmpeg WITHOUT the delay to begin collecting segments
    packager_task = asyncio.create_task(start_ffmpeg_hls())
    
    # Wait for initial segments to be created
    audio_playlist = os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8")
    video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
    
    while not (os.path.exists(audio_playlist) and os.path.exists(video_playlist)):
        print("Waiting for playlists to be created...")
        await asyncio.sleep(1)
    
    # Initialize Gladia and start transcription
    response = init_live_session(STREAMING_CONFIGURATION)
    
    async with ws_connect(response["url"]) as websocket:
        print("\n################ Begin session ################\n")
        
        message_task = asyncio.create_task(process_messages_from_socket(websocket))
        audio_task = asyncio.create_task(stream_audio_from_hls(websocket, EXAMPLE_HLS_STREAM_URL))
        
        # Buffer initial segments and transcriptions
        print(f"Buffering {SEGMENT_BUFFER_COUNT} segments ({INITIAL_BUFFER_SECONDS} seconds)...")
        buffer_start = time.time()
        
        while True:
            if not os.path.exists(video_playlist):
                await asyncio.sleep(1)
                continue
                
            async with aiofiles.open(video_playlist, 'r') as f:
                content = await f.read()
                segments = [line.strip() for line in content.splitlines() if line.strip().endswith(".ts")]
                
            if len(segments) >= SEGMENT_BUFFER_COUNT:
                print(f"Collected {len(segments)} segments")
                print(f"Collected {len(caption_cues['ru'])} Russian cues")
                break
                
            if time.time() - buffer_start > INITIAL_BUFFER_SECONDS + 30:  # 30s extra grace period
                print("Buffer timeout reached")
                break
                
            print(f"Buffering: {len(segments)}/{SEGMENT_BUFFER_COUNT} segments, {len(caption_cues['ru'])} cues")
            await asyncio.sleep(1)
        
        # Start serving content
        print("Starting web server...")
        web_server_task = asyncio.create_task(start_web_server())
        segment_monitor_task = asyncio.create_task(monitor_segments_and_create_vtt())
        
        try:
            await asyncio.gather(message_task, audio_task, web_server_task, packager_task, segment_monitor_task)
        except asyncio.CancelledError:
            print("Tasks cancelled - shutting down...")
        finally:
            if ffmpeg_audio_process:
                ffmpeg_audio_process.terminate()

async def start_web_server():
    """Start the FastAPI web server."""
    config = uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT)
    server = uvicorn.Server(config)
    await server.serve()

# Add new constants at the top with other configuration
INITIAL_BUFFER_SECONDS = 60
SEGMENT_BUFFER_COUNT = INITIAL_BUFFER_SECONDS // SEGMENT_DURATION

if __name__ == "__main__":
    try:
        asyncio.run(transcription_main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        if ffmpeg_audio_process:
            ffmpeg_audio_process.terminate()
        sys.exit(0)