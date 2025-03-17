#!/usr/bin/env python3
"""
Media Module for RainScribe

This module handles FFmpeg operations, caption generation,
and HTTP streaming functionality.
"""

import subprocess
import os
import time
import threading
import shutil
import re
from typing import List, Tuple, Optional, Dict, Any
from http.server import SimpleHTTPRequestHandler, HTTPServer

# Import functions from transcription module
from transcription import transliterate_russian, format_vtt_duration, get_caption_cues

# Directory structure for all generated files
OUTPUT_BASE_DIR = "rainscribe_output"  # Base directory for all output files
OUTPUT_DIR = f"{OUTPUT_BASE_DIR}/segments"  # Directory for HLS segments
CAPTIONS_DIR = f"{OUTPUT_BASE_DIR}/captions"  # Directory for caption files
TEMP_DIR = f"{OUTPUT_BASE_DIR}/temp"  # Directory for temporary files

# Filenames for captions and HLS output
CAPTIONS_VTT = f"{CAPTIONS_DIR}/captions.vtt"
CAPTIONS_SCC = f"{CAPTIONS_DIR}/captions.scc"  # Scenarist Closed Caption format (for EIA-608)
OUTPUT_PLAYLIST = f"{OUTPUT_BASE_DIR}/playlist.m3u8"  # Main playlist in the output directory

# HTTP server configuration
HTTP_PORT = 8080  # Port for HTTP server

# Global process handle for FFmpeg muxing
ffmpeg_mux_process = None

# Flag to track if the initial connection has been made
initial_connection_made = False

def init_directories() -> None:
    """
    Create all required directories for the application.
    This ensures that the output files are organized properly.
    """
    directories = [OUTPUT_BASE_DIR, OUTPUT_DIR, CAPTIONS_DIR, TEMP_DIR]
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        print(f"Ensured directory exists: {directory}")

def cleanup_old_files() -> None:
    """
    Clean up old files from previous runs to avoid conflicts.
    This prevents accumulation of TS segments in the output directory.
    """
    # Clean up old TS segments from output directory
    if os.path.exists(OUTPUT_DIR):
        for file in os.listdir(OUTPUT_DIR):
            if file.endswith('.ts'):
                try:
                    os.remove(os.path.join(OUTPUT_DIR, file))
                except Exception as e:
                    print(f"Error removing old TS file {file}: {e}")
    
    # Clean up old TS segments from the root directory (from previous runs)
    for file in os.listdir('.'):
        if file.endswith('.ts'):
            try:
                os.remove(file)
                print(f"Removed TS file from root directory: {file}")
            except Exception as e:
                print(f"Error removing old TS file from root: {file}, error: {e}")
    
    # Clean up old playlist files
    try:
        if os.path.exists(OUTPUT_PLAYLIST):
            os.remove(OUTPUT_PLAYLIST)
        
        # Also check for output.m3u8 in the root directory
        if os.path.exists("output.m3u8"):
            os.remove("output.m3u8")
            print("Removed old playlist from root directory")
    except Exception as e:
        print(f"Error removing old playlist file: {e}")
    
    # Clean up old caption files in the root
    for name in ["captions.vtt", "captions.scc"]:
        if os.path.exists(name):
            try:
                os.remove(name)
                print(f"Removed old caption file from root: {name}")
            except Exception as e:
                print(f"Error removing old caption file from root: {name}, error: {e}")
    
    print("Cleaned up old files")

def write_vtt_header() -> None:
    """Initialize the WebVTT file."""
    with open(CAPTIONS_VTT, "w", encoding="utf-8") as f:
        f.write("WEBVTT\r\n\r\n")

def update_vtt_file() -> None:
    """
    Create a WebVTT file from the caption cues for web compatibility
    """
    DEFAULT_DURATION = 5.0
    caption_cues = get_caption_cues()
    
    with open(CAPTIONS_VTT, "w", encoding="utf-8") as f:
        # WebVTT header
        f.write("WEBVTT\r\n\r\n")
        
        for i, (start, end, text) in enumerate(caption_cues):
            if i < len(caption_cues) - 1:
                new_end = caption_cues[i+1][0]
            else:
                new_end = start + DEFAULT_DURATION
            
            # Make sure text doesn't contain multiple blank lines (which can cause problems)
            clean_text = text.strip()
            
            # Write VTT entry
            f.write(f"{i+1}\r\n{format_vtt_duration(start)} --> {format_vtt_duration(new_end)}\r\n{clean_text}\r\n\r\n")

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

