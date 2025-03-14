#!/usr/bin/env python3
"""
Offset Calculator Module

This module provides functions for calculating and smoothing timing offsets
between audio transcription and video playback.
"""

import os
import time
import json
import logging
import numpy as np
from datetime import datetime
from collections import deque
from typing import Dict, List, Deque, Optional, Any

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
logger = logging.getLogger("offset-calculator")

# Default configuration - now use environment variables with defaults
DEFAULT_WINDOW_SIZE = int(os.getenv("OFFSET_WINDOW_SIZE", "30"))  # Number of measurements to keep for smoothing
DEFAULT_ALPHA = float(os.getenv("OFFSET_EMA_ALPHA", "0.15"))  # EMA weight for new measurements (lower = more smoothing)
DEFAULT_OUTLIER_THRESHOLD = float(os.getenv("OFFSET_OUTLIER_THRESHOLD", "2.5"))  # Standard deviations for outlier detection
DEFAULT_MEDIAN_WEIGHT = float(os.getenv("OFFSET_MEDIAN_WEIGHT", "0.4"))  # Weight of median in final calculation (higher = more stable)
MIN_MEASUREMENTS = int(os.getenv("OFFSET_MIN_MEASUREMENTS", "5"))  # Minimum measurements needed for reliable calculation
USE_REDIS = os.getenv("USE_REDIS_FOR_OFFSET", "false").lower() in ("true", "1", "yes")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "rainscribe:")
STATE_FILE = os.path.expanduser(os.getenv("OFFSET_STATE_FILE", "~/.rainscribe/offset_state.json"))

