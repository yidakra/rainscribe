#!/usr/bin/env python3
"""
Transcription Service for rainscribe

This service reads audio data from a shared pipe, streams it to the Gladia API
for real-time transcription with word-level timestamps in Russian.
"""

import os
import sys
import json
import asyncio
import logging
import aiohttp
import websockets
from dotenv import load_dotenv
from typing import Dict, List, Optional, TypedDict, Literal
import time  # Added for timestamp reference

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("transcription-service")

# Load environment variables
load_dotenv()

# Configuration
GLADIA_API_KEY = os.getenv("GLADIA_API_KEY")
GLADIA_API_URL = os.getenv("GLADIA_API_URL", "https://api.gladia.io")
TRANSCRIPTION_LANGUAGE = os.getenv("TRANSCRIPTION_LANGUAGE", "ru")
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
AUDIO_PIPE_PATH = f"{SHARED_VOLUME_PATH}/audio_stream"
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
BIT_DEPTH = int(os.getenv("AUDIO_BIT_DEPTH", "16"))
CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
REFERENCE_CLOCK_FILE = f"{SHARED_VOLUME_PATH}/reference_clock.json"

# Type definitions
class LanguageConfiguration(TypedDict):
    languages: List[str]
    code_switching: bool


class StreamingConfiguration(TypedDict):
    encoding: Literal["wav/pcm", "wav/alaw", "wav/ulaw"]
    bit_depth: Literal[8, 16, 24, 32]
    sample_rate: Literal[8_000, 16_000, 32_000, 44_100, 48_000]
    channels: int
    language_config: LanguageConfiguration
    realtime_processing: Dict


class InitiateResponse(TypedDict):
    id: str
    url: str


# Custom vocabulary for better recognition of Russian names and terms
CUSTOM_VOCABULARY = [
    # Opposition Figures
    "Навальный", "Яшин", "Соболь", "Ходорковский", "Чичваркин", "Касьянов", "Пономарев", "Каспаров", "Волков",
    # Political Leaders
    "Путин", "Медведев", "Шойгу", "Лавров", "Зеленский", "Байден", "Эрдоган", "Лукашенко", "Кадыров", "Греф"
]


async def init_live_session() -> InitiateResponse:
    """Initialize a live transcription session with Gladia API."""
    if not GLADIA_API_KEY:
        raise ValueError("GLADIA_API_KEY is not set.")

    # Prepare the streaming configuration
    config: StreamingConfiguration = {
        "encoding": "wav/pcm",
        "sample_rate": SAMPLE_RATE,
        "bit_depth": BIT_DEPTH,
        "channels": CHANNELS,
        "language_config": {
            "languages": [TRANSCRIPTION_LANGUAGE],
            "code_switching": False,
        },
        "realtime_processing": {
            "words_accurate_timestamps": True,
            "custom_vocabulary": True,
            "custom_vocabulary_config": {
                "vocabulary": CUSTOM_VOCABULARY
            }
        }
    }

    # Initialize session with Gladia API
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GLADIA_API_URL}/v2/live",
            headers={"X-Gladia-Key": GLADIA_API_KEY},
            json=config,
            timeout=15,
        ) as response:
            if response.status != 201:
                error_text = await response.text()
                raise ValueError(f"API Error: {response.status}: {error_text}")
            
            return await response.json()


