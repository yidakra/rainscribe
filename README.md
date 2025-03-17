# Rainscribe

Rainscribe is a live transcription and translation tool that creates embedded WebVTT caption tracks for HLS streaming. It transcribes audio from an HLS stream in real-time and provides captions in multiple languages.

## Features

- Live transcription of HLS audio streams using [Gladia API](https://gladia.io/)
- Real-time translation of captions to English and Dutch
- Live updating WebVTT caption files
- HLS stream repackaging with FFmpeg
- Web player with language selection buttons
- Cross-platform compatibility

## Requirements

- Python 3.9+
- FFmpeg installed and available in PATH
- Gladia API key (Sign up at [Gladia](https://gladia.io/))
- Internet connection

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/yourusername/rainscribe.git
   cd rainscribe
   ```

2. Install required Python packages:
   ```
   pip install websockets requests
   ```

3. Ensure FFmpeg is installed on your system.

## Usage

Run the script with your Gladia API key:

```bash
python3 rainscribe.py YOUR_GLADIA_API_KEY
```

Then open `http://localhost:8080/index.html` in your browser to view the stream with embedded captions.

### How It Works

The script performs the following steps:

1. Initializes a live transcription session with Gladia
2. Streams audio from the HLS URL (via FFmpeg) to Gladia's WebSocket endpoint
3. Receives transcription and translation messages continuously
4. Appends each transcript as a WebVTT cue to language-specific files
5. Repackages the original HLS stream into a new HLS stream
6. Generates a master playlist that includes subtitle tracks for each language
7. Starts an HTTP server to serve the player, stream, and captions
8. Allows switching between different language captions in the player

### Configuration

You can customize the following variables in `rainscribe.py`:

- `EXAMPLE_HLS_STREAM_URL`: URL of the HLS stream to transcribe
- `HTTP_PORT`: Port for the HTTP server
- `MIN_CUES`: Minimum number of caption cues to prebuffer before starting playback
- `STREAMING_CONFIGURATION`: Configuration for Gladia API including language settings

## Troubleshooting

- Make sure FFmpeg is properly installed and available in your PATH
- Check that your Gladia API key is valid
- For any errors, check the log file `rainscribe_run.log` created after each run

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

## Acknowledgements

- [Gladia API](https://gladia.io/) for providing the transcription and translation services
- [FFmpeg](https://ffmpeg.org/) for media processing capabilities
- [HLS.js](https://github.com/video-dev/hls.js/) for HLS playback in browsers 