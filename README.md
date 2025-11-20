# Rainscribe

Live transcription with native HLS subtitle integration for HLS streaming.

## Features

- Real-time transcription and translation of live HLS streams using Gladia API's real-time mode
- Native HLS subtitle integration (no WebSocket-based caption overlay)
- Supports multiple languages simultaneously (Russian source + English and Dutch translations)
- 60-second buffered playback for reliable caption synchronization
- Separate audio, video, and subtitle tracks for optimal streaming
- Controlled drip-feed delivery with precisely timed segment release
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

## Technical Details

### Timeline Synchronization

One of the most critical aspects of Rainscribe is properly synchronizing two independent timelines:

1. **Gladia Transcription Timeline**: Timestamps from the Gladia API are relative to when audio streaming to their service began. For example, the first transcript might arrive at timestamp 2.5s, meaning 2.5 seconds after the connection was established.

2. **Video Segment Timeline**: FFmpeg generates HLS segments with epoch-based numbers that are normalized to start at 0.0s for the first segment.

**The Synchronization Process**:
- When the first transcript arrives from Gladia (e.g., at timestamp 2.5s), this is recorded as `transcription_start_time`
- When the first video segment is detected, this establishes the segment timeline starting at 0.0s
- The system calculates `segment_time_offset = -transcription_start_time` to align these timelines
- All subsequent Gladia timestamps are converted to stream-relative timestamps using: `stream_time = gladia_time + segment_time_offset`
- Captions are only processed after both timelines are established and synchronized

This ensures captions appear at the correct time in the video stream, even though transcription and segment generation start at different moments.

**Overlap Detection**:
- For each caption, the system determines which video segments it overlaps with
- Uses strict mathematical overlap: caption overlaps segment if `caption_start < segment_end AND caption_end > segment_start`
- Captions that span multiple segments are correctly included in all relevant segment VTT files
- Each VTT file contains only the captions that overlap with that specific segment's time window

## Detailed Operation

### INITIAL SETUP (First 60 seconds):
- Two FFmpeg instances are started:
  1. One for direct audio streaming to Gladia (low-latency transcription)
  2. One for creating HLS segments (video and audio)
- Video and audio are split into separate streams for better handling
- Segments are stored in separate directories:
  - Video segments in output/video/
  - Audio segments in output/audio/
  - Subtitle segments in output/subtitles/{lang}/
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
- All segments are prepared but not yet exposed to viewers

### DRIP-FEED MECHANISM:
- After the buffer is ready, a new drip-feed system starts:
  - Creates separate serving directories (serving/video/, serving/audio/, serving/subtitles/)
  - Initially adds only the first buffered segment to serving playlists
  - Creates a special serving/master.m3u8 that references these serving playlists
  - Signals that the stream is ready to serve
- The drip-feed then:
  - Adds one new segment every SEGMENT_DURATION seconds (10 seconds)
  - Maintains exactly 2 segments in each serving playlist
  - Creates hard links (or copies) from source segments to serving segments
  - Updates serving playlists to reference only the current serving segments
  - Maintains exactly 60 seconds delay behind the source stream

### SERVING STARTS:
- After 60 seconds, http://localhost:8080/master.m3u8 becomes available
- When a viewer connects, they see content from 60 seconds ago
- The master playlist points to serving playlists:
  - Video playlist (serving/video/playlist.m3u8)
  - Audio playlist (serving/audio/playlist.m3u8)
  - Subtitle playlists (serving/subtitles/{lang}/playlist.m3u8)
- New viewers always see the current point in the delayed stream

### CONTINUOUS OPERATION:
- At any given moment:
  - Viewers are watching segment N
  - FFmpeg is creating segment N+6
  - Gladia is receiving real-time audio and providing immediate transcriptions
  - VTT files are being prepared for segment N+6
  - Drip-feed is exposing only segments N and N+1 to viewers
- Each 10-second segment has:
  - A video file
  - An audio file
  - Three VTT files (Russian, English, Dutch)
- The original playlists maintain a rolling window of 10 segments
- The serving playlists maintain only 2 segments
- Old segments are automatically removed
- Captions that span segment boundaries are properly handled

