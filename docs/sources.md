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
