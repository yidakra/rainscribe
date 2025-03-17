#!/usr/bin/env python3
"""
Live Transcription with Embedded Caption Track for HLS Streaming

This script does the following:
  • Initializes a live transcription session with Gladia.
  • Streams audio from an HLS URL (via FFmpeg) to Gladia's WebSocket endpoint.
  • Receives transcription messages continuously and appends each final transcript as a WebVTT cue to an in‑memory list and a live WebVTT file.
  • Starts an FFmpeg process to repackage the original HLS stream into a new HLS stream.
  • Generates a master playlist that includes an external subtitles track referencing the live‑updating WebVTT file.
  • Starts an HTTP server to serve the new HLS stream, the master playlist, the segments, and the live captions.
  • When you press Ctrl+C the full output of the run is saved to a log file.

Usage:
    python3 rainscribe_embedded.py YOUR_GLADIA_API_KEY

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
from http.server import SimpleHTTPRequestHandler, HTTPServer
from typing import TypedDict, Literal
import requests
from websockets.legacy.client import WebSocketClientProtocol, connect as ws_connect
from websockets.exceptions import ConnectionClosedOK

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

LOG_FILENAME = "rainscribe_run.log"
log_file = open(LOG_FILENAME, "w", encoding="utf-8")
sys.stdout = Tee(sys.stdout, log_file)
sys.stderr = Tee(sys.stderr, log_file)

# === Configuration Constants ===
GLADIA_API_URL = "https://api.gladia.io"
EXAMPLE_HLS_STREAM_URL = "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"

MIN_CUES = 2  # Adjust as needed (2 works well for the example stream)

HTTP_PORT = 8080  # For serving index.html and HLS stream

OUTPUT_VTT_FILE = "captions.vtt"

# Directory for repackaged HLS stream
HLS_OUTPUT_DIR = "output"

# === Type Definitions ===
class InitiateResponse(TypedDict):
    id: str
    url: str

class LanguageConfiguration(TypedDict):
    languages: list[str] | None
    code_switching: bool | None

class StreamingConfiguration(TypedDict):
    encoding: Literal["wav/pcm", "wav/alaw", "wav/ulaw"]
    bit_depth: Literal[8, 16, 24, 32]
    sample_rate: Literal[8000, 16000, 32000, 44100, 48000]
    channels: int
    language_config: LanguageConfiguration | None
    realtime_processing: dict[str, dict[str, list[str]]] | None

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
DEBUG_MESSAGES = False

# === Global In-Memory Storage for Caption Cues ===
caption_cues = {
    "ru": [],  # Original Russian captions
    "en": [],  # English translations
    "nl": []   # Dutch translations
}

# === Utility Functions ===
def format_duration(seconds: float) -> str:
    """Format seconds into WebVTT time format: HH:MM:SS.mmm"""
    milliseconds = int(seconds * 1000)
    hours = milliseconds // 3600000
    minutes = (milliseconds % 3600000) // 60000
    secs = (milliseconds % 60000) // 1000
    ms = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"

def get_gladia_key() -> str:
    """Retrieve the Gladia API key from the first command-line argument."""
    if len(sys.argv) != 2 or not sys.argv[1]:
        print("You must provide a Gladia key as the first argument.")
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

def write_vtt_header():
    """Write the WebVTT header to the live captions file for each language."""
    for lang in caption_cues.keys():
        with open(f"captions_{lang}.vtt", "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")

def append_vtt_cue(start: float, end: float, text: str, lang="ru"):
    """
    Append a WebVTT cue to the in-memory list and to the live captions file for a specific language.
    """
    cue = {"start": start, "end": end, "text": text.strip()}
    caption_cues[lang].append(cue)
    vtt_line = f"{format_duration(start)} --> {format_duration(end)}\n{text.strip()}\n\n"
    with open(f"captions_{lang}.vtt", "a", encoding="utf-8") as f:
        f.write(vtt_line)
    print(f"[{lang}] Appended cue: {vtt_line.strip()}")

# === FFmpeg Repackaging for HLS Streaming with Captions ===
def start_ffmpeg_repackaging():
    """
    Use FFmpeg to repackage the original HLS stream into a new HLS stream.
    The output will be stored in the HLS_OUTPUT_DIR directory.
    """
    os.makedirs(HLS_OUTPUT_DIR, exist_ok=True)
    ffmpeg_command = [
        "ffmpeg", "-re",
        "-i", EXAMPLE_HLS_STREAM_URL,
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        "-hls_segment_filename", os.path.join(HLS_OUTPUT_DIR, "segment_%03d.ts"),
        os.path.join(HLS_OUTPUT_DIR, "stream.m3u8")
    ]
    print("Starting FFmpeg repackaging process for HLS stream...")
    return subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def write_master_playlist():
    """
    Write the master HLS playlist that references the repackaged video stream
    and includes the subtitles track for each language.
    """
    master_playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="ru",NAME="Russian",DEFAULT=YES,AUTOSELECT=YES,URI="captions_ru.vtt"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="en",NAME="English",DEFAULT=NO,AUTOSELECT=NO,URI="captions_en.vtt"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="nl",NAME="Dutch",DEFAULT=NO,AUTOSELECT=NO,URI="captions_nl.vtt"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720,SUBTITLES="subs"
"""
    master_playlist += f"{HLS_OUTPUT_DIR}/stream.m3u8\n"
    with open("stream_master.m3u8", "w", encoding="utf-8") as f:
        f.write(master_playlist)
    print("Master playlist 'stream_master.m3u8' written.")

