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
import random
from typing import Dict, Optional, Callable, List, Tuple, Any, Union

# Try to import Redis for shared state support
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("reference-clock")

# Constants
UPDATE_INTERVAL = int(os.getenv("CLOCK_UPDATE_INTERVAL", "3600"))  # How often to sync with NTP servers (seconds)
DEFAULT_NTP_SERVERS = [
    "time.google.com",
    "pool.ntp.org",
    "time.cloudflare.com",
    "time.apple.com",
]
MAX_DRIFT = float(os.getenv("CLOCK_MAX_DRIFT", "0.1"))  # Maximum allowed drift in seconds before correction
CLOCK_STATE_FILE = os.path.expanduser(os.getenv("CLOCK_STATE_FILE", "~/.rainscribe/clock_state.json"))
USE_REDIS = os.getenv("USE_REDIS_FOR_CLOCK", "false").lower() in ("true", "1", "yes")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "rainscribe:")
SYNC_JITTER = int(os.getenv("CLOCK_SYNC_JITTER", "60"))  # Add random jitter (0-60s) to sync interval to avoid thundering herd

class ReferenceClock:
    """
    A centralized reference clock that maintains synchronization with
    external time sources and provides consistent timestamps.
    """
    
    def __init__(self, 
                 ntp_servers: Optional[List[str]] = None,
                 update_interval: int = UPDATE_INTERVAL,
                 auto_sync: bool = True,
                 use_redis: bool = USE_REDIS,
                 redis_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the reference clock.
        
        Args:
            ntp_servers: List of NTP servers to sync with
            update_interval: How often to sync with NTP servers (seconds)
            auto_sync: Whether to automatically sync with NTP servers
            use_redis: Whether to use Redis for shared state
            redis_config: Redis configuration overrides
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
        self.sync_failures = 0
        self.last_successful_sync = 0.0
        
        # Set up Redis connection if requested
        self.use_redis = use_redis and REDIS_AVAILABLE
        self.redis_client = None
        if self.use_redis:
            try:
                redis_opts = {
                    "host": REDIS_HOST,
                    "port": REDIS_PORT,
                    "db": REDIS_DB,
                    "password": REDIS_PASSWORD,
                    "socket_timeout": 5,
                    "socket_connect_timeout": 5,
                    "retry_on_timeout": True
                }
                if redis_config:
                    redis_opts.update(redis_config)
                
                self.redis_client = redis.Redis(**redis_opts)
                # Test connection
                self.redis_client.ping()
                logger.info("Using Redis for clock state persistence")
            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {str(e)}. Falling back to local state file.")
                self.use_redis = False
                self.redis_client = None
        
        if not self.use_redis:
            # Create directory for state file if it doesn't exist
            state_dir = os.path.dirname(CLOCK_STATE_FILE)
            os.makedirs(state_dir, exist_ok=True)
            logger.info(f"Using local file for clock state persistence: {CLOCK_STATE_FILE}")
        
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
            
            # Shuffle servers to avoid hitting the same one first every time
            servers = list(self.ntp_servers)
            random.shuffle(servers)
            
            for server in servers:
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
                self.last_successful_sync = self.last_sync_time
                self.sync_failures = 0
                
                logger.info(f"Clock synchronized. Offset adjusted by {self.offset - old_offset:.6f}s")
                
                # Save state
                self._save_state()
                
                # Notify listeners
                self._notify_listeners(self.offset)
                
                return True
            else:
                logger.warning("Failed to synchronize with any NTP servers")
                self.sync_failures += 1
                # Save the failure state
                self._save_state()
                
                # If we've failed too many times in a row, we should alert but not too severely
                if self.sync_failures % 10 == 0:  # Only log every 10 failures to reduce noise
                    logger.warning(f"Failed to sync clock {self.sync_failures} times in a row!")
                    # But don't block the application, just continue with the current time
                
                return False
    
    def start_sync(self):
        """Start the automatic synchronization thread."""
        if self.running:
            return
        
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="clock-sync")
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
            "last_successful_sync": self.last_successful_sync,
            "time_since_sync": now - self.last_sync_time if self.last_sync_time > 0 else None,
            "sync_failures": self.sync_failures,
            "current_system_time": now,
            "current_reference_time": self.get_time(),
            "auto_sync_active": self.running,
            "using_redis": self.use_redis,
        }
    
    def _sync_loop(self):
        """Internal loop for automatic synchronization."""
        # Initial sync
        self.sync_once()
        
        while self.running:
            # Sleep until next sync time with jitter to avoid all instances syncing at once
            jitter = random.randint(0, SYNC_JITTER) if SYNC_JITTER > 0 else 0
            time.sleep(self.update_interval + jitter)
            
            if not self.running:
                break
                
            # Perform sync
            self.sync_once()
    
    def _notify_listeners(self, offset: float):
        """
        Notify listeners of a change in offset.
        
        Args:
            offset: The new offset
        """
        for listener in self.listeners:
            try:
                listener(offset)
            except Exception as e:
                logger.error(f"Error in clock listener: {str(e)}")
    
    def _save_state(self):
        """Save the current state to persistent storage."""
        state = {
            "offset": self.offset,
            "drift_rate": self.drift_rate,
            "last_sync_time": self.last_sync_time,
            "last_successful_sync": self.last_successful_sync,
            "sync_failures": self.sync_failures,
            "version": 2,  # Increment version when changing format
        }
        
        if self.use_redis and self.redis_client:
            try:
                self.redis_client.set(f"{REDIS_KEY_PREFIX}clock_state", json.dumps(state))
                logger.debug("Saved clock state to Redis")
                return
            except Exception as e:
                logger.warning(f"Failed to save clock state to Redis: {str(e)}. Falling back to file.")
        
        # Fallback to file
        try:
            # Write to temporary file first to avoid corruption
            temp_file = f"{CLOCK_STATE_FILE}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(state, f)
            
            # Atomic replace
            os.replace(temp_file, CLOCK_STATE_FILE)
            logger.debug("Saved clock state to file")
        except Exception as e:
            logger.error(f"Failed to save clock state to file: {str(e)}")
    
    def _load_state(self):
        """Load state from persistent storage."""
        state = None
        
        # Try Redis first if enabled
        if self.use_redis and self.redis_client:
            try:
                state_json = self.redis_client.get(f"{REDIS_KEY_PREFIX}clock_state")
                if state_json:
                    state = json.loads(state_json)
                    logger.info("Loaded clock state from Redis")
            except Exception as e:
                logger.warning(f"Failed to load clock state from Redis: {str(e)}. Trying file.")
        
        # Fallback to file if Redis failed or not enabled
        if not state and os.path.exists(CLOCK_STATE_FILE):
            try:
                with open(CLOCK_STATE_FILE, 'r') as f:
                    state = json.load(f)
                logger.info("Loaded clock state from file")
            except Exception as e:
                logger.warning(f"Failed to load clock state from file: {str(e)}")
        
        # Apply state if loaded successfully
        if state:
            self.offset = state.get("offset", 0.0)
            self.drift_rate = state.get("drift_rate", 0.0)
            self.last_sync_time = state.get("last_sync_time", 0.0)
            self.last_successful_sync = state.get("last_successful_sync", self.last_sync_time)
            self.sync_failures = state.get("sync_failures", 0)
            
            # Check how old the state is
            time_since_sync = time.time() - self.last_sync_time
            if time_since_sync > UPDATE_INTERVAL * 3:
                logger.warning(
                    f"Clock state is very old ({time_since_sync/3600:.1f} hours). "
                    "Consider performing a sync soon."
                )
            else:
                logger.info(f"Restored clock offset: {self.offset:.6f}s, drift rate: {self.drift_rate * 86400 * 1000:.2f} ms/day")

