from conftest import load_fixture
from watcher import discover_fee_groups, parse_timesheet


def test_discover_fee_groups_eastlake_default_view():
    html = load_fixture("eastlake_calendar.html")
    groups = discover_fee_groups(html, target_date="2026-05-30")

    # Saturday 30 May should have WEEKEND and SUNDOWNER active, not the MON-FRI groups.
    by_id = {g["fee_group_id"]: g for g in groups}
    assert "10230961" in by_id
    assert "3784606" in by_id
    assert "3784032" not in by_id  # MON-FRI before 1pm — not active on Saturday
    assert "9270373" not in by_id  # MON-FRI 1pm-2pm — not active on Saturday
    assert "WEEKEND" in by_id["10230961"]["label"].upper()
    assert "SUNDOWNER" in by_id["3784606"]["label"].upper()


def test_discover_fee_groups_navigated_calendar():
    """The Jun 20 calendar fixture is a navigated view, not the default 6-day window."""
    html = load_fixture("eastlake_calendar_jun20.html")
    groups = discover_fee_groups(html, target_date="2026-06-20")
    by_id = {g["fee_group_id"]: g for g in groups}
    # Saturday June 20 — same WEEKEND + SUNDOWNER pattern.
    assert "10230961" in by_id
    assert "3784606" in by_id


def test_discover_fee_groups_returns_empty_when_date_outside_window():
    """Asking for a date the calendar doesn't show returns empty (the caller will retry with a navigated URL)."""
    html = load_fixture("eastlake_calendar.html")
    groups = discover_fee_groups(html, target_date="2030-01-01")
    assert groups == []


def test_parse_timesheet_eastlake_sundowner_all_open():
    html = load_fixture("eastlake_sundowner.html")
    slots = parse_timesheet(html)
    # Mid-afternoon slots — fixture captured before any bookings, all 4 spots free.
    assert slots["02:30 pm"] == {"free": 4, "taken": 0}
    assert slots["04:00 pm"] == {"free": 4, "taken": 0}
    # First slot of the day was partially booked when captured.
    assert slots["02:10 pm"]["free"] + slots["02:10 pm"]["taken"] == 4


def test_parse_timesheet_eastlake_weekend_mixed():
    html = load_fixture("eastlake_weekend.html")
    slots = parse_timesheet(html)
    # 3 time blocks in the weekend fixture, each 4 cells.
    assert len(slots) == 3
    for time, counts in slots.items():
        assert counts["free"] + counts["taken"] == 4, f"{time}: cell count != 4"


def test_parse_timesheet_strips_js_string_literals():
    """JavaScript at the bottom of the page contains literal 'cell-available' strings.
    These must not pollute cell counts.
    """
    html = load_fixture("eastlake_weekend.html")
    slots = parse_timesheet(html)
    # If JS strings leaked in, totals would exceed 4 cells per time block.
    assert all(c["free"] + c["taken"] == 4 for c in slots.values())
