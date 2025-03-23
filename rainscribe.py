#!/usr/bin/env python3
"""
Live Transcription with Embedded Caption Track for HLS Streaming

This script does the following:
  • Initializes a live transcription session with Gladia.
  • Streams audio from an HLS URL (via FFmpeg) to Gladia's WebSocket endpoint.
  • Receives transcription messages continuously and appends each final transcript as a WebVTT cue to an in‑memory list.
  • Uses FFmpeg to create a new HLS stream with separate audio and video tracks.
  • Generates a master playlist that includes the audio/video streams and external subtitles tracks.
  • Starts an HTTP server to serve the new HLS stream, the master playlist, the segments, and the live captions.
  • When you press Ctrl+C the full output of the run is saved to a log file.

Usage:
    python3 rainscribe.py YOUR_GLADIA_API_KEY

Then open http://localhost:8080/index.html in your browser to view the stream with embedded captions.
"""

import asyncio
import json
import subprocess
import sys
import signal
import threading
import os
import time
import aiofiles
from http.server import SimpleHTTPRequestHandler, HTTPServer
from typing import TypedDict, Literal, Dict, List, Any, Optional, Set
import requests
from websockets.legacy.client import WebSocketClientProtocol, connect as ws_connect
from websockets.exceptions import ConnectionClosedOK
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import uvicorn

# === Logging Setup: Tee stdout and stderr to both console and a log file ===
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
# Allow URL override via environment variable (for Docker)
EXAMPLE_HLS_STREAM_URL = os.environ.get(
    "STREAM_URL", 
    "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"
)

MIN_CUES = int(os.environ.get("MIN_CUES", "2"))  # Adjust as needed
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))  # For serving index.html and HLS stream

# Directory for HLS output
HLS_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

# HLS configuration
SEGMENT_DURATION = "4"  # 4 seconds per segment
WINDOW_SIZE = "5"       # 5 segments in the playlist

# WebVTT configuration
OUTPUT_VTT_DIR = HLS_OUTPUT_DIR
os.makedirs(OUTPUT_VTT_DIR, exist_ok=True)

# === Type Definitions ===
class InitiateResponse(TypedDict):
    id: str
    url: str

class LanguageConfiguration(TypedDict):
    languages: Optional[List[str]]
    code_switching: Optional[bool]

class StreamingConfiguration(TypedDict):
    encoding: Literal["wav/pcm", "wav/alaw", "wav/ulaw"]
    bit_depth: Literal[8, 16, 24, 32]
    sample_rate: Literal[8000, 16000, 32000, 44100, 48000]
    channels: int
    language_config: Optional[LanguageConfiguration]
    realtime_processing: Optional[Dict[str, Any]]

