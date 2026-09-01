import json
from unittest.mock import patch
from pathlib import Path
import pytest

from conftest import load_fixture
import watcher


@pytest.fixture
def repo_tree(tmp_path, monkeypatch):
    """Set up a temp repo tree with config + state files and chdir into it."""
    # Pin "today" so the suite is independent of the real wall clock — the
    # fixtures use 2026-05-30 dates, which must read as future/today, not past.
    monkeypatch.setattr(watcher, "_today_iso", lambda: "2026-05-30")
    courses = [{
        "key": "eastlake",
        "name": "Eastlake Golf Club",
        "base_url": "https://www.eastlakegolfclub.com.au",
        "booking_resource_id": 3000000,
    }]
    (tmp_path / "courses.json").write_text(json.dumps(courses))
    (tmp_path / "watchlist.json").write_text(json.dumps([
        {
            "course": "eastlake",
            "date": "2026-05-30",
            "earliest": "14:00",
            "latest": "16:00",
            "min_spots": 2,
            "label": "Sat arvo",
        }
    ]))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _fake_calendar(_course, _date):
    return load_fixture("eastlake_calendar.html")


def _fake_timesheet(_course, _date, fee_group_id):
    return {
        "10230961": load_fixture("eastlake_weekend.html"),
        "3784606": load_fixture("eastlake_sundowner.html"),
    }[fee_group_id]


def test_first_run_with_empty_state_alerts_on_all_open_slots(repo_tree):
    """state.json starts as {} -> alert on every open slot (no seed-run suppression)."""
    (repo_tree / "state.json").write_text("{}")
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert") as mock_post:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    mock_post.assert_called()  # alerts fired for open slots
    state = json.loads((repo_tree / "state.json").read_text())
    assert "slots" in state and len(state["slots"]) > 0  # state was written


def test_second_run_alerts_on_newly_opened_slot(repo_tree):
    """Prior state had a slot fully booked; current state shows it open -> alert."""
    # Seed prior state with the SUNDOWNER 02:30 slot fully booked.
    prior = {
        "last_run": "2026-05-26T00:00:00Z",
        "slots": {
            "eastlake|2026-05-30|02:30 pm|3784606": {
                "free": 0,
                "fee_group_label": "SUNDOWNER (after 2:00pm)",
            }
        },
    }
    (repo_tree / "state.json").write_text(json.dumps(prior))
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert") as mock_post:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    # The fixture has 02:30 with 4 free, watch needs >=2 inside 14:00-16:00 -> 1 alert.
    assert mock_post.call_count >= 1
    posted_matches = [call.args[1] for call in mock_post.call_args_list]
    assert any(m["time"] == "02:30 pm" for m in posted_matches)


def test_unconfirmed_alert_is_retried_next_run(repo_tree):
    """post_alert returning None (429 exhaustion / missing id) must not silently
    swallow the alert: the slot is dropped from state so the next run re-fires it."""
    (repo_tree / "state.json").write_text("{}")
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert", return_value=None) as mock_post:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    assert mock_post.call_count >= 1
    # The unconfirmed slot must be absent from persisted state so the de-dupe
    # guard (prior_free >= min_spots) doesn't skip it on the next run.
    state = json.loads((repo_tree / "state.json").read_text())
    assert "eastlake|2026-05-30|02:30 pm|3784606" not in state.get("slots", {})
    assert state.get("posted_messages", {}) == {}


def test_booked_slot_strikes_previously_posted_message(repo_tree):
    """Slot was alerted on a previous run; now it's below min_spots -> strike + drop."""
    prior = {
        "last_run": "2026-05-26T00:00:00Z",
        "slots": {
            # 02:30 pm slot used to be open with free=4 (when we posted the alert).
            "eastlake|2026-05-30|02:30 pm|3784606": {
                "free": 4,
                "fee_group_label": "SUNDOWNER (after 2:00pm)",
            }
        },
        "posted_messages": {
            "eastlake|2026-05-30|02:30 pm|3784606": {
                "message_id": "999",
                "content": "@here 🏌️ Eastlake Sat 30 May 02:30 pm\n4 of 4 spots open",
                "min_spots": 2,
            }
        },
    }
    (repo_tree / "state.json").write_text(json.dumps(prior))

    # Make the current scrape show all relevant slots as fully booked so no
    # NEW alerts fire — we only want to exercise the strikethrough path.
    def _booked_timesheet(_course, _date, _fee_group_id):
        return (
            '<script>var publicCaptchaEnabled = false;</script>'
            '<h3>02:30 pm</h3>'
            '<div class="cell-taken"></div>'
            '<div class="cell-taken"></div>'
            '<div class="cell-taken"></div>'
            '<div class="cell-taken"></div>'
        )

    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_booked_timesheet), \
         patch("watcher.post_alert") as mock_post, \
         patch("watcher.edit_message_to_strikethrough") as mock_edit:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])

    assert exit_code == 0
    mock_post.assert_not_called()  # no new alerts — slot is gone, not opening
    mock_edit.assert_called_once()
    args, _ = mock_edit.call_args
    assert args[1] == "999"  # message_id
    assert "Eastlake" in args[2]  # original content was passed through

    # And state no longer tracks the (now-struck) message.
    new_state = json.loads((repo_tree / "state.json").read_text())
    assert new_state.get("posted_messages", {}) == {}


