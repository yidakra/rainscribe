# Rainscribe

Live transcription with embedded caption track for HLS streaming.

## Features

- Initializes a live transcription session with Gladia API
- Streams audio from an HLS URL (via FFmpeg) to Gladia's WebSocket endpoint
- Receives transcription messages continuously and appends each final transcript as a WebVTT cue
- Uses FFmpeg to create HLS output with separate audio and video streams
- Generates a master playlist that includes the audio/video streams and external subtitles tracks
- Serves the HLS stream, master playlist, segments, and live captions via a FastAPI server
- Supports multiple languages (Russian + English and Dutch translations)
- Real-time caption updates via WebSocket
- Containerized with Docker for easy deployment

## Prerequisites

- Docker and Docker Compose installed on your system
- Gladia API key (sign up at [https://app.gladia.io](https://app.gladia.io))

## Quick Start

1. Clone this repository:
   ```bash
   git clone https://github.com/yidakra/rainscribe.git
   cd rainscribe
   ```

2. Create a `.env` file with your Gladia API key:
   ```bash
   echo "GLADIA_API_KEY=your_api_key_here" > .env
   ```

3. Optionally, specify a custom HLS stream URL:
   ```bash
   echo "STREAM_URL=https://your-hls-stream-url.m3u8" >> .env
   ```

4. Start the container:
   ```bash
   docker-compose up
   ```

5. Open your browser and navigate to:
   ```
   http://localhost:8080/
   ```

## Manual Setup (without Docker)

If you prefer to run without Docker:

1. Install Python 3.11
2. Install FFmpeg
3. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the script:
   ```bash
   python rainscribe.py YOUR_GLADIA_API_KEY
   ```

## Configuration

You can customize the behavior through environment variables:

- `GLADIA_API_KEY`: Your Gladia API key (required)
- `STREAM_URL`: URL of the HLS stream to transcribe (default: https://wl.tvrain.tv/transcode/ses_1080p/playlist.m3u8)
- `HTTP_PORT`: Port for the HTTP server (default: 8080)
- `MIN_CUES`: Minimum number of captions to buffer before starting (default: 2)
- `OUTPUT_DIR`: Directory for output files (default: "output")
- `DEBUG_MESSAGES`: Enable detailed message logging (default: false)

## How It Works

1. The script initializes a Gladia API session for live transcription
2. Audio from the HLS stream is extracted using FFmpeg and sent to Gladia
3. Transcripts and translations are received via WebSocket and broadcast to connected clients
4. FFmpeg creates separate HLS streams for audio and video
5. A custom master playlist includes references to the audio/video streams
6. A web server provides access to the HLS stream with captions
7. Captions are updated in real-time via WebSocket connections

## Advanced Usage

### Custom Vocabulary

You can customize the vocabulary to improve transcription of domain-specific terms:

```python
STREAMING_CONFIGURATION = {
    # ...
    "realtime_processing": {
        "custom_vocabulary": True,
        "custom_vocabulary_config": {
            "vocabulary": ["Your", "Custom", "Terms"]
        },
        # ...
    }
}
```

### Additional Languages

To add more translation languages, modify the `target_languages` list:

```python
STREAMING_CONFIGURATION = {
    # ...
    "realtime_processing": {
        # ...
        "translation": True,
        "translation_config": {
            "target_languages": ["en", "fr", "de", "es"]  # Add more languages
        }
    }
}
```

## Troubleshooting

- **No captions appear**: Check the console for error messages. Make sure the Gladia API key is valid and WebSocket connection is established.
- **Stream doesn't play**: Verify that the HLS source URL is accessible and that FFmpeg is properly creating the output streams.
- **Multiple captions showing**: Only one caption track should be active at a time. Use the language buttons to switch between tracks.
- **Container fails to start**: Ensure all required ports are available and the environment variables are set correctly.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- [Gladia API](https://gladia.io/) for the transcription service
- [FFmpeg](https://ffmpeg.org/) for media processing
- [FastAPI](https://fastapi.tiangolo.com/) for the web server
- [HLS.js](https://github.com/video-dev/hls.js/) for HLS playback