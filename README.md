# RainScribe

Cable TV-Ready Live Transcription with EIA-608/708 Captions for HLS Streaming

## Overview

RainScribe is a Python application that generates real-time captions for live streams using the Gladia API for transcription. It specifically focuses on creating EIA-608/708 captions that are compatible with cable TV standards, while also providing fallback options for web streaming.

The application captures audio from an HLS stream, sends it to Gladia API for real-time transcription, and then integrates the resulting captions back into the stream using FFmpeg. The result is a live stream with properly formatted closed captions that can be viewed in a web browser or sent to cable TV distribution systems.

## Features

- Real-time transcription of live HLS streams with minimal latency
- EIA-608/708 caption generation for cable TV compatibility
- Multiple fallback methods for different FFmpeg builds
- WebVTT captions for web streaming
- Built-in HTTP server for viewing the stream with captions
- Modular architecture for easy maintenance and extension
- Automatic handling of Russian to Latin transliteration for captions
- Robust error handling with recovery mechanisms
- Comprehensive logging system

## Project Structure

The project is organized into three main modules:

- `rainscribe.py`: Main entry point that coordinates the other modules
  - Handles command-line interface
  - Sets up logging
  - Initializes the transcription and media services
  - Manages the overall application flow
  
- `transcription.py`: Handles Gladia API interactions and transcription processing
  - Manages the connection to Gladia's transcription service
  - Processes transcription messages in real-time
  - Maintains the caption cue database
  - Handles text formatting and transliteration
  
- `media.py`: Manages FFmpeg operations, caption formatting, and HTTP streaming
  - Contains multiple methods for generating EIA-608/708 captions
  - Manages FFmpeg processes for audio extraction and stream muxing
  - Handles the HTTP server for viewing streams
  - Provides fallback mechanisms for different FFmpeg capabilities

## Technical Details

### EIA-608/708 Captions

The application implements several methods to generate EIA-608/708 (CEA-608/708) closed captions, which are the standard for North American cable television. Unlike simple subtitles, these captions:

- Are encoded directly into the video signal
- Support positioning, colors, and different caption channels
- Can be turned on/off by viewers
- Comply with accessibility regulations

RainScribe attempts multiple methods for adding these captions, depending on your FFmpeg build's capabilities:

1. Direct EIA-608 encoder (if available)
2. CEA-608 with copy codec (compatible with some FFmpeg builds)
3. CC_data encoder approach
4. MOV_text with EIA-608 compatibility flags
5. Video filter to inject 608 captions
6. WebVTT fallback for web-only viewing

### Modular Architecture

The codebase uses a modular design to separate concerns:

- **Transcription Module**: Isolates API interactions and caption generation
- **Media Module**: Handles all FFmpeg and streaming functionality
- **Main Application**: Coordinates between modules and manages the overall process

This separation makes the code more maintainable and allows components to be upgraded or replaced independently.

## Requirements

- Python 3.7+
- FFmpeg with caption support (ideally with EIA-608/CEA-608 encoder)
- Gladia API key (for transcription)
- Stable internet connection
- HLS stream source (example included)

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/yourusername/rainscribe.git
   cd rainscribe
   ```

2. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```

3. Create a `.env` file with your Gladia API key:
   ```
   GLADIA_API_KEY=your_api_key_here
   ```

4. Ensure you have FFmpeg installed with caption support:
   - On Ubuntu/Debian: `sudo apt-get install ffmpeg`
   - On macOS with Homebrew: `brew install ffmpeg`
   - On Windows: Download from [FFmpeg.org](https://ffmpeg.org/download.html)

## Usage

1. Run the application:
   ```
   python rainscribe.py
   ```

2. Open a web browser and navigate to:
   ```
   http://localhost:8080/index.html
   ```

3. The stream will start playing with captions once enough transcription data has been collected (default: after 5 caption cues).

4. The application creates several directories for organization:
   - `rainscribe_output/segments/`: Contains HLS segments
   - `rainscribe_output/captions/`: Contains caption files
   - `rainscribe_output/temp/`: Contains temporary files

## Configuration

You can modify the following settings in `rainscribe.py`:

- `EXAMPLE_HLS_STREAM_URL`: URL of the HLS stream to process
- `MIN_CUES`: Minimum number of caption cues to collect before starting the muxing process
- `MUX_UPDATE_INTERVAL`: Seconds between FFmpeg muxing process restarts
- `HTTP_PORT`: Port for the HTTP server
- `STREAMING_CONFIGURATION`: Settings for the Gladia API transcription

## Troubleshooting

### Common Issues

1. **FFmpeg Errors**:
   - Check your FFmpeg version and build information with `ffmpeg -version`
   - The application will attempt multiple fallback methods if the ideal method fails

2. **Transcription Issues**:
   - Check your API key in the `.env` file
   - Make sure your internet connection is stable
   - The Gladia API requires a clear audio signal for best results

3. **Stream Not Playing**:
   - Check that the source HLS stream is accessible
   - Allow time for initial transcription and buffering
   - Use the "Refresh Player" button in the web interface

4. **Caption Format Issues**:
   - Different media players support different caption formats
   - Check the "Cable TV Compatibility Information" section in the web player

### Logs

The application creates detailed logs in `rainscribe_run.log`. Check this file for more information about errors or issues.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details. This means you can freely use, modify, and distribute this software, but any derivative works must also be distributed under the same license terms.

## Acknowledgements

- [Gladia API](https://gladia.io/) for real-time transcription
- [FFmpeg](https://ffmpeg.org/) for media processing
- [WebSockets](https://websockets.readthedocs.io/) for real-time communication
- The open-source community for various libraries and tools used in this project

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the project
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request 