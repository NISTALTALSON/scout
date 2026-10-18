# Source discovery strategy and component research

Researched **2026-10-19 IST**. Coverage is layered and bounded, not universal web
search. Every fetched response is untrusted input, including publisher statements.

## Free discovery layers

1. Poll verified publisher RSS/Atom, structured public APIs, HTML announcement
   sections, and explicitly configured iCalendar feeds with conditional GET.
2. Extract relevant organizer links and RSS/Atom alternate links while processing
   sources. Normalize, enforce public-IP/redirect/robots policy, then probation-test
   a maximum of three candidate sources per cycle. Known publisher domains may
   enable useful sources automatically after successful extraction. Unknown domains
   remain quarantined and their content can only supply unverified leads.
3. Selected directories/aggregators (MLH, CTFtime for personal data analysis) expand
   coverage to organizers. **Their link is not proof of official identity, free
   admission, Indian eligibility, or a registration deadline.** Each edition remains
   independent. CTFtime's [API policy](https://ctftime.org/api/) permits data analysis
   and mobile apps only, forbids clones. This installation is private personal
   analysis; don't republish a mirrored calendar or use it as a CTFtime clone.
4. A GitHub repository-search adapter can query public source directories/releases,
   within [GitHub rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).
   Unauthenticated REST is **60/hour per IP**; personal authenticated usually
   5,000/hour; Actions GITHUB_TOKEN 1,000/hour/repository. Search has its own lower
   bucket; headers/403/429 govern actual availability, not the general limit.
   Shared runner egress may already be limited. No code search requiring extra
   authorization or arbitrary GitHub file execution is used.