def create_608_captions() -> bool:
    """
    Create EIA-608 caption data in SCC format.
    This is specifically designed for cable TV compatibility with proper EIA-608 formatting.
    """
    caption_cues = get_caption_cues()
    if not caption_cues:
        return False
        
    print(f"Creating EIA-608 captions from {len(caption_cues)} cues")
    
    # Create SCC file
    with open(CAPTIONS_SCC, "w", encoding="utf-8") as f:
        # SCC header
        f.write("Scenarist_SCC V1.0\n\n")
        
        for i, (start, end, text) in enumerate(caption_cues):
            # For maximum compatibility with FFmpeg SCC readers, use standard non-drop-frame
            # format (with colons). Cable TV systems will handle the conversion.
            start_tc = format_scc_timecode(start, use_drop_frame=False)
            
            # Clean and prepare the text
            # 1. Limit line length (32 chars per line, max 2 lines)
            # 2. Remove unsupported characters
            clean_text = text.strip()
            
            # Transliterate Russian characters to Latin if needed
            # This is a simple solution for compatibility
            clean_text = transliterate_russian(clean_text)
            
            # Split into lines with maximum length of 32 characters
            if len(clean_text) > 32:
                # Try to split at word boundaries
                words = clean_text.split()
                line1 = ""
                line2 = ""
                
                for word in words:
                    if len(line1) + len(word) + 1 <= 32:  # +1 for space
                        if line1:
                            line1 += " "
                        line1 += word
                    elif len(line2) + len(word) + 1 <= 32:  # +1 for space
                        if line2:
                            line2 += " "
                        line2 += word
                    else:
                        # No more room, truncate
                        break
                        
                captions = [line1]
                if line2:
                    captions.append(line2)
            else:
                captions = [clean_text]
            
            # Proper EIA-608 formatting for cable TV standards
            # This follows the CEA-608-E standard

            # Create a list of codes
            # 9420 = RCL (Resume Caption Loading) - standard control code
            # 9429 = display at Row 15 Column 0 (typical caption position)
            control_codes = [
                "94ae 94ae",  # Clear buffer and reset
                "9420",       # Resume Caption Loading
                "9429 9429"   # Position at row 15, preferred caption position
            ]

            # Add the text data with proper control codes
            text_codes = []
            for idx, line in enumerate(captions):
                # For second line, position at row 16
                if idx == 1:
                    text_codes.append("94f0 94f0")  # Position at row 16
                
                # Add the text, properly formatted for SCC
                for char in line:
                    if 32 <= ord(char) <= 127:  # Valid ASCII range
                        hex_val = hex(ord(char))[2:].zfill(2)
                        text_codes.append(hex_val)
            
            # Join all codes for this cue
            all_codes = control_codes + text_codes
            
            # Write the SCC line for this cue
            f.write(f"{start_tc}\t{' '.join(all_codes)}\n")
            
            # Add an erase display command at the end time
            end_tc = format_scc_timecode(end, use_drop_frame=False)
            f.write(f"{end_tc}\t942c 942f\n")  # Clear display
    
    print(f"SCC file created at {CAPTIONS_SCC} with EIA-608 formatting for cable TV")
    return True

def check_ffmpeg_eia608_support() -> str:
    """
    Check if FFmpeg has EIA-608 caption support
    
    Returns:
        "encoder" if encoder support is found,
        "decoder" if only decoder support is found,
        False if no support is found
    """
    try:
        # Check encoders specifically
        encoders = subprocess.check_output(
            ["ffmpeg", "-encoders"], 
            stderr=subprocess.STDOUT, 
            universal_newlines=True
        )
        
        # Check for various caption encoders
        has_encoder = False
        for enc in ["eia_608", "cea_608", "cc_data", "mov_text", "closed_caption"]:
            if enc in encoders:
                print(f"FFmpeg has {enc} encoder support")
                has_encoder = True
        
        # Check decoders too
        decoders = subprocess.check_output(
            ["ffmpeg", "-decoders"], 
            stderr=subprocess.STDOUT, 
            universal_newlines=True
        )
        
        has_decoder = False
        for dec in ["cc_dec", "eia_608", "cea_608"]:
            if dec in decoders:
                print(f"FFmpeg has {dec} decoder support")
                has_decoder = True
            
        if has_encoder:
            return "encoder"
        elif has_decoder:
            return "decoder"
        else:
            print("No direct EIA-608 support found in FFmpeg")
            return False
    except Exception as e:
        print(f"Error checking FFmpeg capabilities: {e}")
        return False

