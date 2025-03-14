#!/usr/bin/env python3
"""
Integration Test for Rainscribe Shared Modules

This script tests the integration of the shared modules in the Rainscribe system.
It verifies that the reference clock, offset calculator, WebVTT segmenter,
logging configuration, and monitoring system work properly together.
"""

import os
import sys
import time
import json
import logging
import argparse
from datetime import datetime

# Import shared modules
from shared.reference_clock import get_global_clock, get_time, get_formatted_time
from shared.offset_calculator import get_global_calculator, add_measurement, get_current_offset
from shared.webvtt_segmenter import WebVTTSegmenter, WebVTTCue, parse_vtt_content
from shared.logging_config import configure_logging
from shared.monitoring import get_metrics_manager, MetricsAggregator

# Configure logging
logger = configure_logging("integration-test")

def test_reference_clock():
    """Test the reference clock module."""
    logger.info("Testing reference clock...")
    
    # Get the global clock
    clock = get_global_clock()
    
    # Sync with NTP servers
    sync_result = clock.sync_once()
    logger.info(f"Clock sync result: {sync_result}")
    
    # Get current time
    current_time = get_time()
    formatted_time = get_formatted_time()
    logger.info(f"Current reference time: {current_time} ({formatted_time})")
    
    # Get clock status
    status = clock.get_status()
    logger.info(f"Clock offset: {status['offset']:.6f}s")
    logger.info(f"Clock drift rate: {status['drift_rate'] * 86400 * 1000:.2f} ms/day")
    
    return sync_result

def test_offset_calculator():
    """Test the offset calculator module."""
    logger.info("Testing offset calculator...")
    
    # Reset the global calculator for testing
    calculator = get_global_calculator()
    calculator.reset()
    
    # Add some measurements
    offsets = [2.5, 2.6, 2.45, 2.7, 2.55]
    
    for i, offset in enumerate(offsets):
        # Add some noise
        if i % 2 == 0:
            offset += 0.1
        else:
            offset -= 0.1
            
        new_offset = add_measurement(offset)
        logger.info(f"Added measurement: {offset:.2f}s -> New offset: {new_offset:.2f}s")
    
    # Get current offset
    current_offset = get_current_offset()
    logger.info(f"Final offset: {current_offset:.2f}s")
    
    # Get statistics
    stats = calculator.get_stats()
    logger.info(f"Measurements: {stats['measurement_count']}")
    logger.info(f"Mean offset: {stats.get('mean_offset', 0):.2f}s")
    logger.info(f"Median offset: {stats.get('median_offset', 0):.2f}s")
    
    # Check if offset is reasonable - we just need to make sure it's positive
    # and in a reasonable range based on our test data
    is_valid = 0 < current_offset < 5.0
    logger.info(f"Offset in expected range (0-5s): {is_valid}")
    
    return is_valid

def test_webvtt_segmenter():
    """Test the WebVTT segmenter module."""
    logger.info("Testing WebVTT segmenter...")
    
    # Create a segmenter
    segmenter = WebVTTSegmenter(segment_duration=10)
    
    # Create some test cues
    cues = [
        WebVTTCue("1", 0.5, 3.5, "This is the first subtitle"),
        WebVTTCue("2", 4.0, 6.5, "This is the second subtitle"),
        WebVTTCue("3", 6.0, 9.0, "This spans across segments"),
        WebVTTCue("4", 10.0, 13.0, "This is in the second segment"),
    ]
    
    # Add cues to the segmenter
    for cue in cues:
        segmenter.add_cue(cue)
        logger.info(f"Added cue: {cue}")
    
    # Get cues for each segment
    segment0_cues = segmenter.get_segment_cues(0)
    segment1_cues = segmenter.get_segment_cues(1)
    
    logger.info(f"Segment 0 has {len(segment0_cues)} cues")
    logger.info(f"Segment 1 has {len(segment1_cues)} cues")
    
    # Create test directory
    test_dir = "test_output"
    os.makedirs(test_dir, exist_ok=True)
    
    # Generate segment files
    segment_files = segmenter.generate_all_segments(0, 1, test_dir)
    logger.info(f"Generated {len(segment_files)} segment files")
    
    # Generate playlist
    playlist_path = segmenter.generate_playlist(0, 1, os.path.join(test_dir, "playlist.m3u8"))
    logger.info(f"Generated playlist: {playlist_path}")
    
    # Verify files exist
    segments_exist = all(os.path.exists(f) for f in segment_files)
    playlist_exists = os.path.exists(playlist_path)
    
    logger.info(f"All segment files exist: {segments_exist}")
    logger.info(f"Playlist file exists: {playlist_exists}")
    
    return segments_exist and playlist_exists

