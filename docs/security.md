# Security model

Scout reads hostile public data. Treat every response, feed field, HTML attribute,
JSON-LD value, event title, source-discovery link and email body as untrusted data.
No page can alter configuration, prompt the program into executing a command, install
a package, create a tool, or cause an arbitrary generated code path. AI assistance is
disabled by default and there is no code execution tool in the collector pipeline.

## Network controls

* Only absolute HTTP(S) URLs with no userinfo, controls, backslashes or non-80/443
  ports are accepted. Public loopback, link-local, metadata, private, multicast,
  reserved and IPv4-mapped IPv6 addresses are rejected.
* DNS answers are revalidated at the connection backend; the TCP destination is a
  public answer while TLS/Host uses the original hostname. Redirects are normalized,
  bounded, revalidated, and do not carry validators across hosts. Environment proxy
  variables are ignored. A redirect to a private host fails before a request.
* HTTP responses have timeouts, an encoded/decoded byte cap, supported media-type
  checks, gzip/deflate bomb protection, five redirects maximum and one sequential
  connection. Every host is paced. 403/429/5xx preserve backoff; Scout does not
  retry them immediately.
* robots.txt is checked before target requests and fail-closed on inaccessible or
  rate-limited policy. This is a courtesy/policy control, not a legal authorization.
  Terms, API quotas and publisher instructions still apply. Scout does not bypass
  CAPTCHAs, authentication, access controls, rate limits or robots restrictions.

The system resolver is bounded by the underlying OS resolver behavior; an individual
DNS lookup may take longer than the application HTTP timeout. A timeout is not a
claim that every kernel resolver call is interruptible.

## State and secret controls

Credentials only enter via process environment / Actions secrets. Logs never print
SMTP passwords, GitHub tokens, response bodies or checkpoint content. Private state
is excluded from Git, Docker build context where possible, artifacts, caches and
Pages. The state branch must stay private. `portalocker` excludes simultaneous local
writers; the GitHub backend uses blob-SHA CAS for remote writers.

Outbox state is durable before SMTP. A crash after remote acceptance can duplicate
an email; a stable Message-ID and at-least-once retry reduce ordinary duplication
but cannot prove exactly once. Unknown/corrupt state stops mail rather than mailing
the entire web as new.

## Claim safety

Official verification requires a validated configured official source, exact allowed
publisher hostname, and actual evidence on the original page/feed. A third-party
page linked by an official source is still third-party. Missing dates, timezone,
eligibility, pricing and availability stay unknown. Claims include evidence URL,
excerpt, authority, reliability and check time. HTML is extracted into text; it is
never rendered as trusted application markup.
