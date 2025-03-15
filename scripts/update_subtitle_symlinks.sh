#!/bin/bash

# This script updates the subtitle symlinks in the HLS directory

# Ensure the subtitles directory exists
mkdir -p /shared-data/hls/subtitles

# Create symlink for the playlist
ln -sf /shared-data/webvtt/ru/playlist.m3u8 /shared-data/hls/subtitles/playlist.m3u8

# Find all segment files in the webvtt directory
for src_path in /shared-data/webvtt/ru/segment_*.vtt; do
    if [ -f "$src_path" ]; then
        filename=$(basename "$src_path")
        dst_path="/shared-data/hls/subtitles/$filename"
        
        # Remove existing symlink if it exists
        if [ -L "$dst_path" ]; then
            rm "$dst_path"
        fi
        
        # Create new symlink
        ln -sf "$src_path" "$dst_path"
    fi
done

echo "Updated subtitle symlinks: $(ls -1 /shared-data/hls/subtitles/segment_*.vtt | wc -l) files" 