# === HTTP Server (serving index.html, HLS stream, and captions) ===
class DynamicHTTPRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Serve the complete captions file for any language
        if self.path.startswith("/captions_"):
            lang = self.path.split("_")[1].split(".")[0]
            if lang in caption_cues:
                self.send_response(200)
                self.send_header("Content-type", "text/vtt; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                content = "WEBVTT\n\n"
                for cue in caption_cues[lang]:
                    content += f"{format_duration(cue['start'])} --> {format_duration(cue['end'])}\n{cue['text']}\n\n"
                self.wfile.write(content.encode("utf-8"))
            else:
                self.send_error(404, "File not found")
        else:
            super().do_GET()

def start_http_server(port=HTTP_PORT):
    handler = DynamicHTTPRequestHandler
    with HTTPServer(("", port), handler) as httpd:
        print(f"Serving HTTP on http://localhost:{port} ...")
        httpd.serve_forever()

# === Index HTML Generation (Using native caption support and periodic refresh) ===
def write_index_html():
    """Generate an index.html file that contains an HLS player with native caption support for multiple languages.
       A small script periodically removes and re-adds the <track> elements (with cache-busting queries)
       so that the latest captions are fetched.
    """
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>HLS Player with Embedded Captions</title>
  <style>
    body {{
      font-family: Arial, sans-serif;
      padding: 20px;
      background: #f4f4f4;
    }}
    .player {{
      max-width: 800px;
      margin: 0 auto;
      background: #fff;
      padding: 10px;
      border-radius: 8px;
    }}
    video {{
      width: 100%;
      height: auto;
    }}
    .caption-controls {{
      margin: 10px 0;
    }}
    button {{
      padding: 5px 10px;
      margin-right: 5px;
      cursor: pointer;
    }}
    .active {{
      background: #007bff;
      color: white;
    }}
  </style>
</head>
<body>
  <div class="player">
    <h2>Live HLS Stream with Embedded Captions</h2>
    <video id="video" controls crossorigin="anonymous">
      <source src="stream_master.m3u8" type="application/vnd.apple.mpegurl">
      <track id="subtitles-ru" kind="subtitles" src="captions_ru.vtt?t={int(time.time())}" srclang="ru" label="Russian" default>
      <track id="subtitles-en" kind="subtitles" src="captions_en.vtt?t={int(time.time())}" srclang="en" label="English">
      <track id="subtitles-nl" kind="subtitles" src="captions_nl.vtt?t={int(time.time())}" srclang="nl" label="Dutch">
    </video>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script>
    // Setup HLS playback
    var video = document.getElementById('video');
    if (Hls.isSupported()) {{
      var hls = new Hls();
      hls.loadSource(video.querySelector('source').src);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, function() {{
        video.play();
      }});
    }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
      video.src = video.querySelector('source').src;
      video.addEventListener('loadedmetadata', function() {{
        video.play();
      }});
    }}

    // Function to switch caption tracks
    function switchCaptions(lang) {{
      // Update buttons
      document.querySelectorAll('.caption-controls button').forEach(btn => {{
        btn.classList.remove('active');
      }});
      if (lang !== 'off') {{
        document.getElementById('btn-' + lang).classList.add('active');
      }} else {{
        document.getElementById('btn-off').classList.add('active');
      }}
      
      // Update track mode
      var tracks = video.textTracks;
      for (var i = 0; i < tracks.length; i++) {{
        if (lang === 'off') {{
          tracks[i].mode = 'hidden';
        }} else if (tracks[i].language === lang) {{
          tracks[i].mode = 'showing';
        }} else {{
          tracks[i].mode = 'hidden';
        }}
      }}
    }}

    // Every 10 seconds, remove and re-add the <track> elements with cache-busting query parameters
    setInterval(function() {{
      var langs = ['ru', 'en', 'nl'];
      langs.forEach(function(lang) {{
        var oldTrack = document.getElementById('subtitles-' + lang);
        if (oldTrack) {{
          var wasDefault = oldTrack.default;
          var isActive = false;
          if (oldTrack.track && oldTrack.track.mode === 'showing') {{
            isActive = true;
          }}
          oldTrack.parentNode.removeChild(oldTrack);
          
          var newTrack = document.createElement('track');
          newTrack.id = 'subtitles-' + lang;
          newTrack.kind = 'subtitles';
          newTrack.label = lang === 'ru' ? 'Russian' : (lang === 'en' ? 'English' : 'Dutch');
          newTrack.srclang = lang;
          newTrack.src = 'captions_' + lang + '.vtt?t=' + Date.now();
          newTrack.default = wasDefault;
          video.appendChild(newTrack);
          
          if (isActive) {{
            setTimeout(function() {{
              newTrack.track.mode = 'showing';
            }}, 100);
          }}
        }}
      }});
    }}, 10000);
  </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html for the player with multiple caption options.")

