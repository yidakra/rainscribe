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
from typing import Dict, List, Any, Optional, Set
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
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "4"))  # 4 seconds per segment
WINDOW_SIZE = int(os.environ.get("WINDOW_SIZE", "5"))            # 5 segments in the playlist

# Debug flag
DEBUG_MESSAGES = os.environ.get("DEBUG_MESSAGES", "false").lower() == "true"

# === Global In-Memory Storage for Caption Cues ===
caption_cues = {
    "ru": [],  # Original Russian captions
    "en": [],  # English translations
    "nl": []   # Dutch translations
}

# Global process handles
ffmpeg_audio_process = None
stream_start_time = None  # Track when the stream started

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
    milliseconds = int(float(seconds) * 1000)
    
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

async def create_vtt_segment(segment_number, language="ru"):
    """
    Create a WebVTT segment file for the given segment number and language.
    Each segment covers a specified duration of time based on stream-relative timing.
    """
    subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", language)
    os.makedirs(subtitle_dir, exist_ok=True)
    
    try:
        if stream_start_time is None:
            return False
            
        # Calculate segment time boundaries
        # Convert segment number to stream-relative time
        segment_relative_index = segment_number - stream_start_time
        segment_start = segment_relative_index * SEGMENT_DURATION
        segment_end = segment_start + SEGMENT_DURATION
        
        # Create WebVTT content
        content = "WEBVTT\n\n"
        relevant_cues = []
        
        for cue in caption_cues[language]:
            cue_start = float(cue["start"])
            cue_end = float(cue["end"])
            
            # Check if cue overlaps with this segment's time window
            if cue_end > segment_start and cue_start < segment_end:
                # Calculate relative times within segment
                adjusted_start = max(0, cue_start - segment_start)
                adjusted_end = min(SEGMENT_DURATION, cue_end - segment_start)
                
                # Skip zero-duration cues
                if adjusted_end - adjusted_start <= 0:
                    continue
                
                relevant_cues.append({
                    "start": adjusted_start,
                    "end": adjusted_end,
                    "text": cue["text"]
                })

        # Add cues to content
        for i, cue in enumerate(relevant_cues):
            content += f"{i+1}\n"
            content += f"{format_duration(cue['start'])} --> {format_duration(cue['end'])}\n"
            content += f"{cue['text']}\n\n"

        # Always write the segment file, even if empty
        segment_path = os.path.join(subtitle_dir, f"segment{segment_number}.vtt")
        async with aiofiles.open(segment_path, "w", encoding="utf-8") as f:
            await f.write(content)
            
        print(f"Created {language} segment {segment_number} with {len(relevant_cues)} cues (time window: {format_duration(segment_start)}-{format_duration(segment_end)})")
        
        return True
        
    except Exception as e:
        print(f"Error in create_vtt_segment: {str(e)}")
        return False

async def update_subtitle_playlist(language="ru"):
    """
    Update the subtitle playlist for the given language.
    """
    subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", language)
    os.makedirs(subtitle_dir, exist_ok=True)
    playlist_path = os.path.join(subtitle_dir, "playlist.m3u8")

    # Get video playlist state
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

    # Create matching subtitle playlist
    content = "#EXTM3U\n#EXT-X-VERSION:3\n"
    content += f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n"
    content += f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n"

    for seg_num in segments[-WINDOW_SIZE:]:
        content += f"#EXTINF:{SEGMENT_DURATION}.0,\n"
        content += f"segment{seg_num}.vtt\n"

    async with aiofiles.open(playlist_path, "w", encoding="utf-8") as f:
        await f.write(content)
    
    print(f"Updated {language} subtitle playlist (media_sequence: {media_sequence})")

