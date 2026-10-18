# Troubleshooting

## `state database is missing` / corrupt state

Do not delete `data/scout.sqlite3` or bootstrap unless this is the first deployment.
Run `health`, inspect the private `scout-state` branch and restore the last validated
export/backup:

```bash
uv run python -m scout.cli state import data/scout-export.json --replace
uv run python -m scout.cli --no-email health
```

If the cloud branch is missing after initial setup, stop and recover it; Scout is
deliberately not allowed to send thousands of baseline alerts.

## Nothing is emailed

* `test-email --dry-run` proves templates/outbox only.
* Check `EMAIL_ENABLED=true` and all eight SMTP secrets, then run a deliberate
  `test-email` without `--dry-run`.
* Check `health` for pending/retryable/permanent outbox entries. A missing sender
  disables delivery in dry/local scans; it does not invent success.
* Gmail app passwords require 2-Step Verification and can be unavailable for some
  managed accounts. Use the approved SMTP/OAuth route for that account.

## Source is failing or absent

Run `sources health`. A source can be enabled but still probation/quarantined; the
planner will not use unsafe/uncertain dynamic sources. `robots` denial, 403/429,
content-size/type errors, parser drift and DNS failures are expected visible states.
Fix the configured endpoint/selector or leave it suspended. Do not disable SSRF,
robots, budgets or redirect checks.

## Empty digest

An empty digest can be correct: new qualifying items may be absent, existing items
may be baseline/unknown/expired, or a category may not have a usable source. Use
`report`, source health and a dry scan. Do not lower verification thresholds just
to make the email non-empty.

## GitHub Actions failed or was late

Inspect the run log and private state branch. Scheduled jobs use the default branch,
can be delayed/dropped under load and do not run on the exact minute. Check the
repository's included Actions minutes; never add a paid runner automatically. A
checkpoint conflict means another job wrote state—wait for it and rerun, never force
overwrite.

## Live smoke differs from tests

Offline tests use mock transports and prove Scout's code paths, not publisher uptime,
robots decisions, email deliverability or current source content. Record a live
smoke as a separate result, with date, source IDs, status/parse outcome, request
budget and any publisher refusal. Never turn a live failure into a fabricated
discovery or a security relaxation.
