"""Calendar calculations without scheduler, provider or persistence state.

The existing validation errors remain HTTPException for wire compatibility.
Importing this module does not create an application or read configuration.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, CroniterError, croniter
from dateutil.rrule import rrulestr
from dateutil.tz import datetime_exists
from fastapi import HTTPException


VALID_JOB_SCHEDULE_KINDS = {"interval", "cron", "rrule"}

CRON_RANDOM_TOKEN_RE = re.compile(r"(?i)\bR\b")

CRON_HASH_TOKEN_RE = re.compile(r"(?i)\bH\b")

RRULE_FREQUENCIES = {"SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}

RRULE_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}

RRULE_ALLOWED_PARTS = {
    "FREQ", "UNTIL", "COUNT", "INTERVAL", "BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY",
    "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS", "WKST",
}

RRULE_INTEGER_LIST_RANGES = {
    # Python datetime cannot represent RFC leap-second value 60.
    "BYSECOND": (0, 59, True),
    "BYMINUTE": (0, 59, True),
    "BYHOUR": (0, 23, True),
    "BYMONTHDAY": (-31, 31, False),
    "BYYEARDAY": (-366, 366, False),
    "BYWEEKNO": (-53, 53, False),
    "BYMONTH": (1, 12, True),
    "BYSETPOS": (-366, 366, False),
}

MAX_RRULE_COUNT = 10_000

MAX_RRULE_SCAN = MAX_RRULE_COUNT + 10_000

MAX_JOB_INTERVAL_SECONDS = 10 * 366 * 24 * 60 * 60


def normalize_job_timezone(value: Any) -> str:
    name = str(value or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid IANA timezone: {name}") from exc
    return str(getattr(zone, "key", name) or name)


def parse_job_timestamp(value: str | None, timezone_name: str = "UTC") -> float | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    try:
        numeric = float(clean)
    except ValueError:
        pass
    else:
        if not math.isfinite(numeric):
            raise HTTPException(status_code=400, detail="job timestamp must be finite")
        try:
            datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="job timestamp is outside the supported range") from exc
        return numeric
    normalized = clean[:-1] + "+00:00" if clean.endswith("Z") else clean
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid job timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(normalize_job_timezone(timezone_name)))
        if not datetime_exists(parsed):
            raise HTTPException(status_code=400, detail="job timestamp does not exist in the selected timezone")
    return parsed.timestamp()


def job_schedule_kind(job: dict[str, Any]) -> str:
    kind = str(job.get("schedule_kind") or "").strip().lower()
    if kind in VALID_JOB_SCHEDULE_KINDS:
        return kind
    if str(job.get("cron_expression") or "").strip():
        return "cron"
    if str(job.get("rrule") or "").strip():
        return "rrule"
    return "interval"


def normalize_cron_expression(value: Any, job_id: str) -> str:
    expression = " ".join(str(value or "").split())
    if not expression:
        raise HTTPException(status_code=400, detail="cron_expression is required for a cron schedule")
    if CRON_RANDOM_TOKEN_RE.search(expression):
        raise HTTPException(
            status_code=400,
            detail="cron random (R) fields are not supported; use hashed (H) fields for stable schedules",
        )
    hash_id = job_id if CRON_HASH_TOKEN_RE.search(expression) else None
    try:
        valid = croniter.is_valid(
            expression,
            hash_id=hash_id,
            second_at_beginning=True,
            strict=True,
        )
    except CroniterError as exc:
        raise HTTPException(status_code=400, detail=f"invalid cron expression: {exc}") from exc
    if not valid:
        raise HTTPException(status_code=400, detail="invalid cron expression")
    return expression


def validate_rrule_parts(expression: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for raw_part in expression.split(";"):
        key, separator, raw_value = raw_part.partition("=")
        key = key.strip().upper()
        item = raw_value.strip()
        if not separator or not key or not item:
            raise HTTPException(status_code=400, detail="invalid RFC 5545 RRULE property")
        if key not in RRULE_ALLOWED_PARTS:
            raise HTTPException(status_code=400, detail=f"unsupported RFC 5545 RRULE part: {key}")
        if key in parts:
            raise HTTPException(status_code=400, detail=f"RRULE part may appear only once: {key}")
        parts[key] = item

    frequency = parts.get("FREQ", "").upper()
    if frequency not in RRULE_FREQUENCIES:
        raise HTTPException(status_code=400, detail="RRULE requires a valid FREQ part")
    if "COUNT" in parts and "UNTIL" in parts:
        raise HTTPException(status_code=400, detail="RRULE cannot contain both COUNT and UNTIL")
    for key in ("COUNT", "INTERVAL"):
        if key not in parts:
            continue
        if not re.fullmatch(r"\d+", parts[key]):
            raise HTTPException(status_code=400, detail=f"RRULE {key} must be a positive integer")
        if len(parts[key]) > 10:
            raise HTTPException(status_code=400, detail=f"RRULE {key} is too large")
        number = int(parts[key])
        if number < 1:
            raise HTTPException(status_code=400, detail=f"RRULE {key} must be at least 1")
        if key == "COUNT" and number > MAX_RRULE_COUNT:
            raise HTTPException(status_code=400, detail=f"RRULE COUNT must be at most {MAX_RRULE_COUNT}")

    for key, (minimum, maximum, zero_allowed) in RRULE_INTEGER_LIST_RANGES.items():
        if key not in parts:
            continue
        values = parts[key].split(",")
        if any(not re.fullmatch(r"[+-]?\d+", item) for item in values):
            raise HTTPException(status_code=400, detail=f"RRULE {key} contains an invalid integer")
        if any(len(item.lstrip("+-")) > 3 for item in values):
            raise HTTPException(status_code=400, detail=f"RRULE {key} contains an integer that is too large")
        numbers = [int(item) for item in values]
        if any(number < minimum or number > maximum or (number == 0 and not zero_allowed) for number in numbers):
            raise HTTPException(status_code=400, detail=f"RRULE {key} contains a value outside its RFC 5545 range")

    if "BYDAY" in parts:
        for item in parts["BYDAY"].upper().split(","):
            match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item)
            if not match:
                raise HTTPException(status_code=400, detail="RRULE BYDAY contains an invalid weekday")
            if match.group(1):
                ordinal = int(match.group(1))
                if ordinal == 0 or abs(ordinal) > 53:
                    raise HTTPException(status_code=400, detail="RRULE BYDAY ordinal must be between 1 and 53")
                if frequency not in {"MONTHLY", "YEARLY"}:
                    raise HTTPException(status_code=400, detail="numeric BYDAY is only valid with MONTHLY or YEARLY")
                if frequency == "MONTHLY" and abs(ordinal) > 5:
                    raise HTTPException(status_code=400, detail="numeric MONTHLY BYDAY must be between 1 and 5")
                if frequency == "YEARLY" and "BYWEEKNO" in parts:
                    raise HTTPException(status_code=400, detail="numeric BYDAY cannot be combined with YEARLY BYWEEKNO")
    if "WKST" in parts and parts["WKST"].upper() not in RRULE_WEEKDAYS:
        raise HTTPException(status_code=400, detail="RRULE WKST must be a weekday")
    if "BYWEEKNO" in parts and frequency != "YEARLY":
        raise HTTPException(status_code=400, detail="RRULE BYWEEKNO is only valid with YEARLY")
    if "BYMONTHDAY" in parts and frequency == "WEEKLY":
        raise HTTPException(status_code=400, detail="RRULE BYMONTHDAY is not valid with WEEKLY")
    if "BYYEARDAY" in parts and frequency in {"DAILY", "WEEKLY", "MONTHLY"}:
        raise HTTPException(status_code=400, detail=f"RRULE BYYEARDAY is not valid with {frequency}")
    if "BYSETPOS" in parts and not any(key.startswith("BY") and key != "BYSETPOS" for key in parts):
        raise HTTPException(status_code=400, detail="RRULE BYSETPOS requires another BY rule part")
    return parts


def normalize_rrule_expression(value: Any, timezone_name: str, schedule_start_at: float) -> str:
    expression = str(value or "").strip()
    if expression.upper().startswith("RRULE:"):
        expression = expression[6:].strip()
    if not expression:
        raise HTTPException(status_code=400, detail="rrule is required for an RRULE schedule")
    if "\n" in expression or "\r" in expression:
        raise HTTPException(
            status_code=400,
            detail="rrule must be one RFC 5545 RRULE property, not an iCalendar document",
        )
    validate_rrule_parts(expression)
    zone = ZoneInfo(normalize_job_timezone(timezone_name))
    anchor = datetime.fromtimestamp(float(schedule_start_at), tz=zone)
    try:
        rrulestr(expression, dtstart=anchor)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid RFC 5545 RRULE: {exc}") from exc
    return expression


def cron_hash_id(expression: str, job_id: str) -> str | None:
    return job_id if CRON_HASH_TOKEN_RE.search(expression) else None


def cron_clock_field_allows(values: list[Any], actual: int) -> bool:
    if values == ["*"] or "*" in values:
        return True
    return actual in values


def cron_candidate_has_requested_clock(iterator: croniter, candidate: datetime) -> bool:
    """Reject croniter's synthetic spring-gap clock time.

    croniter correctly keeps ordinary jobs at local wall time across DST, but
    for a nonexistent clock time it may return the first valid time after the
    gap. We use normal cron semantics instead: skip that nonexistent slot.
    """
    expanded = iterator.expanded
    if not cron_clock_field_allows(expanded[0], candidate.minute):
        return False
    if not cron_clock_field_allows(expanded[1], candidate.hour):
        return False
    expected_seconds = expanded[5] if len(expanded) >= 6 else [0]
    return cron_clock_field_allows(expected_seconds, candidate.second)


def next_job_occurrence(
    job: dict[str, Any],
    after_timestamp: float,
    *,
    inclusive: bool = False,
) -> float | None:
    """Return the next canonical occurrence without anchoring to completion time."""
    kind = job_schedule_kind(job)
    after = float(after_timestamp)
    anchor_value = job.get("schedule_start_at")
    if anchor_value is None:
        anchor_value = job.get("scheduled_run_at")
    if anchor_value is None:
        anchor_value = job.get("next_run_at")

    if kind == "interval":
        if anchor_value is None:
            return None
        anchor = float(anchor_value)
        interval_value = job.get("interval_seconds")
        if not interval_value:
            if anchor > after or (inclusive and anchor >= after):
                return anchor
            return None
        interval = int(interval_value)
        if interval <= 0:
            return None
        if anchor > after or (inclusive and anchor >= after):
            return anchor
        elapsed = max(0.0, after - anchor)
        steps = int(elapsed // interval)
        candidate = anchor + (steps * interval)
        if candidate < after or (not inclusive and candidate <= after):
            candidate += interval
        return candidate

    timezone_name = normalize_job_timezone(job.get("timezone"))
    zone = ZoneInfo(timezone_name)
    anchor = float(anchor_value if anchor_value is not None else after)

    if kind == "cron":
        expression = normalize_cron_expression(job.get("cron_expression"), str(job.get("id") or "job"))
        after = max(after, anchor)
        base = datetime.fromtimestamp(after, tz=zone)
        hashed = cron_hash_id(expression, str(job.get("id") or "job"))
        if inclusive and base.microsecond == 0 and not hashed:
            try:
                if (
                    base.fold == 0
                    and datetime_exists(base)
                    and croniter.match(expression, base, second_at_beginning=True)
                ):
                    return base.timestamp()
            except CroniterError:
                pass
        try:
            iterator = croniter(
                expression,
                base,
                ret_type=datetime,
                hash_id=hashed,
                second_at_beginning=True,
            )
            for _ in range(10000):
                candidate = iterator.get_next(datetime)
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=zone)
                if candidate.fold == 1 or not datetime_exists(candidate):
                    continue
                if not cron_candidate_has_requested_clock(iterator, candidate):
                    continue
                timestamp = candidate.timestamp()
                if timestamp > after or (inclusive and timestamp >= after):
                    return timestamp
            raise HTTPException(status_code=400, detail="cron expression produced too many invalid occurrences")
        except CroniterBadDateError:
            return None
        except CroniterError as exc:
            raise HTTPException(status_code=400, detail=f"invalid cron expression: {exc}") from exc

    expression = normalize_rrule_expression(job.get("rrule"), timezone_name, anchor)
    rule_anchor = datetime.fromtimestamp(anchor, tz=zone)
    try:
        parts = validate_rrule_parts(expression)
        count_limit = int(parts["COUNT"]) if "COUNT" in parts else None
        rule_expression = ";".join(
            part for part in expression.split(";") if not part.strip().upper().startswith("COUNT=")
        ) if count_limit is not None else expression
        rule = rrulestr(rule_expression, dtstart=rule_anchor)
        valid_seen = 0
        if count_limit is not None:
            for iteration, candidate in enumerate(rule):
                if iteration >= MAX_RRULE_SCAN:
                    return None
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=zone)
                if candidate.fold == 1 or not datetime_exists(candidate):
                    continue
                valid_seen += 1
                if valid_seen > count_limit:
                    return None
                timestamp = candidate.timestamp()
                if timestamp > after or (inclusive and timestamp >= after):
                    return timestamp
            return None

        cursor = datetime.fromtimestamp(after, tz=zone)
        include_cursor = inclusive
        for _ in range(10_000):
            candidate = rule.after(cursor, inc=include_cursor)
            if candidate is None:
                return None
            include_cursor = False
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=zone)
            if candidate.fold == 1 or not datetime_exists(candidate):
                cursor = candidate
                continue
            timestamp = candidate.timestamp()
            if timestamp > after or (inclusive and timestamp >= after):
                return timestamp
            cursor = candidate
        raise HTTPException(status_code=400, detail="rrule produced too many invalid occurrences")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid RFC 5545 RRULE: {exc}") from exc


def requested_job_schedule_kind(
    schedule_kind: Any,
    interval_seconds: Any,
    cron_expression: Any,
    rrule_expression: Any,
) -> str:
    explicit = str(schedule_kind or "").strip().lower()
    if explicit and explicit not in VALID_JOB_SCHEDULE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"schedule_kind must be one of {sorted(VALID_JOB_SCHEDULE_KINDS)}",
        )
    supplied: list[str] = []
    if interval_seconds is not None:
        supplied.append("interval")
    if str(cron_expression or "").strip():
        supplied.append("cron")
    if str(rrule_expression or "").strip():
        supplied.append("rrule")
    if len(supplied) > 1:
        raise HTTPException(status_code=400, detail="choose only one of interval_seconds, cron_expression, or rrule")
    inferred = supplied[0] if supplied else "interval"
    if explicit and supplied and explicit != inferred:
        raise HTTPException(status_code=400, detail=f"{supplied[0]} schedule fields conflict with schedule_kind={explicit}")
    return explicit or inferred


def normalize_interval_seconds(value: Any) -> int | None:
    if value is None:
        return None
    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="interval_seconds must be a positive integer") from exc
    if interval <= 0:
        raise HTTPException(status_code=400, detail="interval_seconds must be a positive integer")
    if interval > MAX_JOB_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"interval_seconds must be at most {MAX_JOB_INTERVAL_SECONDS}",
        )
    return interval