# Singleton instance
_global_clock: Optional[ReferenceClock] = None

def get_global_clock() -> ReferenceClock:
    """
    Get or create the global reference clock instance.
    
    Returns:
        ReferenceClock: The global clock instance
    """
    global _global_clock
    if _global_clock is None:
        _global_clock = ReferenceClock()
    return _global_clock

def get_time() -> float:
    """
    Get the current reference time from the global clock.
    
    Returns:
        float: Current reference time in seconds since epoch
    """
    return get_global_clock().get_time()

def get_formatted_time(format_str: str = "%Y-%m-%d %H:%M:%S.%f") -> str:
    """
    Get the current reference time as a formatted string from the global clock.
    
    Args:
        format_str: The format string to use
        
    Returns:
        str: Formatted reference time
    """
    return get_global_clock().get_formatted_time(format_str)

def sync_clock() -> bool:
    """
    Synchronize the global clock with NTP servers.
    
    Returns:
        bool: True if sync was successful
    """
    return get_global_clock().sync_once()

if __name__ == "__main__":
    # Test the reference clock
    logging.basicConfig(level=logging.INFO)
    clock = get_global_clock()
    print(f"Initial reference time: {get_formatted_time()}")
    
    # Sync with NTP servers
    sync_result = sync_clock()
    print(f"Sync result: {sync_result}")
    
    # Print updated time
    print(f"Updated reference time: {get_formatted_time()}")
    
    # Print clock status
    status = clock.get_status()
    print(f"Clock offset: {status['offset']:.6f}s")
    print(f"Drift rate: {status['drift_ms_per_day']:.2f} ms/day") 