from watcher import find_new_matches


def make_slot(course, date, time, fee_group_id, fee_group_label, free):
    """Helper: build a single state entry."""
    key = f"{course}|{date}|{time}|{fee_group_id}"
    return key, {"free": free, "fee_group_label": fee_group_label}


def state_from(*entries):
    return dict(entries)


WATCH = [
    {
        "course": "eastlake",
        "date": "2026-05-30",
        "earliest": "14:00",
        "latest": "16:00",
        "min_spots": 2,
        "label": "Sat arvo",
    }
]


def test_no_change_returns_no_matches():
    state = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 4))
    matches = find_new_matches(prior_state=state, current_state=state, watchlist=WATCH)
    assert matches == []


def test_new_slot_inside_window_with_enough_spots_alerts():
    prior = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 0))
    current = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 3))
    matches = find_new_matches(prior, current, WATCH)
    assert len(matches) == 1
    m = matches[0]
    assert m["time"] == "02:30 pm"
    assert m["free"] == 3
    assert m["fee_group_id"] == "3784606"
    assert m["watch_label"] == "Sat arvo"


def test_new_slot_outside_time_window_does_not_alert():
    prior = state_from(make_slot("eastlake", "2026-05-30", "06:30 pm", "3784606", "SUNDOWNER", 0))
    current = state_from(make_slot("eastlake", "2026-05-30", "06:30 pm", "3784606", "SUNDOWNER", 4))
    matches = find_new_matches(prior, current, WATCH)
    assert matches == []


def test_new_slot_with_too_few_spots_does_not_alert():
    prior = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 0))
    current = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 1))
    matches = find_new_matches(prior, current, WATCH)
    assert matches == []


def test_fee_group_filter_matches_case_insensitive():
    watch = [{**WATCH[0], "fee_group_contains": "sundowner"}]
    prior = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER (after 2:00pm)", 0))
    current = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER (after 2:00pm)", 3))
    matches = find_new_matches(prior, current, watch)
    assert len(matches) == 1


def test_fee_group_filter_excludes_non_matching_groups():
    watch = [{**WATCH[0], "fee_group_contains": "Twilight"}]
    prior = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 0))
    current = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 3))
    matches = find_new_matches(prior, current, watch)
    assert matches == []


def test_missing_prior_entry_treated_as_zero_free():
    """If we just added a new fee group to courses.json, prior won't have it.
    The slot should alert if it matches a watch, treating prior as 0 spots."""
    prior = {}
    current = state_from(make_slot("eastlake", "2026-05-30", "02:30 pm", "3784606", "SUNDOWNER", 3))
    matches = find_new_matches(prior, current, WATCH)
    assert len(matches) == 1


def test_time_at_window_boundaries_inclusive():
    """earliest=14:00 latest=16:00 means 2:00pm and 4:00pm both count."""
    prior = state_from(
        make_slot("eastlake", "2026-05-30", "02:00 pm", "3784606", "SUNDOWNER", 0),
        make_slot("eastlake", "2026-05-30", "04:00 pm", "3784606", "SUNDOWNER", 0),
    )
    current = state_from(
        make_slot("eastlake", "2026-05-30", "02:00 pm", "3784606", "SUNDOWNER", 3),
        make_slot("eastlake", "2026-05-30", "04:00 pm", "3784606", "SUNDOWNER", 3),
    )
    matches = find_new_matches(prior, current, WATCH)
    assert {m["time"] for m in matches} == {"02:00 pm", "04:00 pm"}
