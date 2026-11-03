from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def now_ist() -> datetime:
    return datetime.now(IST)


def as_ist(value) -> datetime:
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(IST)


def format_ist_datetime(value) -> str:
    return as_ist(value).strftime("%d %b %Y, %I:%M %p IST")


def format_ist_date(value) -> str:
    return as_ist(value).strftime("%d/%m/%Y")
