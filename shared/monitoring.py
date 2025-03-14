#!/usr/bin/env python3
"""
Monitoring Module

This module provides tools for monitoring system performance, 
synchronization metrics, and health status.
"""

import os
import time
import json
import threading
import logging
import socket
import platform
import psutil
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable, Union, Tuple
from collections import deque, defaultdict

# Configure logging
from shared.logging_config import configure_logging

# Get a logger for this module
logger = configure_logging("monitoring")

# Constants
DEFAULT_METRICS_DIR = os.environ.get("METRICS_DIR", os.path.expanduser("~/.rainscribe/metrics"))
DEFAULT_SAVE_INTERVAL = 60  # Save metrics every minute
DEFAULT_HISTORY_SIZE = 3600  # Keep 1 hour of per-second data
DEFAULT_AGGREGATION_PERIODS = [60, 300, 900, 3600]  # 1min, 5min, 15min, 1hour

# Ensure metrics directory exists
os.makedirs(DEFAULT_METRICS_DIR, exist_ok=True)

class MetricsAggregator:
    """Collects and aggregates metrics over time."""
    
    def __init__(self, 
                 name: str,
                 history_size: int = DEFAULT_HISTORY_SIZE,
                 aggregation_periods: List[int] = None):
        """
        Initialize the metrics aggregator.
        
        Args:
            name: Name of this metrics collection
            history_size: How many time points to keep in history
            aggregation_periods: Time periods (in seconds) to aggregate over
        """
        self.name = name
        self.history_size = history_size
        self.aggregation_periods = aggregation_periods or DEFAULT_AGGREGATION_PERIODS
        
        # Raw metrics storage - one value per second
        self.metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=history_size))
        self.timestamps = deque(maxlen=history_size)
        
        # Aggregated metrics
        self.aggregated: Dict[int, Dict[str, Dict]] = {
            period: {} for period in self.aggregation_periods
        }
        
        # Last update time
        self.last_update_time = 0
    
    def add_metric(self, name: str, value: Union[float, int], timestamp: Optional[float] = None) -> None:
        """
        Add a metric value.
        
        Args:
            name: Name of the metric
            value: Value of the metric
            timestamp: Optional timestamp (defaults to current time)
        """
        timestamp = timestamp or time.time()
        self.metrics[name].append((timestamp, value))
        
        # Add timestamp if it doesn't exist yet
        if not self.timestamps or self.timestamps[-1] != timestamp:
            self.timestamps.append(timestamp)
        
        self.last_update_time = time.time()
    
    def add_metrics(self, metrics: Dict[str, Union[float, int]], timestamp: Optional[float] = None) -> None:
        """
        Add multiple metrics at once.
        
        Args:
            metrics: Dictionary of metric name to value
            timestamp: Optional timestamp (defaults to current time)
        """
        timestamp = timestamp or time.time()
        
        for name, value in metrics.items():
            self.add_metric(name, value, timestamp)
    
    def get_latest(self, metric_name: str) -> Optional[Tuple[float, Union[float, int]]]:
        """
        Get the latest value for a metric.
        
        Args:
            metric_name: Name of the metric
            
        Returns:
            Optional[Tuple]: (timestamp, value) or None if no data
        """
        if metric_name in self.metrics and self.metrics[metric_name]:
            return self.metrics[metric_name][-1]
        return None
    
    def get_history(self, metric_name: str, 
                   count: Optional[int] = None, 
                   start_time: Optional[float] = None,
                   end_time: Optional[float] = None) -> List[Tuple[float, Union[float, int]]]:
        """
        Get historical values for a metric.
        
        Args:
            metric_name: Name of the metric
            count: Number of values to return (from most recent)
            start_time: Start time filter (inclusive)
            end_time: End time filter (inclusive)
            
        Returns:
            List[Tuple]: List of (timestamp, value) pairs
        """
        if metric_name not in self.metrics:
            return []
        
        values = list(self.metrics[metric_name])
        
        # Apply time filters if provided
        if start_time is not None or end_time is not None:
            start_time = start_time or 0
            end_time = end_time or float('inf')
            values = [(ts, val) for ts, val in values if start_time <= ts <= end_time]
        
        # Apply count limit
        if count is not None and count > 0:
            values = values[-count:]
        
        return values
    
    def aggregate_metrics(self) -> Dict:
        """
        Aggregate metrics over different time periods.
        
        Returns:
            Dict: Aggregated metrics
        """
        now = time.time()
        result = {}
        
        for period in self.aggregation_periods:
            period_start = now - period
            result[period] = {}
            
            for metric_name, values in self.metrics.items():
                # Filter values within the period
                period_values = [val for ts, val in values if ts >= period_start]
                
                if not period_values:
                    continue
                
                # Calculate aggregates
                result[period][metric_name] = {
                    "count": len(period_values),
                    "min": min(period_values),
                    "max": max(period_values),
                    "avg": sum(period_values) / len(period_values),
                    "last": period_values[-1]
                }
                
                # Add standard deviation if we have enough values
                if len(period_values) > 1:
                    mean = result[period][metric_name]["avg"]
                    variance = sum((x - mean) ** 2 for x in period_values) / len(period_values)
                    result[period][metric_name]["std"] = variance ** 0.5
        
        self.aggregated = result
        return result
    
    def get_summary(self) -> Dict:
        """
        Get a summary of all metrics.
        
        Returns:
            Dict: Summary of all metrics
        """
        # Aggregate first
        self.aggregate_metrics()
        
        return {
            "name": self.name,
            "metrics_count": len(self.metrics),
            "last_update_time": self.last_update_time,
            "data_points": sum(len(values) for values in self.metrics.values()),
            "aggregated": self.aggregated,
            "latest": {name: values[-1][1] if values else None 
                      for name, values in self.metrics.items()}
        }
    
    def to_dict(self) -> Dict:
        """
        Convert all metrics to a dictionary.
        
        Returns:
            Dict: Dictionary representation of metrics
        """
        return {
            "name": self.name,
            "timestamp": time.time(),
            "metrics": {name: list(values) for name, values in self.metrics.items()},
            "aggregated": self.aggregate_metrics()
        }
    
    def to_json(self, pretty: bool = False) -> str:
        """
        Convert all metrics to a JSON string.
        
        Args:
            pretty: Whether to format the JSON with indentation
            
        Returns:
            str: JSON representation of metrics
        """
        indent = 2 if pretty else None
        return json.dumps(self.to_dict(), indent=indent)
    
    def save(self, directory: str = DEFAULT_METRICS_DIR, filename: Optional[str] = None) -> str:
        """
        Save metrics to a file.
        
        Args:
            directory: Directory to save the file in
            filename: Filename to use (defaults to name-timestamp.json)
            
        Returns:
            str: Path to the saved file
        """
        os.makedirs(directory, exist_ok=True)
        
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{self.name}-{timestamp}.json"
        
        filepath = os.path.join(directory, filename)
        
        with open(filepath, 'w') as f:
            f.write(self.to_json(pretty=True))
        
        logger.debug(f"Saved metrics to {filepath}")
        return filepath
    
    @classmethod
    def load(cls, filepath: str) -> 'MetricsAggregator':
        """
        Load metrics from a file.
        
        Args:
            filepath: Path to the file
            
        Returns:
            MetricsAggregator: Loaded metrics
        """
        with open(filepath, 'r') as f:
            data = json.load(f)
        
        name = data.get("name", os.path.basename(filepath).split('.')[0])
        aggregator = cls(name)
        
        # Load metrics
        for metric_name, values in data.get("metrics", {}).items():
            for timestamp, value in values:
                aggregator.add_metric(metric_name, value, timestamp)
        
        return aggregator