def test_monitoring():
    """Test the monitoring module."""
    logger.info("Testing monitoring system...")
    
    # Get the metrics manager
    metrics_manager = get_metrics_manager()
    
    # Record some test metrics
    metrics_manager.sync.record_latency(2.5, "test")
    metrics_manager.sync.record_offset(1.8, "test")
    metrics_manager.sync.record_processing_time(0.35, "test_operation")
    metrics_manager.sync.record_health_check("test_service", True)
    
    # Record a system metric
    metrics_manager.system.collect_metrics()
    
    # Save metrics to file
    saved_files = metrics_manager.save_all()
    logger.info(f"Saved metrics to: {', '.join(saved_files)}")
    
    # Get dashboard data
    dashboard = metrics_manager.get_dashboard_data()
    
    # Verify metrics were recorded
    sync_metrics_present = "sync" in dashboard and dashboard["sync"] is not None
    system_metrics_present = "system" in dashboard and dashboard["system"] is not None
    
    logger.info(f"Sync metrics present: {sync_metrics_present}")
    logger.info(f"System metrics present: {system_metrics_present}")
    
    return sync_metrics_present and system_metrics_present and len(saved_files) > 0

def main():
    """Run all tests."""
    parser = argparse.ArgumentParser(description="Test Rainscribe shared modules integration")
    parser.add_argument("--skip-clock", action="store_true", help="Skip reference clock test (useful for offline testing)")
    args = parser.parse_args()
    
    logger.info("Starting integration tests for Rainscribe shared modules")
    
    # Track test results
    results = {}
    
    # Test reference clock
    if not args.skip_clock:
        try:
            results["reference_clock"] = test_reference_clock()
        except Exception as e:
            logger.error(f"Reference clock test failed: {e}")
            results["reference_clock"] = False
    else:
        logger.info("Skipping reference clock test")
        results["reference_clock"] = None
    
    # Test offset calculator
    try:
        results["offset_calculator"] = test_offset_calculator()
    except Exception as e:
        logger.error(f"Offset calculator test failed: {e}")
        results["offset_calculator"] = False
    
    # Test WebVTT segmenter
    try:
        results["webvtt_segmenter"] = test_webvtt_segmenter()
    except Exception as e:
        logger.error(f"WebVTT segmenter test failed: {e}")
        results["webvtt_segmenter"] = False
    
    # Test monitoring
    try:
        results["monitoring"] = test_monitoring()
    except Exception as e:
        logger.error(f"Monitoring test failed: {e}")
        results["monitoring"] = False
    
    # Print results
    logger.info("\n==== TEST RESULTS ====")
    for test, result in results.items():
        status = "PASSED" if result else "FAILED" if result is False else "SKIPPED"
        logger.info(f"{test}: {status}")
    
    # Overall result
    passed = all(result for result in results.values() if result is not None)
    skipped = any(result is None for result in results.values())
    
    if passed:
        if skipped:
            logger.info("\nOVERALL RESULT: PASSED (with skipped tests)")
        else:
            logger.info("\nOVERALL RESULT: PASSED")
        return 0
    else:
        logger.info("\nOVERALL RESULT: FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(main()) 