STREAMING_CONFIGURATION: StreamingConfiguration = {
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

# Debug flag to log all incoming messages
DEBUG_MESSAGES = os.environ.get("DEBUG_MESSAGES", "false").lower() == "true"

# === Global In-Memory Storage for Caption Cues ===
caption_cues = {
    "ru": [],  # Original Russian captions
    "en": [],  # English translations
    "nl": []   # Dutch translations
}

# Global process handles
ffmpeg_audio_process = None

# === Utility Functions ===
def format_duration(seconds: float) -> str:
    """Format seconds into WebVTT time format: HH:MM:SS.mmm"""
    if isinstance(seconds, str):
        return seconds  # Already formatted
    
    milliseconds = int(seconds * 1000)
    hours = milliseconds // 3600000
    minutes = (milliseconds % 3600000) // 60000
    secs = (milliseconds % 60000) // 1000
    ms = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"

def get_gladia_key() -> str:
    """Retrieve the Gladia API key from the first command-line argument or environment variable."""
    # First try environment variable (for Docker)
    env_key = os.environ.get("GLADIA_API_KEY")
    if env_key:
        return env_key
        
    # Fallback to command line argument
    if len(sys.argv) != 2 or not sys.argv[1]:
        print("You must provide a Gladia key as the first argument or set GLADIA_API_KEY environment variable.")
        sys.exit(1)
    return sys.argv[1]

def init_live_session(config: StreamingConfiguration) -> InitiateResponse:
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

async def write_vtt_header():
    """Write the WebVTT header to the live captions file for each language."""
    # Clear the in-memory cues
    for lang in caption_cues:
        caption_cues[lang] = []
    
    # Clear and initialize VTT files
    for lang in caption_cues.keys():
        vtt_path = os.path.join(OUTPUT_VTT_DIR, f"captions_{lang}.vtt")
        async with aiofiles.open(vtt_path, "w", encoding="utf-8") as f:
            await f.write("WEBVTT\n\n")

async def append_vtt_cue(start: float, end: float, text: str, lang="ru"):
    """
    Append a WebVTT cue to the in-memory list and broadcast to all connected clients.
    """
    cue = {
        "start": start,  # Keep as float
        "end": end,     # Keep as float
        "text": text.strip()
    }
    caption_cues[lang].append(cue)
    
    # Broadcast the update to all connected clients
    await caption_broadcaster.broadcast_captions(lang, caption_cues[lang])
    
    print(f"[{lang}] Appended cue: {format_duration(start)} --> {format_duration(end)}")
    print(text.strip())

# === FastAPI Server for Serving HLS Stream and Captions ===
app = FastAPI()

class CaptionBroadcaster:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_connections.remove(websocket)

    async def broadcast_captions(self, language: str, cues: List[dict]):
        message = {
            "language": language,
            "cues": cues
        }
        disconnected = set()
        
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.add(connection)
        
        # Clean up disconnected clients
        async with self._lock:
            for connection in disconnected:
                self.active_connections.remove(connection)

# Initialize the broadcaster after FastAPI app initialization
caption_broadcaster = CaptionBroadcaster()

@app.get("/")
async def root():
    return HTMLResponse(content=await generate_index_html(), status_code=200)

@app.get("/index.html")
async def index():
    return HTMLResponse(content=await generate_index_html(), status_code=200)

@app.get("/captions_{lang}.vtt")
async def get_captions(lang: str):
    """Serve the live captions for a specific language with strict cache control headers."""
    if lang not in caption_cues:
        return PlainTextResponse(content="Language not found", status_code=404)
    
    content = "WEBVTT\n\n"
    for cue in caption_cues[lang]:
        content += f"{format_duration(cue['start'])} --> {format_duration(cue['end'])}\n{cue['text']}\n\n"
    
    headers = {
        "Content-Type": "text/vtt; charset=utf-8",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    }
    
    return PlainTextResponse(content=content, headers=headers)

@app.get("/stream_master.m3u8")
async def master_playlist():
    """Serve the master playlist with subtitle tracks."""
    file_path = os.path.join(HLS_OUTPUT_DIR, "stream_master.m3u8")
    return FileResponse(
        path=file_path,
        media_type="application/vnd.apple.mpegurl",
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

@app.websocket("/ws/captions")
async def websocket_endpoint(websocket: WebSocket):
    await caption_broadcaster.connect(websocket)
    try:
        # Send initial captions for all languages
        for lang in caption_cues:
            await websocket.send_json({
                "language": lang,
                "cues": caption_cues[lang]
            })
        
        # Keep connection alive and handle disconnection
        while True:
            try:
                await websocket.receive_text()
            except Exception:
                break
    finally:
        await caption_broadcaster.disconnect(websocket)

async def generate_index_html():
    """Generate an index.html file with an HLS player supporting captions via WebSocket updates."""
    timestamp = int(time.time())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Rainscribe - HLS Player with Embedded Captions</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            padding: 20px;
            background: #f4f4f4;
            color: #333;
        }}
        .player {{
            max-width: 800px;
            margin: 0 auto;
            background: #fff;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1, h2 {{
            color: #2c3e50;
        }}
        h1 {{
            text-align: center;
            margin-bottom: 30px;
        }}
        video {{
            width: 100%;
            height: auto;
            border-radius: 4px;
        }}
        .caption-controls {{
            margin: 15px 0;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        button {{
            padding: 8px 15px;
            background: #f0f0f0;
            border: 1px solid #ddd;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s;
        }}
        button:hover {{
            background: #e0e0e0;
        }}
        button.active {{
            background: #3498db;
            color: white;
            border-color: #2980b9;
        }}
        .status {{
            margin-top: 15px;
            padding: 10px;
            border-radius: 4px;
            background: #f8f9fa;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="player">
        <h1>Live HLS Stream with Embedded Captions</h1>
        <video id="video" controls crossorigin="anonymous">
            <source src="stream_master.m3u8" type="application/vnd.apple.mpegurl">
            <track id="subtitles-ru" kind="subtitles" src="captions_ru.vtt?_={timestamp}" srclang="ru" label="Russian">
            <track id="subtitles-en" kind="subtitles" src="captions_en.vtt?_={timestamp}" srclang="en" label="English">
            <track id="subtitles-nl" kind="subtitles" src="captions_nl.vtt?_={timestamp}" srclang="nl" label="Dutch">
        </video>
        
        <div class="caption-controls">
            <button id="btn-ru" onclick="switchCaptions('ru')">Russian</button>
            <button id="btn-en" onclick="switchCaptions('en')">English</button>
            <button id="btn-nl" onclick="switchCaptions('nl')">Dutch</button>
            <button id="btn-off" onclick="switchCaptions('off')" class="active">Off</button>
        </div>
        
        <div class="status">
            Caption updates: <span id="update-status">Connecting...</span>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script>
        // Format duration in seconds to WebVTT timestamp format
        function format_duration(seconds) {{
            const milliseconds = Math.floor(seconds * 1000);
            const hours = Math.floor(milliseconds / 3600000);
            const minutes = Math.floor((milliseconds % 3600000) / 60000);
            const secs = Math.floor((milliseconds % 60000) / 1000);
            const ms = milliseconds % 1000;
            return hours.toString().padStart(2, '0') + ':' + 
                   minutes.toString().padStart(2, '0') + ':' + 
                   secs.toString().padStart(2, '0') + '.' + 
                   ms.toString().padStart(3, '0');
        }}

        // Setup HLS playback
        const video = document.getElementById('video');
        if (Hls.isSupported()) {{
            const hls = new Hls({{
                capLevelToPlayerSize: true,
                maxBufferLength: 30
            }});
            hls.loadSource(video.querySelector('source').src);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, function() {{
                video.play().catch(error => {{
                    console.log('Auto-play was prevented:', error);
                }});
            }});
            
            // Error handling
            hls.on(Hls.Events.ERROR, function(event, data) {{
                console.warn('HLS error:', data);
                if (data.fatal) {{
                    switch(data.type) {{
                        case Hls.ErrorTypes.NETWORK_ERROR:
                            console.error('Network error, trying to recover');
                            hls.startLoad();
                            break;
                        case Hls.ErrorTypes.MEDIA_ERROR:
                            console.error('Media error, trying to recover');
                            hls.recoverMediaError();
                            break;
                        default:
                            console.error('Fatal error, cannot recover');
                            break;
                    }}
                }}
            }});
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
            // For Safari
            video.src = video.querySelector('source').src;
            video.addEventListener('loadedmetadata', function() {{
                video.play().catch(error => {{
                    console.log('Auto-play was prevented:', error);
                }});
            }});
        }}

        // Function to switch caption tracks
        function switchCaptions(lang) {{
            // Update buttons
            document.querySelectorAll('.caption-controls button').forEach(btn => {{
                btn.classList.remove('active');
            }});
            document.getElementById('btn-' + (lang === 'off' ? 'off' : lang)).classList.add('active');
            
            // Update track modes
            Array.from(video.textTracks).forEach(track => {{
                track.mode = (lang !== 'off' && track.language === lang) ? 'showing' : 'hidden';
            }});
        }}

        // Initialize all tracks as hidden
        window.addEventListener('load', () => {{
            Array.from(video.textTracks).forEach(track => {{
                track.mode = 'hidden';
            }});
        }});

        // WebSocket setup for caption updates
        function setupCaptionWebSocket() {{
            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = wsProtocol + '//' + window.location.host + '/ws/captions';
            const ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {{
                document.getElementById('update-status').textContent = 'Connected';
            }};
            
            ws.onclose = () => {{
                document.getElementById('update-status').textContent = 'Disconnected - Reconnecting...';
                setTimeout(setupCaptionWebSocket, 1000);
            }};
            
            ws.onerror = (error) => {{
                console.error('WebSocket error:', error);
                document.getElementById('update-status').textContent = 'Error - Reconnecting...';
            }};
            
            ws.onmessage = (event) => {{
                const data = JSON.parse(event.data);
                const lang = data.language;
                const cues = data.cues;
                
                // Get the video track for this language
                const track = Array.from(video.textTracks).find(t => t.language === lang);
                if (!track) return;
                
                // Clear existing cues to prevent duplicates
                while (track.cues && track.cues.length) {{
                    track.removeCue(track.cues[0]);
                }}
                
                // Add new cues using the native TextTrack API
                cues.forEach(cue => {{
                    try {{
                        const vttCue = new VTTCue(
                            parseFloat(cue.start),
                            parseFloat(cue.end),
                            cue.text
                        );
                        track.addCue(vttCue);
                    }} catch (e) {{
                        console.error('Error adding cue:', e);
                    }}
                }});
                
                document.getElementById('update-status').textContent = 
                    'Updated at ' + new Date().toLocaleTimeString();
            }};
        }}

        // Initialize WebSocket connection
        setupCaptionWebSocket();
    </script>
</body>
</html>"""

async def start_web_server():
    """Start the FastAPI web server."""
    config = uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT)
    server = uvicorn.Server(config)
    await server.serve()

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
    For each final transcript and translation, print to terminal and append as a WebVTT cue.
    """
    # Log the first message of each type to understand the structure
    seen_types = set()
    
    async for message in socket:
        content = json.loads(message)
        msg_type = content["type"]
        
        # Log first instance of each message type for debugging
        if DEBUG_MESSAGES and msg_type not in seen_types:
            seen_types.add(msg_type)
            print(f"\nFIRST EXAMPLE OF {msg_type.upper()} MESSAGE:")
            print(json.dumps(content, indent=2, ensure_ascii=False))
            print("\n")
            
        # Handle original transcription
        if msg_type == "transcript" and content["data"]["is_final"]:
            utterance = content["data"]["utterance"]
            start = utterance["start"]
            end = utterance["end"]
            text = utterance["text"].strip()
            print(f"[Original] {format_duration(start)} --> {format_duration(end)} | {text}")
            await append_vtt_cue(start, end, text, "ru")
            
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
                    
                    if lang in ["en", "nl"]:
                        print(f"[{lang.upper()}] {format_duration(start)} --> {format_duration(end)} | {text}")
                        await append_vtt_cue(start, end, text, lang)
                        
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
                    
                    text = translation["text"].strip()
                    lang = translation["target_language"]
                    if lang in ["en", "nl"]:
                        print(f"[{lang.upper()}] {format_duration(start)} --> {format_duration(end)} | {text}")
                        await append_vtt_cue(start, end, text, lang)
                
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

# === Main Transcription Flow with Prebuffering ===
async def transcription_main():
    """
    Start transcription by connecting to Gladia.
    Prebuffer until at least MIN_CUES caption cues are available,
    then start the HTTP server and FFmpeg for HLS streaming.
    """
    print("\nTranscribing audio and outputting live WebVTT captions with translations.")
    
    # Clear any existing output files
    for lang in caption_cues.keys():
        vtt_path = os.path.join(OUTPUT_VTT_DIR, f"captions_{lang}.vtt")
        if os.path.exists(vtt_path):
            os.remove(vtt_path)
    
    # Initialize fresh VTT files
    await write_vtt_header()
    
    # Start FFmpeg process for HLS output
    packager_task = asyncio.create_task(start_ffmpeg_hls())
    
    # Initialize Gladia session
    response = init_live_session(STREAMING_CONFIGURATION)
    
    # Start WebSocket connection to Gladia
    async with ws_connect(response["url"]) as websocket:
        print("\n################ Begin session ################\n")
        
        # Set up signal handler for graceful shutdown
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(stop_recording(websocket)))
        
        # Start message processing task
        message_task = asyncio.create_task(process_messages_from_socket(websocket))
        
        # Start audio streaming task
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
        
        # Start the web server
        web_server_task = asyncio.create_task(start_web_server())
        print(f"Web server started on port {HTTP_PORT}")
        
        # Continue processing until the tasks complete or are cancelled
        try:
            await asyncio.gather(message_task, audio_task, web_server_task, packager_task)
        except asyncio.CancelledError:
            print("Tasks cancelled - shutting down...")
        finally:
            # Clean up
            if ffmpeg_audio_process:
                ffmpeg_audio_process.terminate()

# === FFmpeg HLS Output ===
async def start_ffmpeg_hls():
    """
    Use FFmpeg's native HLS output to create a live HLS stream with separate audio and video tracks.
    """
    os.makedirs(HLS_OUTPUT_DIR, exist_ok=True)
    
    # Create output directories for audio and video segments
    os.makedirs(os.path.join(HLS_OUTPUT_DIR, "audio"), exist_ok=True)
    os.makedirs(os.path.join(HLS_OUTPUT_DIR, "video"), exist_ok=True)
    
    try:
        # FFmpeg command to create HLS output
        ffmpeg_command = [
            "ffmpeg", "-y",
            "-i", EXAMPLE_HLS_STREAM_URL,
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-map", "0:a",
            "-f", "hls",
            "-hls_time", SEGMENT_DURATION,
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            "-hls_segment_filename", os.path.join(HLS_OUTPUT_DIR, "audio", "segment%d.ts"),
            os.path.join(HLS_OUTPUT_DIR, "audio", "playlist.m3u8"),
            "-map", "0:v",
            "-c:v", "copy",
            "-f", "hls",
            "-hls_time", SEGMENT_DURATION,
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
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
        
        # Create master playlist
        master_playlist_path = os.path.join(HLS_OUTPUT_DIR, "stream_master.m3u8")
        master_playlist_content = """#EXTM3U
#EXT-X-VERSION:3

#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio/playlist.m3u8"

#EXT-X-STREAM-INF:BANDWIDTH=2500000,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio"
video/playlist.m3u8"""
        
        with open(master_playlist_path, "w") as f:
            f.write(master_playlist_content)
        
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

if __name__ == "__main__":
    try:
        asyncio.run(transcription_main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        if ffmpeg_audio_process:
            ffmpeg_audio_process.terminate()
        sys.exit(0)