# Using Rainscribe

This document contains detailed instructions for setting up, running, and troubleshooting the Rainscribe system.

## Setup

### Environment Variables

Create a `.env` file with the following configuration:

```bash
# Core Configuration
HLS_STREAM_URL=https://example.com/stream/index.m3u8
SHARED_VOLUME_PATH=/shared-data
TRANSCRIPTION_LANGUAGE=ru

# Transcription Service
GLADIA_API_KEY=your_api_key_here  # Required - Get from https://app.gladia.io/

# Clock Configuration
CLOCK_UPDATE_INTERVAL=1800
CLOCK_MAX_DRIFT=0.1
CLOCK_STATE_FILE=/shared-data/state/clock_state.json
CLOCK_SYNC_JITTER=60

# Offset Calculation
OFFSET_WINDOW_SIZE=30
OFFSET_EMA_ALPHA=0.15
OFFSET_OUTLIER_THRESHOLD=2.5
OFFSET_MEDIAN_WEIGHT=0.4
OFFSET_STATE_FILE=/shared-data/state/offset_state.json

# Segmentation
WEBVTT_SEGMENT_DURATION=10
WEBVTT_SEGMENT_OVERLAP=1.0
WEBVTT_TIME_SOURCE=reference_clock
HLS_CLOCK_SYNC=true

# Output Delay
VIDEO_OUTPUT_DELAY_SECONDS=30

# FFmpeg Configuration
FFMPEG_COPYTS=1
FFMPEG_START_AT_ZERO=1
FFMPEG_SEGMENT_DURATION=10
FFMPEG_MAX_RETRIES=10
FFMPEG_RETRY_DELAY=5
```

### Setting up Gladia API Key

The transcription service uses Gladia's API for real-time speech-to-text conversion. To use this service:

1. Sign up for a Gladia account at https://app.gladia.io/
2. Once registered, navigate to your dashboard
3. Generate a new API key or copy your existing API key
4. Add the API key to your `.env` file as `GLADIA_API_KEY=your_api_key_here`

Note: Without a valid Gladia API key, the transcription service will not function, and no subtitles will be generated.

### Running with Docker Compose

```bash
docker-compose up -d
```

## Tuning Synchronization

Synchronization between video and subtitles is a critical aspect of Rainscribe. Here's how to tune it:

### Clock Synchronization

1. **NTP Server Selection:** The reference clock uses NTP servers to synchronize time. By default, it uses `time.google.com`, `pool.ntp.org`, `time.cloudflare.com`, and `time.apple.com`. You can customize these by setting the `NTP_SERVERS` environment variable.

2. **Update Interval:** The `CLOCK_UPDATE_INTERVAL` (in seconds) controls how often the reference clock syncs with NTP servers. Lower values provide more frequent updates but may cause more network traffic.

3. **Drift Monitoring:** The system calculates clock drift over time. If you notice persistent drift, consider reducing the `CLOCK_UPDATE_INTERVAL` to sync more frequently.

### Offset Calculation

The offset calculator maintains the timing relationship between audio transcription and video playback:

1. **Window Size:** `OFFSET_WINDOW_SIZE` determines how many recent measurements to use for calculations. Higher values (20-50) provide more stability but slower adaptation to changes.

2. **EMA Smoothing:** `OFFSET_EMA_ALPHA` controls the Exponential Moving Average weight. Values closer to 0 (e.g., 0.05-0.15) provide more smoothing but slower adaptation to changes.

3. **Median Weight:** `OFFSET_MEDIAN_WEIGHT` (0.0-1.0) controls how much importance is given to the median value vs. the EMA. Higher values (0.5-0.7) provide more stability in noisy conditions.

4. **Outlier Rejection:** `OFFSET_OUTLIER_THRESHOLD` determines when to reject measurements as outliers. Values between 2.0-3.0 are typically good, with lower values being more aggressive at rejecting outliers.

### Segment Timing

The WebVTT segmenter and FFmpeg must use consistent segment durations:

1. **Segment Duration:** Ensure `WEBVTT_SEGMENT_DURATION` and `FFMPEG_SEGMENT_DURATION` are identical (10 seconds by default).

2. **Segment Overlap:** `WEBVTT_SEGMENT_OVERLAP` adds a small overlap between WebVTT segments to prevent subtitles at segment boundaries from disappearing. 1.0 seconds is a good default.

