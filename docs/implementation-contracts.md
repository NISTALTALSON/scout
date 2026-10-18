# Internal implementation contracts

Shared Pydantic records live in `scout.models`. Configuration is `Configuration`
with `.sources`, `.preferences`, `.taxonomy`, `.scoring`. Budget methods:
`check`, `consume_http`, `consume_candidate`, `consume_source`; counts `.requests`,
`.candidates`, `.new_sources`; `.remaining_seconds` property. Exhaustion raises
`BudgetExceeded(reason)` and is not an ordinary source error. All times UTC-aware.

## Collector / discovery boundary

* `scout.http.SafeHttpClient(budget, max_response_bytes=2097152, transport=None,
  resolver=None)`: `.get(url, headers=None, rate_limit_seconds=2,
  respect_robots=True) -> FetchResult`, `.close()`. Injectable transport/resolver
  allow offline tests but defaults must pin validated IP at connection time, keep
  original TLS SNI, disable proxies, validate all redirects, and stop on 403/429.
  Status 304 is usable with persistent source metadata; no raw-page cache needed.
* `scout.urls.normalize_url(url) -> str`, `is_official_url(url, domains) -> bool`:
  exact publisher-host match, not substring/public-suffix trust.
* `scout.collectors.collect(source, http, now) -> CollectionResult` and
  `parse(source, response, now) -> CollectionResult`: bounded byte parsing of
  RSS/Atom, HTML selectors/JSON-LD, API, GitHub and ICS. Candidate `.metadata`
  may contain literal structured fields (`start_at`, `deadline_at`, `price`,
  `eligibility`, `registration_url`, `organizer`) and excerpts; values are NOT
  automatically official or reliable. Unknowns remain absent.
* `scout.discovery.planner.plan_sources(sources, now, category=None,
  urgent_only=False, opportunities=None) -> list[Source]`: due-aware, fair bounded
  backoff/max interval, probation checks allowed, disabled/quarantined skipped.
* `scout.discovery.source_discovery.discover_sources(urls, origin, existing,
  http, budget, now, auto_enable=True) -> list[Source]`: restricted onboarding,
  max three new sources; no promotion of unknown domains to official.
* `scout.discovery.query_engine.run_queries(http, budget, now,
  queries=None) -> list[str]`: bounded GitHub public repository search by default;
  explicit adapter protocol for future providers. No paid API/default search key.

## Intelligence boundary

* `scout.intelligence.extraction.extract(candidate, source, now, taxonomy) ->
  Opportunity | None`: actionability filter, evidence-excerpt extraction, strict
  dates/eligibility/pricing. Incomplete/unzoned dates must not get countdowns.
* `scout.intelligence.verification.verify(opportunity, source, now, http=None) ->
  Opportunity`: official existence separate from verified field claims. Safely
  fetch original page to corroborate if feasible; quoted value/authority/reliable
  evidence per claim. A trusted feed linking an external host does not authorize it.
* `scout.intelligence.scoring.score(opportunity, preferences, scoring, source,
  now, feedback_signals=None) -> Opportunity`: eight normalized dimensions,
  penalties, separate confidence; no blanket keyword confidence.
* `scout.intelligence.deduplication.find_duplicate(item, existing) ->
  Opportunity | None`; `.merge_opportunities(old, new, now) -> (Opportunity,
  list[str])` preserving ID/first_seen/history/feedback and meaningful versioning.
* `scout.intelligence.change_detection.meaningful_changes(old, new) -> list[str]`.
* `scout.intelligence.deadlines.alert_window(opportunity, now, windows) ->
  int | None`: one most-specific crossed window only, future reliable date;
  `hours_remaining(opportunity, now) -> float | None`.

## Persistence boundary

`scout.storage.sqlite_store.Store(path, create=False)` context manager; acquisition
holds a cross-platform process lock until `.close()`. Fail on missing/corrupt DB
unless deliberate `create=True`; never silently reset. Atomic transactions.

Methods: `get_meta(key, default=None)`, `set_meta(key, value)`, `list_sources()`,
`upsert_source(source)`, `list_opportunities()`, `get_opportunity(id)`,
`upsert_opportunity(item)`, `list_notifications()`, `enqueue(notification)->bool`
(unique idempotency key), `update_notification(notification)`,
`due_notifications(now, limit=5)`, `record_run(metrics)`, `list_runs(limit=30)`,
`feedback(id, action)`, `reset_feedback()`, `export_state()->dict`,
`import_state(payload, replace=False)`, `backup(path)`, `restore(path, replace=False)`,
`prune(now, retention_days=180, max_opportunities=1000)`.

Export includes schema version, checksum, all private records/metadata but **no
environment secrets**. Migration tests required; validated import is data, never
SQL execution. Backup replacement must be explicit. Cross-table references checked.

`scout.storage.persistence_backend.GitHubStateBackend(repository, token,
branch='scout-state', path='state/scout.json.gz', client=None)`:
`load(bootstrap=False)->dict|None`, `save(payload)`. Explicit bootstrap may create
branch from repo default branch. Ensure repo private on every load/save; use only
api.github.com with redirects disabled. Contents API blob-SHA compare-and-swap;
409/conflict halts. Gzip payload cap 900,000 bytes and uncompressed cap 16 MiB,
checksum before importing. No unauthenticated artifact/cache persistence. Return
None only for deliberate initialization. Don't log private response bodies/tokens.

## Notification boundary

* `scout.notifications.templates.render_message(kind, opportunities, now,
  timezone='Asia/Kolkata', health=None) -> (subject, text, html)`.
* `scout.notifications.outbox.make_notification(kind, opportunities, now, key,
  timezone='Asia/Kolkata', health=None) -> Notification`: stable Message-ID and
  idempotency key; include deadline/version in urgent keys provided by caller.
* `Outbox(store, sender, max_attempts=4).deliver(now, limit=5, daily_limit=10,
  timezone='Asia/Kolkata', dry_run=False, checkpoint=None) -> dict`: save sending
  intent and **call checkpoint before SMTP**, checkpoint outcome afterward. On
  checkpoint error do not send. Retry bounded exponential across runs, distinguish
  permanent vs transient; old sending lease can retry with acknowledged duplicate
  risk. HTML-escape, plain text always, no credentials in errors. Dry run no status
  mutation, no SMTP. Update opportunity notification_history only on sent.
* `scout.notifications.email_sender.SmtpSender.from_env()` and `.send(notification)`;
  `DeliveryError` has `.retryable`. Require EMAIL_ENABLED=true and required env;
  single validated recipient; TLS STARTTLS or implicit TLS, no plaintext fallback.
  SMTP timeout, header injection defense, no rapid auto-resend on uncertain accept.

## Integration ownership

Main owns CLI, orchestrator, config YAML, packaging, workflows, docs, end-to-end
tests. Module owners add isolated unit tests under distinct filenames. Report
interface deviations explicitly; essential functions cannot be placeholders.
