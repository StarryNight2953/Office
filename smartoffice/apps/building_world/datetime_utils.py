"""Strict timestamp parsing shared by Building World tools."""

from datetime import datetime


def parse_datetime(value: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include an explicit UTC offset")
    return parsed


def parse_interval(start_at: str, end_at: str) -> tuple[datetime, datetime]:
    start = parse_datetime(start_at)
    end = parse_datetime(end_at)
    if start >= end:
        raise ValueError("start_at must be before end_at")
    return start, end
