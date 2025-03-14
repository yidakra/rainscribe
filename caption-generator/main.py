#!/usr/bin/env python3
"""
Caption Generator Service for rainscribe

This service monitors the transcript directory for new transcription/translation files,
processes them, and generates WebVTT subtitle files for each language.
"""

import os
import sys
import json
import time
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("caption-generator")

# Load environment variables
load_dotenv()

# Configuration
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
TRANSCRIPT_DIR = f"{SHARED_VOLUME_PATH}/transcript"
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
SUPPORTED_LANGUAGES = ["ru"] + os.getenv("TRANSLATION_LANGUAGES", "en,nl").split(",")
SEGMENT_DURATION = int(os.getenv("WEBVTT_SEGMENT_DURATION", "10"))

def format_timestamp(seconds: float) -> str:
    """Format a timestamp in seconds to WebVTT format (HH:MM:SS.mmm)."""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    milliseconds = int((seconds - int(seconds)) * 1000)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{milliseconds:03d}"

def create_webvtt_header() -> str:
    """Create the WebVTT header."""
    return "WEBVTT\n\n"

def create_webvtt_cue(start: float, end: float, text: str, cue_id: int = None) -> str:
    """Create a WebVTT cue with optional ID."""
    cue = ""
    if cue_id is not None:
        cue += f"{cue_id}\n"
    cue += f"{format_timestamp(start)} --> {format_timestamp(end)}\n"
    cue += f"{text}\n\n"
    return cue

async def process_transcript_file(file_path: str, language: str):
    """Process a transcript/translation file and update the WebVTT file."""
    try:
        # Read the transcript/translation file
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Extract the necessary information
        text = data.get("text", "")
        start_time = data.get("start", 0)
        end_time = data.get("end", start_time + 5)  # Default to 5 seconds if end is not provided
        
        # For word-level timestamps
        words = data.get("words", [])
        
        # Determine which WebVTT segment file this belongs to
        segment_index = int(start_time / SEGMENT_DURATION)
        segment_file = f"{WEBVTT_DIR}/{language}/segment_{segment_index:05d}.vtt"
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(segment_file), exist_ok=True)
        
        # Create or rewrite the WebVTT file
        # We'll rewrite the entire file to ensure proper formatting
        cues = []
        
        # If the file exists, read existing cues
        if os.path.exists(segment_file):
            with open(segment_file, "r", encoding="utf-8") as f:
                content = f.read()
                # Skip header
                if content.startswith("WEBVTT"):
                    parts = content.split("\n\n")
                    # First part is header, skip it
                    for part in parts[1:]:
                        if part.strip():  # Skip empty parts
                            cues.append(part)
        
        # Prepare the new cue
        cue_id = f"cue{int(start_time * 1000)}"  # Use timestamp as cue ID with prefix
        cue_text = create_webvtt_cue(start_time, end_time, text, cue_id).strip()
        
        # Check if we already have this cue (based on timestamps)
        existing_cue_index = -1
        for i, cue in enumerate(cues):
            if f"{format_timestamp(start_time)} --> {format_timestamp(end_time)}" in cue:
                existing_cue_index = i
                break
        
        # Replace or append the cue
        if existing_cue_index >= 0:
            cues[existing_cue_index] = cue_text
        else:
            cues.append(cue_text)
        
        # Write back the file with header and all cues
        with open(segment_file, "w", encoding="utf-8") as f:
            f.write(create_webvtt_header())
            for cue in cues:
                f.write(cue + "\n\n")
        
        logger.info(f"Updated {segment_file} with cue: {text[:50]}...")
        
        # Update the HLS manifest for subtitles
        await update_hls_manifest(language)
        
    except Exception as e:
        logger.error(f"Error processing transcript file {file_path}: {e}")
        logger.exception(e)

async def update_hls_manifest(language: str):
    """Update the HLS manifest for subtitles."""
    try:
        # Path to the manifest
        manifest_path = f"{WEBVTT_DIR}/{language}/playlist.m3u8"
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        
        # Find all segment files
        segment_files = []
        if os.path.exists(f"{WEBVTT_DIR}/{language}"):
            segment_files = sorted([
                f for f in os.listdir(f"{WEBVTT_DIR}/{language}")
                if f.startswith("segment_") and f.endswith(".vtt")
            ])
        
        # Create the manifest file
        with open(manifest_path, "w", encoding="utf-8") as f:
            # Write the header
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n")
            f.write(f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}\n")
            f.write("#EXT-X-MEDIA-SEQUENCE:0\n")
            
            # Write the segments
            for segment_file in segment_files:
                f.write(f"#EXTINF:{SEGMENT_DURATION}.000,\n")
                f.write(f"{segment_file}\n")
        
        logger.info(f"Updated HLS manifest for {language} with {len(segment_files)} segments")
        
    except Exception as e:
        logger.error(f"Error updating HLS manifest for {language}: {e}")
        logger.exception(e)