async def create_master_playlist():
    """
    Create the master playlist with subtitle tracks.
    """
    master_playlist_path = os.path.join(HLS_OUTPUT_DIR, "master.m3u8")
    
    # Create subtitle directories
    for lang in caption_cues.keys():
        subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang)
        os.makedirs(subtitle_dir, exist_ok=True)
    
    content = """#EXTM3U
#EXT-X-VERSION:3

#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio/playlist.m3u8"
"""
    
    # Add subtitle tracks
    lang_names = {"ru": "Russian", "en": "English", "nl": "Dutch"}
    for lang, name in lang_names.items():
        default = "YES" if lang == "ru" else "NO"
        content += f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",DEFAULT={default},AUTOSELECT=YES,LANGUAGE="{lang}",URI="subtitles/{lang}/playlist.m3u8"\n'
    
    # Add stream info with subtitles
    content += '\n#EXT-X-STREAM-INF:BANDWIDTH=2500000,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio",SUBTITLES="subs"\n'
    content += 'video/playlist.m3u8'
    
    # Write master playlist
    async with aiofiles.open(master_playlist_path, "w") as f:
        await f.write(content)

async def monitor_segments_and_create_vtt():
    """
    Monitor HLS video segments and create corresponding VTT segments.
    """
    global stream_start_time
    video_segment_dir = os.path.join(HLS_OUTPUT_DIR, "video")
    processed_segments = set()
    last_media_sequence = None
    
    while True:
        try:
            # Get current video segment from playlist
            video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
            if not os.path.exists(video_playlist):
                await asyncio.sleep(1)
                continue

            # Parse current segments from video playlist
            current_segments = []
            media_sequence = 0
            with open(video_playlist, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                        media_sequence = int(line.split(":")[1])
                    elif line.endswith(".ts"):
                        seg_num = int(line.replace("segment", "").replace(".ts", ""))
                        current_segments.append(seg_num)

            # Initialize stream_start_time with first segment if not set
            if stream_start_time is None and current_segments:
                stream_start_time = current_segments[0]
                print(f"Initialized stream_start_time: {stream_start_time}")
            
            # If media sequence changed, we need to update all segments
            if last_media_sequence != media_sequence:
                print(f"Media sequence changed from {last_media_sequence} to {media_sequence}")
                processed_segments.clear()
                last_media_sequence = media_sequence
            
            # Process all segments in the current window
            for seg_num in current_segments:
                if seg_num not in processed_segments:
                    for lang in caption_cues.keys():
                        # Create VTT segment
                        created = await create_vtt_segment(seg_num, lang)
                        if created:
                            await update_subtitle_playlist(lang)
                            print(f"Updated {lang} segment {seg_num} and playlist")
                    processed_segments.add(seg_num)
            
            # Clean up old segments beyond window size
            if current_segments:
                min_segment = min(current_segments)
                processed_segments = {s for s in processed_segments if s >= min_segment}
                
                # Also clean up old VTT files
                for lang in caption_cues.keys():
                    subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang)
                    if os.path.exists(subtitle_dir):
                        for file in os.listdir(subtitle_dir):
                            if file.startswith("segment") and file.endswith(".vtt"):
                                seg_num = int(file.replace("segment", "").replace(".vtt", ""))
                                if seg_num < min_segment:
                                    os.remove(os.path.join(subtitle_dir, file))

            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"Segment monitoring error: {str(e)}")
            await asyncio.sleep(2)  # Wait a bit longer if there was an error

async def append_vtt_cue(language, start_time, end_time, text):
    """
    Append a new cue to the caption_cues list for the specified language.
    Timestamps should be in seconds relative to stream start.
    """
    try:
        # Convert epoch-based timestamps to seconds if needed
        if isinstance(start_time, str) and ":" in start_time:
            start_time = float(start_time.split(":")[-1])  # Take seconds part
        if isinstance(end_time, str) and ":" in end_time:
            end_time = float(end_time.split(":")[-1])  # Take seconds part
            
        start_time = float(start_time)
        end_time = float(end_time)
        
        # Ensure we have valid timestamps
        if end_time <= start_time:
            print(f"Invalid timestamps: {start_time} -> {end_time}")
            return
            
        caption_cues[language].append({
            "start": start_time,
            "end": end_time,
            "text": text
        })
        
        # Get current video segment number and media sequence
        video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
        current_segment = None
        media_sequence = 0
        
        if os.path.exists(video_playlist):
            with open(video_playlist, 'r') as f:
                for line in f:
                    if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                        media_sequence = int(line.strip().split(":")[1])
                    elif line.strip().endswith(".ts"):
                        current_segment = int(line.strip().replace("segment", "").replace(".ts", ""))
        
        if current_segment is not None:
            # Calculate which segments this cue affects
            start_segment = int(start_time / SEGMENT_DURATION)
            end_segment = int(end_time / SEGMENT_DURATION)
            
            # Only update segments that are within the current window
            start_segment = max(start_segment, media_sequence)
            end_segment = min(end_segment, current_segment)
            
            # Update affected segments
            for segment_num in range(start_segment, end_segment + 1):
                await create_vtt_segment(segment_num, language)
                await update_subtitle_playlist(language)
        
        print(f"[{language}] Added cue: {format_duration(start_time)} --> {format_duration(end_time)}")
        print(text)
        
    except Exception as e:
        print(f"Error in append_vtt_cue: {str(e)}")
        print(f"Failed to append cue: {start_time} -> {end_time}: {text}")

