"""Calendar contracts exercised without importing the application runtime."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from hyadex.jobs import schedules


def timestamp(value: str, timezone_name: str = "UTC") -> float:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone_name)).timestamp()


def local_time(value: float, timezone_name: str) -> datetime:
    return datetime.fromtimestamp(value, tz=ZoneInfo(timezone_name))


class JobOccurrenceTests(unittest.TestCase):
    def test_interval_skips_missed_slots_without_drifting(self) -> None:
        job = {
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "schedule_start_at": 100.0,
        }
        self.assertEqual(schedules.next_job_occurrence(job, 275.0), 280.0)
        self.assertEqual(schedules.next_job_occurrence(job, 280.0), 340.0)
        with self.assertRaisesRegex(HTTPException, "at most"):
            schedules.normalize_interval_seconds(10**20)

    def test_cron_supports_alias_seconds_year_and_stable_hash(self) -> None:
        for expression in ("@hourly", "*/10 * * * * *", "0 0 9 * * * 2027", "H 9 * * *"):
            normalized = schedules.normalize_cron_expression(expression, "job_stable")
            self.assertEqual(normalized, expression)

        hashed = {
            "id": "job_stable",
            "schedule_kind": "cron",
            "cron_expression": "H 9 * * *",
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-01-01T00:00:00"),
        }
        first = schedules.next_job_occurrence(hashed, timestamp("2026-01-01T00:00:00"))
        second_read = schedules.next_job_occurrence(hashed, timestamp("2026-01-01T00:00:00"))
        self.assertEqual(first, second_read)

    def test_cron_rejects_random_and_invalid_expressions(self) -> None:
        with self.assertRaisesRegex(HTTPException, "random"):
            schedules.normalize_cron_expression("R 9 * * *", "job_1")
        with self.assertRaisesRegex(HTTPException, "invalid cron"):
            schedules.normalize_cron_expression("99 99 99", "job_1")

    def test_cron_keeps_wall_time_and_skips_dst_gap_and_second_fold(self) -> None:
        zone = "America/Los_Angeles"
        daily = {
            "id": "job_daily",
            "schedule_kind": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": zone,
            "schedule_start_at": timestamp("2026-03-07T00:00:00", zone),
        }
        next_daily = schedules.next_job_occurrence(daily, timestamp("2026-03-07T10:00:00", zone))
        self.assertEqual(local_time(next_daily, zone).isoformat(), "2026-03-08T09:00:00-07:00")

        missing = {**daily, "cron_expression": "30 2 * * *"}
        next_missing = schedules.next_job_occurrence(missing, timestamp("2026-03-07T03:00:00", zone))
        self.assertEqual(local_time(next_missing, zone).isoformat(), "2026-03-09T02:30:00-07:00")

        folded = {
            **daily,
            "cron_expression": "30 1 * * *",
            "schedule_start_at": timestamp("2026-10-31T00:00:00", zone),
        }
        first = schedules.next_job_occurrence(folded, timestamp("2026-10-31T03:00:00", zone))
        second = schedules.next_job_occurrence(folded, first)
        self.assertEqual(local_time(first, zone).isoformat(), "2026-11-01T01:30:00-07:00")
        self.assertEqual(local_time(second, zone).isoformat(), "2026-11-02T01:30:00-08:00")

    def test_rrule_accepts_prefix_count_and_all_by_fields(self) -> None:
        zone = "America/New_York"
        anchor = timestamp("2026-01-01T08:00:00", zone)
        expression = schedules.normalize_rrule_expression(
            "RRULE:FREQ=MONTHLY;COUNT=3;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1;BYHOUR=9;BYMINUTE=15",
            zone,
            anchor,
        )
        self.assertTrue(expression.startswith("FREQ=MONTHLY"))
        job = {
            "id": "job_rule",
            "schedule_kind": "rrule",
            "rrule": expression,
            "timezone": zone,
            "schedule_start_at": anchor,
        }
        first = schedules.next_job_occurrence(job, anchor, inclusive=True)
        second = schedules.next_job_occurrence(job, first)
        third = schedules.next_job_occurrence(job, second)
        fourth = schedules.next_job_occurrence(job, third)
        self.assertEqual(local_time(first, zone).strftime("%Y-%m-%d %H:%M"), "2026-01-01 09:15")
        self.assertEqual(local_time(second, zone).strftime("%Y-%m-%d %H:%M"), "2026-02-02 09:15")
        self.assertEqual(local_time(third, zone).strftime("%Y-%m-%d %H:%M"), "2026-03-02 09:15")
        self.assertIsNone(fourth)

    def test_rrule_skips_nonexistent_dst_occurrence(self) -> None:
        zone = "America/Los_Angeles"
        anchor = timestamp("2026-03-07T03:00:00", zone)
        job = {
            "id": "job_rule",
            "schedule_kind": "rrule",
            "rrule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=30",
            "timezone": zone,
            "schedule_start_at": anchor,
        }
        next_run = schedules.next_job_occurrence(job, anchor)
        self.assertEqual(local_time(next_run, zone).isoformat(), "2026-03-09T02:30:00-07:00")

    def test_rrule_rejects_calendar_documents_and_timezone_is_strict(self) -> None:
        with self.assertRaisesRegex(HTTPException, "one RFC 5545 RRULE"):
            schedules.normalize_rrule_expression(
                "DTSTART:20260101T090000\nRRULE:FREQ=DAILY",
                "UTC",
                timestamp("2026-01-01T00:00:00"),
            )
        with self.assertRaisesRegex(HTTPException, "IANA timezone"):
            schedules.normalize_job_timezone("Mars/Olympus_Mons")
        with self.assertRaisesRegex(HTTPException, "does not exist"):
            schedules.parse_job_timestamp("2026-03-08T02:30:00", "America/Los_Angeles")
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaisesRegex(HTTPException, "finite"):
                schedules.parse_job_timestamp(value)
        for value in ("1e300", "-1e300"):
            with self.assertRaisesRegex(HTTPException, "supported range"):
                schedules.parse_job_timestamp(value)

    def test_rrule_rejects_nonprogressing_and_out_of_range_parts(self) -> None:
        anchor = timestamp("2026-01-01T00:00:00")
        for expression in (
            "FREQ=DAILY;INTERVAL=0",
            "FREQ=DAILY;INTERVAL=-1",
            "FREQ=DAILY;COUNT=0",
            "FREQ=YEARLY;BYMONTH=0",
            "FREQ=DAILY;BYHOUR=24",
            "FREQ=MONTHLY;BYDAY=53MO",
            f"FREQ=DAILY;COUNT={'9' * 5000}",
            f"FREQ=DAILY;BYSECOND={'9' * 5000}",
            "FREQ=MINUTELY;BYSECOND=60",
        ):
            with self.subTest(expression=expression), self.assertRaises(HTTPException):
                schedules.normalize_rrule_expression(expression, "UTC", anchor)

    def test_rrule_count_ignores_nonexistent_dst_instances(self) -> None:
        zone = "America/Los_Angeles"
        anchor = timestamp("2026-03-07T02:30:00", zone)
        job = {
            "id": "job_count",
            "schedule_kind": "rrule",
            "rrule": "FREQ=DAILY;COUNT=2;BYHOUR=2;BYMINUTE=30;BYSECOND=0",
            "timezone": zone,
            "schedule_start_at": anchor,
        }
        second = schedules.next_job_occurrence(job, anchor)
        self.assertEqual(local_time(second, zone).isoformat(), "2026-03-09T02:30:00-07:00")
        self.assertIsNone(schedules.next_job_occurrence(job, second))

    def test_large_count_rule_exhaustion_is_bounded(self) -> None:
        anchor = timestamp("2026-01-01T00:00:00")
        job = {
            "id": "job_count",
            "schedule_kind": "rrule",
            "rrule": "FREQ=SECONDLY;COUNT=10000",
            "timezone": "UTC",
            "schedule_start_at": anchor,
        }
        self.assertIsNone(schedules.next_job_occurrence(job, anchor + 20_000))

    def test_exhausted_year_limited_cron_has_no_next_occurrence(self) -> None:
        job = {
            "id": "job_year",
            "schedule_kind": "cron",
            "cron_expression": "0 0 9 1 1 * 2026",
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-01-01T00:00:00"),
        }
        self.assertIsNone(schedules.next_job_occurrence(job, timestamp("2027-01-01T00:00:00")))

