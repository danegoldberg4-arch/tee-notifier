"""Tee Notifier — scrapes golf course tee sheets and pings Discord."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import requests


_SCRIPT_BLOCK_RE = re.compile(r"<script\b.*?</script>", re.DOTALL | re.IGNORECASE)
_FEE_GROUP_ROW_RE = re.compile(
    r'<div\s+class="row\s+feeGroupRow\s+feeGroupId-(?P<id>\d+)[^"]*"[^>]*>(?P<body>.*?)(?=<div\s+class="row\s+feeGroupRow|</div>\s*</div>\s*</div>\s*<!--|\Z)',
    re.DOTALL | re.IGNORECASE,
)
_LABEL_RE = re.compile(r"<h3>\s*(?P<label>[^<]+?)\s*</h3>", re.IGNORECASE)
_CELL_DATE_RE = re.compile(
    r"redirectToTimesheet\(\s*'(?P<fee>\d+)'\s*,\s*'(?P<date>\d{4}-\d{2}-\d{2})'\s*\)",
    re.IGNORECASE,
)


def _strip_scripts(html: str) -> str:
    return _SCRIPT_BLOCK_RE.sub("", html)


_CAPTCHA_FLAG_TRUE_RE = re.compile(r"var\s+publicCaptchaEnabled\s*=\s*true", re.IGNORECASE)
_CAPTCHA_FLAG_PRESENT_RE = re.compile(r"var\s+publicCaptchaEnabled\s*=", re.IGNORECASE)


def _looks_like_captcha_or_broken(html: str) -> bool:
    """True if the response isn't a structurally healthy calendar page.

    Detects two cases that should be fatal:
    - The booking platform explicitly enabled CAPTCHA (`publicCaptchaEnabled = true`)
    - We got a completely different response (no `publicCaptchaEnabled` line at all)

    A page that contains `publicCaptchaEnabled = false` but no booking cells is
    a legitimate "date outside this course's booking horizon" — not a failure.
    """
    if _CAPTCHA_FLAG_TRUE_RE.search(html):
        return True
    if not _CAPTCHA_FLAG_PRESENT_RE.search(html):
        return True
    return False


def discover_fee_groups(html: str, target_date: str) -> list[dict]:
    """Return active fee groups for ``target_date`` from a ViewPublicCalendar page.

    Each result: {"fee_group_id": str, "label": str}.
    A fee group is "active" if the calendar exposes a clickable cell for that date.
    """
    cleaned = _strip_scripts(html)

    fee_ids_with_target_date: set[str] = {
        m.group("fee") for m in _CELL_DATE_RE.finditer(cleaned)
        if m.group("date") == target_date
    }
    if not fee_ids_with_target_date:
        return []

    # Walk each feeGroupRow block and pair its id with its <h3> label.
    results: list[dict] = []
    seen: set[str] = set()
    for m in _FEE_GROUP_ROW_RE.finditer(cleaned):
        fid = m.group("id")
        if fid not in fee_ids_with_target_date or fid in seen:
            continue
        seen.add(fid)
        label_match = _LABEL_RE.search(m.group("body"))
        label = label_match.group("label").strip() if label_match else ""
        results.append({"fee_group_id": fid, "label": label})
    return results


_TIME_BLOCK_RE = re.compile(
    r"<h3>\s*(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*</h3>(?P<body>.*?)(?=<h3>\s*\d{1,2}:\d{2}\s*[ap]m|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def parse_timesheet(html: str) -> dict[str, dict]:
    """Parse a ViewPublicTimesheet page into {time_str: {free, taken}}.

    time_str is the lowercase rendered time as shown on the booking page (e.g. "02:30 pm").
    Each <h3>HH:MM am/pm</h3> heading is followed by 4 booking cells.
    """
    cleaned = _strip_scripts(html)
    slots: dict[str, dict] = {}
    for m in _TIME_BLOCK_RE.finditer(cleaned):
        time_str = m.group("time").lower().strip()
        body = m.group("body")
        free = len(re.findall(r"cell-available", body))
        taken = len(re.findall(r"cell-taken", body))
        slots[time_str] = {"free": free, "taken": taken}
    return slots


def _parse_clock(hhmm: str) -> int:
    """Convert '14:30' to 870."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _slot_time_to_minutes(slot_time: str) -> int:
    """Convert rendered '02:30 pm' to minutes-since-midnight."""
    dt = datetime.strptime(slot_time.strip().lower(), "%I:%M %p")
    return dt.hour * 60 + dt.minute