# === FastAPI Server ===
app = FastAPI()

@app.get("/")
async def root():
    return HTMLResponse(content=await generate_index_html(), status_code=200)

@app.get("/index.html")
async def index():
    return HTMLResponse(content=await generate_index_html(), status_code=200)

@app.get("/master.m3u8")
async def master_playlist():
    """Serve the master playlist with subtitle tracks."""
    file_path = os.path.join(HLS_OUTPUT_DIR, "master.m3u8")
    return FileResponse(
        path=file_path,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
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
            "Expires": "0"
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
    
    # Add cache control headers for m3u8 playlists
    headers = {}
    if file_path.endswith(".m3u8"):
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    
    return FileResponse(path=full_path, media_type=content_type, headers=headers)

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
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script>
        // Player controller
        const player = {
            hlsInstance: null,
            videoElement: document.getElementById('video'),
            currentTrack: null,
            
            init() {
                if (Hls.isSupported()) {
                    this.hlsInstance = new Hls({
                        capLevelToPlayerSize: true,
                        maxBufferLength: 30,
                        backBufferLength: 30,
                        enableWebVTT: true,
                        debug: false
                    });
                    
                    this.hlsInstance.loadSource('master.m3u8');
                    this.hlsInstance.attachMedia(this.videoElement);
                    
                    this.hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
                        console.log('HLS manifest parsed, tracks available:', this.hlsInstance.subtitleTracks);
                        this.videoElement.play().catch(e => console.log('Autoplay prevented:', e));
                        // Auto-select Russian subtitles
                        this.selectTextTrack('ru');
                    });
                    
                    this.hlsInstance.on(Hls.Events.ERROR, (event, data) => {
                        if (data.fatal) {
                            switch(data.type) {
                                case Hls.ErrorTypes.NETWORK_ERROR:
                                    console.error('Network error, attempting recovery');
                                    this.hlsInstance.startLoad();
                                    break;
                                case Hls.ErrorTypes.MEDIA_ERROR:
                                    console.error('Media error, attempting recovery');
                                    this.hlsInstance.recoverMediaError();
                                    break;
                                default:
                                    console.error('Fatal error, cannot recover:', data);
                                    break;
                            }
                        }
                    });
                    
                    // Monitor subtitle changes
                    this.hlsInstance.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, () => {
                        console.log('Subtitle tracks updated:', this.hlsInstance.subtitleTracks);
                        if (this.currentTrack !== null) {
                            this.selectTextTrack(this.currentTrack);
                        }
                    });
                    
                    this.hlsInstance.on(Hls.Events.SUBTITLE_TRACK_SWITCH, () => {
                        console.log('Subtitle track switched:', this.hlsInstance.subtitleTrack);
                    });
                    
                    this.hlsInstance.on(Hls.Events.SUBTITLE_TRACK_LOADED, () => {
                        console.log('Subtitle track loaded');
                    });
                } else if (this.videoElement.canPlayType('application/vnd.apple.mpegurl')) {
                    // Native HLS support (Safari)
                    this.videoElement.src = 'master.m3u8';
                    this.videoElement.addEventListener('loadedmetadata', () => {
                        this.videoElement.play().catch(e => console.log('Autoplay prevented:', e));
                    });
                } else {
                    console.error('HLS is not supported in this browser');
                }
            },
            
            updateButtons(activeLanguage) {
                document.querySelectorAll('.controls button').forEach(btn => {
                    btn.classList.remove('active');
                });
                if (activeLanguage) {
                    document.getElementById(`btn-${activeLanguage}`).classList.add('active');
                } else {
                    document.getElementById('btn-none').classList.add('active');
                }
            },
            
            selectTextTrack(language) {
                this.currentTrack = language;
                if (this.hlsInstance) {
                    const tracks = this.hlsInstance.subtitleTracks;
                    const trackId = tracks.findIndex(track => track.lang === language);
                    
                    if (trackId !== -1) {
                        this.hlsInstance.subtitleTrack = trackId;
                        console.log(`Enabled ${language} subtitles (track ${trackId})`);
                        this.updateButtons(language);
                    } else {
                        console.warn(`No subtitle track found for language: ${language}`);
                    }
                } else if (this.videoElement.textTracks) {
                    Array.from(this.videoElement.textTracks).forEach(track => {
                        track.mode = track.language === language ? 'showing' : 'hidden';
                    });
                    this.updateButtons(language);
                }
            },
            
            disableTextTrack() {
                this.currentTrack = null;
                if (this.hlsInstance) {
                    this.hlsInstance.subtitleTrack = -1;
                    console.log('Disabled subtitles');
                } else if (this.videoElement.textTracks) {
                    Array.from(this.videoElement.textTracks).forEach(track => {
                        track.mode = 'hidden';
                    });
                }
                this.updateButtons(null);
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
    
    ffmpeg_command = [
        "ffmpeg", "-re",
        "-i", hls_url,
        "-ar", str(STREAMING_CONFIGURATION["sample_rate"]),
        "-ac", str(STREAMING_CONFIGURATION["channels"]),
        "-f", "wav",
        "-bufsize", "16K",
        "pipe:1",
    ]
    
    ffmpeg_audio_process = subprocess.Popen(
        ffmpeg_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10**6,
    )
    
    print("Started FFmpeg process for audio streaming")
    
    chunk_size = int(
        STREAMING_CONFIGURATION["sample_rate"]
        * (STREAMING_CONFIGURATION["bit_depth"] / 8)
        * STREAMING_CONFIGURATION["channels"]
        * 0.1  # 100ms chunks
    )
    
    while True:
        audio_chunk = ffmpeg_audio_process.stdout.read(chunk_size)
        if not audio_chunk:
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
    first_transcript_time = None
    video_start_segment = None
    
    async for message in socket:
        content = json.loads(message)
        msg_type = content["type"]
        
        # Handle original transcription
        if msg_type == "transcript" and content["data"]["is_final"]:
            utterance = content["data"]["utterance"]
            start = utterance["start"]
            end = utterance["end"]
            text = utterance["text"].strip()
            
            # Initialize timing reference
            if first_transcript_time is None:
                first_transcript_time = start
                # Get current video segment
                video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
                if os.path.exists(video_playlist):
                    with open(video_playlist, 'r') as f:
                        for line in f:
                            if line.strip().endswith(".ts"):
                                video_start_segment = int(line.strip().replace("segment", "").replace(".ts", ""))
                                break
                print(f"First transcript at {start}s, video segment {video_start_segment}")
            
            # Calculate stream-relative timestamps
            stream_relative_start = start - first_transcript_time
            stream_relative_end = end - first_transcript_time
            
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
                    stream_relative_start = start - first_transcript_time
                    stream_relative_end = end - first_transcript_time
                    
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
                    stream_relative_start = start - first_transcript_time
                    stream_relative_end = end - first_transcript_time
                    
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
        # FFmpeg command to create HLS output
        ffmpeg_command = [
            "ffmpeg", "-y",
            "-i", EXAMPLE_HLS_STREAM_URL,
            # Audio output
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-map", "0:a",
            "-f", "hls",
            "-hls_time", str(SEGMENT_DURATION),
            "-hls_list_size", str(WINDOW_SIZE),
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_segment_type", "mpegts",
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_DURATION})",
            "-hls_start_number_source", "epoch",
            "-hls_segment_filename", os.path.join(HLS_OUTPUT_DIR, "audio", "segment%d.ts"),
            os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8"),
            # Video output
            "-map", "0:v",
            "-c:v", "copy",
            "-f", "hls",
            "-hls_time", str(SEGMENT_DURATION),
            "-hls_list_size", str(WINDOW_SIZE),
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_segment_type", "mpegts",
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_DURATION})",
            "-hls_start_number_source", "epoch",
            "-hls_segment_filename", os.path.join(HLS_OUTPUT_DIR, "video", "segment%d.ts"),
            os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
        ]

        print("Starting FFmpeg for HLS stream...")
        print(f"FFmpeg Command: {' '.join(ffmpeg_command)}")
        
        # Start FFmpeg process
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Create initial master playlist
        await create_master_playlist()
        
        # Monitor the playlists
        audio_playlist = os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8")
        video_playlist = os.path.join(HLS_OUTPUT_DIR, "video", "playlist.m3u8")
        timeout = 30  # 30 seconds timeout
        start_time = time.time()
        
        while not (os.path.exists(audio_playlist) and os.path.exists(video_playlist)):
            if time.time() - start_time > timeout:
                print("Timeout waiting for playlists")
                raise TimeoutError("Failed to generate playlists")
            
            # Check if FFmpeg process has failed
            if ffmpeg_process.poll() is not None:
                stderr = ffmpeg_process.stderr.read()
                raise RuntimeError(f"FFmpeg process failed: {stderr}")
            
            await asyncio.sleep(1)
        
        print("HLS stream is ready")
        
        # Keep the process running
        while True:
            await asyncio.sleep(1)
            if ffmpeg_process.poll() is not None:
                print("FFmpeg process ended unexpectedly")
                break
    
    except Exception as e:
        print(f"Error in start_ffmpeg_hls: {e}")
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
    
    # Clear any existing output files
    if os.path.exists(HLS_OUTPUT_DIR):
        for root, dirs, files in os.walk(HLS_OUTPUT_DIR):
            for file in files:
                if file.endswith(".vtt") or file.endswith(".m3u8") or file.endswith(".ts"):
                    os.remove(os.path.join(root, file))
    
    # Create subtitle directories
    for lang in caption_cues.keys():
        subtitle_dir = os.path.join(HLS_OUTPUT_DIR, "subtitles", lang)
        os.makedirs(subtitle_dir, exist_ok=True)
    
    # Initialize Gladia session
    response = init_live_session(STREAMING_CONFIGURATION)
    
    # Start FFmpeg process for HLS output
    packager_task = asyncio.create_task(start_ffmpeg_hls())
    
    # Start WebSocket connection to Gladia for transcription
    async with ws_connect(response["url"]) as websocket:
        print("\n################ Begin session ################\n")
        
        # Set up signal handler for graceful shutdown
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(stop_recording(websocket)))
        
        # Start message processing task
        message_task = asyncio.create_task(process_messages_from_socket(websocket))
        
        # Start audio streaming task to send audio to Gladia
        audio_task = asyncio.create_task(stream_audio_from_hls(websocket, EXAMPLE_HLS_STREAM_URL))
        
        # Wait for initial captions to buffer
        print(f"Prebuffering transcriptions until at least {MIN_CUES} cues are collected for the original language...")
        
        buffer_timeout = 60  # seconds
        buffer_start = time.time()
        while len(caption_cues["ru"]) < MIN_CUES:
            if time.time() - buffer_start > buffer_timeout:
                print(f"Prebuffer timeout reached after {buffer_timeout} seconds. Proceeding with {len(caption_cues['ru'])} cues.")
                break
            await asyncio.sleep(0.5)
            
        print(f"Prebuffer complete: {len(caption_cues['ru'])} cues collected.")
        
        # Start segment monitoring task to create VTT segments
        segment_monitor_task = asyncio.create_task(monitor_segments_and_create_vtt())
        
        # Start the web server
        web_server_task = asyncio.create_task(start_web_server())
        print(f"Web server started on port {HTTP_PORT}")
        
        # Continue processing until the tasks complete or are cancelled
        try:
            await asyncio.gather(message_task, audio_task, web_server_task, packager_task, segment_monitor_task)
        except asyncio.CancelledError:
            print("Tasks cancelled - shutting down...")
        finally:
            # Clean up
            if ffmpeg_audio_process:
                ffmpeg_audio_process.terminate()

async def start_web_server():
    """Start the FastAPI web server."""
    config = uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(transcription_main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        if ffmpeg_audio_process:
            ffmpeg_audio_process.terminate()
        sys.exit(0)