### VIEWER EXPERIENCE:
- Viewer opens http://localhost:8080 in their browser
- Player loads serving/master.m3u8 and all necessary streams
- Content starts playing from 60 seconds ago
- Captions are available immediately through native HLS subtitle support
- Viewers can switch between languages using the player controls
- Stream maintains consistent 60-second delay throughout playback
- New viewers joining later see the same delayed point in the stream

This architecture ensures that by the time any segment reaches the viewer, its captions are already prepared, synchronized, and ready to display. The drip-feed approach ensures that all viewers see the same content at the same relative point in time, maintaining a consistent 60-second delay.

## Development

### Setup Development Environment

1. Install development dependencies:
```bash
pip install -r requirements-dev.txt
```

2. Install pre-commit hooks:
```bash
pre-commit install
```

### Code Quality Tools

This project uses several tools to maintain code quality:

- **Ruff**: Fast Python linter and formatter
- **Mypy**: Static type checker
- **Pytest**: Testing framework
- **Bandit**: Security vulnerability scanner
- **Pre-commit**: Git hooks for automated checks

### Running Linters

```bash
# Run ruff linter
ruff check .

# Run ruff formatter
ruff format .

# Run type checker (informational)
mypy rainscribe.py test_timestamp_sync.py

# Run security scanner
bandit -r . -x ./venv,./output
```

### Pre-commit Hooks

Pre-commit hooks run automatically before each commit:
- Ruff linting and formatting
- Trailing whitespace removal
- End-of-file fixer
- YAML validation
- Large file detection
- Merge conflict detection

To run hooks manually:
```bash
pre-commit run --all-files
```

To run mypy manually (not run automatically):
```bash
pre-commit run mypy --all-files
```

### Continuous Integration

GitHub Actions CI runs on every push and pull request:
- Linting with ruff
- Type checking with mypy (informational)
- Unit tests with pytest
- Security scanning with bandit
- Docker image build verification

See `.github/workflows/ci.yml` for the full CI configuration.

## Testing

The project includes unit tests for the critical timestamp synchronization logic:

```bash
python3 test_timestamp_sync.py -v
```

These tests verify:
- Timeline synchronization between Gladia transcription and video segments
- Caption-to-segment overlap detection
- WebVTT timestamp formatting
- Edge cases like captions spanning multiple segments

## Troubleshooting

### No captions appear
Check the logs with `TRANSCRIPTION_LOG_LEVEL=DEBUG` to see if transcriptions are being received. Look for:
- "Initialized transcription_start_time" - confirms transcription is starting
- "Synchronized timelines" - confirms synchronization is complete
- Caption log entries showing `[RU]`, `[EN]`, `[NL]` with timestamps

If you see "Skipping transcript - waiting for timeline synchronization", the system is waiting for both timelines to be established. This should resolve within the first 10-20 seconds.

### Captions appear at wrong time
If captions are consistently early or late, check for these log messages:
- "Synchronized timelines" - shows the `segment_time_offset` value
- "Found overlap!" - shows which segments are being updated with captions

The system automatically calculates the offset between transcription and video timelines. If this appears incorrect, ensure:
1. Both FFmpeg instances are running (check with `SYSTEM_LOG_LEVEL=DEBUG`)
2. Gladia API is responding with transcriptions
3. Video segments are being created properly

### Stream doesn't play
Verify that the HLS source URL is accessible and check system logs with `SYSTEM_LOG_LEVEL=DEBUG`. Ensure the source stream is actually live and producing data.

### Multiple captions showing simultaneously
Only one caption track should be active at a time. Use the language buttons in the player to switch between Russian, English, and Dutch tracks. If multiple tracks appear simultaneously, this is a browser rendering issue - try refreshing the page.

### Container fails to start
Ensure all required ports are available and the environment variables are set correctly. The most common issue is port 8080 being in use by another service.

### Captions missing for some segments
This is usually caused by timing issues. Check:
- Are transcriptions being received continuously? Look for gaps in the caption logs
- Is the Gladia API connection stable? Check for WebSocket disconnection messages
- Are VTT files being created? Check the `output/subtitles/` directory

## License

This project is licensed under the GNU General Public License v3 (GPL-3.0) - see the LICENSE file for details.

## Acknowledgments

- [Gladia API](https://gladia.io/) for the real-time transcription service
- [FFmpeg](https://ffmpeg.org/) for media processing
- [FastAPI](https://fastapi.tiangolo.com/) for the web server
- [HLS.js](https://github.com/video-dev/hls.js/) for HLS playback
