#!/usr/bin/env python3
"""
Cable TV-Ready Live Transcription with EIA-608/708 Captions for HLS Streaming

This script specifically focuses on generating EIA-608/708 captions for cable TV standards.
It uses FFmpeg's support for closed captioning and the cc_data codec.

Usage:
    python3 rainscribe.py

Then open http://localhost:8080/index.html to view the stream.
"""

import asyncio
import json
import subprocess
import sys
import signal
import threading
import os
import time
import re
import shutil
from datetime import time as dtime
from http.server import SimpleHTTPRequestHandler, HTTPServer
from typing import TypedDict, Literal
import requests
from websockets.legacy.client import WebSocketClientProtocol, connect as ws_connect
from websockets.exceptions import ConnectionClosedOK
from dotenv import load_dotenv

# Import functions from our modules
from transcription import (
    StreamingConfiguration, init_live_session, 
    stream_audio_to_gladia, process_transcription_messages,
    get_caption_cues, transliterate_russian
)
from media import (
    write_vtt_header, update_vtt_file, create_608_captions,
    start_muxing_process, restart_muxing_process,
    check_ffmpeg_eia608_support, init_directories, cleanup_old_files,
    start_http_server
)

# === Logging Setup: Tee stdout and stderr to console and a log file ===
class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, data):
        for f in self.files:
            try:
                f.write(data)
                f.flush()
            except Exception as e:
                pass
    def flush(self):
        for f in self.files:
            try:
                f.flush()
            except Exception as e:
                pass

LOG_FILENAME = "rainscribe_run.log"
log_file = None
original_stdout = sys.stdout
original_stderr = sys.stderr

def setup_logging():
    global log_file, original_stdout, original_stderr
    
    try:
        # Create directory for log file if needed
        log_dir = os.path.dirname(LOG_FILENAME)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            
        log_file = open(LOG_FILENAME, "w", encoding="utf-8")
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = Tee(sys.stdout, log_file)
        sys.stderr = Tee(sys.stderr, log_file)
    except Exception as e:
        print(f"Warning: Could not set up logging: {e}")

def cleanup_logging():
    global log_file, original_stdout, original_stderr
    
    try:
        # Restore original stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        
        # Close log file if it was opened
        if log_file:
            log_file.close()
    except Exception as e:
        print(f"Warning: Error during logging cleanup: {e}")

# === Configuration Constants ===
GLADIA_API_URL = "https://api.gladia.io"
EXAMPLE_HLS_STREAM_URL = "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"

MIN_CUES = 5               # Wait until we have at least 5 caption cues before starting muxing
MUX_UPDATE_INTERVAL = 30   # Seconds between FFmpeg muxing process restarts
HTTP_PORT = 8080           # Port for HTTP server

# Directory structure for all generated files
OUTPUT_BASE_DIR = "rainscribe_output"  # Base directory for all output files
OUTPUT_DIR = f"{OUTPUT_BASE_DIR}/segments"  # Directory for HLS segments
CAPTIONS_DIR = f"{OUTPUT_BASE_DIR}/captions"  # Directory for caption files
TEMP_DIR = f"{OUTPUT_BASE_DIR}/temp"  # Directory for temporary files

# Filenames for captions and HLS output
CAPTIONS_VTT = f"{CAPTIONS_DIR}/captions.vtt"
CAPTIONS_SCC = f"{CAPTIONS_DIR}/captions.scc"  # Scenarist Closed Caption format (for EIA-608)
OUTPUT_PLAYLIST = f"{OUTPUT_BASE_DIR}/playlist.m3u8"  # Main playlist in the output directory

# Flag to track if the initial connection has been made
initial_connection_made = False

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

# === Example Streaming Configuration ===
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
        "words_accurate_timestamps": True
    }
}

# === Global In-Memory Storage for Cues ===
# Each cue is stored as a tuple: (start, end, text)
caption_cues = []

# === Utility Functions ===
def format_vtt_duration(seconds: float) -> str:
    """Format seconds into WebVTT time format: HH:MM:SS.mmm"""
    milliseconds = int(seconds * 1000)
    hours = milliseconds // 3600000
    minutes = (milliseconds % 3600000) // 60000
    secs = (milliseconds % 60000) // 1000
    ms = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"

