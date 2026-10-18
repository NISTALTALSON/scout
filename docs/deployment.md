# Deployment and operations

## Selected placement

Scout runs as a short-lived Python process in private GitHub Actions. Each run uses
standard Ubuntu, no larger runner, no laptop and no hosted database. SQLite is the
working transaction store. Before a job can send, it loads a validated gzip JSON
snapshot from the private `scout-state` branch; after each meaningful change it may
checkpoint through the GitHub Contents API. The state API checks repository privacy,
uses the previous blob SHA for compare-and-swap, refuses a conflict, and never
stores a token in state. `GITHUB_TOKEN` is limited to `contents: write` for this
repository by workflow permissions.

The workflow uses a three-hour cycle at minute 37 UTC and triggers the 9 AM IST
digest from the cycle that lands around 09:07 IST. It also supports manual scan,
digest, verify, health and maintenance modes. `concurrency` prevents two jobs from
writing the same checkpoint. Five-minute job timeout and application budgets stop
partial work cleanly. A partial scan leaves unprocessed sources due for a later run.

## Exact installation

```bash
git clone <private-repository-url> scout
cd scout
uv sync --frozen
uv run python -m scout.cli --no-email sources health
uv run python -m scout.cli --no-email scan --dry-run --no-discovery
```

For Docker, `docker build -t scout:local .`; mount `/opt/scout/data` and inject
secrets at runtime. The Dockerfile is not an always-on scheduler.

## GitHub setup

1. Push the project to a private repo's default branch.
2. In Actions settings, allow the workflow and standard Ubuntu runners.
3. Add the eight email secrets from `.env.example` as repository Actions secrets.
4. Use a Gmail app password only when that account offers it after 2-Step
   Verification; an organization account may require a different approved SMTP/OAuth
   path. Never put a password in an issue, commit, log or chat.
5. For the first deployment only, dispatch with `dry_run=true` and
   `bootstrap_state=true`; inspect the run and `scout-state` private branch. Leave
   `bootstrap_state=false` afterward so a missing checkpoint fails closed.
6. Set `EMAIL_ENABLED=true` and execute one deliberate `test-email`/digest through a
   local authorized runtime before trusting scheduled delivery.

The workflow does not automatically create a repository, add secrets, install a
GitHub CLI or enable billing. Those are explicit owner actions. GitHub schedule
events run only from the default branch, can be delayed under load and may be
dropped; design around at-least-once best effort, not wall-clock guarantees.

## Recovery

* **Checkpoint conflict:** stop; inspect Actions concurrency and the `scout-state`
  branch. Do not force overwrite. Re-run once the competing job completes.
* **Missing checkpoint:** only first deployment uses explicit bootstrap. A later
  missing state is an incident and fails closed; restore a validated local export or
  branch history, then dispatch again.
* **Corrupt SQLite/export:** do not delete and rescan. Use `state export`/backup or
  restore the last validated private checkpoint. `state import --replace` is explicit.
* **SMTP failure:** check the outbox via `health`; retryable failures remain queued,
  permanent failures remain visible. Provider password changes revoke Gmail app
  passwords; update the secret then use a deliberate test.
* **Source failure:** health records status/error/backoff and the planner continues
  with other sources. Fix/disable the source in config; never reduce security to
  make a blocked page parse.