class SystemMonitor:
    """Monitors system resource usage."""
    
    def __init__(self, 
                 collect_interval: int = 60, 
                 metrics: Optional[MetricsAggregator] = None):
        """
        Initialize the system monitor.
        
        Args:
            collect_interval: How often to collect metrics (seconds)
            metrics: Optional metrics aggregator to use
        """
        self.collect_interval = collect_interval
        self.metrics = metrics or MetricsAggregator("system")
        self.running = False
        self.collector_thread = None
        self.system_info = self._get_system_info()
    
    def _get_system_info(self) -> Dict:
        """
        Get system information.
        
        Returns:
            Dict: System information
        """
        info = {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "memory_total": psutil.virtual_memory().total,
            "boot_time": psutil.boot_time(),
        }
        
        try:
            # Try to get more detailed CPU info
            import cpuinfo
            cpu_info = cpuinfo.get_cpu_info()
            info["cpu_brand"] = cpu_info.get("brand_raw")
            info["cpu_hz"] = cpu_info.get("hz_advertised_raw")
        except ImportError:
            logger.debug("cpuinfo module not available, skipping detailed CPU info")
        
        return info
    
    def collect_metrics(self) -> Dict:
        """
        Collect system metrics.
        
        Returns:
            Dict: Collected metrics
        """
        cpu_percent = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        metrics = {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "memory_used": memory.used,
            "disk_percent": disk.percent,
            "disk_used": disk.used,
        }
        
        # Add network I/O stats
        net_io = psutil.net_io_counters()
        metrics.update({
            "net_bytes_sent": net_io.bytes_sent,
            "net_bytes_recv": net_io.bytes_recv,
        })
        
        # Add detailed CPU stats
        per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)
        for i, percent in enumerate(per_cpu):
            metrics[f"cpu_{i}_percent"] = percent
        
        # Add to metrics aggregator
        self.metrics.add_metrics(metrics)
        
        return metrics
    
    def start_collecting(self):
        """Start the automatic collection thread."""
        if self.running:
            return
        
        self.running = True
        self.collector_thread = threading.Thread(target=self._collection_loop, daemon=True)
        self.collector_thread.start()
    
    def stop_collecting(self):
        """Stop the automatic collection thread."""
        self.running = False
        if self.collector_thread:
            self.collector_thread.join(timeout=1.0)
            self.collector_thread = None
    
    def _collection_loop(self):
        """Internal loop for automatic collection."""
        while self.running:
            try:
                self.collect_metrics()
            except Exception as e:
                logger.error(f"Error collecting system metrics: {e}")
            
            # Sleep until next collection time
            time.sleep(self.collect_interval)
    
    def get_status(self) -> Dict:
        """
        Get current system status.
        
        Returns:
            Dict: System status
        """
        return {
            "system_info": self.system_info,
            "current_metrics": self.collect_metrics(),
            "aggregated": self.metrics.aggregate_metrics()
        }