def format_scc_timecode(seconds: float, use_drop_frame=True) -> str:
    """
    Format seconds into SCC timecode format: HH:MM:SS:FF
    Supports both drop-frame (29.97fps) and non-drop-frame (30fps) formats.
    
    Args:
        seconds: Time in seconds
        use_drop_frame: Whether to use drop-frame timecode (29.97fps)
                        Set to False for standard 30fps timecode
    
    Returns:
        SCC formatted timecode
    """
    if use_drop_frame:
        # NTSC drop-frame timecode (29.97 fps)
        FRAME_RATE = 29.97
        total_frames = int(seconds * FRAME_RATE)
        
        # Calculate drop-frame timecode
        # Every minute except every 10th minute, drop 2 frames
        drop_frames = 2 * (total_frames // (FRAME_RATE * 60))
        # But don't drop frames every 10th minute
        drop_frames -= 2 * (total_frames // (FRAME_RATE * 60 * 10))
        
        # Apply the drop frame correction
        adjusted_frames = total_frames - drop_frames
        
        # Calculate final timecode components
        frame_count = adjusted_frames % 30
        total_seconds = adjusted_frames // 30
        seconds_val = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60
        
        # Format with semicolons for drop-frame
        return f"{hours:02d}:{minutes:02d}:{seconds_val:02d};{frame_count:02d}"
    else:
        # Standard non-drop-frame timecode (30fps)
        total_frames = int(seconds * 30)  # 30 fps
        frames = total_frames % 30
        total_seconds = total_frames // 30
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"

def get_gladia_key() -> str:
    """
    Retrieve the Gladia API key from .env file or command-line argument.
    Prioritizes the GLADIA_API_KEY environment variable if it exists.
    """
    # Load environment variables from .env file
    load_dotenv()
    
    # First try to get the key from environment variables
    gladia_key = os.environ.get("GLADIA_API_KEY")
    
    # If not in environment, try command line argument
    if not gladia_key:
        if len(sys.argv) != 2 or not sys.argv[1]:
            print("Error: Gladia API key not found.")
            print("Either provide it as a command-line argument:")
            print("  python rainscribe.py YOUR_GLADIA_API_KEY")
            print("Or set it in a .env file with:")
            print("  GLADIA_API_KEY=your_api_key")
            sys.exit(1)
        gladia_key = sys.argv[1]
    
    return gladia_key

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

def write_index_html():
    """Generate an index.html file for the HLS player."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Cable TV-Ready Stream with EIA-608/708 Captions</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.4.14/hls.min.js"></script>
  <style>
    body {{
      font-family: Arial, sans-serif;
      background: #222;
      color: #ddd;
      text-align: center;
      padding: 20px;
    }}
    video {{
      width: 100%;
      max-width: 800px;
      margin: 20px auto;
      display: block;
      background: #000;
    }}
    h2 {{
      margin-bottom: 10px;
    }}
    .controls {{
      margin: 20px auto;
      max-width: 800px;
    }}
    button {{
      background: #444;
      color: white;
      border: none;
      padding: 8px 16px;
      margin: 0 5px;
      border-radius: 4px;
      cursor: pointer;
    }}
    button:hover {{
      background: #555;
    }}
    .status {{
      background: #333;
      padding: 10px;
      border-radius: 5px;
      margin: 10px auto;
      max-width: 800px;
    }}
    .instructions {{
      text-align: left;
      max-width: 800px;
      margin: 20px auto;
      background: #333;
      padding: 15px;
      border-radius: 5px;
    }}
    .debug {{
      text-align: left;
      max-width: 800px;
      margin: 20px auto;
      background: #332;
      padding: 15px;
      border-radius: 5px;
      font-family: monospace;
      font-size: 12px;
      height: 150px;
      overflow: auto;
    }}
    .caption-display {{
      background: #000;
      color: #fff;
      padding: 10px;
      border-radius: 5px;
      margin: 10px auto;
      max-width: 800px;
      min-height: 40px;
      text-align: center;
      font-weight: bold;
      font-family: 'Arial', sans-serif;
    }}
    .cable-info {{
      background: #224;
      padding: 10px;
      border-radius: 5px;
      margin: 20px auto;
      max-width: 800px;
    }}
  </style>
</head>
<body>
  <h2>Cable TV-Ready Stream with EIA-608/708 Captions</h2>
  
  <video id="video" controls autoplay muted></video>
  
  <div class="caption-display" id="caption-display">Captions will appear here</div>
  
  <div class="controls">
    <button id="cc-btn">Toggle Captions</button>
    <button id="refresh-btn">Refresh Player</button>
    <button id="mute-btn">Toggle Mute</button>
  </div>
  
  <div class="status" id="status">Waiting for stream...</div>
  
  <div class="cable-info">
    <h3>Cable TV Compatibility Information</h3>
    <p>This stream attempts to include EIA-608/708 closed captions for cable TV compatibility.</p>
    <p>Caption format: <span id="caption-format">Detecting...</span></p>
  </div>
  
  <div class="instructions">
    <h3>About This Player</h3>
    <p>This player displays the live stream with cable TV-compatible captions.</p>
    <h3>Troubleshooting</h3>
    <ul>
      <li>If stream doesn't load, wait a few more seconds for transcription to buffer</li>
      <li>Click the "Refresh Player" button if the stream doesn't appear after 10 seconds</li>
      <li>For in-video captions, make sure captions are enabled in your player</li>
      <li>External players like VLC can use the direct stream URL below</li>
    </ul>
  </div>
  
  <div class="debug" id="debug"></div>
  
  <p>Direct stream URL (for VLC or other players):</p>
  <p><a href="http://localhost:{HTTP_PORT}/playlist.m3u8" style="color:#0bf;">http://localhost:{HTTP_PORT}/playlist.m3u8</a></p>
  
  <script>
    document.addEventListener('DOMContentLoaded', function() {{
      const video = document.getElementById('video');
      const ccBtn = document.getElementById('cc-btn');
      const refreshBtn = document.getElementById('refresh-btn');
      const muteBtn = document.getElementById('mute-btn');
      const statusDiv = document.getElementById('status');
      const debugDiv = document.getElementById('debug');
      const captionDisplay = document.getElementById('caption-display');
      const captionFormatSpan = document.getElementById('caption-format');
      let hlsInstance;
      let captionsEnabled = true;
      
      function updateStatus(message) {{
        statusDiv.textContent = message;
        logDebug(message);
      }}
      
      function logDebug(message) {{
        const timestamp = new Date().toLocaleTimeString();
        debugDiv.innerHTML += '<div>[' + timestamp + '] ' + message + '</div>';
        debugDiv.scrollTop = debugDiv.scrollHeight; // Auto-scroll to bottom
      }}
      
      // Load and display current captions
      function loadCaptions() {{
        const fetchCaptions = async () => {{
          try {{
            // Fetch the WebVTT captions file
            const response = await fetch('/rainscribe_output/captions/captions.vtt');
            if (!response.ok) {{
              throw new Error('Failed to load captions');
            }}
            
            const text = await response.text();
            
            // Parse the WebVTT file and find the current caption
            const lines = text.split('\\n');
            if (lines.length > 2) {{ // Make sure we have at least one caption
              const currentTime = video.currentTime;
              
              // Find the caption that corresponds to the current time
              let currentCaption = '';
              for (let i = 2; i < lines.length; i++) {{
                const line = lines[i].trim();
                
                // Look for timestamp lines
                if (line.includes('-->')) {{
                  const timestamps = line.split('-->');
                  const startTime = parseVttTimestamp(timestamps[0].trim());
                  const endTime = parseVttTimestamp(timestamps[1].trim());
                  
                  if (currentTime >= startTime && currentTime <= endTime) {{
                    // Found a caption for current time, get the next line
                    currentCaption = lines[i+1] ? lines[i+1].trim() : '';
                    break;
                  }}
                }}
              }}
              
              // Update the caption display
              if (captionsEnabled) {{
                captionDisplay.textContent = currentCaption;
              }}
            }}
          }} catch (error) {{
            console.error('Error fetching captions:', error);
          }}
        }};
        
        // Parse VTT timestamp to seconds
        function parseVttTimestamp(timestamp) {{
          const parts = timestamp.split(':');
          const seconds = parseFloat(parts[2]);
          const minutes = parseInt(parts[1]);
          const hours = parseInt(parts[0]);
          return hours * 3600 + minutes * 60 + seconds;
        }}
        
        // Poll for captions every second
        setInterval(fetchCaptions, 1000);
      }}
      
      // Detect caption format
      function detectCaptionFormat() {{
        // First, check if the video has embedded tracks
        if (video.textTracks && video.textTracks.length > 0) {{
          const formats = [];
          for (let i = 0; i < video.textTracks.length; i++) {{
            const track = video.textTracks[i];
            formats.push(track.label || track.kind || 'Unknown');
          }}
          
          if (formats.length > 0) {{
            captionFormatSpan.textContent = formats.join(', ');
            logDebug('Detected caption tracks: ' + formats.join(', '));
            return;
          }}
        }}
        
        // Try to check the stream directly
        fetch('/playlist.m3u8')
          .then(response => response.text())
          .then(content => {{
            if (content.includes('EIA-608') || content.includes('CEA-608')) {{
              captionFormatSpan.textContent = 'EIA-608/CEA-608';
            }} else if (content.includes('WEBVTT') || content.includes('webvtt')) {{
              captionFormatSpan.textContent = 'WebVTT';
            }} else {{
              captionFormatSpan.textContent = 'Fallback captions';
            }}
          }})
          .catch(err => {{
            captionFormatSpan.textContent = 'Unknown format';
          }});
      }}
      
      // Setup HLS.js player
      function setupPlayer() {{
        if(Hls.isSupported()) {{
          updateStatus("Setting up player...");
          
          if (hlsInstance) {{
            hlsInstance.destroy();
          }}
          
          hlsInstance = new Hls({{
            debug: false,
            enableWorker: true,
            lowLatencyMode: true,
            backBufferLength: 60,
            manifestLoadingTimeOut: 20000, // Longer timeout
            manifestLoadingMaxRetry: 6,    // More retries
            levelLoadingTimeOut: 20000,
            fragLoadingTimeOut: 20000
          }});
          
          function loadStream() {{
            updateStatus("Loading stream...");
            const streamUrl = '/playlist.m3u8?t=' + new Date().getTime();
            hlsInstance.loadSource(streamUrl);
            hlsInstance.attachMedia(video);
          }}
          
          hlsInstance.on(Hls.Events.MANIFEST_PARSED, function() {{
            updateStatus("Stream loaded successfully! Starting playback...");
            video.play();
            
            // Check for caption tracks
            setTimeout(() => {{
              if (video.textTracks && video.textTracks.length > 0) {{
                logDebug('Detected ' + video.textTracks.length + ' text tracks');
                for (let i = 0; i < video.textTracks.length; i++) {{
                  const track = video.textTracks[i];
                  logDebug('Track ' + i + ': ' + (track.label || 'Unlabeled') + ' (' + track.kind + ')');
                  
                  // Enable the first track
                  if (i === 0) {{
                    track.mode = captionsEnabled ? 'showing' : 'hidden';
                  }}
                }}
              }} else {{
                logDebug('No caption tracks detected in video, using WebVTT fallback');
                loadCaptions();
              }}
              
              detectCaptionFormat();
            }}, 2000);
          }});
          
          hlsInstance.on(Hls.Events.ERROR, function(event, data) {{
            logDebug('HLS Error: ' + JSON.stringify(data.details));
            if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {{
              updateStatus("Network error: " + data.details + ". Retrying...");
              setTimeout(() => {{
                loadStream();
              }}, 3000);
            }} else if (data.fatal) {{
              updateStatus("Fatal error: " + data.details + ". Try refreshing.");
            }}
          }});
          
          loadStream();
        }}
        else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          // Native HLS support (Safari)
          updateStatus("Using native HLS support");
          video.src = '/playlist.m3u8?t=' + new Date().getTime();
          video.addEventListener('loadedmetadata', function() {{
            updateStatus("Stream loaded successfully!");
            video.play();
            // Check for caption tracks
            setTimeout(() => {{
              if (video.textTracks && video.textTracks.length > 0) {{
                logDebug('Detected ' + video.textTracks.length + ' text tracks with native player');
                for (let i = 0; i < video.textTracks.length; i++) {{
                  const track = video.textTracks[i];
                  logDebug('Track ' + i + ': ' + (track.label || 'Unlabeled') + ' (' + track.kind + ')');
                  
                  // Enable the first track
                  if (i === 0) {{
                    track.mode = captionsEnabled ? 'showing' : 'hidden';
                  }}
                }}
              }} else {{
                logDebug('No caption tracks detected, using WebVTT fallback');
                loadCaptions();
              }}
              
              detectCaptionFormat();
            }}, 2000);
          }});
          video.addEventListener('error', function(e) {{
            updateStatus("Error loading stream: " + (video.error ? video.error.message : "unknown error"));
          }});
        }} else {{
          updateStatus("HLS playback not supported in this browser");
        }}
      }}
      
      // Toggle captions
      ccBtn.addEventListener('click', function() {{
        captionsEnabled = !captionsEnabled;
        ccBtn.textContent = captionsEnabled ? 'Hide Captions' : 'Show Captions';
        
        // Toggle in-video captions if available
        if (video.textTracks && video.textTracks.length > 0) {{
          for (let i = 0; i < video.textTracks.length; i++) {{
            video.textTracks[i].mode = captionsEnabled ? 'showing' : 'hidden';
          }}
        }}
        
        // Clear fallback caption display if disabled
        if (!captionsEnabled) {{
          captionDisplay.textContent = '';
        }}
        
        logDebug('Captions ' + (captionsEnabled ? 'enabled' : 'disabled'));
      }});
      
      // Toggle mute
      muteBtn.addEventListener('click', function() {{
        video.muted = !video.muted;
        muteBtn.textContent = video.muted ? 'Unmute' : 'Mute';
      }});
      
      // Refresh player
      refreshBtn.addEventListener('click', function() {{
        updateStatus("Refreshing player...");
        setupPlayer();
      }});
      
      // Poll for stream availability
      function pollForStream() {{
        fetch('/playlist.m3u8', {{ method: 'HEAD' }})
          .then(response => {{
            if (response.ok) {{
              updateStatus("Stream is available! Loading...");
              setupPlayer();
              return true;
            }}
            return false;
          }})
          .catch(err => {{
            logDebug("Still waiting for stream...");
            return false;
          }})
          .then(success => {{
            if (!success) {{
              // If not successful, poll again in 5 seconds
              setTimeout(pollForStream, 5000);
            }}
          }});
      }}
      
      // Initialize
      video.muted = true; // Start muted (better autoplay)
      muteBtn.textContent = 'Unmute';
      updateStatus("Waiting for stream to be available...");
      pollForStream();
    }});
  </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html for the player.")

async def transcription_main():
    """
    Start transcription by connecting to Gladia.
    Prebuffer until at least MIN_CUES cues are collected,
    then start the FFmpeg muxing process.
    Periodically restart the muxing process so that new captions are picked up.
    """
    socket = None
    mux_task = None
    try:
        print("\nStarting live transcription session with EIA-608/708 captions.")
        write_index_html()  # Generate index.html for the player
        write_vtt_header()  # Initialize WebVTT file
        response = init_live_session(STREAMING_CONFIGURATION)
        
        # Start HTTP server in a separate thread
        http_server_thread = threading.Thread(target=start_http_server, args=(HTTP_PORT,), daemon=True)
        http_server_thread.start()
        
        from websockets.legacy.client import connect as ws_connect
        async with ws_connect(response["url"]) as socket:
            print("\n################ Begin session ################\n")
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(socket.send(json.dumps({"type": "stop_recording"}))))
            
            # Start a callback function to update captions when new transcriptions arrive
            transcription_task = asyncio.create_task(
                process_transcription_messages(socket, update_vtt_file)
            )
            
            # Stream audio to Gladia
            audio_task = asyncio.create_task(
                stream_audio_to_gladia(socket, EXAMPLE_HLS_STREAM_URL, STREAMING_CONFIGURATION)
            )
            
            print(f"Prebuffering transcriptions until at least {MIN_CUES} cues are collected...")
            while len(get_caption_cues()) < MIN_CUES:
                await asyncio.sleep(0.5)
            
            print(f"Prebuffer complete: {len(get_caption_cues())} cues collected. Starting FFmpeg muxing process.")
            start_muxing_process(EXAMPLE_HLS_STREAM_URL)
            
            # Periodically restart the muxing process to update captions
            async def mux_updater():
                while True:
                    await asyncio.sleep(MUX_UPDATE_INTERVAL)
                    restart_muxing_process(EXAMPLE_HLS_STREAM_URL)
                    
            mux_task = asyncio.create_task(mux_updater())
            await asyncio.gather(transcription_task, audio_task)
        
    except asyncio.CancelledError:
        print("Transcription tasks cancelled")
    except Exception as e:
        print(f"Error in transcription: {e}")
    finally:
        # Cancel the mux task if it exists
        if mux_task:
            mux_task.cancel()
            try:
                await mux_task
            except asyncio.CancelledError:
                pass
            
        # Ensure the socket is closed
        if socket and not socket.closed:
            try:
                await socket.close()
            except:
                pass

def main():
    """
    Main function to run the script.
    """
    try:
        # Set up logging
        setup_logging()
        
        # Create output directories
        init_directories()
        
        # Clean up old files
        cleanup_old_files()
        
        # Check FFmpeg capabilities
        print("Checking FFmpeg capabilities...")
        has_eia608 = check_ffmpeg_eia608_support()
        if has_eia608:
            print("Your FFmpeg installation supports EIA-608 captions.")
        else:
            print("Your FFmpeg installation may not fully support EIA-608 captions.")
            print("The script will try to create EIA-608 captions, but they may not work in all players.")
        
        # Run the async event loop
        asyncio.run(transcription_main())
        
    except KeyboardInterrupt:
        print("KeyboardInterrupt received, shutting down.")
    except Exception as e:
        print(f"Error in main execution: {e}")
    finally:
        # Clean up logging
        cleanup_logging()
        
        print("Run complete. Full output has been saved to", LOG_FILENAME)

if __name__ == "__main__":
    main()