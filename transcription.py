#!/usr/bin/env python3
"""
Transcription Module for RainScribe

This module handles Gladia API interactions, transcription processing,
and caption generation.
"""

import asyncio
import json
import subprocess
import os
import time
from typing import List, Tuple, Dict, Any, Callable, Optional
from websockets.legacy.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosedOK
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Type definition for streaming configuration
StreamingConfiguration = Dict[str, Any]

# Global storage for caption cues
caption_cues: List[Tuple[float, float, str]] = []

def init_live_session(streaming_configuration: StreamingConfiguration) -> Dict[str, Any]:
    """
    Initialize a live transcription session with Gladia API.
    
    Args:
        streaming_configuration: Configuration for the streaming session
        
    Returns:
        Response from Gladia API with WebSocket URL
    """
    import requests
    
    # Get API key from environment variable
    api_key = os.getenv("GLADIA_API_KEY")
    if not api_key:
        raise ValueError("GLADIA_API_KEY environment variable not set. Please create a .env file with your API key.")
    
    # Initialize the session
    url = "https://api.gladia.io/audio/text/audio-transcription/live-transcription"
    headers = {
        "x-gladia-key": api_key,
        "Content-Type": "application/json",
    }
    
    # Send the request to initialize the session
    response = requests.post(url, json=streaming_configuration, headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Failed to initialize live session: {response.text}")
    
    return response.json()

async def stream_audio_to_gladia(socket: WebSocketClientProtocol, stream_url: str, streaming_configuration: StreamingConfiguration) -> None:
    """
    Launch FFmpeg to stream audio from the HLS stream to Gladia via WebSocket.
    
    Args:
        socket: WebSocket connection to Gladia
        stream_url: URL of the HLS stream to process
        streaming_configuration: Configuration for the streaming session
    """
    proc = None
    try:
        cmd = [
            "ffmpeg", "-re",
            "-i", stream_url,
            "-ar", str(streaming_configuration["sample_rate"]),
            "-ac", str(streaming_configuration["channels"]),
            "-f", "wav",
            "-bufsize", "16K",
            "pipe:1",
        ]
        print("Starting FFmpeg process for audio streaming to Gladia")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**6)
        
        # Calculate chunk size based on configuration (100ms chunks)
        chunk_size = int(
            streaming_configuration["sample_rate"]
            * (streaming_configuration["bit_depth"] / 8)
            * streaming_configuration["channels"]
            * 0.1
        )
        
        # Stream audio data to Gladia
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            try:
                await socket.send(chunk)
                await asyncio.sleep(0.1)
            except ConnectionClosedOK:
                print("Gladia WebSocket connection closed")
                break
            except Exception as e:
                print(f"Error sending audio data: {e}")
                break
                
        print("Finished sending audio data")
        try:
            await socket.send(json.dumps({"type": "stop_recording"}))
        except Exception:
            pass
    except Exception as e:
        print(f"Error in audio streaming: {e}")
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except:
                try:
                    proc.kill()
                except:
                    pass

async def process_transcription_messages(socket: WebSocketClientProtocol, caption_update_callback: Callable = None) -> None:
    """
    Process transcription messages from Gladia.
    For each final transcript, convert it into a caption cue.
    
    Args:
        socket: WebSocket connection to Gladia
        caption_update_callback: Optional callback function to call when captions are updated
    """
    global caption_cues
    
    async for message in socket:
        content = json.loads(message)
        
        if content["type"] == "transcript" and content["data"]["is_final"]:
            utterance = content["data"]["utterance"]
            start = utterance["start"]
            end = utterance["end"]
            text = utterance["text"].strip()
            
            print(f"Caption Cue: {format_vtt_duration(start)} --> {format_vtt_duration(end)} | {text}")
            caption_cues.append((start, end, text))
            
            # Call the callback function if provided
            if caption_update_callback:
                caption_update_callback()
                
        if content["type"] == "post_final_transcript":
            print("\n################ End of session ################\n")
            print(json.dumps(content, indent=2, ensure_ascii=False))

def get_caption_cues() -> List[Tuple[float, float, str]]:
    """
    Get the current list of caption cues.
    
    Returns:
        List of caption cues as (start_time, end_time, text) tuples
    """
    global caption_cues
    return caption_cues

def format_vtt_duration(seconds: float) -> str:
    """
    Format seconds into WebVTT timestamp format: HH:MM:SS.mmm
    
    Args:
        seconds: Time in seconds
        
    Returns:
        WebVTT formatted timestamp
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds_remainder = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds_remainder:06.3f}"

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

def transliterate_russian(text: str) -> str:
    """
    Simple transliteration of Russian characters to Latin
    for better compatibility with EIA-608
    
    Args:
        text: Text to transliterate
        
    Returns:
        Transliterated text
    """
    # This is a very basic transliteration
    russian_to_latin = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch',
        'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya'
    }
    
    result = ""
    for char in text:
        result += russian_to_latin.get(char, char)
    
    return result 