class OffsetCalculator:
    """
    Calculates and maintains a smoothed offset value between audio transcription
    and video playback times.
    """
    
    def __init__(self, 
                 window_size: int = DEFAULT_WINDOW_SIZE,
                 alpha: float = DEFAULT_ALPHA,
                 outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
                 median_weight: float = DEFAULT_MEDIAN_WEIGHT,
                 initial_offset: float = 0.0,
                 use_redis: bool = USE_REDIS,
                 redis_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the offset calculator.
        
        Args:
            window_size: Number of recent measurements to keep
            alpha: Weight for new measurements in EMA calculation (0-1)
            outlier_threshold: Threshold for outlier detection (in std deviations)
            median_weight: Weight to apply to median in final calculation (0-1)
            initial_offset: Initial offset value
            use_redis: Whether to use Redis for state persistence
            redis_config: Redis configuration overrides
        """
        self.window_size = window_size
        self.alpha = alpha
        self.outlier_threshold = outlier_threshold
        self.median_weight = median_weight
        self.current_offset = initial_offset
        self.ema_offset = initial_offset
        self.measurements: Deque[float] = deque(maxlen=window_size)
        self.last_update_time = time.time()
        self.update_count = 0
        self.outlier_count = 0
        
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
                logger.info("Using Redis for offset calculator state persistence")
            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {str(e)}. Falling back to local state file.")
                self.use_redis = False
                self.redis_client = None
        
        if not self.use_redis:
            # Create directory for state file if it doesn't exist
            state_dir = os.path.dirname(STATE_FILE)
            os.makedirs(state_dir, exist_ok=True)
            logger.info(f"Using local file for offset calculator state persistence: {STATE_FILE}")
        
        # Try to load saved state
        self._load_state()
        
    def add_measurement(self, measured_latency: float) -> float:
        """
        Add a new latency measurement and update the calculated offset.
        
        Args:
            measured_latency: The measured latency from transcription timestamp
                             to processing time
                             
        Returns:
            float: The updated offset value
        """
        # Check if the measurement is an outlier
        is_outlier = False
        if self.measurements and len(self.measurements) >= MIN_MEASUREMENTS:
            mean = np.mean(self.measurements)
            std = np.std(self.measurements)
            if std > 0 and abs(measured_latency - mean) > (std * self.outlier_threshold):
                logger.warning(f"Outlier detected: {measured_latency:.2f}s (mean: {mean:.2f}s, std: {std:.2f}s)")
                self.outlier_count += 1
                is_outlier = True
        
        # Only add non-outliers to the measurement history
        if not is_outlier:
            # Add measurement to history
            self.measurements.append(measured_latency)
            self.update_count += 1
        
        # Calculate smoothed offset if we have enough measurements
        if len(self.measurements) >= MIN_MEASUREMENTS:
            # Calculate basic statistics
            mean_offset = np.mean(self.measurements)
            median_offset = np.median(self.measurements)
            
            # Update EMA (Exponential Moving Average) - but only if not an outlier
            if not is_outlier:
                if self.ema_offset is None:
                    self.ema_offset = mean_offset
                else:
                    self.ema_offset = (self.alpha * measured_latency) + ((1 - self.alpha) * self.ema_offset)
            
            # Weighted combination of EMA and median for robustness
            # This balances responsiveness with stability
            # More weight to median = more stable but slower to respond to changes
            # More weight to EMA = more responsive but more susceptible to noise
            self.current_offset = ((1 - self.median_weight) * self.ema_offset) + (self.median_weight * median_offset)
            
            logger.info(f"Updated offset: {self.current_offset:.3f}s (mean: {mean_offset:.3f}s, median: {median_offset:.3f}s, EMA: {self.ema_offset:.3f}s)")
        else:
            # Not enough measurements yet, use simple average
            self.current_offset = np.mean(self.measurements)
            logger.info(f"Initial offset calculation: {self.current_offset:.3f}s (from {len(self.measurements)} measurements)")
        
        self.last_update_time = time.time()
        
        # Save state periodically - every 5 non-outlier updates or when we have enough measurements
        if (not is_outlier and self.update_count % 5 == 0) or len(self.measurements) == MIN_MEASUREMENTS:
            self._save_state()
            
        return self.current_offset
    
    def get_current_offset(self) -> float:
        """
        Get the current calculated offset value.
        
        Returns:
            float: The current offset
        """
        return self.current_offset
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the offset calculations.
        
        Returns:
            Dict: Statistics including current offset, number of measurements, etc.
        """
        stats = {
            "current_offset": self.current_offset,
            "ema_offset": self.ema_offset,
            "measurement_count": len(self.measurements),
            "update_count": self.update_count,
            "outlier_count": self.outlier_count,
            "last_update_time": self.last_update_time,
            "last_update_age": time.time() - self.last_update_time,
            "using_redis": self.use_redis,
            "window_size": self.window_size,
            "alpha": self.alpha,
            "outlier_threshold": self.outlier_threshold,
            "median_weight": self.median_weight
        }
        
        if self.measurements:
            stats.update({
                "mean_offset": np.mean(self.measurements),
                "median_offset": np.median(self.measurements),
                "min_offset": min(self.measurements),
                "max_offset": max(self.measurements),
                "std_offset": np.std(self.measurements),
                "latest_measurement": self.measurements[-1] if self.measurements else None
            })
        
        return stats

    def reset(self, initial_offset: float = 0.0):
        """
        Reset the calculator to its initial state.
        
        Args:
            initial_offset: New initial offset value
        """
        self.current_offset = initial_offset
        self.ema_offset = initial_offset
        self.measurements.clear()
        self.last_update_time = time.time()
        self.update_count = 0
        self.outlier_count = 0
        
        # Save state to reflect reset
        self._save_state()
    
    def _save_state(self):
        """Save the current state to persistent storage."""
        state = {
            "current_offset": self.current_offset,
            "ema_offset": self.ema_offset,
            "measurements": list(self.measurements),
            "last_update_time": self.last_update_time,
            "update_count": self.update_count,
            "outlier_count": self.outlier_count,
            "config": {
                "window_size": self.window_size,
                "alpha": self.alpha,
                "outlier_threshold": self.outlier_threshold,
                "median_weight": self.median_weight
            },
            "version": 2  # Increment version when changing format
        }
        
        if self.use_redis and self.redis_client:
            try:
                self.redis_client.set(f"{REDIS_KEY_PREFIX}offset_state", json.dumps(state))
                logger.debug("Saved offset calculator state to Redis")
                return
            except Exception as e:
                logger.warning(f"Failed to save offset state to Redis: {str(e)}. Falling back to file.")
        
        # Fallback to file
        try:
            # Write to temporary file first to avoid corruption
            temp_file = f"{STATE_FILE}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(state, f)
            
            # Atomic replace
            os.replace(temp_file, STATE_FILE)
            logger.debug("Saved offset calculator state to file")
        except Exception as e:
            logger.error(f"Failed to save offset state to file: {str(e)}")
    
    def _load_state(self):
        """Load state from persistent storage."""
        state = None
        
        # Try Redis first if enabled
        if self.use_redis and self.redis_client:
            try:
                state_json = self.redis_client.get(f"{REDIS_KEY_PREFIX}offset_state")
                if state_json:
                    state = json.loads(state_json)
                    logger.info("Loaded offset calculator state from Redis")
            except Exception as e:
                logger.warning(f"Failed to load offset state from Redis: {str(e)}. Trying file.")
        
        # Fallback to file if Redis failed or not enabled
        if not state and os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                logger.info("Loaded offset calculator state from file")
            except Exception as e:
                logger.warning(f"Failed to load offset state from file: {str(e)}")
        
        # Apply state if loaded successfully
        if state:
            # Apply configuration only if version is compatible
            self.current_offset = state.get("current_offset", self.current_offset)
            self.ema_offset = state.get("ema_offset", self.ema_offset)
            self.last_update_time = state.get("last_update_time", self.last_update_time)
            self.update_count = state.get("update_count", 0)
            self.outlier_count = state.get("outlier_count", 0)
            
            # Load measurements
            measurements = state.get("measurements", [])
            # Create a new deque with our window size and extend with saved measurements
            self.measurements = deque(maxlen=self.window_size)
            self.measurements.extend(measurements[-self.window_size:])  # Only keep up to window_size most recent
            
            # Apply config if compatible
            config = state.get("config", {})
            if config.get("window_size") != self.window_size:
                logger.info(f"Window size changed from {config.get('window_size')} to {self.window_size}")
            
            # Check how old the state is
            time_since_update = time.time() - self.last_update_time
            if time_since_update > 3600:  # 1 hour
                logger.warning(
                    f"Offset calculator state is old ({time_since_update/3600:.1f} hours old). "
                    "Recent measurements may not reflect current conditions."
                )
            else:
                logger.info(f"Restored offset: {self.current_offset:.3f}s with {len(self.measurements)} measurements")

    def to_json(self) -> str:
        """
        Serialize the current state to JSON.
        
        Returns:
            str: JSON representation of the calculator state
        """
        state = {
            "current_offset": self.current_offset,
            "ema_offset": self.ema_offset,
            "measurements": list(self.measurements),
            "last_update_time": self.last_update_time,
            "update_count": self.update_count,
            "outlier_count": self.outlier_count,
            "config": {
                "window_size": self.window_size,
                "alpha": self.alpha,
                "outlier_threshold": self.outlier_threshold,
                "median_weight": self.median_weight
            }
        }
        return json.dumps(state)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'OffsetCalculator':
        """
        Create an OffsetCalculator from a JSON string.
        
        Args:
            json_str: JSON representation of calculator state
            
        Returns:
            OffsetCalculator: Reconstructed calculator
        """
        state = json.loads(json_str)
        config = state.get("config", {})
        
        calculator = cls(
            window_size=config.get("window_size", DEFAULT_WINDOW_SIZE),
            alpha=config.get("alpha", DEFAULT_ALPHA),
            outlier_threshold=config.get("outlier_threshold", DEFAULT_OUTLIER_THRESHOLD),
            median_weight=config.get("median_weight", DEFAULT_MEDIAN_WEIGHT),
            initial_offset=state.get("current_offset", 0.0)
        )
        
        calculator.ema_offset = state.get("ema_offset", calculator.current_offset)
        calculator.update_count = state.get("update_count", 0)
        calculator.outlier_count = state.get("outlier_count", 0)
        calculator.last_update_time = state.get("last_update_time", time.time())
        
        # Initialize measurements
        measurements = state.get("measurements", [])
        calculator.measurements = deque(maxlen=calculator.window_size)
        calculator.measurements.extend(measurements[-calculator.window_size:])
        
        return calculator

# Singleton instance for global use
_global_calculator: Optional[OffsetCalculator] = None

def get_global_calculator() -> OffsetCalculator:
    """
    Get or create the global offset calculator instance.
    
    Returns:
        OffsetCalculator: The global calculator instance
    """
    global _global_calculator
    if _global_calculator is None:
        _global_calculator = OffsetCalculator()
    return _global_calculator

def add_measurement(measured_latency: float) -> float:
    """
    Add a measurement to the global calculator.
    
    Args:
        measured_latency: The measured latency
        
    Returns:
        float: The updated offset
    """
    return get_global_calculator().add_measurement(measured_latency)

def get_current_offset() -> float:
    """
    Get the current offset from the global calculator.
    
    Returns:
        float: The current offset
    """
    return get_global_calculator().get_current_offset()

def get_offset_stats() -> Dict[str, Any]:
    """
    Get statistics from the global calculator.
    
    Returns:
        Dict[str, Any]: Offset statistics
    """
    return get_global_calculator().get_stats()

def reset_offset_calculator(initial_offset: float = 0.0) -> None:
    """
    Reset the global offset calculator.
    
    Args:
        initial_offset: Initial offset value to use
    """
    get_global_calculator().reset(initial_offset)

if __name__ == "__main__":
    # Test the offset calculator
    logging.basicConfig(level=logging.INFO)
    calculator = OffsetCalculator()
    
    # Simulate some measurements with noise
    base_latency = 2.5
    for i in range(40):
        # Add some noise and occasional outliers
        noise = np.random.normal(0, 0.2)
        outlier = (np.random.random() > 0.9) * np.random.choice([-2, 2])
        latency = base_latency + noise + outlier
        
        offset = calculator.add_measurement(latency)
        print(f"Measurement {i+1}: {latency:.2f}s -> Offset: {offset:.2f}s")
    
    # Print final statistics
    stats = calculator.get_stats()
    print("\nFinal Statistics:")
    for key, value in stats.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.3f}")
        else:
            print(f"  {key}: {value}") 