class SyncMetricsCollector:
    """Collects metrics about subtitle synchronization."""
    
    def __init__(self, metrics: Optional[MetricsAggregator] = None):
        """
        Initialize the sync metrics collector.
        
        Args:
            metrics: Optional metrics aggregator to use
        """
        self.metrics = metrics or MetricsAggregator("sync")
    
    def record_latency(self, latency: float, source: str = "default") -> None:
        """
        Record a latency measurement.
        
        Args:
            latency: Latency in seconds
            source: Source of the measurement
        """
        self.metrics.add_metric(f"latency.{source}", latency)
    
    def record_offset(self, offset: float, source: str = "default") -> None:
        """
        Record an offset measurement.
        
        Args:
            offset: Offset in seconds
            source: Source of the measurement
        """
        self.metrics.add_metric(f"offset.{source}", offset)
    
    def record_processing_time(self, duration: float, operation: str) -> None:
        """
        Record the time taken for an operation.
        
        Args:
            duration: Duration in seconds
            operation: Name of the operation
        """
        self.metrics.add_metric(f"processing_time.{operation}", duration)
    
    def record_error(self, error_type: str) -> None:
        """
        Record an error.
        
        Args:
            error_type: Type of the error
        """
        # Just count errors
        error_counts = self.metrics.get_latest(f"error_count.{error_type}")
        count = 1
        if error_counts:
            _, count = error_counts
            count += 1
        
        self.metrics.add_metric(f"error_count.{error_type}", count)
    
    def record_health_check(self, service: str, is_healthy: bool) -> None:
        """
        Record a health check result.
        
        Args:
            service: Name of the service
            is_healthy: Whether the service is healthy
        """
        self.metrics.add_metric(f"health.{service}", 1 if is_healthy else 0)
    
    def get_summary(self) -> Dict:
        """
        Get a summary of the sync metrics.
        
        Returns:
            Dict: Summary of the metrics
        """
        return self.metrics.get_summary()
    
    def save(self, directory: str = DEFAULT_METRICS_DIR) -> str:
        """
        Save metrics to a file.
        
        Args:
            directory: Directory to save the file in
            
        Returns:
            str: Path to the saved file
        """
        return self.metrics.save(directory)

