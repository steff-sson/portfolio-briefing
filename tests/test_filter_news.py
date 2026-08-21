"""Regression tests for scripts.filter_news RSS date parsing (Phase 1).

Der RSS-Datumsparser interpretierte frueher die Jahreszahl (z.B. 2026) als
Unix-Timestamp -> 1. Januar 1970, dann .replace(year=...) -> 1. Januar 2026
00:00 Uhr. Monat/Tag/Stunde gingen verloren, Eintraege wurden faelschlich
als aelter als 7 Tage gefiltert. Fix: calendar.timegm(pp) direkt.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from scripts.filter_news import MAX_AGE_DAYS, _is_recent, _parse_published


def _struct_time(y: int, mo: int, d: int, h: int = 12, mi: int = 0, s: int = 0) -> time.struct_time:
    return time.struct_time((y, mo, d, h, mi, s, 0, 1, 0))


def test_published_parsed_preserves_full_datetime():
    """published_parsed (struct_time) -> korrektes Datum UND Uhrzeit (UTC)."""
    pp = _struct_time(2026, 8, 15, 10, 30, 0)
    parsed = _parse_published({"published_parsed": pp})
    assert parsed == datetime(2026, 8, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_published_parsed_keeps_month_day_hour():
    """Regression: Monat/Tag/Stunde duerfen nicht verloren gehen (Bug: 1. Januar)."""
    pp = _struct_time(2026, 8, 15, 10, 30, 0)
    parsed = _parse_published({"published_parsed": pp})
    assert parsed is not None
    assert parsed.month == 8
    assert parsed.day == 15
    assert parsed.hour == 10
    assert parsed.minute == 30
    assert parsed.year == 2026


def test_published_parsed_utc_aware():
    """Fix liefert tz-aware datetime (UTC) statt naive datetime."""
    pp = _struct_time(2026, 8, 15, 10, 30, 0)
    parsed = _parse_published({"published_parsed": pp})
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_published_parsed_midnight():
    """Eintrag um 00:00 Uhr bleibt 00:00 Uhr (kein Offset durch falschen Timestamp)."""
    pp = _struct_time(2026, 8, 15, 0, 0, 0)
    parsed = _parse_published({"published_parsed": pp})
    assert parsed == datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)


def test_strptime_fallback_preserved():
    """Fallback-Pfad (kein published_parsed) bleibt erhalten."""
    entry = {"published": "Sat, 15 Aug 2026 10:30:00 +0000"}
    parsed = _parse_published(entry)
    assert parsed == datetime(2026, 8, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_missing_dates_return_none():
    assert _parse_published({}) is None
    assert _parse_published({"published": "not a date"}) is None


def test_is_recent_true_for_today():
    """Eintrag von heute -> _is_recent True."""
    now = datetime.now(tz=timezone.utc)
    entry = {"published_parsed": now.utctimetuple()}
    assert _is_recent(entry) is True


def test_is_recent_false_for_older_than_7_days():
    """Eintrag von vor > MAX_AGE_DAYS -> _is_recent False."""
    old = datetime.now(tz=timezone.utc) - timedelta(days=MAX_AGE_DAYS + 1)
    entry = {"published_parsed": old.utctimetuple()}
    assert _is_recent(entry) is False


def test_is_recent_boundary_within_7_days():
    """Eintrag von vor 6 Tagen -> _is_recent True (Grenzwert)."""
    recent = datetime.now(tz=timezone.utc) - timedelta(days=MAX_AGE_DAYS - 1)
    entry = {"published_parsed": recent.utctimetuple()}
    assert _is_recent(entry) is True


def test_is_recent_missing_date_false():
    assert _is_recent({}) is False
