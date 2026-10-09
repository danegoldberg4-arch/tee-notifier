"""Unit tests for pure helpers: timezone-aware 'today' and watchlist generation."""
from datetime import datetime, timezone

import watcher


def test_today_iso_uses_sydney_local_date(monkeypatch):
    """At UTC 22:30 the Sydney date is already the next day (UTC+10, no DST in July).

    The cron's 22:00-23:59 UTC block runs during AU 08:00-09:59, prime booking
    hours. 'today' must reflect the Sydney date the tee sheets use, not UTC.
    """
    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 3, 22, 30, tzinfo=timezone.utc)  # Sydney 2026-07-04 08:30
            return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)

    monkeypatch.setattr(watcher, "datetime", _FixedDateTime)
    assert watcher._today_iso() == "2026-07-04"


# 2026-06-29 is a Monday; the first weekend is Sat 2026-07-04 / Sun 2026-07-05.

def test_generate_watchlist_covers_upcoming_weekends_for_all_courses():
    courses = [{"key": "eastlake"}, {"key": "moorepark"}]
    wl = watcher.generate_watchlist(courses, "2026-06-29", weeks=1)
    assert sorted({e["date"] for e in wl}) == ["2026-07-04", "2026-07-05"]
    assert len(wl) == 2 * 2  # one entry per course per weekend day
    assert all(e["earliest"] == "06:00" and e["latest"] == "13:30" for e in wl)
    assert all(e["min_spots"] == 2 for e in wl)


def test_generate_watchlist_carries_per_course_fee_filter():
    courses = [{"key": "eastlake"}, {"key": "moorepark", "fee_group_contains": "18"}]
    wl = watcher.generate_watchlist(courses, "2026-06-29", weeks=1)
    assert all("fee_group_contains" not in e for e in wl if e["course"] == "eastlake")
    assert all(e["fee_group_contains"] == "18" for e in wl if e["course"] == "moorepark")


def test_generate_watchlist_labels_saturday_and_sunday():
    wl = watcher.generate_watchlist([{"key": "eastlake"}], "2026-06-29", weeks=1)
    labels = {e["date"]: e["label"] for e in wl}
    assert labels["2026-07-04"] == "Sat morning"
    assert labels["2026-07-05"] == "Sun morning"


def test_generate_watchlist_never_emits_past_dates():
    """Generation starts at today, so a watch can never run dry or look backward."""
    wl = watcher.generate_watchlist([{"key": "eastlake"}], "2026-06-29", weeks=4)
    assert wl, "expected several weekends of entries"
    assert all(e["date"] >= "2026-06-29" for e in wl)