def find_new_matches(
    prior_state: dict,
    current_state: dict,
    watchlist: Iterable[dict],
) -> list[dict]:
    """Compare prior vs current state and return alerts for each watchlist match.

    A match fires when a slot is inside a watch's time window, the fee-group filter
    (if any) matches, current free spots >= min_spots, AND prior free spots < min_spots.
    The last condition is the de-dupe guard — we only ping on the transition.
    """
    matches: list[dict] = []
    for watch in watchlist:
        win_lo = _parse_clock(watch["earliest"])
        win_hi = _parse_clock(watch["latest"])
        min_spots = int(watch["min_spots"])
        fee_filter = (watch.get("fee_group_contains") or "").strip().lower()

        for key, slot in current_state.items():
            course, date, time_str, fee_group_id = key.split("|")
            if course != watch["course"] or date != watch["date"]:
                continue
            slot_minutes = _slot_time_to_minutes(time_str)
            if not (win_lo <= slot_minutes <= win_hi):
                continue
            if fee_filter and fee_filter not in slot["fee_group_label"].lower():
                continue
            if slot["free"] < min_spots:
                continue
            prior_free = prior_state.get(key, {"free": 0})["free"]
            if prior_free >= min_spots:
                continue  # was already alertable last run — don't ping again
            matches.append({
                "course": course,
                "date": date,
                "time": time_str,
                "fee_group_id": fee_group_id,
                "fee_group_label": slot["fee_group_label"],
                "free": slot["free"],
                "total": slot.get("total", slot["free"]),
                "watch_label": watch.get("label", ""),
                "min_spots": min_spots,
            })
    return matches


USER_AGENT = "Mozilla/5.0 (compatible; golf-watcher/1.0; +https://github.com/)"
HTTP_TIMEOUT_SECONDS = 20


def fetch_calendar(course: dict, target_date: str) -> str:
    """Fetch the ViewPublicCalendar page navigated to ``target_date``."""
    url = (
        f"{course['base_url']}/guests/bookings/ViewPublicCalendar.msp"
        f"?bookingResourceId={course['booking_resource_id']}"
        f"&selectedDate={target_date}"
    )
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def fetch_timesheet(course: dict, target_date: str, fee_group_id: str) -> str:
    url = (
        f"{course['base_url']}/guests/bookings/ViewPublicTimesheet.msp"
        f"?bookingResourceId={course['booking_resource_id']}"
        f"&selectedDate={target_date}"
        f"&feeGroupId={fee_group_id}"
    )
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def timesheet_url(course: dict, target_date: str, fee_group_id: str) -> str:
    """Public URL form (used in Discord messages)."""
    return (
        f"{course['base_url']}/guests/bookings/ViewPublicTimesheet.msp"
        f"?bookingResourceId={course['booking_resource_id']}"
        f"&selectedDate={target_date}"
        f"&feeGroupId={fee_group_id}"
    )

_MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _format_date_for_humans(iso_date: str) -> str:
    """'2026-05-30' -> 'Sat 30 May'."""
    y, m, d = (int(x) for x in iso_date.split("-"))
    return f"{_DAY_NAMES[_date(y, m, d).weekday()]} {d} {_MONTH_NAMES[m]}"


def build_discord_payload(match: dict) -> dict:
    pretty_date = _format_date_for_humans(match["date"])
    content = (
        f"@here 🏌️ **{match['course_name']}** — {pretty_date}, {match['time']}\n"
        f"{match['free']} of {match.get('total', 4)} spots open · _{match['fee_group_label']}_\n"
        f"Matches **\"{match['watch_label']}\"** (need ≥{match['min_spots']})\n"
        f"<{match['booking_url']}>"
    )
    return {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},
    }


