#!/bin/bash

# Subtitle Symlink Service
# This script manages the continuous updating of subtitle symlinks

LOG_FILE="/shared-data/logs/subtitle_symlinks.log"
PID_FILE="/shared-data/state/subtitle_symlinks.pid"
SYMLINK_SCRIPT="/app/update_subtitle_symlinks_loop.sh"

# Create necessary directories
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$PID_FILE")"

# Function to log messages with timestamp
log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC'): $1" | tee -a "$LOG_FILE"
}

# Function to clean up on exit
cleanup() {
    if [ -f "$PID_FILE" ]; then
        local old_pid=$(cat "$PID_FILE")
        if ps -p "$old_pid" > /dev/null; then
            log "Stopping subtitle symlink service (PID: $old_pid)"
            kill "$old_pid" 2>/dev/null || kill -9 "$old_pid" 2>/dev/null
        fi
        rm -f "$PID_FILE"
    fi
    log "Subtitle symlink service stopped"
    exit 0
}

# Register cleanup function on exit
trap cleanup EXIT INT TERM

# Check if the script exists
if [ ! -f "$SYMLINK_SCRIPT" ]; then
    log "Error: Script not found: $SYMLINK_SCRIPT"
    log "Checking for the script in current directory..."
    
    if [ -f "./update_subtitle_symlinks_loop.sh" ]; then
        SYMLINK_SCRIPT="./update_subtitle_symlinks_loop.sh"
        log "Found script in current directory, using: $SYMLINK_SCRIPT"
    else
        log "Error: Cannot find update_subtitle_symlinks_loop.sh"
        exit 1
    fi
fi

# Check if script is executable, make it executable if not
if [ ! -x "$SYMLINK_SCRIPT" ]; then
    log "Making script executable: $SYMLINK_SCRIPT"
    chmod +x "$SYMLINK_SCRIPT"
fi

# Check if service is already running
if [ -f "$PID_FILE" ]; then
    old_pid=$(cat "$PID_FILE")
    if ps -p "$old_pid" > /dev/null; then
        log "Subtitle symlink service already running (PID: $old_pid)"
        log "Stopping previous instance"
        kill "$old_pid" 2>/dev/null || kill -9 "$old_pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
fi

# Start the symlink update script in the background
log "Starting subtitle symlink service"
$SYMLINK_SCRIPT >> "$LOG_FILE" 2>&1 &
new_pid=$!
echo $new_pid > "$PID_FILE"
log "Subtitle symlink service started (PID: $new_pid)"

# If running in foreground (not as a service), wait for the process
if [ -t 0 ]; then
    log "Running in foreground mode, press Ctrl+C to stop"
    wait $new_pid
fi 