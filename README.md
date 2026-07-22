# Tee Notifier

Watches public tee-sheets of participating golf courses and posts a Discord `@here` ping to a friends channel when a watched slot becomes newly available.

## How it works

GitHub Actions runs `watcher.py` on a cron schedule (`*/10 22-23,0-9 * * *` UTC = every 10 min during AU daytime). Each run:

1. Builds the watch. By default it generates rolling upcoming weekend slots (every Sat/Sun for the next `WATCH_WEEKS` weeks, all courses, 06:00 to 12:30, 2+ spots) from `courses.json`, so the watch never runs dry and needs no hand-editing. If a `watchlist.json` file is present it overrides generation and becomes the source of truth (manual mode).
2. Fetches each course's public calendar + timesheet pages.
3. Diffs against `state.json` (the previous run's snapshot, persisted via GitHub Actions cache).
4. Posts a Discord webhook message for any slot that newly satisfies a watch entry.
5. Edits previously-alerted messages to ~~strikethrough~~ when their slot is booked / passes / vanishes.

## GitHub Actions setup (one-time)

1. **Add a repository secret:**
   - Settings -> Secrets and variables -> Actions -> New repository secret
   - Name: `DISCORD_WEBHOOK_URL` -- your Discord channel webhook URL

2. **Optional: adjust the cron schedule.**
   The workflow in `.github/workflows/watch.yml` declares `*/10 22-23,0-9 * * *` (UTC). Edit and push to change cadence.

3. **First run will seed `state.json`.** No Discord pings on the first run (seed-only behaviour). Subsequent runs alert on newly-available slots.

State persistence uses GitHub Actions cache -- `state.json` is saved after each run and restored before the next. No persistent server or volume needed.

### Creating the Discord webhook

In Discord: Channel -> Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy URL. Paste into the `DISCORD_WEBHOOK_URL` repository secret.

### Manual trigger

You can manually trigger a run from the Actions tab (workflow_dispatch) to test or force a check.

## What gets watched

By default there is no `watchlist.json`. The watch is generated each run as rolling upcoming weekends across every course in `courses.json` (Sat/Sun, 06:00 to 12:30, `min_spots` 2), carrying each course's `fee_group_contains` filter. This is what you want for the standard "weekend morning rounds" use case, and it never needs maintaining.

To watch something non-standard (a weekday, a different window, a one-off date), create a `watchlist.json` file. Its presence switches the watcher into manual mode and it becomes the sole source of truth. Add entries like:

```json
{
  "course": "eastlake",
  "date": "2026-06-13",
  "earliest": "14:00",
  "latest": "16:00",
  "min_spots": 2,
  "fee_group_contains": "Sundowner",
  "label": "Sat arvo with the boys"
}
```

Commit and push. The next scheduled run will pick it up.

Fields:
- `course` -- must match a `key` in `courses.json`.
- `date` -- ISO `YYYY-MM-DD`, in the course's local (Sydney) time.
- `earliest`, `latest` -- 24-hour `HH:MM`, inclusive.
- `min_spots` -- integer 1-4.
- `fee_group_contains` -- *optional*, case-insensitive substring match against the course's fee-group label (e.g. `"Sundowner"`, `"Weekend"`, `"Twilight"`).
- `label` -- free text, shown in the Discord alert.

Past-dated entries are auto-removed once they're more than a day old.

## Adding a new course

Append to `courses.json`:

```json
{
  "key": "shortname",
  "name": "Pretty Display Name",
  "base_url": "https://<course-domain>",
  "booking_resource_id": 3000000,
  "fee_group_contains": "18"
}
```

The `booking_resource_id` is the `bookingResourceId` query param on the course's public calendar URL. Most courses use `3000000`; some use a different number.

`fee_group_contains` is *optional*, a case-insensitive substring matched against the fee-group label. It is used by generated mode so that course only matches the right fee groups (e.g. `"18"` for 18-hole rounds). Omit it to match any fee group (Eastlake does this).

## Local development

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest -v
PYTHONPATH=. python3 watcher.py --dry-run   # against current watchlist
```

## When the watcher dies

It's designed to fail loud -- the GitHub Actions run will show as failed:

- The booking platform enabled CAPTCHA (`publicCaptchaEnabled = true` detected, or unfamiliar HTML returned).
- The Discord webhook returns non-2xx.
- A watchlist entry references an unknown course key.

Check the failed run's logs in the Actions tab to diagnose. Non-fatal cases (HTTP errors per course, dates outside a course's booking horizon) log a `WARN` but continue.

## License

Personal use.