def post_alert(webhook_url: str, match: dict, max_retries: int = 5) -> str | None:
    """Post the alert; return the Discord message ID so we can edit it later.

    Handles Discord's HTTP 429 rate limit by sleeping for the Retry-After period
    and retrying. Raises on other HTTP errors and after exhausting retries.
    """
    payload = build_discord_payload(match)
    url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
    for attempt in range(max_retries):
        response = requests.post(
            url,
            json=payload,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            # Discord may return Retry-After in seconds (text) or X-RateLimit-Reset-After (float secs).
            wait = float(response.headers.get("Retry-After")
                         or response.headers.get("X-RateLimit-Reset-After")
                         or 1.0)
            wait = min(max(wait, 1.0), 60.0)
            print(f"WARN: Discord rate-limited (attempt {attempt+1}/{max_retries}); sleeping {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        response.raise_for_status()
        try:
            return response.json().get("id")
        except Exception:
            return None
    print(f"ERROR: gave up on Discord post after {max_retries} retries", file=sys.stderr)
    return None


def strikethrough_content(original: str) -> str:
    """Rewrite a previously-posted alert's content into strikethrough form.

    Wraps each non-empty visible line in ``~~...~~`` and drops the leading
    ``@here`` mention (no need to re-ping on edit). Appends a small footer so
    readers know why the message is struck.
    """
    out: list[str] = []
    for line in original.split("\n"):
        bare = line[len("@here"):].lstrip() if line.startswith("@here") else line
        if not bare.strip():
            out.append("")
            continue
        out.append(f"~~{bare}~~")
    out.append("*— no longer available*")
    return "\n".join(out)


def build_count_update_content(match: dict, prev_free: int) -> str:
    """Rebuild an alert's content reflecting a changed free-spot count.

    Drops the @here mention (an edit must not re-ping) and annotates the spots
    line with the previous count + a direction arrow so readers see the change
    at a glance: e.g. "📉 now 2 of 4 spots · _SUNDOWNER_ (was 4)".
    """
    pretty_date = _format_date_for_humans(match["date"])
    free = match["free"]
    arrow = "📉" if free < prev_free else "📈"
    return (
        f"🏌️ **{match['course_name']}** — {pretty_date}, {match['time']}\n"
        f"{arrow} now {free} of {match.get('total', 4)} spots · _{match['fee_group_label']}_ (was {prev_free})\n"
        f"Matches **\"{match['watch_label']}\"** (need ≥{match['min_spots']})\n"
        f"<{match['booking_url']}>"
    )


def edit_message(webhook_url: str, message_id: str, content: str) -> bool:
    """PATCH a previously-posted Discord message to new content (no mention).

    Returns False if the message no longer exists (404 — deleted by hand) so the
    caller can stop tracking it; True on success. Any other HTTP error raises so
    the caller can retry next run.
    """
    base = webhook_url.split("?", 1)[0].rstrip("/")
    response = requests.patch(
        f"{base}/messages/{message_id}",
        json={"content": content, "allowed_mentions": {"parse": []}},
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def edit_message_to_strikethrough(webhook_url: str, message_id: str, original_content: str) -> None:
    """PATCH a previously-posted Discord message to strikethrough text.

    Treats a 404 as success (message was already deleted by hand) so we stop
    tracking it. Any other HTTP error raises so the caller can retry next run.
    """
    edit_message(webhook_url, message_id, strikethrough_content(original_content))


# Tee sheets and watchlist dates are expressed in Sydney local time. The cron
# runs in UTC, so 'today' must be derived in Sydney time or it slips a day during
# the AU-morning window (UTC 22:00-23:59 = AEST 08:00-09:59).
SYDNEY_TZ = ZoneInfo("Australia/Sydney")


COURSES_PATH = Path("courses.json")
WATCHLIST_PATH = Path("watchlist.json")
# STATE_DIR env var lets the container point state.json at a persistent
# volume (e.g. Railway mounts /data). Defaults to CWD for local + tests.
STATE_PATH = Path(os.environ.get("STATE_DIR", ".")) / "state.json"


def _today_iso() -> str:
    """Today's date in Sydney local time. Wrapped so tests can monkeypatch it."""
    return datetime.now(SYDNEY_TZ).date().isoformat()


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _reap_watchlist(watchlist: list[dict], today_iso: str) -> list[dict]:
    """Drop entries whose date is more than one day in the past."""
    cutoff = (datetime.fromisoformat(today_iso) - timedelta(days=1)).date().isoformat()
    return [w for w in watchlist if w["date"] >= cutoff]


# Rolling watchlist generation. When no watchlist.json is present, the watch is
# derived from courses.json so it never runs dry and needs no hand-editing.
DEFAULT_WATCH_WEEKS = 8
_WEEKEND_LABELS = {5: "Sat morning", 6: "Sun morning"}  # Mon=0 .. Sun=6


def generate_watchlist(
    courses: list[dict],
    today_iso: str,
    *,
    weeks: int = DEFAULT_WATCH_WEEKS,
    weekdays: tuple[int, ...] = (5, 6),
    earliest: str = "06:00",
    latest: str = "13:30",
    min_spots: int = 2,
) -> list[dict]:
    """Build rolling watch entries for upcoming weekend days across all courses.

    One entry per (course, matching date) from today through ``weeks`` weeks
    ahead. Each course's ``fee_group_contains`` (e.g. "18" for 18-hole rounds)
    is carried through so the generated watch matches the same fee groups the
    hand-maintained watchlist did.
    """
    start = _date.fromisoformat(today_iso)
    end = start + timedelta(weeks=weeks)
    entries: list[dict] = []
    day = start
    while day <= end:
        if day.weekday() in weekdays:
            label = _WEEKEND_LABELS.get(day.weekday(), f"{_DAY_NAMES[day.weekday()]} morning")
            for course in courses:
                entry = {
                    "course": course["key"],
                    "date": day.isoformat(),
                    "earliest": earliest,
                    "latest": latest,
                    "min_spots": min_spots,
                    "label": label,
                }
                fee = (course.get("fee_group_contains") or "").strip()
                if fee:
                    entry["fee_group_contains"] = fee
                entries.append(entry)
        day += timedelta(days=1)
    return entries


def _state_keys_for_date(course_key: str, date: str, parsed_slots: dict, fee_group_label: str, fee_group_id: str) -> dict:
    out = {}
    for time_str, counts in parsed_slots.items():
        key = f"{course_key}|{date}|{time_str}|{fee_group_id}"
        out[key] = {
            "free": counts["free"],
            "total": counts["free"] + counts["taken"],
            "fee_group_label": fee_group_label,
        }
    return out


# --- posted_messages accessors -------------------------------------------------
# Entries are either the new shape {"message_id", "match": <decorated match>} or
# the legacy shape {"message_id", "content", "min_spots"}. These read either.

def _posted_min_spots(info: dict) -> int:
    match = info.get("match")
    return match["min_spots"] if match else info.get("min_spots", 1)


def _posted_content(info: dict) -> str:
    match = info.get("match")
    return build_discord_payload(match)["content"] if match else info.get("content", "")


def _posted_free(info: dict):
    """Last-shown free count, or None for legacy entries that didn't track it."""
    match = info.get("match")
    return match.get("free") if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook", required=False, default=None,
                        help="Discord webhook URL. Falls back to $DISCORD_WEBHOOK_URL.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip Discord posts and state writes; just log what would happen.")
    args = parser.parse_args(argv)

    webhook_url = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")

    courses = _load_json(COURSES_PATH) or []
    courses_by_key = {c["key"]: c for c in courses}
    today = _today_iso()

    # Source the watchlist. If watchlist.json is present it is the source of truth
    # (manual mode). Otherwise the watch is generated as rolling upcoming weekends
    # from courses.json, so it never runs dry and needs no hand-editing.
    watchlist = _load_json(WATCHLIST_PATH)
    if watchlist:
        active_watchlist = [w for w in _reap_watchlist(watchlist, today) if w["date"] >= today]
    else:
        weeks = int(os.environ.get("WATCH_WEEKS", DEFAULT_WATCH_WEEKS))
        active_watchlist = generate_watchlist(courses, today, weeks=weeks)
        print(
            f"INFO: no watchlist.json; generated {len(active_watchlist)} rolling "
            f"weekend entries ({weeks} weeks ahead)",
            file=sys.stderr,
        )

    # Validate watchlist references known courses.
    for w in active_watchlist:
        if w["course"] not in courses_by_key:
            print(f"FATAL: watchlist entry references unknown course key {w['course']!r}", file=sys.stderr)
            return 2

    raw_prior = _load_json(STATE_PATH)
    prior_slots: dict = (raw_prior or {}).get("slots", {})
    posted_messages: dict = (raw_prior or {}).get("posted_messages", {})

    # PURGE_STATE_KEYS env var: comma-separated state keys to drop from prior
    # so the next find_new_matches treats them as fresh. Used after widening
    # the watch window or when an alert was missed.
    purge_keys = [k.strip() for k in os.environ.get("PURGE_STATE_KEYS", "").split(",") if k.strip()]
    if purge_keys:
        print(
            f"WARN: PURGE_STATE_KEYS is set ({len(purge_keys)} key(s)). This is a one-shot "
            "escape hatch but nothing clears it, so it re-fires those alerts EVERY run until "
            "you remove the env var AND redeploy (Railway snapshots vars at deploy time).",
            file=sys.stderr,
        )
        for k in purge_keys:
            if k in prior_slots:
                prior_slots.pop(k)
                print(f"INFO: purged {k} from prior state", file=sys.stderr)

    # Scrape every (course, date) referenced by active entries.
    current_slots: dict = {}
    targets = sorted({(w["course"], w["date"]) for w in active_watchlist})
    for course_key, target_date in targets:
        course = courses_by_key[course_key]
        try:
            calendar_html = fetch_calendar(course, target_date)
        except Exception as exc:
            print(f"WARN: failed to fetch calendar for {course_key} {target_date}: {exc}", file=sys.stderr)
            continue
        fee_groups = discover_fee_groups(calendar_html, target_date)
        if not fee_groups:
            if _looks_like_captcha_or_broken(calendar_html):
                print(
                    f"FATAL: {course_key} returned an unexpected or CAPTCHA page on {target_date}. "
                    "The booking platform may have enabled CAPTCHA or changed structure.",
                    file=sys.stderr,
                )
                return 3
            # Healthy calendar with no cells for our target date -> outside horizon.
            print(
                f"WARN: {target_date} is outside {course_key}'s booking horizon; skipping.",
                file=sys.stderr,
            )
            continue
        for fg in fee_groups:
            try:
                ts_html = fetch_timesheet(course, target_date, fg["fee_group_id"])
            except Exception as exc:
                print(f"WARN: failed to fetch timesheet for {course_key} {target_date} {fg['fee_group_id']}: {exc}", file=sys.stderr)
                continue
            parsed = parse_timesheet(ts_html)
            current_slots.update(_state_keys_for_date(
                course_key, target_date, parsed, fg["label"], fg["fee_group_id"],
            ))

    print(f"INFO: scraped {len(current_slots)} slots across {len({(k.split('|')[0], k.split('|')[1]) for k in current_slots})} (course,date) pairs", file=sys.stderr)

    # Identify matches. Empty prior state means every open slot is new.
    matches = find_new_matches(prior_slots, current_slots, active_watchlist)
    print(f"INFO: {len(matches)} matches to post", file=sys.stderr)

    # Decorate matches with course_name and booking_url for the notifier.
    for m in matches:
        course = courses_by_key[m["course"]]
        m["course_name"] = course["name"]
        m["booking_url"] = timesheet_url(course, m["date"], m["fee_group_id"])

    # Categorise previously-alerted messages:
    #  - STALE: slot booked (free < min_spots), gone from scrape, or date passed
    #           -> strikethrough the message.
    #  - CHANGED: still alertable but the free count moved (e.g. 4 -> 2)
    #           -> edit the message in place to show the new count.
    stale_message_keys: list[str] = []
    changed_message_keys: list[tuple[str, int]] = []  # (slot_key, prev_free)
    for slot_key, info in posted_messages.items():
        _, slot_date, _, _ = slot_key.split("|")
        cur = current_slots.get(slot_key)
        min_spots = _posted_min_spots(info)
        if slot_date < today or cur is None or cur.get("free", 0) < min_spots:
            stale_message_keys.append(slot_key)
            continue
        prev_free = _posted_free(info)
        if prev_free is not None and cur.get("free") != prev_free:
            changed_message_keys.append((slot_key, prev_free))

    if args.dry_run:
        for m in matches:
            print(f"DRY-RUN ALERT: {m['course_name']} {m['date']} {m['time']} ({m['free']} free)")
        for k in stale_message_keys:
            print(f"DRY-RUN STRIKETHROUGH: {k}")
        for k, pf in changed_message_keys:
            print(f"DRY-RUN COUNT-UPDATE: {k} ({pf} -> {current_slots[k]['free']})")
        return 0

    EDIT_PACING_SECONDS = 1.2  # Discord webhook channels allow ~30 msg/min

    def _persist_state() -> None:
        new_state = {
            "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "slots": current_slots,
            "posted_messages": posted_messages,
        }
        STATE_PATH.write_text(json.dumps(new_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Strikethrough stale messages.
    for slot_key in stale_message_keys:
        info = posted_messages[slot_key]
        if not webhook_url:
            break
        try:
            edit_message_to_strikethrough(webhook_url, info["message_id"], _posted_content(info))
            posted_messages.pop(slot_key, None)
            _persist_state()
        except Exception as exc:
            print(f"WARN: failed to strikethrough {slot_key}: {exc}", file=sys.stderr)
        time.sleep(EDIT_PACING_SECONDS)

    # Edit messages whose free count changed (but slot is still alertable).
    for slot_key, prev_free in changed_message_keys:
        info = posted_messages[slot_key]
        if not webhook_url:
            break
        cur = current_slots[slot_key]
        updated = {
            **info["match"],
            "free": cur["free"],
            "fee_group_label": cur.get("fee_group_label", info["match"].get("fee_group_label")),
        }
        try:
            alive = edit_message(webhook_url, info["message_id"], build_count_update_content(updated, prev_free))
            if alive:
                posted_messages[slot_key] = {"message_id": info["message_id"], "match": updated}
            else:
                posted_messages.pop(slot_key, None)  # message deleted by hand
            _persist_state()
        except Exception as exc:
            print(f"WARN: failed to edit count for {slot_key}: {exc}", file=sys.stderr)
        time.sleep(EDIT_PACING_SECONDS)

    # Send new alerts. Persist state incrementally — if a post fails, we still
    # save what already succeeded so the next cron run doesn't replay duplicates.
    if matches and not webhook_url:
        print("ERROR: no webhook configured; cannot send alerts", file=sys.stderr)
        _persist_state()
        return 4

    for idx, m in enumerate(matches):
        slot_key = f"{m['course']}|{m['date']}|{m['time']}|{m['fee_group_id']}"
        try:
            message_id = post_alert(webhook_url, m)
        except Exception as exc:
            print(f"WARN: failed to post alert for {m['course']} {m['date']} {m['time']}: {exc}", file=sys.stderr)
            # Drop the slot from current_slots so the NEXT run treats it as a
            # fresh transition and retries the alert. Otherwise persisting the
            # slot would make prior_free >= min_spots next time, skipping it.
            current_slots.pop(slot_key, None)
            _persist_state()
            continue
        if message_id:
            # Store the full decorated match so later runs can edit the message
            # to reflect count changes or strike it through.
            posted_messages[slot_key] = {"message_id": str(message_id), "match": m}
            _persist_state()  # checkpoint each successful post
        else:
            # post_alert returned no message id (gave up after 429 retries, or
            # Discord responded without an id). Drop the slot so the NEXT run
            # sees it as a fresh transition and re-attempts, rather than letting
            # it be swallowed by the prior_free >= min_spots de-dupe guard.
            print(
                f"WARN: alert for {m['course']} {m['date']} {m['time']} not confirmed; "
                "will retry next run",
                file=sys.stderr,
            )
            current_slots.pop(slot_key, None)
            _persist_state()
        # Proactive pacing — Discord webhook channels allow ~30 msg/min.
        if idx + 1 < len(matches):
            time.sleep(EDIT_PACING_SECONDS)

    _persist_state()
    # Reap past entries only in manual mode; generated watchlists are never on disk.
    if watchlist:
        reaped = _reap_watchlist(watchlist, today)
        if reaped != watchlist:
            WATCHLIST_PATH.write_text(json.dumps(reaped, indent=2) + "\n", encoding="utf-8")

    print(f"INFO: run complete; state persisted with {len(current_slots)} slots and {len(posted_messages)} tracked messages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
