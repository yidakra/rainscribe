# rainscribe Usage Guide

This document provides instructions on how to build, run, and deploy the rainscribe system for automated transcription and translation of Russian TV streams.

## Prerequisites

- Docker and Docker Compose for local development
- Kubernetes cluster for production deployment
- Helm for managing Kubernetes deployments
- Poetry for Python dependency management
- FFmpeg for audio/video processing

## Local Development with Docker Compose

The easiest way to run the complete rainscribe system locally is using Docker Compose.

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/rainscribe.git
   cd rainscribe
   ```

2. Build the Docker images:
   ```bash
   make build
   ```

3. Start all services:
   ```bash
   make up
   ```

4. View the logs:
   ```bash
   make logs
   ```

5. Access the HLS player in your browser:
   ```
   http://localhost:8080
   ```

6. To stop all services:
   ```bash
   make down
   ```

## Kubernetes Deployment with Helm

For production deployment, use Kubernetes with Helm:

1. Build and push the Docker images to a registry:
   ```bash
   make build-images
   make push-images
   ```

2. Install or upgrade the Helm chart:
   ```bash
   make helm-install
   ```

3. Access the deployed application:
   The application will be available at the hostname configured in the ingress settings. By default, it's set to `rainscribe.local`. You may need to add this to your /etc/hosts file or configure your DNS accordingly.

4. To uninstall the deployment:
   ```bash
   make helm-uninstall
   ```

## Configuration

The system's behavior can be customized by modifying the environment variables in the `.env` file or the values in `helm-chart/values.yaml` for Kubernetes deployment.

Key configuration parameters:

- `GLADIA_API_KEY`: Your Gladia API key for transcription and translation services
- `HLS_STREAM_URL`: URL of the HLS stream to process
- `TRANSCRIPTION_LANGUAGE`: Primary language for transcription (default: "ru")
- `TRANSLATION_LANGUAGES`: Languages to translate to, comma-separated (default: "en,nl")

## Architecture

rainscribe consists of the following microservices:

1. **Audio Extractor Service**: Captures the HLS stream and extracts audio
2. **Transcription & Translation Service**: Uses Gladia API for real-time transcription and translation
3. **Caption Generator Service**: Converts transcription data into WebVTT subtitle files
4. **Stream Mirroring Service**: Merges original video with subtitles into a new HLS stream
5. **NGINX Server**: Hosts the final HLS content and web player

All services communicate through a shared data volume.

## Troubleshooting

- **No audio extraction**: Check if the HLS stream URL is accessible
- **No transcription**: Verify your Gladia API key is valid
- **Service crashes**: Check the logs with `make logs` for detailed error messages
- **WebVTT files not generated**: Ensure the shared volume is properly mounted and accessible by all services

For more detailed logs, you can view individual service logs:
```bash
docker-compose logs audio-extractor
docker-compose logs transcription-service
docker-compose logs caption-generator
docker-compose logs stream-mirroring
docker-compose logs nginx
```

## Support

For issues, questions, or contributions, please open an issue on the GitHub repository. 