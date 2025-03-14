#!/bin/sh
# Shared Volume Initialization Script for rainscribe

# Load configuration from environment variables
SHARED_VOLUME_PATH=${SHARED_VOLUME_PATH:-"/shared-data"}
TRANSCRIPTION_LANGUAGE=${TRANSCRIPTION_LANGUAGE:-"ru"}
TRANSLATION_LANGUAGES=${TRANSLATION_LANGUAGES:-"en,nl"}

echo "Initializing shared volume at $SHARED_VOLUME_PATH"

# Create main directories
mkdir -p $SHARED_VOLUME_PATH
mkdir -p $SHARED_VOLUME_PATH/transcript
mkdir -p $SHARED_VOLUME_PATH/webvtt
mkdir -p $SHARED_VOLUME_PATH/hls

# Create language-specific directories
echo "Creating directories for language: $TRANSCRIPTION_LANGUAGE"
mkdir -p $SHARED_VOLUME_PATH/webvtt/$TRANSCRIPTION_LANGUAGE
mkdir -p $SHARED_VOLUME_PATH/hls/$TRANSCRIPTION_LANGUAGE

# Process comma-separated languages without array
echo "$TRANSLATION_LANGUAGES" | tr ',' '\n' | while read lang; do
    echo "Creating directories for language: $lang"
    mkdir -p $SHARED_VOLUME_PATH/webvtt/$lang
    mkdir -p $SHARED_VOLUME_PATH/hls/$lang
done

# Create a named pipe for audio streaming
echo "Creating named pipe for audio stream"
if [ -e "$SHARED_VOLUME_PATH/audio_stream" ]; then
    rm -f "$SHARED_VOLUME_PATH/audio_stream"
fi
mkfifo "$SHARED_VOLUME_PATH/audio_stream"

# Set permissions
echo "Setting permissions"
chmod -R 777 $SHARED_VOLUME_PATH

echo "Shared volume initialization complete" 