# Rainscribe

Live transcription with native HLS subtitle integration for HLS streaming.

## Features

- Real-time transcription and translation of live HLS streams using Gladia API's real-time mode
- Native HLS subtitle integration (no WebSocket-based caption overlay)
- Supports multiple languages simultaneously (Russian source + English and Dutch translations)
- 60-second buffered playback for reliable caption synchronization
- Separate audio, video, and subtitle tracks for optimal streaming
- Clean player interface with native caption controls
- Docker containerization for easy deployment
- Configurable logging levels for different types of messages
- Low-latency transcription pipeline for faster caption delivery

## Prerequisites

- Docker and Docker Compose installed on your system
- Gladia API key (sign up at [https://app.gladia.io](https://app.gladia.io))

## Quick Start

1. Clone this repository:
   ```bash
   git clone https://github.com/yidakra/rainscribe.git
   cd rainscribe
   ```

2. Create a `.env` file with your configuration:
   ```bash
   GLADIA_API_KEY=your_api_key_here
   STREAM_URL=https://your-hls-stream-url.m3u8  # Optional
   
   # Logging configuration (optional)
   CAPTIONS_LOG_LEVEL=INFO    # Show caption text
   SYSTEM_LOG_LEVEL=ERROR     # Hide system messages
   TRANSCRIPTION_LOG_LEVEL=ERROR  # Hide transcription details
   ```

3. Start the container:
   ```bash
   docker-compose up --build
   ```

4. Open your browser and navigate to:
   ```
   http://localhost:8080/
   ```

## Configuration

### Environment Variables

#### Required:
- `GLADIA_API_KEY`: Your Gladia API key

#### Optional:
- `STREAM_URL`: URL of the HLS stream to transcribe (default: TV Rain stream)
- `HTTP_PORT`: Port for the HTTP server (default: 8080)
- `SEGMENT_DURATION`: Duration of each HLS segment in seconds (default: 10)
- `WINDOW_SIZE`: Number of segments to keep in the playlist (default: 10)
- `OUTPUT_DIR`: Directory for output files (default: "output")

#### Logging Configuration:
- `CAPTIONS_LOG_LEVEL`: Controls visibility of caption text (default: INFO)
- `SYSTEM_LOG_LEVEL`: Controls system-level messages (default: INFO)
- `TRANSCRIPTION_LOG_LEVEL`: Controls technical transcription details (default: ERROR)

Available log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

### Logging Examples

1. Show only captions:
```bash
CAPTIONS_LOG_LEVEL=INFO SYSTEM_LOG_LEVEL=ERROR TRANSCRIPTION_LOG_LEVEL=ERROR docker-compose up --build
```

2. Show everything (debug mode):
```bash
CAPTIONS_LOG_LEVEL=DEBUG SYSTEM_LOG_LEVEL=DEBUG TRANSCRIPTION_LOG_LEVEL=DEBUG docker-compose up --build
```

3. Show captions and important system messages:
```bash
CAPTIONS_LOG_LEVEL=INFO SYSTEM_LOG_LEVEL=INFO TRANSCRIPTION_LOG_LEVEL=ERROR docker-compose up --build
```

## Detailed Operation

### INITIAL SETUP (First 60 seconds):
- Two FFmpeg instances are started:
  1. One for direct audio streaming to Gladia (low-latency transcription)
  2. One for creating HLS segments (video and audio)
- Video and audio are split into separate streams for better handling
- Video segments go to output/video/
- Audio segments go to output/audio/
- Real-time audio is streamed directly to Gladia for immediate transcription
- Transcriptions and translations start accumulating in memory
- Nothing is served yet - http://localhost:8080/master.m3u8 returns 404

### BUFFERING PHASE:
- Script waits until it has:
  - 6 complete video segments (60 seconds of content)
  - Matching audio segments
  - At least 3 transcriptions for this content
- During this time, it's building three synchronized streams:
  1. Video segments (.ts files)
  2. Audio segments (.ts files)
  3. Caption segments (.vtt files) in three languages (ru, en, nl)

### SERVING STARTS:
- After 60 seconds, http://localhost:8080/master.m3u8 becomes available
- When a viewer connects, they see content from 60 seconds ago
- The master playlist points to:
  - Video playlist (video/playlist.m3u8)
  - Audio playlist (audio/playlist.m3u8)
  - Subtitle playlists (subtitles/{lang}/playlist.m3u8)

### CONTINUOUS OPERATION:
- At any given moment:
  - Viewers are watching segment N
  - FFmpeg is creating segment N+6
  - Gladia is receiving real-time audio and providing immediate transcriptions
  - VTT files are being prepared for segment N+6
- Each 10-second segment has:
  - A video file
  - An audio file
  - Three VTT files (Russian, English, Dutch)
- The playlists maintain a rolling window of 10 segments
- Old segments are automatically removed
- Captions that span segment boundaries are properly handled

### VIEWER EXPERIENCE:
- Viewer opens http://localhost:8080 in their browser
- Player loads master.m3u8 and all necessary streams
- Content starts playing from 60 seconds ago
- Captions are available immediately through native HLS subtitle support
- Viewers can switch between languages using the player controls
- Stream maintains consistent 60-second delay throughout playback

This architecture ensures that by the time any segment reaches the viewer, its captions are already prepared, synchronized, and ready to display. The dual FFmpeg approach (one for real-time transcription, one for HLS segments) ensures fast caption generation while maintaining stable playback.

## Troubleshooting

- **No captions appear**: Check the logs with `TRANSCRIPTION_LOG_LEVEL=DEBUG` to see if transcriptions are being received and processed correctly.
- **Stream doesn't play**: Verify that the HLS source URL is accessible and check system logs with `SYSTEM_LOG_LEVEL=DEBUG`.
- **Multiple captions showing**: Only one caption track should be active at a time. Use the language buttons to switch between tracks.
- **Container fails to start**: Ensure all required ports are available and the environment variables are set correctly.
- **Caption timing issues**: If captions appear out of sync, check the logs for timing information and ensure both FFmpeg instances are running properly.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- [Gladia API](https://gladia.io/) for the real-time transcription service
- [FFmpeg](https://ffmpeg.org/) for media processing
- [FastAPI](https://fastapi.tiangolo.com/) for the web server
- [HLS.js](https://github.com/video-dev/hls.js/) for HLS playback