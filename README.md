# rainscribe: Automated Real-time Transcription for HLS

A scalable, containerized solution for real-time transcription of Russian TV streams with WebVTT subtitle generation and precise synchronization.

## System Overview

rainscribe processes a live HLS stream by:
1. Extracting audio from an HLS stream in real-time
2. Using Gladia API for real-time transcription with word-level timestamps in Russian
3. Converting transcription data into synchronized WebVTT subtitles
4. Mirroring the original video stream with added subtitles
5. Serving the resulting HLS content via NGINX

## Architecture

The solution consists of several microservices:

- **Audio Extractor Service**: Captures the HLS stream and extracts audio
- **Transcription Service**: Uses Gladia API to generate Russian transcriptions with word-level timestamps
- **Caption Generator Service**: Converts transcription data into WebVTT subtitle files with precise synchronization
- **Stream Mirroring Service**: Merges original video with subtitles to create a new HLS stream using EXT-X-PROGRAM-DATE-TIME for synchronization
- **NGINX Server**: Hosts the final HLS content (manifests, video segments, WebVTT files)

## Synchronization

The system uses several mechanisms to ensure accurate synchronization between video and subtitles:

1. **Reference Clock**: A shared reference time used by all services to coordinate timestamps
2. **Latency Measurement**: The transcription service measures pipeline latency and adjusts timestamps accordingly
3. **Adaptive Offset**: The caption generator dynamically adjusts timestamp offsets to compensate for drift
4. **EXT-X-PROGRAM-DATE-TIME Tags**: The stream mirroring service adds program date timestamps to ensure proper playback alignment

## Synchronization Framework Improvements

Recent improvements to the synchronization framework include:

### 1. Reference Clock System

A unified reference clock system has been implemented to provide a consistent time reference across all components of the system. This ensures that all services work with the same time information, eliminating drift and inconsistencies.

Features:
- NTP-based synchronization with external time sources
- Drift compensation and monitoring
- Persistent state to maintain clock accuracy across restarts
- Easy-to-use global clock instance for all services

### 2. Enhanced Offset Calculation

The offset calculation system has been improved with advanced smoothing algorithms to provide stable synchronization even with noisy measurements.

Features:
- Exponential Moving Average (EMA) smoothing
- Outlier detection and removal
- Statistical analysis of offset measurements
- Automatic adaptation to changing conditions

### 3. WebVTT Segment Alignment

WebVTT subtitles are now properly segmented to align perfectly with HLS video segments, ensuring consistent synchronization across all playback platforms.

Features:
- Time-based segmentation with configurable duration
- Segment boundary overlap handling
- Automatic timestamp adjustment
- HLS playlist generation

### 4. Centralized Logging

A standardized logging system ensures consistent log format and collection across all services.

Features:
- Multiple log levels and formats (text, JSON)
- File rotation and retention policies
- Centralized configuration

### 5. Monitoring and Metrics

Real-time monitoring of synchronization quality and system performance helps identify and address issues quickly.

Features:
- System resource usage tracking
- Synchronization metrics collection
- Latency and offset measurements
- Dashboard data generation
- Persistent metrics storage

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