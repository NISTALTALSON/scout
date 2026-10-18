# Source catalog and policy

`config/sources.yaml` is declarative. A source record includes its type, adapter,
publisher domains, trust level, categories, scan interval, per-host delay, selectors,
validation state and expected coverage. Dynamic sources are kept in SQLite/remote
state rather than written into Python or allowed to change configuration.

## Initial enabled/probation coverage

* GitHub Blog, Cloudflare Blog, OpenAI News and Hugging Face Blog RSS/Atom targets.
  The Hugging Face feed includes community authors; do not treat every item as
  official Hugging Face policy.
* Anthropic News, GitHub Student Developer Pack, MLH events, PortSwigger Academy,
  Kerala Startup Mission events, and CTFtime event API target pages/data.
* OWASP, Lovable, Vercel, Supabase, AWS, Microsoft, Google, GDG, Startup India,
  GSoC and TryHackMe targets are present as disabled/quarantined research targets
  where a collector or current coverage was not verified. No opportunity is inferred
  from their presence.

At first run, sources configured as `probation` may be fetched and tested. Scout
enables only useful known official publisher hosts after a successful restricted
probation; unknown domains remain quarantined. A successful collector proves only
that the source can be parsed, not that every claim is official. A source failure is
isolated and backed off; it does not stop unrelated categories.

## Registry and bounded live snapshot

The registry contains **21 records: 10 enabled/probation and 11 disabled/quarantined**.
The following table records the configured endpoint and the latest bounded smoke
observation available to this workspace. “Not reached” is not a failure; the run
stopped at the 90-second application budget. The smoke used a temporary state
directory, `--no-email` and `--no-discovery`; it did not change this checked-in
registry.

| ID | Endpoint | Trust / collector | Categories | Config state | Smoke observation |
|---|---|---|---|---|---|
| `github-blog` | `https://github.blog/feed/` | official / RSS | AI, student, career | enabled, probation | Not reached before duration budget |
| `cloudflare-blog` | `https://blog.cloudflare.com/rss/` | official / RSS | AI, startups, security | enabled, probation | Retrieved; parsed `ok`, 0 useful records |
| `openai-news` | `https://openai.com/news/rss.xml` | official / RSS | AI, startups, learning | enabled, probation | Not reached before duration budget |
| `huggingface-blog` | `https://huggingface.co/blog/feed.xml` | community / RSS | AI, learning, career | enabled, probation | Not reached before duration budget |
| `anthropic-news` | `https://www.anthropic.com/news` | official / HTML | AI, startups, security | enabled, probation | Retrieved; parsed `ok`, 0 useful records |
| `github-student-pack` | `https://education.github.com/pack` | official / HTML `student_pack` | student, AI, learning | enabled, probation | Not reached before duration budget |
| `mlh-events` | `https://mlh.io/seasons/2027/events` | aggregator / HTML | hackathons, career | enabled, probation | Retrieved; parsed `ok`, 1 useful record |
| `ctftime-events` | `https://ctftime.org/api/v1/events/?limit=20` | aggregator / API `ctftime` | security | enabled, probation | Retrieved; parsed `ok`, 20 records |
| `portswigger-academy` | `https://portswigger.net/web-security` | official / single-page HTML | security, learning | enabled, probation | Robots retrieval failed; backed off |
| `ksum-events` | `https://startupmission.kerala.gov.in/events` | official / HTML | startups, hackathons | enabled, probation | Retrieved; `empty_expected` |
| `owasp-events` | `https://owasp.org/events/` | official / HTML | security | disabled, quarantined | Not attempted; usable event collector not established |
| `lovable-announcements` | `https://lovable.dev/blog` | unknown / HTML | AI | disabled, quarantined | Not attempted; URL/collector not live validated |
| `vercel-announcements` | `https://vercel.com/blog` | unknown / HTML | AI, startups | disabled, quarantined | Not attempted; collector not live validated |
| `supabase-announcements` | `https://supabase.com/blog` | unknown / HTML | AI, startups | disabled, quarantined | Not attempted; collector not live validated |
| `aws-startups` | `https://aws.amazon.com/startups/` | unknown / HTML | startups, student | disabled, quarantined | Not attempted; collector and redemption requirements unvalidated |
| `microsoft-students` | `https://azure.microsoft.com/en-us/free/students/` | unknown / HTML | student | disabled, quarantined | Not attempted; eligibility and collector untested |
| `google-developers` | `https://developers.googleblog.com/` | unknown / HTML | AI, career | disabled, quarantined | Not attempted; no validated feed/collector |
| `gdg-events` | `https://gdg.community.dev/events/` | unknown / HTML | hackathons | disabled, quarantined | Not attempted; dynamic calendar unvalidated |
| `startup-india` | `https://www.startupindia.gov.in/` | unknown / HTML | startups | disabled, quarantined | Not attempted; site-specific collector unestablished |
| `gsoc-program` | `https://summerofcode.withgoogle.com/` | unknown / HTML | career, learning | disabled, quarantined | Not attempted; current-edition dates unvalidated |
| `tryhackme-news` | `https://tryhackme.com/` | unknown / HTML | security | disabled, quarantined | Not attempted; no free-certification claim made |

The smoke result was **5 successful, 1 source failure and 4 not reached** out of 10
planned enabled sources, with 62 candidates and 55 HTTP attempts before the duration
budget stopped the run. It demonstrates bounded partial coverage, not universal
source health or unattended reliability.

## Adding a source

1. Confirm the publisher URL and access policy from the original organization.
2. Prefer an official RSS/Atom/API/ICS endpoint; otherwise configure exact HTML
   selectors. Do not invent `/feed`, `/rss`, or search URLs.
3. Set `enabled: false`, `validation_status: quarantined` until the URL is tested.
4. Add an official exact hostname only when you own evidence of publisher identity.
5. Run `sources health`, then a dry scan. Inspect extracted candidates and claim
   evidence. Change to enabled/probation only when coverage is useful and compliant.
6. Keep a reason for rejected/suspended sources. Never enable a page requiring login,
   CAPTCHA bypass, proxy evasion or an unapproved private API.

Discovery follows explicit alternate feed links, selected organizer links, and
bounded public GitHub repository search. It does not scrape general search engines,
social platforms, private groups or every HTML anchor. Public directory records link
to organizers but do not prove eligibility, price, deadline or official confirmation.

The system's coverage gap is intentional: RSS/API/known sources are reliable within
their own availability, while all-web discovery remains probabilistic and not
guaranteed at zero cost.
