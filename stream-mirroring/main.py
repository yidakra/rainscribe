#!/usr/bin/env python3
"""
Stream Mirroring Service for rainscribe

This service mirrors the original HLS stream and merges it with WebVTT subtitles
to create a new HLS stream with embedded captions in multiple languages.
"""

import os
import sys
import time
import logging
import asyncio
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("stream-mirroring")

# Load environment variables
load_dotenv()

# Configuration
HLS_STREAM_URL = os.getenv("HLS_STREAM_URL")
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
WEBVTT_DIR = f"{SHARED_VOLUME_PATH}/webvtt"
OUTPUT_HLS_DIR = f"{SHARED_VOLUME_PATH}/hls"
TRANSCRIPTION_LANGUAGE = os.getenv("TRANSCRIPTION_LANGUAGE", "ru")
TRANSLATION_LANGUAGES = os.getenv("TRANSLATION_LANGUAGES", "en,nl").split(",")
SUPPORTED_LANGUAGES = [TRANSCRIPTION_LANGUAGE] + TRANSLATION_LANGUAGES
SEGMENT_DURATION = int(os.getenv("WEBVTT_SEGMENT_DURATION", "10"))


async def create_directory_structure():
    """Create the necessary directory structure."""
    os.makedirs(OUTPUT_HLS_DIR, exist_ok=True)
    for language in SUPPORTED_LANGUAGES:
        os.makedirs(f"{OUTPUT_HLS_DIR}/{language}", exist_ok=True)


async def run_ffmpeg_command(cmd):
    """Run an FFmpeg command as a subprocess."""
    logger.info(f"Running FFmpeg command: {' '.join(cmd)}")
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        logger.error(f"FFmpeg error (code {process.returncode}): {stderr.decode()}")
        return False
    
    return True


async def mirror_hls_stream_with_subtitles(language):
    """
    Mirror the original HLS stream and merge it with subtitles for a specific language.
    """
    output_dir = f"{OUTPUT_HLS_DIR}/{language}"
    subtitle_manifest = f"{WEBVTT_DIR}/{language}/playlist.m3u8"
    
    # Check if subtitle manifest exists
    if not os.path.exists(subtitle_manifest):
        logger.warning(f"Subtitle manifest for {language} not found, creating placeholder...")
        os.makedirs(os.path.dirname(subtitle_manifest), exist_ok=True)
        with open(subtitle_manifest, "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:0\n")
    
    # Build FFmpeg command
    cmd = [
        "ffmpeg", "-y",
        "-i", HLS_STREAM_URL,  # Input HLS stream
        "-i", subtitle_manifest,  # Input subtitles
        "-map", "0:v",  # Map video from first input
        "-map", "0:a",  # Map audio from first input
        "-map", "1",    # Map subtitles from second input
        "-c:v", "copy",  # Copy video codec
        "-c:a", "copy",  # Copy audio codec
        "-c:s", "webvtt",  # WebVTT subtitle format
        "-f", "hls",  # HLS output format
        "-hls_time", str(SEGMENT_DURATION),  # Segment duration
        "-hls_list_size", "10",  # Number of segments to keep in the playlist
        "-hls_flags", "independent_segments",  # Each segment can be decoded independently
        "-hls_segment_filename", f"{output_dir}/segment_%05d.ts",  # Segment filename pattern
        f"{output_dir}/playlist.m3u8"  # Output manifest
    ]
    
    # Run FFmpeg
    success = await run_ffmpeg_command(cmd)
    
    if success:
        logger.info(f"Successfully mirrored HLS stream with {language} subtitles")
    else:
        logger.error(f"Failed to mirror HLS stream with {language} subtitles")


async def create_master_playlist():
    """
    Create a master playlist that references all language-specific playlists.
    """
    logger.info("Creating master playlist")
    
    with open(f"{OUTPUT_HLS_DIR}/master.m3u8", "w") as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n")
        
        # Define subtitle group
        f.write(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Russian",DEFAULT=YES,AUTOSELECT=YES,')
        f.write(f'LANGUAGE="ru",URI="/webvtt/ru/playlist.m3u8"\n')
        
        for lang in TRANSLATION_LANGUAGES:
            lang_name = {"en": "English", "nl": "Dutch"}.get(lang, lang.upper())
            f.write(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{lang_name}",DEFAULT=NO,AUTOSELECT=YES,')
            f.write(f'LANGUAGE="{lang}",URI="/webvtt/{lang}/playlist.m3u8"\n')
        
        # Add video variant with subtitles
        f.write(f'#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,SUBTITLES="subs"\n')
        f.write(f'{TRANSCRIPTION_LANGUAGE}/playlist.m3u8\n')
    
    logger.info("Master playlist created")


async def monitor_subtitle_changes():
    """
    Monitor the WebVTT directory for changes to subtitle files and
    update the HLS stream accordingly.
    """
    # Track last modification times of subtitle manifests
    last_modified = {lang: 0 for lang in SUPPORTED_LANGUAGES}
    
    while True:
        try:
            for language in SUPPORTED_LANGUAGES:
                subtitle_manifest = f"{WEBVTT_DIR}/{language}/playlist.m3u8"
                
                # Check if manifest exists and has been modified
                if os.path.exists(subtitle_manifest):
                    mtime = os.path.getmtime(subtitle_manifest)
                    
                    if mtime > last_modified[language]:
                        logger.info(f"Detected changes in {language} subtitles, updating HLS stream")
                        last_modified[language] = mtime
                        await mirror_hls_stream_with_subtitles(language)
                        await create_master_playlist()
            
            # Sleep before checking again
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Error monitoring subtitle changes: {e}")
            await asyncio.sleep(10)


async def main():
    """Main function for the Stream Mirroring Service."""
    logger.info("Starting Stream Mirroring Service")
    
    # Create directory structure
    await create_directory_structure()
    
    # Initial mirroring for all languages
    for language in SUPPORTED_LANGUAGES:
        await mirror_hls_stream_with_subtitles(language)
    
    # Create master playlist
    await create_master_playlist()
    
    # Monitor for subtitle changes
    await monitor_subtitle_changes()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service interrupted, shutting down")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        sys.exit(1) 