def start_muxing_process(stream_url: str = "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"):
    """
    Start the FFmpeg muxing process that creates an HLS stream with EIA-608/708 captions.
    This is designed to work with most FFmpeg builds while attempting to include
    cable TV-compatible captions.
    """
    global ffmpeg_mux_process

    # Ensure the output directories exist
    init_directories()
    
    # Clean up any old files
    cleanup_old_files()
    
    # Ensure any previous process is terminated properly
    if ffmpeg_mux_process:
        try:
            ffmpeg_mux_process.terminate()
            ffmpeg_mux_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ffmpeg_mux_process.kill()
            ffmpeg_mux_process.wait()
            
    # Update caption files
    update_vtt_file()
    
    # Try to create CEA-608 captions if possible
    try:
        create_608_captions()
    except Exception as e:
        print(f"Error creating 608 captions: {e}")
    
    # Try to detect if we have EIA-608 support
    eia608_support = check_ffmpeg_eia608_support()

    # Try different methods in order of preference for true EIA-608/708 compatibility
    
    # Method 1: Try direct EIA-608 encoder if available
    if eia608_support == "encoder":
        try:
            print("Trying EIA-608 encoder method (best for cable TV)...")
            
            cmd = [
                "ffmpeg", "-re",
                "-i", stream_url,
                "-f", "scc", "-i", CAPTIONS_SCC,
                "-map", "0:v", "-map", "0:a", "-map", "1",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "eia_608",  # Try explicitly with EIA-608 encoder
                "-metadata:s:s:0", "language=rus",
                "-hls_time", "6",
                "-hls_list_size", "10",
                "-hls_flags", "delete_segments+append_list",
                "-hls_segment_type", "mpegts",
                "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
                OUTPUT_PLAYLIST
            ]
            
            print("Starting FFmpeg with EIA-608 encoder:")
            print(" ".join(cmd))
            
            ffmpeg_mux_process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True
            )
            
            # Read output for errors
            error_output = ""
            for i in range(50):
                if ffmpeg_mux_process.poll() is not None:
                    break
                    
                try:
                    line = ffmpeg_mux_process.stderr.readline()
                    if line:
                        error_output += line
                        if "Error" in line or "error" in line or "Unknown" in line:
                            print(f"FFmpeg error: {line.strip()}")
                            raise Exception("FFmpeg error detected")
                except Exception as e:
                    break
                    
                time.sleep(0.1)
                
            # If process is still running, start the output reader thread
            if ffmpeg_mux_process.poll() is None:
                threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()
                return
            else:
                print(f"EIA-608 encoder method failed with exit code {ffmpeg_mux_process.poll()}")
                print(f"Error output: {error_output}")
                raise Exception("EIA-608 encoder method failed")
                
        except Exception as e:
            print(f"Error with EIA-608 encoder method: {e}")
            print("Trying alternative methods...")
            
    # Method 2: Try CEA-608 data format with copy codec (works on some builds)
    try:
        print("Trying CEA-608 data format with copy codec...")
        
        cmd = [
            "ffmpeg", "-re",
            "-i", stream_url,
            "-f", "scc", "-i", CAPTIONS_SCC,
            "-map", "0:v", "-map", "0:a", "-map", "1",
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "copy",  # Just copy the subtitle data (preserves CEA-608 format)
            "-f", "mpegts",  # Force mpegts format to support caption data
            "-metadata:s:s:0", "language=rus",
            "-metadata:s:s:0", "handler_name=EIA-608 Captions",
            "-hls_time", "6",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
            OUTPUT_PLAYLIST
        ]
        
        print("Starting FFmpeg with CEA-608 copy mode:")
        print(" ".join(cmd))
        
        ffmpeg_mux_process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
        )
        
        # Read output for errors
        error_output = ""
        for i in range(50):
            if ffmpeg_mux_process.poll() is not None:
                break
                
            try:
                line = ffmpeg_mux_process.stderr.readline()
                if line:
                    error_output += line
                    if "Error" in line or "error" in line or "Unknown" in line:
                        print(f"FFmpeg error: {line.strip()}")
                        raise Exception("FFmpeg error detected")
            except Exception as e:
                break
                
            time.sleep(0.1)
            
        # If process is still running, start the output reader thread
        if ffmpeg_mux_process.poll() is None:
            threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()
            return
        else:
            print(f"CEA-608 copy method failed with exit code {ffmpeg_mux_process.poll()}")
            print(f"Error output: {error_output}")
            raise Exception("CEA-608 copy method failed")
            
    except Exception as e:
        print(f"Error with CEA-608 copy method: {e}")
        print("Trying next method...")
        
    # Method 3: Try with cc_data encoder (often available)
    try:
        print("Trying cc_data encoder method...")
        
        cmd = [
            "ffmpeg", "-re",
            "-i", stream_url,
            "-f", "scc", "-i", CAPTIONS_SCC,
            "-map", "0:v", "-map", "0:a", "-map", "1",
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "cc_data",  # Use cc_data encoder if available
            "-metadata:s:s:0", "language=rus",
            "-hls_time", "6",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
            OUTPUT_PLAYLIST
        ]
        
        print("Starting FFmpeg with cc_data encoder:")
        print(" ".join(cmd))
        
        ffmpeg_mux_process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
        )
        
        # Read output for errors
        error_output = ""
        for i in range(50):
            if ffmpeg_mux_process.poll() is not None:
                break
                
            try:
                line = ffmpeg_mux_process.stderr.readline()
                if line:
                    error_output += line
                    if "Error" in line or "error" in line or "Unknown" in line:
                        print(f"FFmpeg error: {line.strip()}")
                        raise Exception("FFmpeg error detected")
            except Exception as e:
                break
                
            time.sleep(0.1)
            
        # If process is still running, start the output reader thread
        if ffmpeg_mux_process.poll() is None:
            threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()
            return
        else:
            print(f"cc_data method failed with exit code {ffmpeg_mux_process.poll()}")
            print(f"Error output: {error_output}")
            raise Exception("cc_data method failed")
            
    except Exception as e:
        print(f"Error with cc_data method: {e}")
        print("Trying next method...")
    
    # Method 4: Try using mov_text with EIA-608 compatible flags (widely compatible)
    try:
        print("Trying mov_text with EIA-608 compatibility flags...")
        
        cmd = [
            "ffmpeg", "-re",
            "-i", stream_url,
            "-i", CAPTIONS_VTT,
            "-map", "0:v", "-map", "0:a", "-map", "1",
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "mov_text",  # Try mov_text format (widely available)
            "-metadata:s:s:0", "language=rus",
            "-metadata:s:s:0", "handler_name=CEA-608",  # Mark as CEA-608 
            "-tag:s:0", "c608",  # Attempt to tag as 608 format
            "-hls_time", "6",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
            OUTPUT_PLAYLIST
        ]
        
        print("Starting FFmpeg with mov_text + EIA-608 compatibility flags:")
        print(" ".join(cmd))
        
        ffmpeg_mux_process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
        )
        
        # Read output for errors
        error_output = ""
        for i in range(50):
            if ffmpeg_mux_process.poll() is not None:
                break
                
            try:
                line = ffmpeg_mux_process.stderr.readline()
                if line:
                    error_output += line
                    if "Error" in line or "error" in line or "Unknown" in line:
                        print(f"FFmpeg error: {line.strip()}")
                        raise Exception("FFmpeg error detected")
            except Exception as e:
                break
                
            time.sleep(0.1)
            
        # If process is still running, start the output reader thread
        if ffmpeg_mux_process.poll() is None:
            threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()
            return
        else:
            print(f"mov_text + EIA-608 method failed with exit code {ffmpeg_mux_process.poll()}")
            print(f"Error output: {error_output}")
            raise Exception("mov_text + EIA-608 method failed")
            
    except Exception as e:
        print(f"Error with mov_text + EIA-608 method: {e}")
        print("Trying last EIA-608 attempt...")
    
    # Method 5: Try a specific method to inject EIA-608 captions into video stream
    try:
        print("Trying video filter to inject 608 captions (last EIA-608 attempt)...")
        
        # This attempts to use ffmpeg's video filter to inject 608 data rather than burning in
        cmd = [
            "ffmpeg", "-re",
            "-i", stream_url,
            "-f", "scc", "-i", CAPTIONS_SCC,
            "-filter_complex", "[0:v][1:s]ccaption=EIA-608[outv]",  # Try to use ccaption filter if available
            "-map", "[outv]", 
            "-map", "0:a",
            "-c:v", "copy",  # Try to copy video (may fail and fallback to encoding)
            "-c:a", "copy",
            "-hls_time", "6",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
            OUTPUT_PLAYLIST
        ]
        
        print("Starting FFmpeg with ccaption filter:")
        print(" ".join(cmd))
        
        ffmpeg_mux_process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
        )
        
        # Read output for errors
        error_output = ""
        for i in range(50):
            if ffmpeg_mux_process.poll() is not None:
                break
                
            try:
                line = ffmpeg_mux_process.stderr.readline()
                if line:
                    error_output += line
                    # For this method, we only fail on specific errors, not all unknown items
                    if "Error initializing" in line or "Invalid argument" in line:
                        print(f"FFmpeg error: {line.strip()}")
                        raise Exception("FFmpeg error detected with ccaption filter")
            except Exception as e:
                break
                
            time.sleep(0.1)
            
        # If process is still running, start the output reader thread
        if ffmpeg_mux_process.poll() is None:
            threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()
            return
        else:
            print(f"ccaption filter method failed with exit code {ffmpeg_mux_process.poll()}")
            print(f"Error output: {error_output}")
            raise Exception("ccaption filter method failed")
            
    except Exception as e:
        print(f"Error with ccaption filter method: {e}")
        print("All EIA-608/708 methods failed. Falling back to WebVTT...")
    
    # Final fallback: Use WebVTT captions (web compatible but not for cable TV)
    fallback_to_webvtt(stream_url)