class MetricsManager:
    """Manages all metrics collectors and provides a central interface."""
    
    def __init__(self, 
                 save_interval: int = DEFAULT_SAVE_INTERVAL,
                 metrics_dir: str = DEFAULT_METRICS_DIR,
                 auto_save: bool = True):
        """
        Initialize the metrics manager.
        
        Args:
            save_interval: How often to save metrics (seconds)
            metrics_dir: Directory to save metrics in
            auto_save: Whether to automatically save metrics
        """
        self.metrics_dir = metrics_dir
        self.save_interval = save_interval
        self.auto_save = auto_save
        
        # Create metrics collectors
        self.system = SystemMonitor(collect_interval=10)
        self.sync = SyncMetricsCollector()
        
        # Create a thread for auto-saving
        self.running = False
        self.save_thread = None
        
        # Start collecting and saving if requested
        os.makedirs(metrics_dir, exist_ok=True)
        self.system.start_collecting()
        
        if auto_save:
            self.start_auto_save()
    
    def start_auto_save(self):
        """Start the automatic save thread."""
        if self.running:
            return
        
        self.running = True
        self.save_thread = threading.Thread(target=self._save_loop, daemon=True)
        self.save_thread.start()
    
    def stop_auto_save(self):
        """Stop the automatic save thread."""
        self.running = False
        if self.save_thread:
            self.save_thread.join(timeout=1.0)
            self.save_thread = None
    
    def save_all(self) -> List[str]:
        """
        Save all metrics.
        
        Returns:
            List[str]: Paths to the saved files
        """
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved_files = []
        
        try:
            saved_files.append(self.system.metrics.save(
                self.metrics_dir, f"system-{timestamp}.json"))
            saved_files.append(self.sync.metrics.save(
                self.metrics_dir, f"sync-{timestamp}.json"))
        except Exception as e:
            logger.error(f"Error saving metrics: {e}")
        
        return saved_files
    
    def get_dashboard_data(self) -> Dict:
        """
        Get data for the monitoring dashboard.
        
        Returns:
            Dict: Dashboard data
        """
        return {
            "timestamp": time.time(),
            "system": self.system.get_status(),
            "sync": self.sync.get_summary()
        }
    
    def _save_loop(self):
        """Internal loop for automatic saving."""
        while self.running:
            try:
                self.save_all()
            except Exception as e:
                logger.error(f"Error in auto save loop: {e}")
            
            # Sleep until next save time
            time.sleep(self.save_interval)
    
    def __del__(self):
        """Clean up when the object is deleted."""
        self.stop_auto_save()
        self.system.stop_collecting()

# Global metrics manager
_global_metrics_manager: Optional[MetricsManager] = None

def get_metrics_manager() -> MetricsManager:
    """
    Get or create the global metrics manager.
    
    Returns:
        MetricsManager: The global metrics manager
    """
    global _global_metrics_manager
    if _global_metrics_manager is None:
        _global_metrics_manager = MetricsManager()
    return _global_metrics_manager

if __name__ == "__main__":
    # Test the monitoring system
    print("Starting monitoring test...")
    manager = get_metrics_manager()
    
    # Record some sync metrics
    for i in range(10):
        # Simulate some processing
        start_time = time.time()
        time.sleep(0.1)  # Simulate work
        duration = time.time() - start_time
        
        # Record metrics
        manager.sync.record_processing_time(duration, "test_operation")
        manager.sync.record_latency(0.5 + (i * 0.01), "test")
        manager.sync.record_offset(0.2 - (i * 0.005), "test")
    
    # Record an error
    manager.sync.record_error("test_error")
    
    # Save metrics
    saved_files = manager.save_all()
    print(f"Saved metrics to: {', '.join(saved_files)}")
    
    # Get dashboard data
    dashboard_data = manager.get_dashboard_data()
    print("\nDashboard data sample:")
    print(f"System CPU: {dashboard_data['system']['current_metrics']['cpu_percent']}%")
    print(f"System Memory: {dashboard_data['system']['current_metrics']['memory_percent']}%")
    
    # Show sync metrics summary
    sync_summary = manager.sync.get_summary()
    print("\nSync metrics summary:")
    for metric, value in sync_summary["latest"].items():
        if value is not None:
            print(f"  {metric}: {value}")
    
    print("\nMonitoring test complete.") 