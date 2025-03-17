# Quick Start Guide for RainScribe

This guide will help you get RainScribe up and running quickly.

## Prerequisites

Before starting, make sure you have:

1. Python 3.7 or higher installed
2. FFmpeg installed on your system
3. A Gladia API key (get one at https://app.gladia.io/)

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/rainscribe.git
   cd rainscribe
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set up your environment:
   ```bash
   cp .env.example .env
   ```

4. Edit the `.env` file and add your Gladia API key:
   ```
   GLADIA_API_KEY=your_api_key_here
   ```

## Running RainScribe

1. Start the application:
   ```bash
   python rainscribe.py
   ```

2. Open your web browser and navigate to:
   ```
   http://localhost:8080/index.html
   ```

3. Wait a few moments for transcription to begin. The stream will start once enough captions have been collected.

4. Use the controls in the web player to manage captions and playback.

## Troubleshooting

If you encounter issues:

- Check the console output for errors
- Look at the `rainscribe_run.log` file for detailed logs
- Make sure your FFmpeg installation supports captions
- Verify your API key is correct
- Ensure the source stream is accessible

## Next Steps

Once you have RainScribe running:

1. Try different source streams by editing the `EXAMPLE_HLS_STREAM_URL` in `rainscribe.py`
2. Explore the different FFmpeg caption methods in `media.py`
3. Customize the web player appearance by modifying the HTML in `write_index_html()`

For more detailed information, see the full [README.md](README.md) file. 