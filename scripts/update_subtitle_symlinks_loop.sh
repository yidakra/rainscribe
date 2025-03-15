#!/bin/bash

# This script updates the subtitle symlinks in the HLS directory in a loop

# Function to log messages with timestamp
log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC'): $1"
}

log "Starting subtitle symlink updater loop"

# Ensure the directories exist
mkdir -p /shared-data/hls/subtitles
mkdir -p /shared-data/webvtt/ru

# Counter for statistics
update_count=0

while true; do
    # Create symlink for the playlist
    if [ -f /shared-data/webvtt/ru/playlist.m3u8 ]; then
        ln -sf /shared-data/webvtt/ru/playlist.m3u8 /shared-data/hls/subtitles/playlist.m3u8
    else
        log "Warning: Source playlist file does not exist"
    fi

    # Count files before update
    before_count=$(ls -1 /shared-data/hls/subtitles/segment_*.vtt 2>/dev/null | wc -l)

    # Read the playlist to get the segment numbers
    if [ -f /shared-data/webvtt/ru/playlist.m3u8 ]; then
        # Extract segment numbers from the playlist
        segment_files=$(grep -o "segment_[0-9]*.vtt" /shared-data/webvtt/ru/playlist.m3u8 | sort | uniq)
        
        # Log the number of segments in the playlist
        segment_count=$(echo "$segment_files" | wc -l)
        log "Found $segment_count segment files in playlist"
        
        # Create symlinks for each segment file
        for segment in $segment_files; do
            src_path="/shared-data/webvtt/ru/$segment"
            dst_path="/shared-data/hls/subtitles/$segment"
            
            # Create symlink only if the source file exists
            if [ -f "$src_path" ]; then
                # Remove existing symlink if it exists
                if [ -L "$dst_path" ]; then
                    rm "$dst_path"
                fi
                
                # Create new symlink
                ln -sf "$src_path" "$dst_path"
            else
                log "Warning: Source file not found: $src_path"
            fi
        done
    else
        log "Warning: Playlist file not found, falling back to directory scan"
    fi

    # As a fallback, scan the directory for VTT files
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

    # Count files after update
    after_count=$(ls -1 /shared-data/hls/subtitles/segment_*.vtt 2>/dev/null | wc -l)
    
    # Log statistics
    update_count=$((update_count + 1))
    log "Update #$update_count: Symlinks updated. Before: $before_count, After: $after_count"
    
    # List newest VTT files in webvtt directory
    newest_files=$(ls -lt /shared-data/webvtt/ru/segment_*.vtt 2>/dev/null | head -3)
    if [ -n "$newest_files" ]; then
        log "Newest VTT files:"
        echo "$newest_files"
    else
        log "No VTT files found in source directory!"
    fi
    
    # Check for source files that don't have corresponding symlinks
    missing_symlinks=0
    for src_path in /shared-data/webvtt/ru/segment_*.vtt; do
        if [ -f "$src_path" ]; then
            filename=$(basename "$src_path")
            dst_path="/shared-data/hls/subtitles/$filename"
            
            if [ ! -L "$dst_path" ]; then
                missing_symlinks=$((missing_symlinks + 1))
                log "Warning: Missing symlink for $filename"
            fi
        fi
    done
    
    if [ "$missing_symlinks" -gt 0 ]; then
        log "Warning: $missing_symlinks source files don't have corresponding symlinks"
    fi
    
    # Sleep for 5 seconds
    sleep 5
done 