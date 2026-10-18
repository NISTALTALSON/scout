# ADR 001 — Private, bounded Python agent on scheduled cloud runners

Status: selected 2026-10-19; **deployment not yet authorized or verified**.
Evidence and current limits: [automation landscape](automation-landscape.md).

## Context and selection

The owner needs unattended personal intelligence, not a public scraping service.
An ordinary Windows laptop cannot be production infrastructure. A deterministic
Python pipeline is easier to test, restore and move than visual flows or an
unconstrained model loop. We select a **public source/workflow repository plus a
separate private state repository** using standard Ubuntu hosted runners and
existing personal SMTP (Gmail app-password route when the account permits it). No
domain purchase, search key, AI key, server, database subscription or student
verification is required for the default application.

SQLite provides transactional local memory; a versioned, checksummed JSON export is
the durable cloud checkpoint on a **scout-state branch of a separate private state
repo**. The source/workflow repo may be public. The Contents API supplies optimistic
concurrency using the previous blob SHA. All state-bearing jobs share one concurrency
group; a local process lock excludes overlapping CLI runs. Public repos and a state
repo equal to the workflow repo are refused by the runtime/backend boundary. No
private exports in Actions artifacts, caches, logs or Pages. Git history is backup,
not an excuse to publish private state. No credentials enter the export.

Pending notifications and sending intent must be checkpointed **before SMTP**;
sent/failure state is checkpointed afterward. SMTP and git cannot share a
transaction: an acceptance/connection-loss or crash after sending may duplicate an
email. Stable Message-ID and idempotency keys suppress ordinary duplicates, not
exactly-once delivery. A remote checkpoint conflict or corruption halts sending.
Missing cloud state is a hard error except deliberate bootstrap, which baselines
existing discoveries without mailing them.

## Cost budget

Eight scheduled jobs/day = 240 in 30 days (248 in 31). At an enforced five-minute job timeout
that is at most 1,350/1,395 runner minutes, leaving part of Free's 2,000 for CI and
other uses; setup/download delays count too. This is a capacity estimate, not a
reservation: all private repos on the account share minutes. Configure a GitHub
Actions **$0 stop-usage spending budget** or keep no payment method. Use standard
Ubuntu, never larger/Windows/macOS runners. Do not auto-enable any paid fallback.

Application budgets stop each cycle after 90 seconds, 80 HTTP attempts including
robots and redirects, 120 candidates and three new sources by default. State is
checkpointed after processing; unprocessed sources stay due. No required model or
commercial search calls; zero AI calls/tokens by default. Email max five messages
per cycle and ten per owner-local day. Tune conservatively after observing real
runtime/quality. Estimates do not guarantee the provider's continued free plan.

Default cycles every three hours; full ordinary-source freshness target six hours;
daily digest 03:37 UTC = 09:07 IST. **One-hour windows can be missed**, as can short
promotions between scans. Hourly monitoring is a conscious cadence/quota trade-off,
not a free reliability promise. No artificial keep-alive traffic or fake commits.

## Rejected alternatives and switch conditions

* Apps Script is the strongest simple-email alternative (100 consumer recipients/day,
  no external SMTP secret). Rejected as the primary implementation because Python,
  SQLite, robust DNS-pinned transport and a substantial extraction test corpus do
  not port directly into its six-minute JavaScript runtime. Reconsider for a much
  smaller feed-only product or accounts that cannot use SMTP.
* Workers/D1 are appealing for cheap persistence and frequent wakes. Free cron's
  **10 ms CPU** is unsuitable for this parsing workload. Reconsider if processing
  moves to small structured APIs and measured CPU fits, or owner approves paid CPU.
* Activepieces cloud is genuinely free software execution, but reset wording
  contradicts itself and bespoke evidence/state logic still requires code. Recheck
  clarified quotas and production schedule guarantees before changing architecture.
* n8n CE, Huginn, changedetection.io are useful software, not a free always-on host.
  Reconsider only if an existing server is available; Docker supports portability.
* Render free sleep/ephemeral disk/SMTP restrictions and expiring DB disqualify it.
* Pipedream's announced legacy workflow shutdown disqualifies a new dependency.
* A public source repository plus a separate private state repository is selected for
  the buildathon and safe source publication. It adds a narrowly scoped state token
  and repository setup, but avoids exposing checkpoint history on a public branch.
* No vector database, browser, broker, always-on scheduler service, LLM framework or
  autonomous code deployment: no measured need justifies their cost/attack surface.

Move off best-effort Actions if reliable sub-hour alerts become necessary, aggregate
minutes exceed allowance, state export reaches its size cap, or repository policy
prevents branch writes. Migrate via the validated state export/SQLite backup to a
Docker worker with persistent disk; that host is not claimed free.

## Email evidence

[Gmail limits](https://support.google.com/mail/answer/22839?hl=en) describe a consumer
500-email/day limit, not a delivery SLA. Scout's cap is much lower. [App passwords](https://support.google.com/accounts/answer/185833?hl=en)
require 2-Step Verification; unavailable for some school/work, security-key-only
and Advanced Protection accounts, and revoked when account password changes.
Google recommends OAuth where available. SMTP credentials belong in GitHub Actions
Secrets or an untracked local environment, never chat. Different SMTP accounts can
be used without changing orchestration. Delivery from a hosted runner must be
manually tested; this research does not prove that a particular account can send.