def fallback_to_webvtt(stream_url: str):
    """
    Fall back to WebVTT captions when all EIA-608/708 methods fail.
    Not suitable for cable TV but works for web streaming.
    """
    global ffmpeg_mux_process
    
    print("FALLBACK TO WEBVTT: This is NOT cable TV compatible but will work for web streaming")
    
    cmd = [
        "ffmpeg", "-re",
        "-i", stream_url,
        "-i", CAPTIONS_VTT,
        "-map", "0:v", "-map", "0:a", "-map", "1",
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "webvtt",
        "-metadata:s:s:0", "language=rus",  # Set language metadata
        "-metadata:s:s:0", "title=Russian",  # Add title to help players
        "-metadata:s:s:0", "handler_name=Russian",  # Add handler name
        "-hls_time", "6",
        "-hls_list_size", "10",
        "-hls_flags", "delete_segments+append_list+program_date_time",  # Add program date time for better syncing
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
        OUTPUT_PLAYLIST
    ]
    
    print("Starting FFmpeg muxing process with WebVTT captions (web fallback):")
    print(" ".join(cmd))
    
    try:
        ffmpeg_mux_process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
        )
        
        # Read output for errors
        error_output = ""
        for i in range(50):
            if ffmpeg_mux_process.poll() is not None:
                break
                
            try:
                line = ffmpeg_mux_process.stderr.readline()
                if line:
                    error_output += line
                    if "Error" in line or "error" in line or "Unknown" in line:
                        print(f"FFmpeg error: {line.strip()}")
                        raise Exception("FFmpeg error detected")
            except Exception as e:
                break
                
            time.sleep(0.1)
            
        # If process is still running, start the output reader thread
        if ffmpeg_mux_process.poll() is None:
            threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()
        else:
            print(f"WebVTT method failed with exit code {ffmpeg_mux_process.poll()}")
            print(f"Error output: {error_output}")
            raise Exception("WebVTT method failed")
            
    except Exception as e:
        print(f"Error with WebVTT method: {e}")
        print("All caption methods have failed. Running with no captions as absolute last resort.")
        last_resort_bare_minimum(stream_url)

