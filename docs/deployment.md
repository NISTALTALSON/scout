# Deployment and operations

## Selected placement

Scout runs as a short-lived Python process in GitHub Actions. Each run uses standard
Ubuntu, no larger runner, no laptop and no hosted database. SQLite is the working
transaction store. The source/workflow repository may be public; before a job can send,
it loads a validated gzip JSON snapshot from the `scout-state` branch of a separate
private state repository. After each meaningful change it may checkpoint through the
GitHub Contents API. The state API checks repository privacy, uses the previous blob
SHA for compare-and-swap, refuses a conflict, and never stores a token in state.
The workflow's built-in token is read-only for source checkout and is not used for
state; `SCOUT_STATE_TOKEN` is independently scoped to the private state repository.

The workflow uses a three-hour scan cycle at minute 37 UTC and triggers the 9 AM IST
digest from the cycle that lands around 09:07 IST. It also supports manual scan,
digest, verify, health and maintenance modes. Weekly health is queued by explicit
`maintain`; it is not a separate scheduled job. `concurrency` prevents two jobs from
writing the same checkpoint. Five-minute job timeout and application budgets stop
partial work cleanly. A partial scan leaves unprocessed sources due for a later run.

## Exact installation

```bash
git clone <source-repository-url> scout
cd scout
uv sync --frozen
uv run python -m scout.cli --no-email sources health
uv run python -m scout.cli --no-email scan --dry-run --no-discovery
```

For Docker, `docker build -t scout:local .`; run the offline checks with
`docker run --rm --network none scout:local /opt/scout/.venv/bin/python scripts/test.py`
and `docker run --rm --network none scout:local /opt/scout/.venv/bin/python scripts/demo.py`.
Use a named volume for `/opt/scout/data` and inject secrets at runtime. The Dockerfile
is not an always-on scheduler, and the build has not been verified on this Windows
host because Docker is unavailable.

## GitHub setup

1. Push the project to a public source/workflow repository and create a separate
   private state repository with a default branch.
2. In Actions settings, allow the workflow and standard Ubuntu runners. The workflow
   permission is `contents: read`; it cannot write the source repository.
3. Create a fine-grained token (or equivalent GitHub App token) with Contents
   read/write access to only the private state repository. Add
   `SCOUT_STATE_REPOSITORY`, `SCOUT_STATE_TOKEN`, and `SCOUT_STATE_BRANCH` (usually
   `scout-state`) plus the email secrets from `.env.example` as repository Actions
   secrets. Do not use `GITHUB_TOKEN` for checkpoint state.
4. Use a Gmail app password only when that account offers it after 2-Step
   Verification; an organization account may require a different approved SMTP/OAuth
   path. Never put a password in an issue, commit, log or chat.
5. For the first deployment only, dispatch with `dry_run=true` and
   `bootstrap_state=true`; inspect the run and `scout-state` private branch. Leave
   `bootstrap_state=false` afterward so a missing checkpoint fails closed.
6. Set `EMAIL_ENABLED=true` and execute one deliberate `test-email`/digest through a
   local authorized runtime before trusting scheduled delivery.

### Migrating the earlier same-repository checkpoint design

The old design used the workflow repository's `GITHUB_TOKEN` and is intentionally
not accepted by current cloud mode. Do not make that repository public while it may
contain a `scout-state` branch. First keep the old repository private, export its
validated state with `state export`, create the separate private state repository,
configure `SCOUT_STATE_REPOSITORY`, `SCOUT_STATE_TOKEN` and `SCOUT_STATE_BRANCH`, and
perform an explicit `state import`/bootstrap there. Review the private repository's
branches before publishing the source repository; if state was ever pushed to a
public location, rotate affected credentials and treat the history as exposed.

The workflow does not automatically create a repository, add secrets, install a
GitHub CLI or enable billing. Those are explicit owner actions. GitHub schedule
events run only from the default branch, can be delayed under load and may be
dropped; design around at-least-once best effort, not wall-clock guarantees.

## Recovery

* **Checkpoint conflict:** stop; inspect Actions concurrency and the `scout-state`
  branch in the private state repository. Do not force overwrite. Re-run once the
  competing job completes.
* **Missing checkpoint:** only first deployment uses explicit bootstrap. A later
  missing state is an incident and fails closed; restore a validated local export or
  private state-repository branch history, then dispatch again.
* **Corrupt SQLite/export:** do not delete and rescan. Use `state export`/backup or
  restore the last validated private checkpoint. `state import --replace` is explicit.
* **SMTP failure:** check the outbox via `health`; retryable failures remain queued,
  permanent failures remain visible. Provider password changes revoke Gmail app
  passwords; update the secret then use a deliberate test.
* **Source failure:** health records status/error/backoff and the planner continues
  with other sources. Fix/disable the source in config; never reduce security to
  make a blocked page parse.
