"""
Recurrence rule parsing and next-run computation.

Recurrence is stored in the database as a small JSON dict, e.g.:
    {"type": "interval", "seconds": 1800}
    {"type": "daily",    "hour": 9,  "minute": 0}
    {"type": "weekly",   "weekday": 1, "hour": 15, "minute": 0}
    {"type": "monthly",  "day": 1,   "hour": 9,  "minute": 0}

All datetime objects passed to / returned from these functions must be
timezone-aware (using pytz).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

import pytz

# ── Constants ─────────────────────────────────────────────────────────────────

_WEEKDAYS: dict[str, int] = {
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    # French
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
}

_WEEKDAY_NAMES_EN = [
    "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday", "Sunday",
]

_ORDINAL_SUFFIX = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$")


# ── Custom exception ──────────────────────────────────────────────────────────

class RecurrenceError(ValueError):
    """Raised when a recurrence string cannot be parsed."""


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_recurrence(text: str) -> dict:
    """
    Convert a human-readable recurrence string to a structured dict.

    Supported English (and partial French) syntax:
    • every 30 minutes       → {"type": "interval", "seconds": 1800}
    • every 2 hours          → {"type": "interval", "seconds": 7200}
    • every day at 9:00      → {"type": "daily", "hour": 9, "minute": 0}
    • every tuesday at 15:00 → {"type": "weekly", "weekday": 1, "hour": 15, "minute": 0}
    • every month on the 1st at 9:00
                             → {"type": "monthly", "day": 1, "hour": 9, "minute": 0}
    """
    text = text.strip().lower()

    # ── Interval: every N minutes / hours ─────────────────────────────────────
    m = re.fullmatch(
        r"every\s+(\d+)\s+(minutes?|mins?|heures?|hours?|h)",
        text,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = n * (3600 if unit.startswith(("h", "heur")) else 60)
        if seconds < 60:
            raise RecurrenceError("Minimum interval is 1 minute.")
        if seconds > 365 * 24 * 3600:
            raise RecurrenceError("Maximum interval is 1 year.")
        return {"type": "interval", "seconds": seconds}

    # ── Daily: every day at HH:MM ─────────────────────────────────────────────
    m = re.fullmatch(r"every\s+(?:day|jour)\s+at\s+(\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        _assert_time(hour, minute)
        return {"type": "daily", "hour": hour, "minute": minute}

    # ── Weekly: every <weekday> at HH:MM ──────────────────────────────────────
    weekday_pattern = "|".join(_WEEKDAYS.keys())
    m = re.fullmatch(
        rf"every\s+({weekday_pattern})\s+at\s+(\d{{1,2}}):(\d{{2}})",
        text,
    )
    if m:
        weekday = _WEEKDAYS[m.group(1)]
        hour, minute = int(m.group(2)), int(m.group(3))
        _assert_time(hour, minute)
        return {"type": "weekly", "weekday": weekday, "hour": hour, "minute": minute}

    # ── Monthly: every month on the Nth at HH:MM ─────────────────────────────
    m = re.fullmatch(
        r"every\s+month\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2}):(\d{2})",
        text,
    )
    if m:
        day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not 1 <= day <= 28:
            # Cap at 28 so the rule is valid for every month, including February.
            raise RecurrenceError(
                "For monthly recurrence, day must be between 1 and 28 "
                "(to be valid in February)."
            )
        _assert_time(hour, minute)
        return {"type": "monthly", "day": day, "hour": hour, "minute": minute}

    raise RecurrenceError(
        "Recurrence format not recognised. Supported examples:\n"
        "• `every 30 minutes`\n"
        "• `every 2 hours`\n"
        "• `every day at 9:00`\n"
        "• `every tuesday at 15:00`\n"
        "• `every month on the 1st at 9:00`"
    )


def _assert_time(hour: int, minute: int) -> None:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RecurrenceError(f"Invalid time {hour:02d}:{minute:02d}.")


# ── First-run computation ─────────────────────────────────────────────────────

def compute_first_run(recurrence: dict, now: datetime) -> datetime:
    """
    Return the earliest future datetime that satisfies the recurrence rule,
    relative to `now`.  `now` must be timezone-aware.
    """
    r_type = recurrence["type"]

    if r_type == "interval":
        return now + timedelta(seconds=recurrence["seconds"])

    if r_type == "daily":
        target = now.replace(
            hour=recurrence["hour"], minute=recurrence["minute"],
            second=0, microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return target

    if r_type == "weekly":
        target_wd = recurrence["weekday"]
        hour, minute = recurrence["hour"], recurrence["minute"]
        days_ahead = (target_wd - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        if target <= now:
            target += timedelta(weeks=1)
        return target

    if r_type == "monthly":
        day, hour, minute = recurrence["day"], recurrence["hour"], recurrence["minute"]
        try:
            target = now.replace(
                day=day, hour=hour, minute=minute, second=0, microsecond=0,
            )
        except ValueError:
            # The day doesn't exist in this month — advance to next month.
            target = _add_month(now.replace(
                day=1, hour=hour, minute=minute, second=0, microsecond=0,
            )).replace(day=day)
        if target <= now:
            target = _add_month(target)
        return target

    raise ValueError(f"Unknown recurrence type: {r_type!r}")


# ── Next-run computation (called after each execution) ────────────────────────

def compute_next_run(recurrence: dict, last_scheduled: datetime) -> datetime:
    """
    Return the next scheduled datetime after `last_scheduled` was executed.

    Using `last_scheduled` (not wall-clock time) prevents drift:
    daily events always fire at the same time of day regardless of
    how late the scheduler actually ran.
    """
    r_type = recurrence["type"]

    if r_type == "interval":
        # For intervals we advance from *now* to avoid bursting after downtime.
        # Accept last_scheduled as the base to stay true to the intent.
        return last_scheduled + timedelta(seconds=recurrence["seconds"])

    if r_type == "daily":
        return last_scheduled + timedelta(days=1)

    if r_type == "weekly":
        return last_scheduled + timedelta(weeks=1)

    if r_type == "monthly":
        return _add_month(last_scheduled)

    raise ValueError(f"Unknown recurrence type: {r_type!r}")


def _add_month(dt: datetime) -> datetime:
    """Advance a datetime by exactly one calendar month."""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


# ── Human-readable description ────────────────────────────────────────────────

def describe_recurrence(recurrence: dict) -> str:
    """Return a short English description of a recurrence rule."""
    r_type = recurrence["type"]

    if r_type == "interval":
        seconds = recurrence["seconds"]
        if seconds % 3600 == 0:
            n = seconds // 3600
            return f"Every {n} hour{'s' if n != 1 else ''}"
        n = seconds // 60
        return f"Every {n} minute{'s' if n != 1 else ''}"

    if r_type == "daily":
        return f"Every day at {recurrence['hour']:02d}:{recurrence['minute']:02d}"

    if r_type == "weekly":
        day_name = _WEEKDAY_NAMES_EN[recurrence["weekday"]]
        return f"Every {day_name} at {recurrence['hour']:02d}:{recurrence['minute']:02d}"

    if r_type == "monthly":
        day = recurrence["day"]
        # Ordinal suffix: 1st, 2nd, 3rd, 4th...
        if 11 <= day <= 13:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return (
            f"Every month on the {day}{suffix} "
            f"at {recurrence['hour']:02d}:{recurrence['minute']:02d}"
        )

    return "Unknown recurrence"


# ── Date/time string parser (for the `when` command argument) ─────────────────

def parse_datetime(text: str, tz: pytz.BaseTzInfo) -> datetime:
    """
    Parse a user-supplied date/time string and return a timezone-aware datetime.

    Supported formats (all interpreted in the guild's timezone):
    • 2026-03-10 15:00
    • 10/03/2026 15:00
    • tomorrow at 9:00   /  tomorrow 9:00
    • today at 14:30     /  today 14:30
    • 9:00               (today if in the future, tomorrow otherwise)
    """
    text = text.strip().lower()
    now = datetime.now(tz)

    def _localize(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
        try:
            naive = datetime(year, month, day, hour, minute, 0)
        except ValueError as exc:
            raise ValueError(f"Invalid date/time: {exc}") from exc
        return tz.localize(naive, is_dst=None)

    # YYYY-MM-DD HH:MM
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        return _localize(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]))

    # DD/MM/YYYY HH:MM
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})", text)
    if m:
        return _localize(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]))

    # today [at] HH:MM
    m = re.fullmatch(r"today\s+(?:at\s+)?(\d{1,2}):(\d{2})", text)
    if m:
        return now.replace(
            hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0,
        )

    # tomorrow [at] HH:MM
    m = re.fullmatch(r"tomorrow\s+(?:at\s+)?(\d{1,2}):(\d{2})", text)
    if m:
        return (now + timedelta(days=1)).replace(
            hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0,
        )

    # HH:MM only — today if still in the future, otherwise tomorrow
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        target = now.replace(
            hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return target

    raise ValueError(
        "Date format not recognised. Supported examples:\n"
        "• `2026-03-10 15:00`\n"
        "• `10/03/2026 15:00`\n"
        "• `tomorrow at 9:00`\n"
        "• `today at 14:30`\n"
        "• `9:00`  *(today if in the future, tomorrow otherwise)*"
    )