async def initialize_reference_clock():
    """Initialize a reference clock for synchronization."""
    reference_time = {
        "start_time": time.time(),
        "creation_timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    os.makedirs(os.path.dirname(REFERENCE_CLOCK_FILE), exist_ok=True)
    with open(REFERENCE_CLOCK_FILE, "w") as f:
        json.dump(reference_time, f)
    
    logger.info(f"Reference clock initialized: {reference_time}")
    return reference_time["start_time"]


async def stream_audio_to_websocket(websocket, pipe_path):
    """Stream audio data from the named pipe to the WebSocket."""
    try:
        logger.info(f"Opening audio pipe at {pipe_path}")
        
        # Calculate chunk size (0.1 second of audio data)
        chunk_size = int(
            SAMPLE_RATE
            * (BIT_DEPTH / 8)
            * CHANNELS
            * 0.1
        )
        
        # Open the pipe for reading
        with open(pipe_path, "rb") as audio_pipe:
            logger.info("Started streaming audio to Gladia API")
            
            while True:
                # Read a chunk of audio data
                chunk = audio_pipe.read(chunk_size)
                if not chunk:
                    logger.warning("Audio pipe returned empty data, waiting...")
                    await asyncio.sleep(0.5)
                    continue
                
                # Send the chunk to the WebSocket
                await websocket.send(chunk)
                
                # Small delay to prevent overwhelming the socket
                await asyncio.sleep(0.1)
    
    except Exception as e:
        logger.error(f"Error streaming audio: {e}")
        raise


async def write_transcription_data(data, reference_start_time):
    """
    Write transcription data to files in the shared volume.
    Adjusts timestamps based on the reference clock.
    """
    os.makedirs(f"{SHARED_VOLUME_PATH}/transcript", exist_ok=True)
    
    # For Russian transcription
    if data.get("type") == "transcript" and data.get("data", {}).get("is_final"):
        utterance = data["data"]["utterance"]
        
        # Adjust timestamps based on reference clock
        current_time = time.time()
        pipeline_latency = current_time - reference_start_time - utterance["end"]
        
        # Create a copy of the utterance with adjusted timestamps
        adjusted_utterance = utterance.copy()
        
        # Add latency information for debugging and tuning
        adjusted_utterance["original_start"] = utterance["start"]
        adjusted_utterance["original_end"] = utterance["end"]
        adjusted_utterance["reference_time"] = reference_start_time
        adjusted_utterance["processing_time"] = current_time
        adjusted_utterance["measured_latency"] = pipeline_latency
        
        # Adjust word timestamps if they exist
        if "words" in adjusted_utterance and adjusted_utterance["words"]:
            for word in adjusted_utterance["words"]:
                word["original_start"] = word["start"]
                word["original_end"] = word["end"]
        
        filename = f"{SHARED_VOLUME_PATH}/transcript/ru_transcript_{int(utterance['start'] * 1000)}.json"
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(adjusted_utterance, f, ensure_ascii=False, indent=2)
            
        logger.info(f"Saved transcription: {utterance['text'][:50]}... (latency: {pipeline_latency:.2f}s)")


async def process_websocket_messages(websocket, reference_start_time):
    """Process messages received from the WebSocket."""
    async for message in websocket:
        try:
            data = json.loads(message)
            
            # Process transcriptions
            if data["type"] == "transcript" and data["data"]["is_final"]:
                await write_transcription_data(data, reference_start_time)
                
                # Debug output of transcript
                utterance = data["data"]["utterance"]
                start_time = utterance["start"]
                end_time = utterance["end"]
                text = utterance["text"]
                logger.debug(f"{start_time:.2f} --> {end_time:.2f} | {text}")
            
            # Process final transcript
            elif data["type"] == "post_final_transcript":
                logger.info("Received final transcript")
                
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON message")
        except Exception as e:
            logger.error(f"Error processing message: {e}")


async def stop_recording(websocket):
    """Send a stop recording signal to the WebSocket."""
    try:
        await websocket.send(json.dumps({"type": "stop_recording"}))
        logger.info("Sent stop_recording signal")
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")


async def main():
    """Main function for the Transcription Service."""
    logger.info("Starting Transcription Service")
    
    # Ensure shared volume and audio pipe exists
    os.makedirs(SHARED_VOLUME_PATH, exist_ok=True)
    os.makedirs(f"{SHARED_VOLUME_PATH}/transcript", exist_ok=True)
    
    if not os.path.exists(AUDIO_PIPE_PATH):
        logger.info(f"Creating audio pipe at {AUDIO_PIPE_PATH}")
        os.mkfifo(AUDIO_PIPE_PATH)
    
    # Initialize reference clock for synchronization
    reference_start_time = await initialize_reference_clock()
    
    # Initialize session with Gladia API
    try:
        logger.info("Initializing Gladia API session")
        response = await init_live_session()
        session_id = response["id"]
        websocket_url = response["url"]
        logger.info(f"Session initialized: {session_id}")
        
        # Connect to the WebSocket and start processing
        async with websockets.connect(websocket_url) as websocket:
            logger.info("Connected to Gladia WebSocket")
            
            # Create tasks for streaming audio and processing messages
            audio_task = asyncio.create_task(stream_audio_to_websocket(websocket, AUDIO_PIPE_PATH))
            message_task = asyncio.create_task(process_websocket_messages(websocket, reference_start_time))
            
            # Wait for both tasks to complete
            await asyncio.gather(audio_task, message_task)
            
            # Send stop recording signal before closing
            await stop_recording(websocket)
        
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main()) 