def test_count_change_edits_existing_message(repo_tree):
    """A tracked slot whose free count changes (but stays >= min_spots) gets its
    Discord message edited in place — not re-posted, not struck."""
    match = {
        "course": "eastlake", "course_name": "Eastlake Golf Club",
        "date": "2026-05-30", "time": "02:30 pm",
        "fee_group_id": "3784606", "fee_group_label": "SUNDOWNER (after 2:00pm)",
        "free": 2, "watch_label": "Sat arvo", "min_spots": 2,
        "booking_url": "https://example/book",
    }
    # Narrow the watch window to exactly 02:30 pm so the many other open fixture
    # slots in 14:00-16:00 don't register as new matches — we want to isolate the
    # count-change path for this one tracked slot.
    (repo_tree / "watchlist.json").write_text(json.dumps([
        {"course": "eastlake", "date": "2026-05-30", "earliest": "14:30",
         "latest": "14:30", "min_spots": 2, "label": "Sat arvo"}
    ]))
    prior = {
        "last_run": "2026-05-26T00:00:00Z",
        # prior free=2 == min_spots, so find_new_matches does NOT re-fire a new alert.
        "slots": {
            "eastlake|2026-05-30|02:30 pm|3784606": {
                "free": 2, "fee_group_label": "SUNDOWNER (after 2:00pm)",
            }
        },
        # posted_messages tracks the message at free=2; fixture scrape shows free=4.
        "posted_messages": {
            "eastlake|2026-05-30|02:30 pm|3784606": {"message_id": "111", "match": match},
        },
    }
    (repo_tree / "state.json").write_text(json.dumps(prior))

    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert") as mock_post, \
         patch("watcher.edit_message", return_value=True) as mock_edit:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])

    assert exit_code == 0
    mock_post.assert_not_called()  # it's an edit, not a new post
    mock_edit.assert_called_once()
    args, _ = mock_edit.call_args
    assert args[1] == "111"          # edited the tracked message id
    assert "2 of 4" not in args[2]   # not the stale count...
    assert "4 of 4" in args[2]       # ...the new count
    assert "was 2" in args[2]        # and the previous count

    # State now tracks the updated free count.
    new_state = json.loads((repo_tree / "state.json").read_text())
    pm = new_state["posted_messages"]["eastlake|2026-05-30|02:30 pm|3784606"]
    assert pm["match"]["free"] == 4


def test_dry_run_does_not_post_or_write_state(repo_tree):
    (repo_tree / "state.json").write_text("{}")
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert") as mock_post:
        watcher.main(["--webhook", "https://discord/fake", "--dry-run"])
    mock_post.assert_not_called()
    # state.json untouched
    assert (repo_tree / "state.json").read_text() == "{}"