def last_resort_bare_minimum(stream_url: str):
    """Absolute minimum fallback with no captions at all"""
    global ffmpeg_mux_process
    
    print("Falling back to basic method without captions (absolute last resort)")
    
    cmd = [
        "ffmpeg", "-re",
        "-i", stream_url,
        "-c:v", "copy",
        "-c:a", "copy",
        "-hls_time", "6",
        "-hls_list_size", "10",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", f"{OUTPUT_DIR}/segment_%03d.ts",  # Specify segment filename
        OUTPUT_PLAYLIST
    ]
    
    print("Bare minimum command (no captions):", " ".join(cmd))
    ffmpeg_mux_process = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE,
        bufsize=1,
        universal_newlines=True
    )
    
    # Start background thread to read FFmpeg output
    threading.Thread(target=read_ffmpeg_output, args=(ffmpeg_mux_process,), daemon=True).start()

def read_ffmpeg_output(process):
    """Read and log FFmpeg output in a background thread"""
    for line in process.stderr:
        if "Error" in line or "error" in line or "Invalid" in line:
            print(f"FFmpeg error: {line.strip()}")
        # Uncomment for verbose FFmpeg output
        # elif "info" in line.lower() or "warning" in line.lower():
        #     print(f"FFmpeg info: {line.strip()}")
            
    # When the process ends, this thread will end too
    print("FFmpeg process ended")

