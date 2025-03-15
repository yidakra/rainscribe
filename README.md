# Rainscribe

Rainscribe is a comprehensive system for real-time video streaming with synchronized subtitles, designed for live broadcasting applications. It captures audio from HLS streams, uses transcription services to generate subtitles, and adds those subtitles to the output stream.

## System Components

- **Audio Extractor**: Extracts audio from an HLS stream
- **Transcription Service**: Transcribes the audio in real-time
- **Caption Generator**: Generates WebVTT subtitles from transcriptions
- **Stream Mirroring**: Re-encodes the HLS stream with the generated subtitles
- **Nginx**: Serves the HLS stream with subtitles

## Key Features

- Real-time audio extraction from HLS streams
- Low-latency transcription processing
- Precise synchronization between video and subtitles
- Robust timing management via centralized reference clock
- HLS-compatible WebVTT subtitle generation
- Containerized microservice architecture

## Clock Synchronization System

Rainscribe uses a sophisticated time synchronization system to ensure that all components work with a consistent time reference:

- **Reference Clock**: A centralized clock that synchronizes with NTP servers to provide a global time reference
- **State Persistence**: Clock state is saved to file or Redis to maintain consistent timing across service restarts
- **Offset Calculation**: A robust algorithm for calculating and smoothing timing offsets between audio/video streams
- **HLS Segment Alignment**: WebVTT segments are precisely aligned with HLS video segments

## Configuration

The system can be configured through environment variables. See the `.env` file for an example configuration.

### Core Environment Variables

- `HLS_STREAM_URL`: URL of the input HLS stream to process
- `SHARED_VOLUME_PATH`: Path to the shared volume where files are stored (default: `/shared-data`)
- `WEBVTT_SEGMENT_DURATION`: Duration of each WebVTT segment in seconds (default: 10)
- `TRANSCRIPTION_LANGUAGE`: Language code for transcription (default: "ru")
- `GLADIA_API_KEY`: API key for Gladia transcription service (required)
- `VIDEO_OUTPUT_DELAY_SECONDS`: Delay for the output stream in seconds to ensure captions are ready (default: 30)

### Clock and Synchronization Variables

- `CLOCK_UPDATE_INTERVAL`: How often to sync with NTP servers in seconds (default: 3600)
- `CLOCK_MAX_DRIFT`: Maximum allowed drift in seconds before correction (default: 0.1)
- `CLOCK_STATE_FILE`: Path to store clock state (default: "~/.rainscribe/clock_state.json")
- `USE_REDIS_FOR_CLOCK`: Whether to use Redis for clock state storage (default: false)
- `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`: Redis connection parameters if Redis is enabled

### Offset Calculation Variables

- `OFFSET_WINDOW_SIZE`: Number of measurements to keep for smoothing (default: 30)
- `OFFSET_EMA_ALPHA`: EMA weight for new measurements (default: 0.15, lower = more smoothing)
- `OFFSET_OUTLIER_THRESHOLD`: Standard deviations for outlier detection (default: 2.5)
- `OFFSET_MEDIAN_WEIGHT`: Weight of median in calculation (default: 0.4, higher = more stable)
- `OFFSET_STATE_FILE`: Path to store offset state (default: "~/.rainscribe/offset_state.json")

### FFmpeg Configuration

- `FFMPEG_COPYTS`: Whether to use -copyts option (default: 1)
- `FFMPEG_START_AT_ZERO`: Whether to use -start_at_zero option (default: 1)
- `FFMPEG_SEGMENT_DURATION`: Duration of HLS segments in seconds (default: 10)
- `FFMPEG_MAX_RETRIES`: Maximum number of FFmpeg restart attempts (default: 10)
- `FFMPEG_RETRY_DELAY`: Delay between restart attempts in seconds (default: 5)
- `FFMPEG_EXTRA_OPTIONS`: Additional FFmpeg command-line options

## Deployment

### Using Docker Compose (Development)

1. Edit the `.env` file to set up your configuration
2. Start the services:

```bash
docker-compose up -d
```

3. Access the stream at http://localhost:8080/master.m3u8

### Using Kubernetes (Production)

1. Configure your values in the Helm chart (`helm-chart/values.yaml`)
2. Deploy using Helm:

```bash
helm install rainscribe ./helm-chart
```

## Troubleshooting

### Clock Synchronization Issues

If subtitles appear out of sync with the video:

1. Check the reference clock metrics (`http://localhost:8080/metrics`)
2. Verify that the clock offset is stable and not drifting
3. Adjust `OFFSET_EMA_ALPHA` and `OFFSET_MEDIAN_WEIGHT` to tune the offset calculation
4. Ensure all services are using the same `WEBVTT_SEGMENT_DURATION` and `FFMPEG_SEGMENT_DURATION`

### FFmpeg Errors

If you see FFmpeg crashing or producing errors:

1. Check the FFmpeg logs in `/shared-data/logs/`
2. Consider adjusting buffer sizes or adding specific FFmpeg options using `FFMPEG_EXTRA_OPTIONS`
3. Ensure all services are using the same segment duration (10 seconds by default)

### Subtitle Display Issues

If subtitles are not appearing or are appearing incorrectly:

1. Verify that the WebVTT files are being generated in `/shared-data/webvtt/`
2. Check that the master playlist references the subtitle tracks correctly
3. Adjust `SUBTITLE_DISPLAY_WINDOW` if subtitles are disappearing too quickly
4. Use a player that supports HLS with WebVTT subtitles (like VLC or hls.js-based players)
5. Try increasing the `VIDEO_OUTPUT_DELAY_SECONDS` value to give more time for subtitle generation

## Monitoring

The system exposes metrics for monitoring:

- Each service exposes metrics on its `/metrics` endpoint
- Metrics include timing offsets, process health, and synchronization status
- The metrics can be collected by Prometheus and visualized with Grafana

## License

This project is licensed under the MIT License - see the LICENSE file for details.