def test_past_dated_watchlist_entries_are_reaped(repo_tree, monkeypatch):
    """Entries with date < today - 1 are removed from watchlist.json on commit.

    Uses only past-dated entries so the scrape loop is naturally bypassed —
    the reap-and-write step still runs because the watchlist file changed.
    """
    monkeypatch.setattr(watcher, "_today_iso", lambda: "2026-06-15")
    (repo_tree / "watchlist.json").write_text(json.dumps([
        # date < today-1 -> reaped
        {"course": "eastlake", "date": "2026-05-30", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Old entry"},
        # date == today-1 -> kept by reap, but date < today so not in active scrape
        {"course": "eastlake", "date": "2026-06-14", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Future entry"},
    ]))
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    with patch("watcher.fetch_calendar") as fc, \
         patch("watcher.fetch_timesheet") as ft, \
         patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
        # No active entries -> scrape loop should never call the fetchers.
        fc.assert_not_called()
        ft.assert_not_called()
    assert exit_code == 0
    remaining = json.loads((repo_tree / "watchlist.json").read_text())
    assert len(remaining) == 1
    assert remaining[0]["label"] == "Future entry"


def test_date_outside_booking_horizon_is_skipped_not_fatal(repo_tree):
    """Calendar HTML is healthy (has booking cells) but none for the target date.
    This is just outside the course's booking horizon - log a WARN and continue,
    don't fatal."""
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    # Watch is for a date the fixture calendar doesn't cover - but the fixture
    # itself has plenty of booking cells for adjacent dates.
    (repo_tree / "watchlist.json").write_text(json.dumps([
        {"course": "eastlake", "date": "2030-01-01", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Far future"}
    ]))
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert") as mock_post:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    mock_post.assert_not_called()


def test_unknown_course_in_watchlist_is_fatal(repo_tree):
    (repo_tree / "watchlist.json").write_text(json.dumps([
        {"course": "nonexistent", "date": "2026-06-20", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Bad"}
    ]))
    (repo_tree / "state.json").write_text("{}")
    with patch("watcher.fetch_calendar"), patch("watcher.fetch_timesheet"), patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code != 0


def test_captcha_enabled_page_is_fatal(repo_tree):
    """publicCaptchaEnabled = true means the platform has switched CAPTCHA on; fatal."""
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    (repo_tree / "watchlist.json").write_text(json.dumps([
        {"course": "eastlake", "date": "2026-05-30", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Watch this"}
    ]))
    captcha_on_html = """
    <html><head></head><body>
    <script>
      var publicCaptchaEnabled = true;
    </script>
    </body></html>
    """
    with patch("watcher.fetch_calendar", return_value=captcha_on_html), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 3


def test_unfamiliar_response_is_fatal(repo_tree):
    """Response without the publicCaptchaEnabled marker isn't a known booking page; fatal."""
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    (repo_tree / "watchlist.json").write_text(json.dumps([
        {"course": "eastlake", "date": "2026-05-30", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Watch this"}
    ]))
    cloudflare_like_html = "<html><body><div>verify you are human</div></body></html>"
    with patch("watcher.fetch_calendar", return_value=cloudflare_like_html), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 3


def test_healthy_page_with_no_cells_is_horizon_skip(repo_tree):
    """publicCaptchaEnabled = false but no booking cells -> date past this course's
    booking horizon; non-fatal skip."""
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    (repo_tree / "watchlist.json").write_text(json.dumps([
        {"course": "eastlake", "date": "2026-05-30", "earliest": "14:00", "latest": "16:00",
         "min_spots": 2, "label": "Watch this"}
    ]))
    empty_but_healthy_html = """
    <html><head><title>Eastlake</title></head><body>
    <script>
      var publicCaptchaEnabled = false;
    </script>
    <div id="navigation"></div>
    </body></html>
    """
    with patch("watcher.fetch_calendar", return_value=empty_but_healthy_html), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert") as mock_post:
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    mock_post.assert_not_called()


def test_purge_state_keys_env_emits_loud_warning(repo_tree, monkeypatch, capsys):
    """PURGE_STATE_KEYS re-fires alerts every run until the env var is cleared,
    so its presence must be surfaced loudly, not silently honoured."""
    (repo_tree / "state.json").write_text("{}")
    monkeypatch.setenv("PURGE_STATE_KEYS", "eastlake|2026-05-30|02:30 pm|3784606")
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert"):
        watcher.main(["--webhook", "https://discord/fake"])
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "PURGE_STATE_KEYS" in err


def test_dump_state_env_is_no_longer_a_special_early_exit(repo_tree, monkeypatch):
    """The DUMP_STATE diagnostic shipped in production main(); it should be gone
    so DUMP_STATE=1 just runs a normal scrape instead of dumping and exiting."""
    (repo_tree / "state.json").write_text("{}")
    monkeypatch.setenv("DUMP_STATE", "1")
    with patch("watcher.fetch_calendar", side_effect=_fake_calendar) as fc, \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    fc.assert_called()  # scraped normally rather than short-circuiting on DUMP_STATE


def test_main_generates_rolling_watchlist_when_file_absent(repo_tree):
    """No watchlist.json -> production generation mode rolls upcoming weekends.

    repo_tree pins today to 2026-05-30 (a Saturday) with only eastlake, so the
    generated watch must drive a scrape of that Saturday."""
    (repo_tree / "watchlist.json").unlink()
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    seen_dates = []

    def _cal(course, date):
        seen_dates.append(date)
        return load_fixture("eastlake_calendar.html")

    with patch("watcher.fetch_calendar", side_effect=_cal), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    assert "2026-05-30" in seen_dates  # generated and scraped the pinned Saturday


def test_main_manual_watchlist_file_overrides_generation(repo_tree):
    """When watchlist.json IS present it remains the source of truth (manual mode),
    so only its dates are scraped, not generated weekends."""
    (repo_tree / "state.json").write_text(json.dumps({"slots": {}}))
    # repo_tree already wrote a single 2026-05-30 entry. Scrape must hit only it.
    seen_dates = []

    def _cal(course, date):
        seen_dates.append(date)
        return load_fixture("eastlake_calendar.html")

    with patch("watcher.fetch_calendar", side_effect=_cal), \
         patch("watcher.fetch_timesheet", side_effect=_fake_timesheet), \
         patch("watcher.post_alert"):
        exit_code = watcher.main(["--webhook", "https://discord/fake"])
    assert exit_code == 0
    assert set(seen_dates) == {"2026-05-30"}  # only the file's entry, no rolling dates
