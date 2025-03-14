#!/usr/bin/env python3
"""
Reference Clock Module

This module provides a centralized reference clock for the entire system
to ensure consistent time references across all components.
"""

import os
import time
import json
import logging
import threading
import requests
import datetime
from typing import Dict, Optional, Callable, List, Tuple, Any, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("reference-clock")

# Constants
UPDATE_INTERVAL = 3600  # How often to sync with NTP servers (seconds)
DEFAULT_NTP_SERVERS = [
    "time.google.com",
    "pool.ntp.org",
    "time.cloudflare.com",
    "time.apple.com",
]
MAX_DRIFT = 0.1  # Maximum allowed drift in seconds before correction
CLOCK_STATE_FILE = os.path.expanduser("~/.rainscribe/clock_state.json")

class ReferenceClock:
    """
    A centralized reference clock that maintains synchronization with
    external time sources and provides consistent timestamps.
    """
    
    def __init__(self, 
                 ntp_servers: Optional[List[str]] = None,
                 update_interval: int = UPDATE_INTERVAL,
                 auto_sync: bool = True):
        """
        Initialize the reference clock.
        
        Args:
            ntp_servers: List of NTP servers to sync with
            update_interval: How often to sync with NTP servers (seconds)
            auto_sync: Whether to automatically sync with NTP servers
        """
        self.ntp_servers = ntp_servers or DEFAULT_NTP_SERVERS
        self.update_interval = update_interval
        self.offset = 0.0  # Offset between system time and reference time
        self.drift_rate = 0.0  # Estimated drift per second
        self.last_sync_time = 0.0  # Last time we synced with NTP
        self.sync_lock = threading.Lock()
        self.running = False
        self.sync_thread = None
        self.listeners: List[Callable[[float], None]] = []
        
        # Create directory for state file if it doesn't exist
        os.makedirs(os.path.dirname(CLOCK_STATE_FILE), exist_ok=True)
        
        # Try to load saved state
        self._load_state()
        
        # Start auto-sync if requested
        if auto_sync:
            self.start_sync()
    
    def get_time(self) -> float:
        """
        Get the current reference time.
        
        Returns:
            float: Current reference time in seconds since epoch
        """
        current_system_time = time.time()
        time_since_sync = current_system_time - self.last_sync_time
        
        # Apply known drift if we have a reasonable estimate
        drift_correction = self.drift_rate * time_since_sync if abs(self.drift_rate) > 1e-9 else 0.0
        
        # Calculate reference time
        reference_time = current_system_time + self.offset + drift_correction
        
        return reference_time
    
    def get_formatted_time(self, format_str: str = "%Y-%m-%d %H:%M:%S.%f") -> str:
        """
        Get the current reference time as a formatted string.
        
        Args:
            format_str: The format string to use
            
        Returns:
            str: Formatted reference time
        """
        ref_time = self.get_time()
        dt = datetime.datetime.fromtimestamp(ref_time)
        return dt.strftime(format_str)
    
    def sync_once(self) -> bool:
        """
        Perform a single synchronization with NTP servers.
        
        Returns:
            bool: True if sync was successful
        """
        with self.sync_lock:
            logger.info("Synchronizing with NTP servers...")
            
            successful_syncs = 0
            total_offset = 0.0
            
            for server in self.ntp_servers:
                try:
                    # Query the time server
                    t0 = time.time()
                    response = requests.get(f"https://{server}", timeout=5)
                    t3 = time.time()
                    
                    # Get server time from response headers
                    date_str = response.headers.get('date')
                    if not date_str:
                        logger.warning(f"No date header from {server}")
                        continue
                    
                    # Parse the server time
                    server_time = datetime.datetime.strptime(
                        date_str, "%a, %d %b %Y %H:%M:%S %Z"
                    ).timestamp()
                    
                    # Estimate network delay (assumes symmetric delay)
                    network_delay = (t3 - t0) / 2
                    
                    # Calculate offset: server_time is roughly at the midpoint of request
                    midpoint_local = (t0 + t3) / 2
                    new_offset = server_time - midpoint_local
                    
                    logger.info(f"Time from {server}: offset={new_offset:.6f}s, delay={network_delay:.6f}s")
                    
                    # Only use reliable measurements (low network delay)
                    if network_delay < 0.5:  # 500ms threshold
                        total_offset += new_offset
                        successful_syncs += 1
                
                except Exception as e:
                    logger.warning(f"Failed to sync with {server}: {str(e)}")
            
            if successful_syncs > 0:
                # Calculate average offset
                avg_offset = total_offset / successful_syncs
                
                # Calculate drift rate if we've synced before
                if self.last_sync_time > 0:
                    time_since_last_sync = time.time() - self.last_sync_time
                    if time_since_last_sync > 60:  # Need at least a minute to calculate meaningful drift
                        drift = avg_offset - self.offset
                        self.drift_rate = drift / time_since_last_sync
                        logger.info(f"Clock drift rate: {self.drift_rate * 86400 * 1000:.2f} ms/day")
                
                # Update offset
                old_offset = self.offset
                self.offset = avg_offset
                self.last_sync_time = time.time()
                
                logger.info(f"Clock synchronized. Offset adjusted by {self.offset - old_offset:.6f}s")
                
                # Save state
                self._save_state()
                
                # Notify listeners
                self._notify_listeners(self.offset)
                
                return True
            else:
                logger.warning("Failed to synchronize with any NTP servers")
                return False
    
    def start_sync(self):
        """Start the automatic synchronization thread."""
        if self.running:
            return
        
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
    
    def stop_sync(self):
        """Stop the automatic synchronization thread."""
        self.running = False
        if self.sync_thread:
            self.sync_thread.join(timeout=1.0)
            self.sync_thread = None
    
    def add_listener(self, callback: Callable[[float], None]):
        """
        Add a listener to be notified of offset changes.
        
        Args:
            callback: Function to call with the new offset
        """
        self.listeners.append(callback)
    
    def remove_listener(self, callback: Callable[[float], None]):
        """
        Remove a listener.
        
        Args:
            callback: The callback function to remove
        """
        if callback in self.listeners:
            self.listeners.remove(callback)
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get the current status of the reference clock.
        
        Returns:
            Dict: Status information
        """
        now = time.time()
        return {
            "offset": self.offset,
            "drift_rate": self.drift_rate,
            "drift_ms_per_day": self.drift_rate * 86400 * 1000,
            "last_sync_time": self.last_sync_time,
            "time_since_sync": now - self.last_sync_time if self.last_sync_time > 0 else None,
            "current_system_time": now,
            "current_reference_time": self.get_time(),
            "auto_sync_active": self.running,
        }
    
    def _sync_loop(self):
        """Internal loop for automatic synchronization."""
        # Initial sync
        self.sync_once()
        
        while self.running:
            # Sleep until next sync time
            time.sleep(self.update_interval)
            
            if not self.running:
                break
                
            # Perform sync
            self.sync_once()
    
    def _notify_listeners(self, offset: float):
        """Notify all listeners of a new offset."""
        for listener in self.listeners:
            try:
                listener(offset)
            except Exception as e:
                logger.error(f"Error in clock listener: {str(e)}")
    
    def _save_state(self):
        """Save the current state to disk."""
        state = {
            "offset": self.offset,
            "drift_rate": self.drift_rate,
            "last_sync_time": self.last_sync_time,
            "saved_at": time.time(),
        }
        
        try:
            with open(CLOCK_STATE_FILE, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"Failed to save clock state: {str(e)}")
    
    def _load_state(self):
        """Load the saved state from disk."""
        try:
            if os.path.exists(CLOCK_STATE_FILE):
                with open(CLOCK_STATE_FILE, 'r') as f:
                    state = json.load(f)
                
                # Only load state if it's recent (< 1 day old)
                if time.time() - state.get("saved_at", 0) < 86400:
                    self.offset = state.get("offset", 0.0)
                    self.drift_rate = state.get("drift_rate", 0.0)
                    self.last_sync_time = state.get("last_sync_time", 0.0)
                    logger.info(f"Loaded clock state: offset={self.offset:.6f}s, drift={self.drift_rate * 86400 * 1000:.2f}ms/day")
                else:
                    logger.info("Saved clock state is too old, will resynchronize")
        except Exception as e:
            logger.warning(f"Failed to load clock state: {str(e)}")

# Singleton instance
_global_clock: Optional[ReferenceClock] = None

def get_global_clock() -> ReferenceClock:
    """
    Get or create the global reference clock.
    
    Returns:
        ReferenceClock: The global clock instance
    """
    global _global_clock
    if _global_clock is None:
        _global_clock = ReferenceClock()
    return _global_clock

def get_time() -> float:
    """
    Get the current reference time.
    
    Returns:
        float: Current reference time in seconds since epoch
    """
    return get_global_clock().get_time()

def get_formatted_time(format_str: str = "%Y-%m-%d %H:%M:%S.%f") -> str:
    """
    Get the current reference time as a formatted string.
    
    Args:
        format_str: The format string to use
        
    Returns:
        str: Formatted reference time
    """
    return get_global_clock().get_formatted_time(format_str)

def sync_clock() -> bool:
    """
    Manually trigger a clock synchronization.
    
    Returns:
        bool: True if sync was successful
    """
    return get_global_clock().sync_once()

if __name__ == "__main__":
    # Test the reference clock
    clock = ReferenceClock()
    
    print("Initial clock status:")
    status = clock.get_status()
    for key, value in status.items():
        print(f"  {key}: {value}")
    
    # Sync with NTP servers
    print("\nSynchronizing with NTP servers...")
    clock.sync_once()
    
    print("\nAfter synchronization:")
    status = clock.get_status()
    for key, value in status.items():
        print(f"  {key}: {value}")
    
    # Show current time
    print(f"\nCurrent reference time: {clock.get_formatted_time()}")
    print(f"Current system time:    {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
    
    # Test offset
    print(f"\nOffset between reference and system time: {clock.offset * 1000:.2f} ms") 