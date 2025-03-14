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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("offset-calculator")

# Default configuration
DEFAULT_WINDOW_SIZE = 20  # Number of measurements to keep for smoothing
DEFAULT_ALPHA = 0.2  # EMA weight for new measurements (lower = more smoothing)
DEFAULT_OUTLIER_THRESHOLD = 3.0  # Standard deviations for outlier detection
MIN_MEASUREMENTS = 5  # Minimum measurements needed for reliable calculation

class OffsetCalculator:
    """
    Calculates and maintains a smoothed offset value between audio transcription
    and video playback times.
    """
    
    def __init__(self, 
                 window_size: int = DEFAULT_WINDOW_SIZE,
                 alpha: float = DEFAULT_ALPHA,
                 outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
                 initial_offset: float = 0.0):
        """
        Initialize the offset calculator.
        
        Args:
            window_size: Number of recent measurements to keep
            alpha: Weight for new measurements in EMA calculation (0-1)
            outlier_threshold: Threshold for outlier detection (in std deviations)
            initial_offset: Initial offset value
        """
        self.window_size = window_size
        self.alpha = alpha
        self.outlier_threshold = outlier_threshold
        self.current_offset = initial_offset
        self.ema_offset = initial_offset
        self.measurements: Deque[float] = deque(maxlen=window_size)
        self.last_update_time = time.time()
        
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
        if self.measurements and len(self.measurements) >= MIN_MEASUREMENTS:
            mean = np.mean(self.measurements)
            std = np.std(self.measurements)
            if std > 0 and abs(measured_latency - mean) > (std * self.outlier_threshold):
                logger.warning(f"Outlier detected: {measured_latency:.2f}s (mean: {mean:.2f}s, std: {std:.2f}s)")
                return self.current_offset
        
        # Add measurement to history
        self.measurements.append(measured_latency)
        
        # Calculate smoothed offset if we have enough measurements
        if len(self.measurements) >= MIN_MEASUREMENTS:
            # Calculate mean and median for comparison
            mean_offset = np.mean(self.measurements)
            median_offset = np.median(self.measurements)
            
            # Update EMA (Exponential Moving Average)
            if self.ema_offset is None:
                self.ema_offset = mean_offset
            else:
                self.ema_offset = (self.alpha * measured_latency) + ((1 - self.alpha) * self.ema_offset)
            
            # Weighted combination of EMA and median for robustness
            # This balances responsiveness with stability
            self.current_offset = (0.7 * self.ema_offset) + (0.3 * median_offset)
            
            logger.info(f"Updated offset: {self.current_offset:.2f}s (mean: {mean_offset:.2f}s, median: {median_offset:.2f}s, EMA: {self.ema_offset:.2f}s)")
        else:
            # Not enough measurements yet, use simple average
            self.current_offset = np.mean(self.measurements)
            logger.info(f"Initial offset calculation: {self.current_offset:.2f}s (from {len(self.measurements)} measurements)")
        
        self.last_update_time = time.time()
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
            "last_update_time": self.last_update_time,
            "last_update_age": time.time() - self.last_update_time
        }
        
        if self.measurements:
            stats.update({
                "mean_offset": np.mean(self.measurements),
                "median_offset": np.median(self.measurements),
                "min_offset": min(self.measurements),
                "max_offset": max(self.measurements),
                "std_offset": np.std(self.measurements)
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
            "config": {
                "window_size": self.window_size,
                "alpha": self.alpha,
                "outlier_threshold": self.outlier_threshold
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
            initial_offset=state.get("current_offset", 0.0)
        )
        
        calculator.ema_offset = state.get("ema_offset", calculator.current_offset)
        calculator.measurements = deque(state.get("measurements", []), maxlen=calculator.window_size)
        calculator.last_update_time = state.get("last_update_time", time.time())
        
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

if __name__ == "__main__":
    # Test the offset calculator
    calculator = OffsetCalculator()
    
    # Simulate some measurements with noise
    base_latency = 2.5
    for i in range(30):
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
            print(f"  {key}: {value:.2f}")
        else:
            print(f"  {key}: {value}")
    
    # Test serialization
    json_str = calculator.to_json()
    print(f"\nJSON Representation:\n{json_str}")
    
    # Test deserialization
    new_calculator = OffsetCalculator.from_json(json_str)
    print(f"\nDeserialized Offset: {new_calculator.get_current_offset():.2f}") 