5. Optional replacement discovery adapters can implement a supported search API.
   Default query engine uses GitHub search only, not search-engine HTML scraping.
   [Tavily](https://docs.tavily.com/documentation/api-credits) currently documents
   **1,000 credits/month, no card**; basic search one credit, advanced two, paid
   pay-as-you-go $0.008/credit. Optional credentials and provider terms are still
   needed; no dependency on that allowance or automatic paid fallback is created.
   [Brave](https://brave.com/search/api/) currently advertises prepaid credits,
   Search **$5/1,000**, **$5 monthly free credits** in its plan section, and requires
   a credit card for abuse prevention. Its header says "up to $20" rather than
   defining eligibility for that larger amount; do not assume it. The plans endpoint
   returned 403 here. Storage rights are plan-dependent. Not a no-card default.
6. Authorized public newsletter archives can be added with permission and a usable
   feed. No private inbox, social account, search-engine scraping, CAPTCHA bypass,
   residential proxy or sign-in automation. No student search API benefit was
   established as universally available; student offers remain conditional.

## Initial source evidence

The following original endpoints returned relevant content during research:

* [GitHub Blog RSS](https://github.blog/feed/) — actual RSS; large full-text feed,
  must enforce response limits; general engineering news needs actionability filtering.
* [Cloudflare RSS](https://blog.cloudflare.com/rss/) — actual RSS, product updates
  not automatically promotions.
* [OpenAI news RSS](https://openai.com/news/rss.xml) — actual RSS, many non-opportunity
  case studies; no inferred promotional access.
* [Hugging Face feed](https://huggingface.co/blog/feed.xml) — actual RSS, **includes
  community/organization-authored posts**, so not all content is official HF policy.
* [Anthropic news](https://www.anthropic.com/news) — working announcement listing,
  no invented RSS endpoint; HTML selectors require regression/live checks.
* [GitHub Student Pack](https://education.github.com/pack) — working benefit catalog,
  partner-specific redemption terms; pack verification is not automatic redemption.
* [MLH 2027 season](https://mlh.io/seasons/2027/events) — upcoming and historical
  organizer links, including India; in-person elsewhere is not eligible online.
* [KSUM events](https://startupmission.kerala.gov.in/events) — accessible, explicitly
  **no upcoming events** at research time, with historical events. This is source
  monitoring evidence, not a newly discovered active opportunity.
* [PortSwigger Academy](https://portswigger.net/web-security) — free training/labs
  confirmed. A separately linked certification is **not** thereby free/included.
* [OWASP events](https://owasp.org/events/) — accessible shell with little useful
  event content in fetched markup. Keep disabled/probation until a functioning
  extraction method is established. Don't call an HTTP 200 successful event coverage.

Other requested publishers (Lovable, Google, Vercel, Supabase, AWS, Microsoft,
TryHackMe, GDG, Startup India, GSoC) are catalog targets, not fabricated working
feeds. Keep untested/broken ones disabled with a reason. The live smoke report is
the final authority for enabled collectors on this machine; research retrieval via
a fetch tool is distinct from Scout's SSRF/robots-compliant transport.

## Reusable components

Use versions in `uv.lock` (all transitive versions/hashes), not floating examples.
The supported baseline is Python 3.12.13, a [security release](https://www.python.org/downloads/release/python-31213/).
Upstream now notes 3.12.15; the supplied uv runtime catalog cannot install that
version, so 3.12.13 is the tested pin, not a claim to be the newest security patch.
Upgrade the pin and regenerate/check the container when an updated runtime is available.

| Component / original docs | Evaluation and decision |
|---|---|
| [feedparser](https://feedparser.readthedocs.io/en/latest/) | Current docs 6.0.14; mature BSD parser, RSS/Atom, bozo/encoding/date behavior documented. Feedparser only parses **bytes already fetched securely**, never accepts a URL to fetch itself. Sanitized HTML still isn't trustworthy evidence. |
| [HTTPX transports](https://www.python-httpx.org/advanced/transports/) / [httpcore](https://github.com/encode/httpcore) | BSD, supported Python runtimes, mock transports for offline tests. Add a DNS-pinning network backend because checking DNS before an ordinary second resolver lookup does not stop rebinding. Pin httpcore to tested 1.0.x and recheck on upgrades. |
| [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/) | MIT, tolerant HTML/selectors with stdlib parser; no browser/JS or arbitrary execution. Target explicit cards/article/main, reject selector drift rather than silently parsing navigation. |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT, Python 3.10+, actively maintained validation; forbid extra fields and naive timestamps. Missing facts remain unknown. No framework or hosted telemetry dependency. |
| [dateparser settings](https://dateparser.readthedocs.io/en/latest/settings.html) | BSD, strict/required parts, timezone settings. Default parser may invent missing day/year or timezone; Scout only promotes explicit year/time/zone to a countdown. Date-only and ambiguous dates retain uncertainty. |
| [icalendar](https://icalendar.readthedocs.io/) | BSD, maintained standards parser rather than hand-written ICS. Recurrence expansion is deliberately not unbounded; floating/all-day timestamps are not precise registration deadlines. |
| [Jinja](https://jinja.palletsprojects.com/en/stable/) | BSD, HTML autoescape, static local templates; scraped content never becomes a template. Plain text alternative always included. |
| [SQLite](https://www.sqlite.org/backup.html) / [portalocker](https://portalocker.readthedocs.io/) | SQLite public domain, atomic transactions and online backup; BSD cross-platform process locking. Versioned migrations and validated export protect separate executions. |
| Python `smtplib` / `email` / `zoneinfo`, hashlib/difflib | No SMTP SDK, fuzzy/vector DB or datetime service needed. TLS, conservative URL/title/date matching, explicit UTC, IST display. No home-grown SMTP protocol. |
| Huginn / changedetection.io / n8n | Maintained reusable ideas, but entire products bring a worker/database/browser/service footprint. Reimplement small bounded planning/change tasks rather than bundling a second orchestration system. |

Upstream changelog and issue surfaces: [feedparser changelog](https://feedparser.readthedocs.io/en/latest/changelog/),
[HTTPX issues](https://github.com/encode/httpx/issues), [dateparser issues](https://github.com/scrapinghub/dateparser/issues).
These are maintenance leads, not claims every issue is reproduced. Known documented
risks (malformed feed bozo flags, async transport lifecycle, date defaults) are
explicitly covered in tests. `pip-audit` checks installed dependencies online;
the reproducible offline gate cannot certify a current vulnerability database.

## Coverage truth

Reliable within source availability: new entries and material changes on functioning
configured feeds/adapters, persisted history, explicit timestamp alerts.
Probabilistic: new organizers found through relevant links, directory entries,
GitHub repository searches, local keyword classification.
Not covered: all social posts, closed communities, login/CAPTCHA/JS-only pages,
every newsletter, newly created sites with no incoming link, or offers that start
and finish between scheduled checks. Lack of discovery never means no opportunity
exists. No RSS/search provider provides universal free coverage.
