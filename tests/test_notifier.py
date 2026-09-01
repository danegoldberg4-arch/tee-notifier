from unittest.mock import patch, MagicMock
from watcher import (
    build_discord_payload,
    post_alert,
    strikethrough_content,
    edit_message_to_strikethrough,
    build_count_update_content,
    edit_message,
)


SAMPLE_MATCH = {
    "course": "eastlake",
    "course_name": "Eastlake Golf Club",
    "date": "2026-05-30",
    "time": "02:30 pm",
    "fee_group_id": "3784606",
    "fee_group_label": "SUNDOWNER (after 2:00pm)",
    "free": 3,
    "total": 4,
    "watch_label": "Sat arvo with mates",
    "min_spots": 2,
    "booking_url": "https://www.eastlakegolfclub.com.au/guests/bookings/ViewPublicTimesheet.msp?bookingResourceId=3000000&selectedDate=2026-05-30&feeGroupId=3784606",
}


def test_payload_starts_with_at_here_mention():
    payload = build_discord_payload(SAMPLE_MATCH)
    assert payload["content"].startswith("@here")


def test_payload_includes_allowed_mentions_everyone():
    """Discord webhooks silently swallow @here without this opt-in."""
    payload = build_discord_payload(SAMPLE_MATCH)
    assert payload["allowed_mentions"] == {"parse": ["everyone"]}


def test_payload_wraps_url_in_angle_brackets():
    """Suppresses Discord's embed preview for a cleaner notification."""
    payload = build_discord_payload(SAMPLE_MATCH)
    assert f"<{SAMPLE_MATCH['booking_url']}>" in payload["content"]


def test_payload_includes_match_details():
    payload = build_discord_payload(SAMPLE_MATCH)
    content = payload["content"]
    assert "Eastlake Golf Club" in content
    assert "02:30 pm" in content
    assert "3 of 4" in content
    assert "SUNDOWNER" in content
    assert "Sat arvo with mates" in content


def test_post_alert_calls_webhook_with_payload_and_returns_message_id():
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {"id": "1234567890"}
    with patch("watcher.requests.post", return_value=fake_response) as mock_post:
        message_id = post_alert("https://discord.com/api/webhooks/fake/url", SAMPLE_MATCH)
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    # wait=true is required so Discord returns the posted message object.
    assert args[0] == "https://discord.com/api/webhooks/fake/url?wait=true"
    assert kwargs["json"]["content"].startswith("@here")
    assert message_id == "1234567890"


def test_post_alert_raises_on_http_error():
    fake_response = MagicMock(status_code=500)
    fake_response.raise_for_status.side_effect = RuntimeError("500")
    with patch("watcher.requests.post", return_value=fake_response):
        try:
            post_alert("https://example/webhook", SAMPLE_MATCH)
        except RuntimeError as exc:
            assert "500" in str(exc)
        else:
            raise AssertionError("expected RuntimeError to propagate")


def test_strikethrough_drops_at_here_and_wraps_each_line():
    original = (
        "@here 🏌️ **Eastlake Golf Club** — Sat 30 May, 02:30 pm\n"
        "3 of 4 spots open · _SUNDOWNER (after 2:00pm)_\n"
        "Matches **\"Sat arvo\"** (need ≥2)\n"
        "<https://example.com/booking>"
    )
    result = strikethrough_content(original)
    # @here is dropped on the edited copy — no double-mention.
    assert "@here" not in result
    # Each visible line gets ~~...~~ wrapping.
    assert "~~🏌️ **Eastlake Golf Club** — Sat 30 May, 02:30 pm~~" in result
    assert "~~<https://example.com/booking>~~" in result
    # And we tell the reader why it's struck.
    assert "no longer available" in result


def test_edit_message_uses_messages_endpoint_and_strips_query_string():
    """PATCH must target /messages/{id} on the bare webhook URL, no ?wait=true leakage."""
    fake_response = MagicMock(status_code=200)
    with patch("watcher.requests.patch", return_value=fake_response) as mock_patch:
        edit_message_to_strikethrough(
            "https://discord.com/api/webhooks/fake/url?wait=true",
            "9876",
            "@here some content\nline2",
        )
    mock_patch.assert_called_once()
    args, kwargs = mock_patch.call_args
    assert args[0] == "https://discord.com/api/webhooks/fake/url/messages/9876"
    assert "~~some content~~" in kwargs["json"]["content"]
    assert kwargs["json"]["allowed_mentions"] == {"parse": []}


def test_edit_message_treats_404_as_success():
    """If the message was already deleted, just stop tracking — don't error."""
    fake_response = MagicMock(status_code=404)
    fake_response.raise_for_status.side_effect = RuntimeError("should not be called on 404")
    with patch("watcher.requests.patch", return_value=fake_response):
        edit_message_to_strikethrough("https://hook/x", "1", "some text")


def test_count_update_drops_at_here_and_shows_decrease():
    """4 -> 2 should show the new count and the previous count, with no re-ping."""
    match = {**SAMPLE_MATCH, "free": 2}
    content = build_count_update_content(match, prev_free=4)
    assert "@here" not in content                 # editing must not re-ping
    assert "2 of 4" in content                    # current count
    assert "was 4" in content                     # previous count
    assert "📉" in content                         # down arrow for a decrease
    assert "SUNDOWNER" in content                 # still shows the fee group
    assert f"<{SAMPLE_MATCH['booking_url']}>" in content  # still clickable


def test_count_update_shows_increase_with_up_arrow():
    """2 -> 4 (more spots freed up) should read as an increase."""
    match = {**SAMPLE_MATCH, "free": 4}
    content = build_count_update_content(match, prev_free=2)
    assert "4 of 4" in content
    assert "was 2" in content
    assert "📈" in content


def test_payload_uses_dynamic_total_not_hardcoded_four():
    """A course with a non-4-slot tee should show its real total, not 'of 4'."""
    match = {**SAMPLE_MATCH, "free": 2, "total": 3}
    content = build_discord_payload(match)["content"]
    assert "2 of 3" in content
    assert "2 of 4" not in content


def test_count_update_uses_dynamic_total():
    match = {**SAMPLE_MATCH, "free": 1, "total": 3}
    content = build_count_update_content(match, prev_free=3)
    assert "1 of 3" in content
    assert "was 3" in content


def test_edit_message_patches_content_and_returns_true():
    fake_response = MagicMock(status_code=200)
    with patch("watcher.requests.patch", return_value=fake_response) as mock_patch:
        alive = edit_message(
            "https://discord.com/api/webhooks/fake/url?wait=true",
            "555",
            "new body text",
        )
    assert alive is True
    args, kwargs = mock_patch.call_args
    assert args[0] == "https://discord.com/api/webhooks/fake/url/messages/555"
    assert kwargs["json"]["content"] == "new body text"
    assert kwargs["json"]["allowed_mentions"] == {"parse": []}


def test_edit_message_returns_false_on_404():
    fake_response = MagicMock(status_code=404)
    fake_response.raise_for_status.side_effect = RuntimeError("should not be called on 404")
    with patch("watcher.requests.patch", return_value=fake_response):
        alive = edit_message("https://hook/x", "1", "text")
    assert alive is False
