import unittest
from datetime import datetime, timezone

from botwork.schedule import allows_scan, parse


class ScheduleTests(unittest.TestCase):
    def test_moscow_window_boundaries_and_weekday(self):
        schedule = 'mon,fri 09:00-18:00'
        self.assertFalse(allows_scan(schedule, datetime(2026, 11, 2, 5, 59, tzinfo=timezone.utc)))
        self.assertTrue(allows_scan(schedule, datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)))
        self.assertFalse(allows_scan(schedule, datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)))
        self.assertFalse(allows_scan(schedule, datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)))
        self.assertTrue(allows_scan('off', datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)))

    def test_invalid_windows_rejected(self):
        for value in ('mon 09:00-09:00', 'mon,mon 09:00-18:00', 'xyz 09:00-18:00', 'mon 24:00-25:00', 'mon 09:00-18:00;', ';'.join(['mon 09:00-18:00'] * 15)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(value)

    def test_multiple_windows_and_overnight_week_boundary(self):
        schedule = 'sun 22:00-03:00; mon 09:00-12:00; mon 15:00-18:00'
        self.assertTrue(allows_scan(schedule, datetime(2026, 11, 1, 19, 0, tzinfo=timezone.utc)))
        self.assertTrue(allows_scan(schedule, datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc)))
        self.assertFalse(allows_scan(schedule, datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)))
        self.assertTrue(allows_scan(schedule, datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)))
        self.assertFalse(allows_scan(schedule, datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)))
        self.assertTrue(allows_scan(schedule, datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)))
