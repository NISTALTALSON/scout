# Automation landscape

Research date: **2026-10-19, Asia/Kolkata**. Official pages were retrieved during
implementation, not inferred from historical free-tier lists. Prices are USD unless
stated. Signup/account-specific anti-abuse checks can change; "not documented" is
not a promise that no verification will be requested. No service account was created.

## Decision summary

Use a **private GitHub repository, standard Ubuntu Actions runner, bounded Python
cycles, SQLite locally, private versioned JSON checkpoints on a separate state
branch, and the owner's existing SMTP account**. No paid search or model is needed.
Default schedule: eight cycles/day (every three hours); ordinary sources are due
every six hours or slower. One additional daily digest at 03:37 UTC (09:07 IST).
Urgent monitoring is best-effort, not a one-hour SLA. See the ADR for the privacy
and durable-before-send rules.

## Verified allowances and restrictions

| Platform | Free execution / actual money | Card / eligibility | Cadence, quotas, persistence, unattended blockers |
|---|---|---|---|
| [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) | Public standard hosted runners free; GitHub Free private: **2,000 minutes/month, 500 MB artifacts**, 10 GB cache/repo | No valid payment method: usage **blocks at included quota**. Larger runners always paid. Pro/student eligibility is not assumed. | Python/Linux supported; secrets and scoped GITHUB_TOKEN. Runner disk ephemeral. State must leave runner. [Schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule): min five minutes; default branch only; delay/drop under load; public schedules disabled after **60 days** inactivity. No laptop dependency, no ordinary idle sleep, no punctuality guarantee. |
| [Google Apps Script quotas](https://developers.google.com/apps-script/guides/services/quotas) | Consumer account allowance, not paid Workspace requirements | Existing Google account and OAuth consent; no billing setup needed for built-in consumer services | **6 min/execution**, **90 min/day trigger runtime**, **20,000 URL Fetch/day**, **100 email recipients/day** consumer; properties **9 KB/value, 500 KB/store**. Workspace differs (6 h trigger runtime, 100,000 fetches, 1,500 recipients/day). Quotas reset 24 h after first request, subject to changes. JavaScript V8, not CPython/SQLite. Drive/Sheets can persist. Runs laptop-off; authorization revocation, quotas, randomized trigger times are blockers. |
| [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/) | Free **100,000 requests/day**, **10 ms CPU/request and free cron invocation**, 128 MB, 50 subrequests | Free plan; payment method not established by this limits page. Paid plan minimum must not be enabled by assumption | Five cron triggers/account, UTC, 15 min cron wall time does **not** remove the free CPU limit. Python uses a different runtime/packaging model. [D1](https://developers.cloudflare.com/d1/platform/pricing/): free **5 million rows read/day, 100,000 written/day, 5 GB total**; hard errors at free quotas. Excellent small JS fetch jobs; poor fit for multi-page Python HTML/date parsing in 10 ms. Secrets supported. No laptop/sleep requirement. |
| [n8n](https://n8n.io/pricing/) | Community self-hosted software; cloud Starter **EUR20/month billed annually**, 2,500 executions | Cloud trial no card; Business trial card. Trial is not free production | Self-hosted needs always-on compute, database, patching/backups. Schedule/RSS/HTTP/email, code JS/Python, AI nodes; credential store. Source-available [Sustainable Use license](https://docs.n8n.io/sustainable-use-license/), not unrestricted OSI open source. Sleep depends on host. Windows Docker setup adds overhead. No independently verified permanent free n8n cloud. |
| [Activepieces](https://www.activepieces.com/pricing) | Cloud Free **$0, no card, 1,000 credits/month**, unlimited flows, up to five users as displayed | No card/time limit stated; AI consumes extra credits; free BYOK not included | **Conflicting page**: table says daily reset, FAQ says both monthly and next-day refresh. Do not design a critical budget around either reset until clarified. HTTP piece, TypeScript pieces, flows/agents/tables cloud; CE is automation core, self-host infra extra. Connector auth/plan changes/credits interrupt runs. Cloud avoids laptop; exact minimum free schedule and sleep policy not established by fetched pricing page. |
| [Huginn](https://github.com/huginn/huginn) | MIT software, not free hosting | No software card; host may require one | Agent graph, RSS/web/calendar-like extraction, digests, SMTP, persisted events in MySQL/Postgres. Ruby plus web/background workers, Docker recommended. Requires continuously running host; substantial Windows/DB/upgrade burden. No zero-cost always-on host established. |
| [Pipedream pricing](https://pipedream.com/pricing) / [workflow limits](https://pipedream.com/docs/workflows/limits) | Current pricing emphasizes Connect: free **development only**, production Startup $99/mo annual or $150 monthly | Free development no card; account-specific legacy workflow quotas not independently confirmed | Workflow docs still mention free 300 s executions and seven days event history, but also explicitly announce **Workflows/String shutdown March 31, 2027**. Current pricing and legacy workflow pages describe different products. Do not reuse old "100 credits/day" claims. Python/HTTP/RSS/email and data stores exist in legacy workflows; migration risk excludes it. |
| RSS-first scheduled scripts | RSS/Atom and parsing software free; cloud execution inherits runner's allowance | No feed key unless publisher requires one | Lightweight, cached conditional GET; only detects publishers covered by feeds/links. SQLite or versioned JSON state, own SMTP credentials. Best baseline; not universal search. |
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | Free self-hosted software; README hosted **$8.99/mo** | Host/payment as applicable | Python/Docker, optional browser adds RAM/maintenance. Persist watch directory; Apprise SMTP notifications. Content diffs useful but do not verify eligibility/pricing. Browser login/proxies are not permitted bypasses. Laptop-off only with always-on infrastructure. |
| [Render Free](https://render.com/docs/free) | Free previews, not production guarantee | Without payment method, quota excess suspends services; billing with method can incur extra bandwidth | Web sleeps after **15 min** idle; filesystem lost on restart/sleep/redeploy; **750 instance hours/workspace/month**; no free background worker/cron listed; free Postgres **expires after 30 days**; SMTP **25/465/587 blocked** on free web. Not an unattended SQLite/SMTP host. |

For cloud products, public/private code repository treatment is generally not a
runtime billing distinction except GitHub. Their state instead lives in vendor
datastores. For self-hosted products execution, persistence, email connectivity,
secrets and sleep are the infrastructure owner's responsibility, not a free SaaS
allowance. None of these options includes free arbitrary paid model usage merely
because it supports agents/tool calls.

### Setup, security, lock-in, and failure comparison

* Actions: moderate Windows setup (Python/uv/Git), no local daemon; private repository
  ACL and managed secrets. Portable Python/SQLite/export format; small GitHub-specific
  persistence adapter. Risks: shared account minutes exhausted, deleted state branch,
  token permission change, SMTP anti-abuse, delayed schedules, dependency download.
* Apps Script: easiest browser setup and built-in MailApp; [installable triggers](https://developers.google.com/apps-script/guides/triggers/installable)
  run as creator and may be time-randomized. Script/Drive ACLs, OAuth scope, script
  properties. Higher JS/Google lock-in; six-minute cap and small properties store
  require sharding/batching rather than a direct Python port.
* Workers: Wrangler/Node or supported Python Workers toolchain; KV/D1 bindings and
  secrets. Service-specific storage/cron lock-in; parser CPU termination rather
  than network wall time is the decisive constraint. No free general SMTP relay
  is established here. D1's free limits fail rather than auto-upgrade.
* n8n/Activepieces/Huginn/change detection: visual integrations help simple workflows,
  but claim-level evidence, conservative dedup, SSRF-safe onboarding, versioned
  deadlines and durable outbox still need custom code. Connector/OAuth expiry and
  workflow logs can leak private payloads. Back up credentials DBs securely; upgrades
  and underlying host security are recurring maintenance, not one-time free setup.
* Pipedream: cloud auth/secret store and Python code are convenient; legacy product
  retirement and a development-only current free offer make a new deployment risky.

### Community evidence (leads, not provider guarantees)

[Activepieces community](https://community.activepieces.com/) was inspected live.
Topics listed include [duplicate emails from duplicate webhook runs](https://community.activepieces.com/t/low-runs-twice-per-webhook-event-sending-duplicate-emails/12430),
[unexpected free account credit use](https://community.activepieces.com/t/unexpected-credit-usage-on-new-free-account/12349),
and [missing failure emails](https://community.activepieces.com/t/not-receiving-emails-for-failed-runs/12380).
These are individual reports, not established platform-wide defects. They motivate
Scout's own outbox keys, explicit budgets and missed-run detection. GitHub's official
schedule documentation independently confirms delays/drops. No Reddit assertion is
used to establish a quota or promise reliability.