def restart_muxing_process(stream_url: str = "https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8"):
    """Restart the FFmpeg muxing process to update captions."""
    global ffmpeg_mux_process
    
    # Check if the process is actually running before attempting to restart
    if ffmpeg_mux_process:
        try:
            # Check if the process is still running
            if ffmpeg_mux_process.poll() is None:
                print("Restarting muxing process to update captions.")
                start_muxing_process(stream_url)
            else:
                # If process has terminated, check the return code
                return_code = ffmpeg_mux_process.poll()
                if return_code != 0:
                    print(f"Previous FFmpeg process exited with error code {return_code}. Restarting...")
                    start_muxing_process(stream_url)
                else:
                    print("Previous FFmpeg process completed successfully. Starting a new one...")
                    start_muxing_process(stream_url)
        except Exception as e:
            print(f"Error checking muxing process status: {e}. Restarting...")
            start_muxing_process(stream_url)
    else:
        print("No previous muxing process. Starting a new one...")
        start_muxing_process(stream_url)

class HLSRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        global initial_connection_made
        
        # Only clear captions on the very first connection to index.html
        if self.path == "/" or self.path == "/index.html":
            if not initial_connection_made:
                print("Initial connection to player interface - stream ready for viewing")
                initial_connection_made = True
        
        # Remap paths to the new directory structure
        if self.path.startswith("/playlist.m3u8"):
            self.path = f"/{OUTPUT_BASE_DIR}{self.path}"
            print(f"Remapped request to: {self.path}")
        
        # Handle segment requests
        elif self.path.endswith('.ts'):
            self.path = f"/{OUTPUT_BASE_DIR}/segments{self.path[self.path.rfind('/'):]}"
            print(f"Remapped segment request to: {self.path}")
        
        # Handle caption file requests
        elif self.path.endswith('.vtt') or self.path.endswith('.scc'):
            self.path = f"/{OUTPUT_BASE_DIR}/captions{self.path[self.path.rfind('/'):]}"
            print(f"Remapped caption file request to: {self.path}")
            
        # Log requests for the m3u8 playlist but don't clear captions
        if self.path.endswith(".m3u8") or self.path.startswith(f"/{OUTPUT_BASE_DIR}/playlist.m3u8"):
            print(f"Player requested {self.path}")
            
        # Determine content type based on file extension
        content_type = None
        if self.path.endswith('.m3u8'):
            content_type = 'application/vnd.apple.mpegurl'
        elif self.path.endswith('.ts'):
            content_type = 'video/mp2t'
        elif self.path.endswith('.vtt'):
            content_type = 'text/vtt'
        elif self.path.endswith('.scc'):
            content_type = 'text/plain'
            
        # Try to send the file
        try:
            f = self.send_head()
            if f:
                try:
                    self.copyfile(f, self.wfile)
                finally:
                    f.close()
        except FileNotFoundError:
            # Handle missing files gracefully
            self.send_error(404, "File not found")
            print(f"404: File not found: {self.path}")

def start_http_server(port=HTTP_PORT):
    """Start HTTP server to serve the player and HLS segments."""
    handler = HLSRequestHandler
    with HTTPServer(("", port), handler) as httpd:
        print(f"Serving HTTP on http://localhost:{port} ...")
        httpd.serve_forever() 