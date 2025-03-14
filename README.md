# rainscribe: Automated Real-time Transcription and Translation for HLS

A scalable, containerized solution for real-time transcription and translation of Russian TV streams with WebVTT subtitle generation.

## System Overview

rainscribe processes a live HLS stream by:
1. Extracting audio from an HLS stream in real-time
2. Using Gladia API for real-time transcription with word-level timestamps in Russian
3. Simultaneously translating content to English and Dutch
4. Converting transcription/translation data into WebVTT subtitles
5. Mirroring the original video stream with added subtitles
6. Serving the resulting HLS content via NGINX

## Architecture

The solution consists of several microservices:

- **Audio Extractor Service**: Captures the HLS stream and extracts audio
- **Transcription & Translation Service**: Uses Gladia API to generate transcriptions and translations
- **Caption Generator Service**: Converts transcription data into WebVTT subtitle files
- **Stream Mirroring Service**: Merges original video with subtitles to create a new HLS stream
- **NGINX Server**: Hosts the final HLS content (manifests, video segments, WebVTT files)

## Deployment

The solution is containerized using Docker and deployed on Kubernetes with Helm charts.

## Prerequisites

- Python 3.10+
- Poetry for dependency management
- Docker and Kubernetes
- Rancher Desktop
- Helm

## License

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>.