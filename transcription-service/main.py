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
import aiohttp
import websockets
from dotenv import load_dotenv
from typing import Dict, List, Optional, TypedDict, Literal
import time  # Added for timestamp reference

# Import shared modules
from shared.reference_clock import get_global_clock, get_time, get_formatted_time
from shared.offset_calculator import get_global_calculator, add_measurement, get_current_offset
from shared.logging_config import configure_logging
from shared.monitoring import get_metrics_manager

# Configure logging
logger = configure_logging("transcription-service")

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

# Get metrics manager
metrics_manager = get_metrics_manager()
sync_metrics = metrics_manager.sync

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

    try:
        # Initialize session with Gladia API
        start_time = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GLADIA_API_URL}/v2/live",
                headers={"X-Gladia-Key": GLADIA_API_KEY},
                json=config,
                timeout=15,
            ) as response:
                if response.status != 201:
                    error_text = await response.text()
                    sync_metrics.record_error("api_initialization_error")
                    raise ValueError(f"API Error: {response.status}: {error_text}")
                
                response_data = await response.json()
                
                # Record API connection time
                api_init_time = time.time() - start_time
                sync_metrics.record_processing_time(api_init_time, "api_initialization")
                
                return response_data
    except Exception as e:
        logger.error(f"Failed to initialize API session: {e}")
        sync_metrics.record_error("api_connection_error")
        raise


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
            chunks_sent = 0
            bytes_sent = 0
            
            while True:
                # Read a chunk of audio data
                chunk = audio_pipe.read(chunk_size)
                if not chunk:
                    logger.warning("Audio pipe returned empty data, waiting...")
                    await asyncio.sleep(0.5)
                    continue
                
                # Send the chunk to the WebSocket
                await websocket.send(chunk)
                
                # Track metrics
                chunks_sent += 1
                bytes_sent += len(chunk)
                if chunks_sent % 50 == 0:  # Update metrics every ~5 seconds
                    sync_metrics.metrics.add_metric("audio_chunks_sent", chunks_sent)
                    sync_metrics.metrics.add_metric("audio_bytes_sent", bytes_sent)
                
                # Small delay to prevent overwhelming the socket
                await asyncio.sleep(0.1)
    
    except Exception as e:
        logger.error(f"Error streaming audio: {e}")
        sync_metrics.record_error("audio_streaming_error")
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
        current_time = get_time()
        pipeline_latency = current_time - reference_start_time - utterance["end"]
        
        # Add the measurement to the global offset calculator
        add_measurement(pipeline_latency)
        
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
            
        # Record metrics
        sync_metrics.record_latency(pipeline_latency, "pipeline")
        sync_metrics.record_processing_time(utterance["end"] - utterance["start"], "utterance_duration")
        
        logger.info(f"Saved transcription: {utterance['text'][:50]}... (latency: {pipeline_latency:.2f}s)")


async def process_websocket_messages(websocket, reference_start_time):
    """Process messages received from the WebSocket."""
    messages_processed = 0
    transcripts_received = 0
    
    async for message in websocket:
        try:
            messages_processed += 1
            data = json.loads(message)
            
            # Process transcriptions
            if data["type"] == "transcript" and data["data"]["is_final"]:
                transcripts_received += 1
                await write_transcription_data(data, reference_start_time)
                
                # Track metrics
                if transcripts_received % 10 == 0:
                    sync_metrics.metrics.add_metric("transcripts_received", transcripts_received)
                    sync_metrics.metrics.add_metric("messages_processed", messages_processed)
                
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
            sync_metrics.record_error("json_decode_error")
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            sync_metrics.record_error("message_processing_error")


async def stop_recording(websocket):
    """Send a stop recording signal to the WebSocket."""
    try:
        await websocket.send(json.dumps({"type": "stop_recording"}))
        logger.info("Sent stop_recording signal")
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")
        sync_metrics.record_error("stop_recording_error")


async def health_check():
    """Periodically check system health and update metrics."""
    while True:
        try:
            # Check the offset calculator status
            offset_stats = get_global_calculator().get_stats()
            for key, value in offset_stats.items():
                if isinstance(value, (int, float)):
                    sync_metrics.metrics.add_metric(f"offset_calculator.{key}", value)
            
            # Check the reference clock status
            clock_status = get_global_clock().get_status()
            for key, value in clock_status.items():
                if isinstance(value, (int, float)) and key not in ["current_system_time", "current_reference_time"]:
                    sync_metrics.metrics.add_metric(f"reference_clock.{key}", value)
            
            # Report service as healthy
            sync_metrics.record_health_check("transcription_service", True)
            
        except Exception as e:
            logger.error(f"Health check error: {e}")
            sync_metrics.record_health_check("transcription_service", False)
            sync_metrics.record_error("health_check_error")
        
        # Run health check every 30 seconds
        await asyncio.sleep(30)


async def main():
    """Main function for the Transcription Service."""
    logger.info("Starting Transcription Service")
    
    # Ensure shared volume and audio pipe exists
    os.makedirs(SHARED_VOLUME_PATH, exist_ok=True)
    os.makedirs(f"{SHARED_VOLUME_PATH}/transcript", exist_ok=True)
    
    if not os.path.exists(AUDIO_PIPE_PATH):
        logger.info(f"Creating audio pipe at {AUDIO_PIPE_PATH}")
        os.mkfifo(AUDIO_PIPE_PATH)
    
    # Start the metrics manager
    metrics_manager.start_auto_save()
    
    # Initialize the global reference clock
    reference_clock = get_global_clock()
    reference_clock.sync_once()  # Synchronize with NTP servers
    reference_start_time = reference_clock.get_time()
    
    logger.info(f"Reference clock initialized: {get_formatted_time()}")
    
    try:
        logger.info("Initializing Gladia API session")
        response = await init_live_session()
        session_id = response["id"]
        websocket_url = response["url"]
        logger.info(f"Session initialized: {session_id}")
        
        # Connect to the WebSocket and start processing
        async with websockets.connect(websocket_url) as websocket:
            logger.info("Connected to Gladia WebSocket")
            
            # Create tasks for streaming audio, processing messages, and health checks
            audio_task = asyncio.create_task(stream_audio_to_websocket(websocket, AUDIO_PIPE_PATH))
            message_task = asyncio.create_task(process_websocket_messages(websocket, reference_start_time))
            health_task = asyncio.create_task(health_check())
            
            # Wait for tasks to complete
            await asyncio.gather(audio_task, message_task, health_task)
            
            # Send stop recording signal before closing
            await stop_recording(websocket)
        
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        sync_metrics.record_error("service_error")
        raise

if __name__ == "__main__":
    asyncio.run(main()) 