async def regenerate_all_vtt_files():
    """
    Regenerate all VTT files with proper formatting.
    This helps fix any existing VTT files that might have incorrect formatting.
    """
    logger.info("Regenerating all WebVTT files with proper formatting")
    
    for language in SUPPORTED_LANGUAGES:
        language_dir = f"{WEBVTT_DIR}/{language}"
        if not os.path.exists(language_dir):
            continue
            
        segment_files = [
            f for f in os.listdir(language_dir)
            if f.startswith("segment_") and f.endswith(".vtt")
        ]
        
        for segment_file in segment_files:
            file_path = os.path.join(language_dir, segment_file)
            try:
                # Read existing file
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                # Parse cues
                cues = []
                if content.startswith("WEBVTT"):
                    parts = content.split("\n\n")
                    # Skip header
                    for i, part in enumerate(parts[1:]):
                        if part.strip():
                            lines = part.strip().split("\n")
                            if len(lines) >= 2:
                                # Check if first line is a timestamp
                                if "-->" in lines[0]:
                                    # No cue ID, add one
                                    timestamp = lines[0].split(" --> ")[0]
                                    cue_id = f"cue{i}"
                                    cues.append(f"{cue_id}\n{part}")
                                else:
                                    # Already has a cue ID
                                    cues.append(part)
                
                # Rewrite file with proper formatting
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(create_webvtt_header())
                    for cue in cues:
                        f.write(cue + "\n\n")
                
                logger.info(f"Regenerated {file_path} with {len(cues)} cues")
                
            except Exception as e:
                logger.error(f"Error regenerating {file_path}: {e}")
        
        # Update manifest
        await update_hls_manifest(language)

async def monitor_transcript_directory():
    """Monitor the transcript directory for new files and process them."""
    # Ensure the WebVTT directories exist
    for language in SUPPORTED_LANGUAGES:
        os.makedirs(f"{WEBVTT_DIR}/{language}", exist_ok=True)
    
    # Regenerate all existing VTT files to ensure proper formatting
    await regenerate_all_vtt_files()
    
    # Keep track of processed files
    processed_files = set()
    
    while True:
        try:
            # Get all transcript files
            if not os.path.exists(TRANSCRIPT_DIR):
                logger.warning(f"Transcript directory {TRANSCRIPT_DIR} does not exist, creating...")
                os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
                await asyncio.sleep(1)
                continue
            
            all_files = os.listdir(TRANSCRIPT_DIR)
            new_files = [f for f in all_files if f not in processed_files]
            
            for file_name in new_files:
                file_path = os.path.join(TRANSCRIPT_DIR, file_name)
                
                # Skip directories and non-JSON files
                if os.path.isdir(file_path) or not file_name.endswith(".json"):
                    continue
                
                # Determine the language from the file name
                # Expected format: [lang]_transcript_[timestamp].json or [lang]_translation_[timestamp].json
                parts = file_name.split("_")
                if len(parts) < 2:
                    logger.warning(f"Invalid file name format: {file_name}, skipping...")
                    processed_files.add(file_name)
                    continue
                    
                lang_prefix = parts[0]
                
                if lang_prefix in SUPPORTED_LANGUAGES:
                    await process_transcript_file(file_path, lang_prefix)
                    processed_files.add(file_name)
                    logger.info(f"Processed {file_name}")
                else:
                    logger.warning(f"Unknown language prefix '{lang_prefix}' in file {file_name}, skipping...")
                    processed_files.add(file_name)
            
            # Sleep for a short time before checking again
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error monitoring transcript directory: {e}")
            logger.exception(e)
            await asyncio.sleep(5)  # Sleep longer on error

async def main():
    """Main function for the Caption Generator Service."""
    logger.info("Starting Caption Generator Service")
    
    # Ensure shared volume directory exists
    os.makedirs(SHARED_VOLUME_PATH, exist_ok=True)
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    os.makedirs(WEBVTT_DIR, exist_ok=True)
    
    # Start monitoring the transcript directory
    await monitor_transcript_directory()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service interrupted, shutting down")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        sys.exit(1) 