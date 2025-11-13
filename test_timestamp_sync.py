#!/usr/bin/env python3
"""
Unit tests for timestamp synchronization logic in rainscribe.

These tests verify that the critical timestamp normalization and synchronization
logic works correctly. This is the core mechanism that ensures captions appear
at the correct time in the video stream.
"""

import unittest
from collections import deque


class TestTimestampSynchronization(unittest.TestCase):
    """Test the timestamp normalization and synchronization logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.SEGMENT_DURATION = 10  # 10 second segments
        self.transcription_start_time = None
        self.first_segment_timestamp = None
        self.segment_time_offset = None

    def normalize_timestamp(self, ts):
        """
        Replica of the normalize_timestamp function from rainscribe.py.
        Convert Gladia timestamp to stream-relative timestamp.
        """
        if self.transcription_start_time is None:
            return None  # Cannot normalize without transcription start time

        if self.segment_time_offset is None:
            return None  # Cannot normalize without proper synchronization

        # Convert Gladia timestamp to stream timeline:
        # 1. Gladia timestamp is relative to transcription start
        # 2. Add the offset to align with segment timeline
        normalized = float(ts) + self.segment_time_offset

        return normalized

    def initialize_transcription(self, start_time):
        """Initialize transcription timing."""
        self.transcription_start_time = float(start_time)

        # Calculate offset if segments are ready
        if self.first_segment_timestamp is not None:
            self.segment_time_offset = -self.transcription_start_time

    def initialize_segments(self, first_segment):
        """Initialize segment timing."""
        self.first_segment_timestamp = first_segment

        # Calculate offset if transcription is ready
        if self.transcription_start_time is not None:
            self.segment_time_offset = -self.transcription_start_time

    def get_segment_timestamp(self, segment_number):
        """Convert a segment number to a timestamp."""
        if self.first_segment_timestamp is None:
            return None
        normalized_segment = segment_number - self.first_segment_timestamp
        return normalized_segment * self.SEGMENT_DURATION

    def check_overlap(self, caption_start, caption_end, segment_number):
        """Check if a caption overlaps with a segment."""
        segment_start = self.get_segment_timestamp(segment_number)
        if segment_start is None:
            return False

        segment_end = segment_start + self.SEGMENT_DURATION

        # Strict overlap check: start_time < segment_end AND end_time > segment_start
        return caption_start < segment_end and caption_end > segment_start

    # Test cases

    def test_normalization_requires_both_timelines(self):
        """Test that normalization returns None until both timelines are initialized."""
        # Before initialization
        result = self.normalize_timestamp(5.0)
        self.assertIsNone(result, "Should return None before any initialization")

        # After transcription init only
        self.initialize_transcription(2.5)
        result = self.normalize_timestamp(5.0)
        self.assertIsNone(result, "Should return None with only transcription initialized")

        # After both initialized
        self.initialize_segments(1000)
        result = self.normalize_timestamp(5.0)
        self.assertIsNotNone(result, "Should return value after both timelines initialized")

    def test_transcription_before_segments(self):
        """Test synchronization when transcription starts before segment tracking."""
        # Transcription starts at 2.5 seconds (from Gladia's perspective)
        self.initialize_transcription(2.5)

        # Then segments start (epoch-based, e.g., 1000)
        self.initialize_segments(1000)

        # Verify offset calculation
        self.assertEqual(self.segment_time_offset, -2.5,
                        "Offset should be negative of transcription start time")

        # Test normalization
        # Gladia sends timestamp 5.0 (2.5 seconds after transcription started)
        # Should map to 2.5 seconds in stream timeline
        result = self.normalize_timestamp(5.0)
        self.assertEqual(result, 2.5,
                        "Timestamp 5.0 should normalize to 2.5")

        # Gladia's transcription_start_time (2.5) should map to stream time 0.0
        result = self.normalize_timestamp(2.5)
        self.assertEqual(result, 0.0,
                        "Transcription start should map to stream time 0.0")

    def test_segments_before_transcription(self):
        """Test synchronization when segment tracking starts before transcription."""
        # Segments start first
        self.initialize_segments(1000)

        # Then transcription starts at 3.2 seconds
        self.initialize_transcription(3.2)

        # Verify offset calculation
        self.assertEqual(self.segment_time_offset, -3.2,
                        "Offset should be negative of transcription start time")

        # Test normalization
        result = self.normalize_timestamp(3.2)
        self.assertEqual(result, 0.0,
                        "Transcription start should map to stream time 0.0")

        result = self.normalize_timestamp(13.2)
        self.assertEqual(result, 10.0,
                        "10 seconds after transcription start should map to 10.0")

    def test_caption_overlap_detection(self):
        """Test caption-to-segment overlap detection."""
        self.initialize_transcription(2.0)
        self.initialize_segments(1000)

        # Caption from 5.0 to 8.0 (Gladia time) = 3.0 to 6.0 (stream time)
        caption_start_gladia = 5.0
        caption_end_gladia = 8.0

        caption_start_stream = self.normalize_timestamp(caption_start_gladia)
        caption_end_stream = self.normalize_timestamp(caption_end_gladia)

        self.assertEqual(caption_start_stream, 3.0)
        self.assertEqual(caption_end_stream, 6.0)

        # Segment 1000 = 0-10s, should overlap
        self.assertTrue(
            self.check_overlap(caption_start_stream, caption_end_stream, 1000),
            "Caption [3.0, 6.0] should overlap with segment [0, 10]"
        )

        # Segment 1001 = 10-20s, should NOT overlap
        self.assertFalse(
            self.check_overlap(caption_start_stream, caption_end_stream, 1001),
            "Caption [3.0, 6.0] should not overlap with segment [10, 20]"
        )

    def test_caption_spanning_segments(self):
        """Test caption that spans multiple segments."""
        self.initialize_transcription(1.0)
        self.initialize_segments(2000)

        # Caption from 10.0 to 22.0 (Gladia) = 9.0 to 21.0 (stream)
        # Should overlap segments 2000 (0-10), 2001 (10-20), 2002 (20-30)
        caption_start = self.normalize_timestamp(10.0)
        caption_end = self.normalize_timestamp(22.0)

        self.assertEqual(caption_start, 9.0)
        self.assertEqual(caption_end, 21.0)

        # Should overlap segment 2000 (0-10s)
        self.assertTrue(
            self.check_overlap(caption_start, caption_end, 2000),
            "Caption [9.0, 21.0] should overlap segment [0, 10]"
        )

        # Should overlap segment 2001 (10-20s)
        self.assertTrue(
            self.check_overlap(caption_start, caption_end, 2001),
            "Caption [9.0, 21.0] should overlap segment [10, 20]"
        )

        # Should overlap segment 2002 (20-30s)
        self.assertTrue(
            self.check_overlap(caption_start, caption_end, 2002),
            "Caption [9.0, 21.0] should overlap segment [20, 30]"
        )

        # Should NOT overlap segment 1999 (before)
        self.assertFalse(
            self.check_overlap(caption_start, caption_end, 1999),
            "Caption [9.0, 21.0] should not overlap segment before"
        )

        # Should NOT overlap segment 2003 (after)
        self.assertFalse(
            self.check_overlap(caption_start, caption_end, 2003),
            "Caption [9.0, 21.0] should not overlap segment [30, 40]"
        )

    def test_edge_case_caption_at_boundary(self):
        """Test caption that ends exactly at segment boundary."""
        self.initialize_transcription(0.5)
        self.initialize_segments(5000)

        # Caption from 10.5 to 10.5 (Gladia) = 10.0 to 10.0 (stream)
        # This is exactly at the boundary between segment 5001 and 5002
        caption_start = self.normalize_timestamp(10.5)
        caption_end = self.normalize_timestamp(10.5)

        self.assertEqual(caption_start, 10.0)
        self.assertEqual(caption_end, 10.0)

        # With strict overlap (start < seg_end AND end > seg_start):
        # For segment 5001 (10-20): 10.0 < 20 is True, but 10.0 > 10 is False
        # So it should NOT overlap
        overlap_5001 = self.check_overlap(caption_start, caption_end, 5001)

        # For segment 5000 (0-10): 10.0 < 10 is False
        # So it should NOT overlap
        overlap_5000 = self.check_overlap(caption_start, caption_end, 5000)

        # Zero-duration captions at boundaries should not overlap either segment
        # This is correct behavior - such captions should be filtered out
        self.assertFalse(overlap_5000 or overlap_5001,
                        "Zero-duration caption at boundary should not overlap")

    def test_realistic_scenario(self):
        """Test a realistic scenario with multiple captions."""
        # Segments start at epoch time 1699900000
        self.initialize_segments(1699900000)

        # Transcription starts 0.8 seconds later (from Gladia's perspective)
        self.initialize_transcription(0.8)

        # First caption: Gladia says 2.3 to 5.1 seconds
        # Should map to stream time 1.5 to 4.3 seconds
        caption1_start = self.normalize_timestamp(2.3)
        caption1_end = self.normalize_timestamp(5.1)

        self.assertAlmostEqual(caption1_start, 1.5, places=5)
        self.assertAlmostEqual(caption1_end, 4.3, places=5)

        # Should be in first segment (0-10s)
        self.assertTrue(self.check_overlap(caption1_start, caption1_end, 1699900000))

        # Second caption: Gladia says 15.2 to 18.9 seconds
        # Should map to stream time 14.4 to 18.1 seconds
        caption2_start = self.normalize_timestamp(15.2)
        caption2_end = self.normalize_timestamp(18.9)

        self.assertAlmostEqual(caption2_start, 14.4, places=5)
        self.assertAlmostEqual(caption2_end, 18.1, places=5)

        # Should be in second segment (10-20s)
        self.assertTrue(self.check_overlap(caption2_start, caption2_end, 1699900001))

        # Should NOT be in first segment
        self.assertFalse(self.check_overlap(caption2_start, caption2_end, 1699900000))


class TestFormatDuration(unittest.TestCase):
    """Test WebVTT timestamp formatting."""

    def format_duration(self, seconds):
        """Replica of format_duration from rainscribe.py."""
        try:
            if isinstance(seconds, str):
                if ":" in seconds and len(seconds.split(":")) > 2:
                    parts = seconds.split(":")
                    seconds = float(parts[-2]) * 60 + float(parts[-1])

            milliseconds = int(float(seconds) * 1000)
            hours = milliseconds // 3600000
            minutes = (milliseconds % 3600000) // 60000
            secs = (milliseconds % 60000) // 1000
            ms = milliseconds % 1000

            hours = hours % 100

            return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
        except (ValueError, TypeError):
            return "00:00:00.000"

    def test_zero_seconds(self):
        """Test formatting zero seconds."""
        self.assertEqual(self.format_duration(0), "00:00:00.000")

    def test_subsecond_timing(self):
        """Test formatting subsecond durations."""
        self.assertEqual(self.format_duration(0.5), "00:00:00.500")
        self.assertEqual(self.format_duration(0.123), "00:00:00.123")

    def test_seconds_only(self):
        """Test formatting seconds without minutes."""
        self.assertEqual(self.format_duration(5), "00:00:05.000")
        self.assertEqual(self.format_duration(45.678), "00:00:45.678")

    def test_minutes_and_seconds(self):
        """Test formatting with minutes."""
        self.assertEqual(self.format_duration(65), "00:01:05.000")
        self.assertEqual(self.format_duration(125.5), "00:02:05.500")

    def test_hours_minutes_seconds(self):
        """Test formatting with hours."""
        self.assertEqual(self.format_duration(3661), "01:01:01.000")
        self.assertEqual(self.format_duration(3723.456), "01:02:03.456")

    def test_large_values(self):
        """Test formatting large durations."""
        # 2 hours
        self.assertEqual(self.format_duration(7200), "02:00:00.000")
        # 10 hours
        self.assertEqual(self.format_duration(36000), "10:00:00.000")


if __name__ == "__main__":
    unittest.main()
