# Tee Notifier

Watches public tee-sheets of participating golf courses and posts a Discord `@here` ping to a friends channel when a watched slot becomes newly available.

## How it works

The watcher runs on GitHub Actions, fired by an external cron pinger hitting `workflow_dispatch` every 20 minutes (see setup below). Each run:

1. Builds the watch. By default it generates rolling upcoming weekend slots (every Sat/Sun for the next `WATCH_WEEKS` weeks — default 6, all courses, 06:00 to 14:00, 2+ spots) from `courses.json`, so the watch never runs dry and needs no hand-editing. If a `watchlist.json` file is present it overrides generation and becomes the source of truth (manual mode).
2. Fetches each course's public calendar + timesheet pages.
3. Diffs against `state.json` (the previous run's snapshot, persisted via GitHub Actions cache).
4. Posts a Discord webhook message for any slot that newly satisfies a watch entry.
5. Edits previously-alerted messages to ~~strikethrough~~ when their slot is booked / passes / vanishes.
6. Maintains a rolling "⛳ Open now" dashboard message — edited every run to list every slot currently matching the watch (current state, not just transitions). Pin it in the channel; it never pings. Set an optional `DISCORD_DASHBOARD_WEBHOOK_URL` secret to keep it in its own channel so it never gets buried by alerts.

## GitHub Actions setup (one-time)

1. **Add a repository secret:**
   - Settings -> Secrets and variables -> Actions -> New repository secret
   - Name: `DISCORD_WEBHOOK_URL` -- your Discord channel webhook URL

2. **Set up the external pinger (this is what makes it run every 20 min).**
   GitHub's built-in scheduler throttles frequent cron workflows to roughly 3 runs/day (observed at both `*/10` and `*/20`), so the Watch workflow is dispatch-only and an external cron service fires it:

   1. Create a fine-grained PAT: GitHub Settings -> Developer settings -> Fine-grained personal access tokens -> Generate. Repository access: only this repo. Permissions: Actions -> Read and write.
   2. Create a free [cron-job.org](https://cron-job.org) account and add a job running every 20 minutes with:

      ```
      curl -fsS -X POST \
        -H "Authorization: Bearer <YOUR_PAT>" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/repos/danegoldberg4-arch/tee-notifier/actions/workflows/watch.yml/dispatches \
        -d '{"ref":"main"}'
      ```

   Overlapping dispatches are safe -- the workflow's concurrency group serializes them.

3. **First run will seed `state.json`.** No Discord pings on the first run (seed-only behaviour). Subsequent runs alert on newly-available slots.

4. **Optional: give the dashboard its own channel.** The dashboard is edited in place, so in the alerts channel it gets buried as messages arrive. Create a channel (e.g. `#tee-dashboard`), add a webhook there (Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy URL), and add a repository secret `DISCORD_DASHBOARD_WEBHOOK_URL` with that URL. The watcher then posts and maintains the dashboard there instead; pin it for one-click access. After switching, delete the old dashboard message(s) from the alerts channel by hand once.
5. **Keepalive is automatic.** A weekly workflow pushes an empty commit, resetting GitHub's 60-day inactivity clock that would otherwise auto-disable the watcher.

State persistence uses GitHub Actions cache -- `state.json` is saved after each run and restored before the next. No persistent server or volume needed.

### Creating the Discord webhook

In Discord: Channel -> Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy URL. Paste into the `DISCORD_WEBHOOK_URL` repository secret.

### Manual trigger

You can manually trigger a run from the Actions tab (workflow_dispatch) to test or force a check.

## What gets watched

By default there is no `watchlist.json`. The watch is generated each run as rolling upcoming weekends across every course in `courses.json` (Sat/Sun, 06:00 to 14:00, `min_spots` 2), carrying each course's `fee_group_contains` filter. This is what you want for the standard "weekend morning rounds" use case, and it never needs maintaining.

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

Commit and push. The next watcher run will pick it up.

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
- No runs at all -- the external pinger died or its PAT expired. Check the cron-job.org job history and the token's expiry date first.

Check the failed run's logs in the Actions tab to diagnose. Non-fatal cases (HTTP errors per course, dates outside a course's booking horizon) log a `WARN` but continue.

## License

Personal use.
