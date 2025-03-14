#!/bin/sh
#
# Initialization script for shared volume
#

set -e

# Get the shared volume path from environment or use default
SHARED_VOLUME_PATH=${SHARED_VOLUME_PATH:-/shared-data}

# Create required directories
mkdir -p ${SHARED_VOLUME_PATH}/audio
mkdir -p ${SHARED_VOLUME_PATH}/transcript
mkdir -p ${SHARED_VOLUME_PATH}/webvtt
mkdir -p ${SHARED_VOLUME_PATH}/hls
mkdir -p ${SHARED_VOLUME_PATH}/logs
mkdir -p ${SHARED_VOLUME_PATH}/state

# Create empty README files to document directory purpose
echo "This directory contains audio files extracted from the input stream." > ${SHARED_VOLUME_PATH}/audio/README.txt
echo "This directory contains transcript files generated from audio." > ${SHARED_VOLUME_PATH}/transcript/README.txt
echo "This directory contains WebVTT subtitle files generated from transcripts." > ${SHARED_VOLUME_PATH}/webvtt/README.txt
echo "This directory contains HLS stream files mirrored from the source." > ${SHARED_VOLUME_PATH}/hls/README.txt
echo "This directory contains log files from various services." > ${SHARED_VOLUME_PATH}/logs/README.txt
echo "This directory contains persistent state files for clock and offset synchronization." > ${SHARED_VOLUME_PATH}/state/README.txt

# Ensure directories have proper permissions
chmod -R 777 ${SHARED_VOLUME_PATH}

echo "Shared volume initialized successfully at ${SHARED_VOLUME_PATH}"
echo "Created directories: audio, transcript, webvtt, hls, logs, state"

exit 0 