# === Audio & Transcription Handling ===
async def stream_audio_from_hls(socket: WebSocketClientProtocol, hls_url: str) -> None:
    """
    Launch FFmpeg to stream audio from the HLS URL to Gladia via WebSocket.
    """
    ffmpeg_command = [
        "ffmpeg", "-re",
        "-i", hls_url,
        "-ar", str(STREAMING_CONFIGURATION["sample_rate"]),
        "-ac", str(STREAMING_CONFIGURATION["channels"]),
        "-f", "wav",
        "-bufsize", "16K",
        "pipe:1",
    ]
    ffmpeg_process = subprocess.Popen(
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
        audio_chunk = ffmpeg_process.stdout.read(chunk_size)
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
    except Exception:
        pass
    ffmpeg_process.terminate()

async def print_messages_from_socket(socket: WebSocketClientProtocol) -> None:
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
            append_vtt_cue(start, end, text, "ru")
            
        # Handle translations - there are multiple possible formats
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
                        append_vtt_cue(start, end, text, lang)
                
                # Other formats - kept for fallback compatibility
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
                        append_vtt_cue(start, end, text, lang)
                
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
    except Exception:
        pass
    await asyncio.sleep(0)

# === Main Transcription Flow with Prebuffering ===
async def transcription_main():
    """
    Start transcription by connecting to Gladia.
    Prebuffer until at least MIN_CUES caption cues are available,
    then start the HTTP server and FFmpeg repackaging for HLS streaming.
    """
    print("\nTranscribing audio and outputting live WebVTT captions with translations.")
    write_index_html()
    write_vtt_header()
    write_master_playlist()
    # Start FFmpeg repackaging process for HLS stream
    ffmpeg_hls_proc = start_ffmpeg_repackaging()
    response = init_live_session(STREAMING_CONFIGURATION)
    async with ws_connect(response["url"]) as websocket:
        print("\n################ Begin session ################\n")
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(stop_recording(websocket)))
        message_task = asyncio.create_task(print_messages_from_socket(websocket))
        audio_task = asyncio.create_task(stream_audio_from_hls(websocket, EXAMPLE_HLS_STREAM_URL))
        print(f"Prebuffering transcriptions until at least {MIN_CUES} cues are collected for the original language...")
        while len(caption_cues["ru"]) < MIN_CUES:
            await asyncio.sleep(0.5)
        print(f"Prebuffer complete: {len(caption_cues['ru'])} cues collected.")
        # Start HTTP server in a separate thread
        http_thread = threading.Thread(target=start_http_server, daemon=True)
        http_thread.start()
        print("HTTP server started on port", HTTP_PORT)
        # Continue processing until the transcription tasks complete
        await asyncio.gather(message_task, audio_task)
        ffmpeg_hls_proc.terminate()

# === Main Function ===
async def main():
    await transcription_main()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("KeyboardInterrupt received, shutting down.")
    finally:
        print("Run complete. Full output has been saved to", LOG_FILENAME)
        log_file.close()