3. **HLS Sync:** Setting `HLS_CLOCK_SYNC=true` makes WebVTT segments align precisely with HLS segments using program date time values.

### Output Delay

The system can intentionally delay the output stream to ensure captions have enough time to be generated:

1. **Delay Configuration:** Set `VIDEO_OUTPUT_DELAY_SECONDS` to specify how many seconds the output stream should be delayed (default is 30 seconds).

2. **How It Works:** The Stream Mirroring service creates a delayed playlist that references older segments, giving the caption generation pipeline enough time to process and create subtitles for those segments.

3. **Trade-offs:** Increasing the delay improves caption reliability but increases the overall latency for viewers. In most cases, 20-30 seconds provides a good balance.

4. **Dynamic Adjustment:** You can modify this value based on your specific requirements. For slower transcription services or more complex audio, you might need to increase the delay.

## Troubleshooting

### Subtitle Delay

If subtitles appear consistently too early or too late:

1. Check the offset calculator metrics by examining `/metrics` endpoints
2. Look for patterns in the `current_offset` value - it should stabilize after a few minutes
3. Adjust the `BUFFER_DURATION` if needed - this adds extra delay to account for processing time
4. Restart the system with a different initial offset value if necessary
5. Consider increasing the `VIDEO_OUTPUT_DELAY_SECONDS` value if subtitles are consistently missing or appearing too late

### Subtitle Drift

If subtitles start in sync but gradually drift:

1. Check the reference clock metrics to ensure it's not drifting
2. Verify the FFmpeg process is using `-copyts` and `-start_at_zero` options
3. Make sure both the caption generator and stream mirroring services use the same segment duration
4. Look for abnormal latency patterns in the transcription service logs

### Inconsistent Subtitle Format

If subtitles appear with incorrect formatting or timing:

1. Examine the raw WebVTT files in the `/shared-data/webvtt` directory
2. Check for timestamp formatting issues or overlapping cue times
3. Adjust `MINIMUM_CUE_DURATION` if subtitles flash too quickly
4. Verify the player correctly interprets the WebVTT format

### FFmpeg Crashes

If FFmpeg processes crash frequently:

1. Examine logs in `/shared-data/logs`
2. Consider increasing buffer sizes with `FFMPEG_EXTRA_OPTIONS`
3. Check for network issues with the input stream
4. Adjust `FFMPEG_MAX_RETRIES` and `FFMPEG_RETRY_DELAY` for more resilience

## Advanced Configuration

### Redis Integration

For multi-node deployments, you can use Redis to share clock and offset state:

```bash
USE_REDIS_FOR_CLOCK=true
USE_REDIS_FOR_OFFSET=true
REDIS_HOST=redis-service
REDIS_PORT=6379
REDIS_DB=0
REDIS_KEY_PREFIX=rainscribe:
```

### Custom FFmpeg Options

You can provide additional FFmpeg options for specific needs:

```bash
FFMPEG_EXTRA_OPTIONS="-threads 4 -preset ultrafast -tune zerolatency"
```

### Monitoring

Each service exposes metrics on port 9090 by default:

- Audio Extractor: http://localhost:9091/metrics
- Transcription Service: http://localhost:9092/metrics
- Caption Generator: http://localhost:9093/metrics
- Stream Mirroring: http://localhost:9094/metrics

Set up Prometheus and Grafana to collect and visualize these metrics for production monitoring.

## Testing

To test if subtitles are properly synchronized:

1. Use the `player.html` test page: http://localhost:8080/player.html
2. Use VLC with the master playlist URL: http://localhost:8080/hls/master.m3u8
3. Monitor the metrics endpoints to see offset and clock values

## Common Issues

### HLS Stream Not Available

- Check if the input URL is accessible and valid
- Verify network connectivity from the container
- Check for any required authentication or headers

### No WebVTT Files Generated

- Verify the transcription service is receiving audio
- Check for errors in the transcription service logs
- Make sure the API credentials are correct (if using an external transcription service)

### Player Not Showing Subtitles

- Ensure the player supports HLS with WebVTT subtitles
- Check that subtitle tracks are properly referenced in the master playlist
- Try a different player (VLC, hls.js-based players, etc.)
- Verify that captions are enabled in the player settings 