#!/usr/bin/env python3
"""
Transcription & Translation Service for rainscribe

This service reads audio data from a shared pipe, streams it to the Gladia API
for real-time transcription with word-level timestamps, and obtains translations
in specified target languages (English and Dutch).
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
TRANSLATION_LANGUAGES = os.getenv("TRANSLATION_LANGUAGES", "en,nl").split(",")
SHARED_VOLUME_PATH = os.getenv("SHARED_VOLUME_PATH", "/shared-data")
AUDIO_PIPE_PATH = f"{SHARED_VOLUME_PATH}/audio_stream"
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
BIT_DEPTH = int(os.getenv("AUDIO_BIT_DEPTH", "16"))
CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))

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
            },
            "translation": True,
            "translation_config": {
                "target_languages": TRANSLATION_LANGUAGES
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


async def write_transcription_data(data, transcript_type="transcript"):
    """
    Write transcription data to files in the shared volume.
    """
    os.makedirs(f"{SHARED_VOLUME_PATH}/{transcript_type}", exist_ok=True)
    
    if transcript_type == "transcript":
        # For Russian transcription
        if data.get("type") == "transcript" and data.get("data", {}).get("is_final"):
            utterance = data["data"]["utterance"]
            filename = f"{SHARED_VOLUME_PATH}/transcript/ru_transcript_{int(utterance['start'] * 1000)}.json"
            
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(utterance, f, ensure_ascii=False, indent=2)
                
            logger.info(f"Saved transcription: {utterance['text'][:50]}...")
    
    elif transcript_type == "translation":
        # For translations (en, nl)
        if data.get("type") == "translation":
            translation = data["data"]["translation"]
            language = translation["language"]
            filename = f"{SHARED_VOLUME_PATH}/transcript/{language}_translation_{int(translation['start'] * 1000)}.json"
            
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(translation, f, ensure_ascii=False, indent=2)
                
            logger.info(f"Saved {language} translation: {translation['text'][:50]}...")


async def process_websocket_messages(websocket):
    """Process messages received from the WebSocket."""
    async for message in websocket:
        try:
            data = json.loads(message)
            
            # Process transcriptions
            if data["type"] == "transcript" and data["data"]["is_final"]:
                await write_transcription_data(data, "transcript")
                
                # Debug output of transcript
                utterance = data["data"]["utterance"]
                start_time = utterance["start"]
                end_time = utterance["end"]
                text = utterance["text"]
                logger.debug(f"{start_time:.2f} --> {end_time:.2f} | {text}")
            
            # Process translations
            elif data["type"] == "translation":
                await write_transcription_data(data, "translation")
                
                # Debug output of translation
                translation = data["data"]["translation"]
                language = translation["language"]
                text = translation["text"]
                logger.debug(f"Translation ({language}): {text}")
            
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
    """Main function for the Transcription & Translation Service."""
    logger.info("Starting Transcription & Translation Service")
    
    # Ensure shared volume and audio pipe exists
    os.makedirs(SHARED_VOLUME_PATH, exist_ok=True)
    os.makedirs(f"{SHARED_VOLUME_PATH}/transcript", exist_ok=True)
    
    if not os.path.exists(AUDIO_PIPE_PATH):
        logger.info(f"Creating audio pipe at {AUDIO_PIPE_PATH}")
        os.mkfifo(AUDIO_PIPE_PATH)
    
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
            message_task = asyncio.create_task(process_websocket_messages(websocket))
            
            # Wait for both tasks to complete
            try:
                await asyncio.gather(audio_task, message_task)
            except asyncio.CancelledError:
                logger.info("Tasks canceled, stopping recording")
                await stop_recording(websocket)
                raise
            except Exception as e:
                logger.error(f"Error during processing: {e}")
                await stop_recording(websocket)
                
    except Exception as e:
        logger.error(f"Critical error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service interrupted, shutting down")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        sys.exit(1) 